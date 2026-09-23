"""
sharp_feed.py — Sharp-book (Pinnacle / Circa / aggregator) odds ingestion.

PIECES
    SharpLine            one side of a two-way sharp market (our internal shape)
    SharpBook            cache of the latest lines, keyed by CANONICAL team names,
                         with a strict freshness rule (SHARP_MAX_AGE_SECONDS = 30s)
    ProviderSharpSource  polls YOUR provider's HTTP JSON feed and translates its
                         field names into SharpLine via a JSON config file —
                         no code change needed to plug in a provider
    MockSharpSource      fixed demo lines, used ONLY by tests and --simulate
    SharpPoller          calls a source every N seconds, loads the book, purges stale lines

FRESHNESS RULE (strict)
    A line's age is measured from the PROVIDER's own timestamp when it sends one
    (falls back to the time we received it). A line older than 30s is:
      * rejected at ingest if it already arrives stale, and
      * invisible to lookup() the moment it crosses 30s, and
      * purged from memory on the next poll.
    So if the provider goes down, every sharp line stops being usable within
    30 seconds and the engine simply stops finding edges — it never trades on
    an old price.

PROVIDER CONFIG (see config/sharp_provider.example.json)
    {
      "url": "https://provider.example/v1/odds?sport=nba,nfl",
      "headers": {"Authorization": "Bearer ${SHARP_API_KEY}"},   # env vars substituted
      "list_path": "data",                                       # where the list lives
      "fields": {"league": "sport", "home_team": "home", ...},   # ours -> theirs (dotted paths ok)
      "constants": {"source": "pinnacle"},
      "odds_format": "american" | "decimal",
      "timestamp_format": "epoch" | "epoch_ms" | "iso"
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

import aiohttp
from pydantic import BaseModel, Field, ValidationError

from team_normalizer import normalize_outcome, normalize_team_name

SHARP_MAX_AGE_SECONDS = 30.0      # the freshness rule
CLOCK_SKEW_TOLERANCE_SECONDS = 5.0

bridge_log = logging.getLogger("trading.bridge")
sharp_log = logging.getLogger("trading.sharp")


# ==========================================================================
# Model + cache
# ==========================================================================
class SharpLine(BaseModel):
    """One side of a sharp two-way market."""
    league: str
    home_team: str
    away_team: str
    market_type: Literal["spread", "moneyline", "total"]
    side: str                          # team name, or "over"/"under"
    odds_for: float                    # American odds on `side`
    odds_against: float                # American odds on the other side
    line: Optional[float] = None       # spread from `side`'s perspective, or the total
    source: str = "sharp"
    updated_at: Optional[float] = None # provider timestamp, epoch seconds (UTC)

    def mirrored(self) -> "SharpLine":
        """The same market seen from the other side (assumes names are already canonical)."""
        if self.market_type == "total":
            other, line = ("under" if self.side == "over" else "over"), self.line
        else:
            other = self.away_team if self.side == self.home_team else self.home_team
            line = -self.line if self.line is not None else None
        return self.model_copy(update=dict(side=other, line=line, odds_for=self.odds_against,
                                           odds_against=self.odds_for))


BookKey = tuple[str, str, str, str, str]  # (league, home, away, market_type, side)


class SharpBook:
    """In-memory cache of the latest sharp lines, keyed by CANONICAL names."""

    def __init__(self, max_age_seconds: float = SHARP_MAX_AGE_SECONDS,
                 clock: Callable[[], float] = time.time,
                 on_move: Optional[Callable[[BookKey, SharpLine, SharpLine], None]] = None) -> None:
        self.max_age = max_age_seconds
        self.clock = clock
        self.on_move = on_move    # called with (key, old, new) whenever a stored line/price changes
        self._lines: dict[BookKey, tuple[SharpLine, float]] = {}   # value: (line, observed_at epoch)
        self._seen: set[tuple[str, str]] = set()

    def __len__(self) -> int:
        return len(self._lines)

    def _translate(self, raw: str, league: str, outcome: bool = False) -> Optional[str]:
        canonical = (normalize_outcome if outcome else normalize_team_name)(raw, league)
        key = (raw, canonical or "")
        first_time = key not in self._seen
        self._seen.add(key)
        if canonical is None:
            bridge_log.log(logging.WARNING if first_time else logging.DEBUG,
                           "BRIDGE unmapped sharp name %r (%s) -> line skipped", raw, league)
        elif canonical != raw:
            bridge_log.log(logging.INFO if first_time else logging.DEBUG,
                           "BRIDGE %r -> %r (%s)", raw, canonical, league)
        return canonical

    def ingest(self, raw_lines: Any) -> tuple[int, int]:
        """Validate, normalise, freshness-check and store lines. Returns (stored, rejected). Never raises."""
        stored = rejected = stale = 0
        now = self.clock()
        for raw in raw_lines if isinstance(raw_lines, list) else []:
            try:
                line = raw if isinstance(raw, SharpLine) else SharpLine.model_validate(raw)
            except ValidationError as exc:
                sharp_log.warning("SHARP_POLL invalid line skipped (%d errors)", exc.error_count())
                rejected += 1
                continue

            observed_at = now if line.updated_at is None else line.updated_at
            if observed_at > now + CLOCK_SKEW_TOLERANCE_SECONDS:
                sharp_log.warning("SHARP_POLL provider timestamp %.0fs in the future; using receipt time",
                                  observed_at - now)
                observed_at = now
            if now - observed_at > self.max_age:
                stale += 1
                rejected += 1
                continue

            league = line.league.upper()
            home = self._translate(line.home_team, league)
            away = self._translate(line.away_team, league)
            side = self._translate(line.side, league, outcome=True)
            if None in (home, away, side):
                rejected += 1
                continue
            canon = line.model_copy(update=dict(league=league, home_team=home, away_team=away, side=side))
            key = (league, home, away, canon.market_type, canon.side)
            old = self._lines.get(key)
            for item in (canon, canon.mirrored()):
                self._lines[(league, home, away, item.market_type, item.side)] = (item, observed_at)
            stored += 1
            if old is not None and self.on_move is not None and (
                    old[0].line != canon.line or old[0].odds_for != canon.odds_for
                    or old[0].odds_against != canon.odds_against):
                try:
                    self.on_move(key, old[0], canon)
                except Exception:  # noqa: BLE001 — a listener bug must not break ingestion
                    sharp_log.exception("SHARP_MOVE listener failed")
        if stale:
            sharp_log.warning("SHARP_POLL rejected %d line(s) already older than %.0fs at the source",
                              stale, self.max_age)
        return stored, rejected

    def lookup(self, league: str, home: str, away: str, market_type: str, side: str) -> Optional[SharpLine]:
        """The fresh line for this side, or None if missing or older than max_age."""
        hit = self._lines.get((league, home, away, market_type, side))
        if hit is None:
            return None
        line, observed_at = hit
        if self.clock() - observed_at > self.max_age:
            return None
        return line

    def age_of(self, league: str, home: str, away: str, market_type: str, side: str) -> Optional[float]:
        hit = self._lines.get((league, home, away, market_type, side))
        return None if hit is None else self.clock() - hit[1]

    def purge_stale(self) -> int:
        now = self.clock()
        dead = [k for k, (_, t) in self._lines.items() if now - t > self.max_age]
        for k in dead:
            del self._lines[k]
        return len(dead)


# ==========================================================================
# Sources
# ==========================================================================
SharpFetch = Callable[[], Awaitable[list[Any]]]


def decimal_to_american(decimal_odds: float) -> float:
    """2.50 -> +150, 1.6667 -> -150. Decimal odds must be > 1.0."""
    if decimal_odds <= 1.0:
        raise ValueError(f"decimal odds must be > 1.0, got {decimal_odds}")
    if decimal_odds >= 2.0:
        return round((decimal_odds - 1.0) * 100.0, 4)
    return round(-100.0 / (decimal_odds - 1.0), 4)


def parse_timestamp(value: Any, fmt: str) -> Optional[float]:
    if value is None or value == "":
        return None
    if fmt == "epoch":
        return float(value)
    if fmt == "epoch_ms":
        return float(value) / 1000.0
    if fmt == "iso":
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise ValueError(f"unknown timestamp_format {fmt!r}")


def _dig(obj: Any, path: str) -> Any:
    """Follow a dotted path ('data.lines', 'teams.home', 'prices.0') through dicts/lists."""
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _substitute_env(text: str) -> str:
    def repl(m: re.Match) -> str:
        value = os.environ.get(m.group(1))
        if value is None:
            raise ValueError(f"environment variable {m.group(1)} is referenced by the provider config but not set")
        return value
    return _ENV_REF.sub(repl, text)


class ProviderConfig(BaseModel):
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    list_path: str = ""
    # "flat": one record = one two-way line (fields below).
    # "outcomes": one record = one fixture holding an array of per-side prices
    #             (OpticOdds / OddsJam style); sides are paired into two-way lines.
    mode: Literal["flat", "outcomes"] = "flat"
    fields: dict[str, str] = Field(default_factory=dict)       # our field -> provider path
    constants: dict[str, Any] = Field(default_factory=dict)    # fixed values, e.g. {"source": "pinnacle"}
    odds_format: Literal["american", "decimal"] = "american"
    timestamp_format: Literal["epoch", "epoch_ms", "iso"] = "epoch"
    timeout_seconds: float = 5.0
    # ---- "outcomes" mode only ----
    fixture_fields: dict[str, str] = Field(default_factory=dict)   # league / home_team / away_team -> path
    outcomes_path: str = "odds"                                     # path to the per-side array in a fixture
    outcome_fields: dict[str, str] = Field(default_factory=dict)   # market/selection/price/points/timestamp/...
    market_map: dict[str, str] = Field(default_factory=lambda: {
        "moneyline": "moneyline", "point spread": "spread", "spread": "spread",
        "total points": "total", "total": "total", "totals": "total"})
    sportsbooks: list[str] = Field(default_factory=list)            # e.g. ["Pinnacle"]; empty = any
    main_only: bool = True                                          # ignore alternate lines

    @classmethod
    def from_file(cls, path: str | Path) -> "ProviderConfig":
        cfg = cls.model_validate(json.loads(Path(path).read_text()))
        return cfg.model_copy(update=dict(url=_substitute_env(cfg.url),
                                          headers={k: _substitute_env(v) for k, v in cfg.headers.items()}))


OUR_FIELDS = ("league", "home_team", "away_team", "market_type", "side",
              "odds_for", "odds_against", "line", "source", "updated_at")


def map_provider_record(record: Any, cfg: ProviderConfig) -> dict[str, Any]:
    """Translate one provider record into SharpLine fields (validation happens in SharpBook)."""
    out: dict[str, Any] = dict(cfg.constants)
    for field in OUR_FIELDS:
        path = cfg.fields.get(field, field)       # unmapped fields default to identical names
        value = _dig(record, path)
        if value is not None:
            out[field] = value
    if isinstance(out.get("market_type"), str):
        out["market_type"] = out["market_type"].strip().lower()
    if cfg.odds_format == "decimal":
        for k in ("odds_for", "odds_against"):
            if k in out:
                out[k] = decimal_to_american(float(out[k]))
    if "updated_at" in out:
        out["updated_at"] = parse_timestamp(out["updated_at"], cfg.timestamp_format)
    return out


def _outcome_value(rec: dict, cfg: ProviderConfig, field: str) -> Any:
    return _dig(rec, cfg.outcome_fields.get(field, field))


def pair_fixture_outcomes(fixture: Any, cfg: ProviderConfig) -> tuple[list[dict[str, Any]], int]:
    """
    Turn one fixture's per-side price array into two-way SharpLine dicts.
      moneyline: the home-team entry paired with the away-team entry
      spread:    home at -x paired with away at +x (same book)
      total:     Over x paired with Under x (same book)
    The pair's timestamp is the OLDER of its two sides (conservative freshness).
    Returns (lines, records_skipped).
    """
    f = cfg.fixture_fields
    league = _dig(fixture, f.get("league", "league"))
    if isinstance(league, dict):
        league = league.get("name") or league.get("id")
    home = _dig(fixture, f.get("home_team", "home_team"))
    away = _dig(fixture, f.get("away_team", "away_team"))
    records = _dig(fixture, cfg.outcomes_path)
    if not (isinstance(league, str) and isinstance(home, str) and isinstance(away, str)
            and isinstance(records, list)):
        return [], 1
    league = league.upper()
    home_c, away_c = normalize_team_name(home, league), normalize_team_name(away, league)
    wanted_books = {b.lower() for b in cfg.sportsbooks}

    # group: (book, market) -> list of (role, points, american_odds, ts)
    groups: dict[tuple[str, str], list[tuple[str, Optional[float], float, Optional[float]]]] = {}
    skipped = 0
    for rec in records:
        try:
            book = str(_outcome_value(rec, cfg, "sportsbook") or "")
            if wanted_books and book.lower() not in wanted_books:
                continue
            if cfg.main_only and _outcome_value(rec, cfg, "is_main") is False:
                continue
            market = cfg.market_map.get(str(_outcome_value(rec, cfg, "market") or "").strip().lower())
            if market is None:
                continue
            selection = _outcome_value(rec, cfg, "selection")
            price = float(_outcome_value(rec, cfg, "price"))
            if cfg.odds_format == "decimal":
                price = decimal_to_american(price)
            points = _outcome_value(rec, cfg, "points")
            points = None if points in (None, "") else float(points)
            ts = parse_timestamp(_outcome_value(rec, cfg, "timestamp"), cfg.timestamp_format)
            if market == "total":
                role = normalize_outcome(selection, league)
                if role not in {"over", "under"}:
                    raise ValueError(f"total selection {selection!r}")
            else:
                team = normalize_team_name(selection, league)
                role = "home" if team is not None and team == home_c else "away" if team is not None and team == away_c else None
                if role is None:
                    raise ValueError(f"selection {selection!r} is neither {home!r} nor {away!r}")
            groups.setdefault((book, market), []).append((role, points, price, ts))
        except (ValueError, TypeError) as exc:
            skipped += 1
            sharp_log.debug("SHARP_POLL outcome skipped: %s", exc)

    lines: list[dict[str, Any]] = []
    for (book, market), sides in groups.items():
        first_role, second_role = ("over", "under") if market == "total" else ("home", "away")
        for role, pts, price, ts in sides:
            if role != first_role:
                continue
            for role2, pts2, price2, ts2 in sides:
                if role2 != second_role:
                    continue
                if market == "spread" and (pts is None or pts2 is None or abs(pts + pts2) > 1e-9):
                    continue
                if market == "total" and (pts is None or pts != pts2):
                    continue
                stamps = [t for t in (ts, ts2) if t is not None]
                lines.append(dict(
                    league=league, home_team=home, away_team=away, market_type=market,
                    side="Over" if market == "total" else home, odds_for=price, odds_against=price2,
                    line=pts if market != "moneyline" else None, source=cfg.constants.get("source", book or "sharp"),
                    updated_at=min(stamps) if stamps else None))
                break
    return lines, skipped


class ProviderSharpSource:
    """Polls the configured provider over one persistent HTTP session (low latency)."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None

    async def __call__(self) -> list[dict[str, Any]]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds),
                                                  headers=self.config.headers)
        async with self._session.get(self.config.url) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        records = _dig(payload, self.config.list_path)
        if not isinstance(records, list):
            raise ValueError(f"provider response has no list at {self.config.list_path!r}")
        if self.config.mode == "outcomes":
            mapped, bad = [], 0
            for fixture in records:
                lines, skipped = pair_fixture_outcomes(fixture, self.config)
                mapped.extend(lines)
                bad += skipped
            if bad:
                sharp_log.warning("SHARP_POLL %d provider outcome record(s) could not be used", bad)
            return mapped
        mapped, bad = [], 0
        for rec in records:
            try:
                mapped.append(map_provider_record(rec, self.config))
            except (ValueError, TypeError) as exc:
                bad += 1
                sharp_log.debug("SHARP_POLL provider record skipped: %s", exc)
        if bad:
            sharp_log.warning("SHARP_POLL %d provider record(s) could not be mapped", bad)
        return mapped

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


class MockSharpSource:
    """Fixed demo lines for tests and --simulate. NEVER used in live mode."""

    def __init__(self, lines: list[dict], jitter_cents: int = 0, latency: float = 0.0) -> None:
        self.lines = lines
        self.jitter = jitter_cents
        self.latency = latency

    async def __call__(self) -> list[dict]:
        if self.latency:
            await asyncio.sleep(self.latency)
        out = []
        for x in self.lines:
            x = dict(x)
            if self.jitter:
                for k in ("odds_for", "odds_against"):
                    x[k] = _jitter_american(x[k], self.jitter)
            out.append(x)
        return out


def _jitter_american(odds: float, cents: int) -> float:
    moved = odds + random.randint(-cents, cents)
    if -100 < moved < 100:
        moved = 100 if odds > 0 else -100
    return moved


class SharpPoller:
    """Calls `fetch` every `interval` seconds, loads the SharpBook, and purges expired lines."""

    def __init__(self, fetch: SharpFetch, book: SharpBook, interval: float = 2.0) -> None:
        self.fetch = fetch
        self.book = book
        self.interval = interval
        self.polls = self.failures = 0

    async def poll_once(self) -> None:
        started = time.monotonic()
        try:
            raw = await self.fetch()
            stored, rejected = self.book.ingest(raw)
            self.polls += 1
            sharp_log.info("SHARP_POLL ok stored=%d rejected=%d took=%.1fms",
                           stored, rejected, (time.monotonic() - started) * 1000)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad poll must not stop polling
            self.failures += 1
            sharp_log.warning("SHARP_POLL failed (%s: %s); retrying in %.1fs",
                              type(exc).__name__, exc, self.interval)
        purged = self.book.purge_stale()
        if purged:
            sharp_log.warning("SHARP_STALE purged %d line(s) older than %.0fs", purged, self.book.max_age)

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.interval)
