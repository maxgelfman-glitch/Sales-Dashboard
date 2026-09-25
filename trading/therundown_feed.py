"""
therundown_feed.py — TheRundown v2 as the sharp-odds source (REST snapshot + real-time WebSocket).

WHY THIS INSTEAD OF A GENERIC PROVIDER CONFIG
    TheRundown's v2 events endpoint nests prices as events -> markets -> participants -> lines ->
    prices keyed by sportsbook ("affiliate") id, and the Ultra plan streams every price change over a
    WebSocket. This adapter keeps an in-memory table of those prices and hands the engine two-way
    SharpLines (one per book), exactly like the generic ProviderSharpSource does.

ENDPOINTS (from TheRundown's published v2 OpenAPI description)
    REST  GET https://therundown.io/api/v2/sports/{sport_id}/events/{YYYY-MM-DD}
              ?market_ids=1,2,3&affiliate_ids=3&main_line=true      header X-TheRundown-Key
    WS    wss://therundown.io/api/v2/ws/markets?key=...&affiliate_ids=3&market_ids=1,2,3&sport_ids=2,4
          heartbeat {"meta":{"type":"heartbeat"},...} every 15s; price rows under "data"
    market ids   1 moneyline, 2 spread ("handicap"), 3 total      (period_id 0 = full game only)
    sport ids    2 NFL, 4 NBA, 3 MLB, 6 NHL, 8 WNBA (1 NCAAF, 5 NCAAB not tracked yet)
    affiliate    3 = Pinnacle (default). Others: 19 DraftKings, 23 FanDuel, 22 BetMGM, 2 Bovada
    price        American odds; 0.0001 = off the board (ignored)

ASSUMED (the published spec shows "..." for WebSocket rows): a WS price row carries the same fields as a
/markets/delta row: event_id, affiliate_id, market_id, participant_id, participant_name, line, price,
change_type ("price_change" | "new" | "close" | "reopen"), updated_at. Verify against the first live frames.

FRESHNESS
    TheRundown's updated_at is when a price last CHANGED, not when it was last confirmed, so an unchanged
    Pinnacle price can be hours "old" and still correct. Prices therefore count as fresh only while the
    source is demonstrably alive: a WS heartbeat/message within 30s, or (REST-only) a successful snapshot
    within the refresh window. When neither holds, the fetch raises, nothing is handed to the engine, and
    the engine's 30s sharp freshness rule expires every line.

LIVE GAMES
    Any event status other than STATUS_SCHEDULED marks the game's lines is_live -> the engine stops
    trading that game. Pregame prices that close (change_type "close") simply disappear.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

import aiohttp

log = logging.getLogger("trading.therundown")

REST_BASE = "https://therundown.io/api/v2"
WS_URL = "wss://therundown.io/api/v2/ws/markets"
SPORT_IDS = {"NFL": 2, "NBA": 4, "MLB": 3, "NHL": 6, "WNBA": 8, "NCAAF": 1, "NCAAB": 5}
LEAGUE_BY_SPORT = {v: k for k, v in SPORT_IDS.items()}
MARKETS = {1: "moneyline", 2: "spread", 3: "total"}
AFFILIATE_NAMES = {3: "pinnacle", 19: "draftkings", 23: "fanduel", 22: "betmgm", 2: "bovada"}
OFF_BOARD = 0.0001
PREGAME_STATUSES = {"STATUS_SCHEDULED"}
WS_ALIVE_SECONDS = 30.0            # heartbeat every 15s: two missed = dead
REST_REFRESH_WS_SECONDS = 300.0    # with the WebSocket: full snapshot (new games, statuses) every 5 minutes
REST_REFRESH_POLL_SECONDS = 15.0   # REST only: snapshot this often (each snapshot = 1 request per sport per date)


def _num(v: Any) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _epoch(v: Any) -> Optional[float]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class TheRundownSource:
    """SharpFetch-compatible: `await source()` returns SharpLine dicts for every tracked game and book."""

    def __init__(self, api_key: str, leagues: tuple[str, ...] = ("NFL", "NBA", "MLB", "NHL", "WNBA", "NCAAF", "NCAAB"),
                 affiliate_ids: tuple[int, ...] = (3,), use_websocket: bool = True,
                 rest_base: str = REST_BASE, ws_url: str = WS_URL, days_ahead: int = 1,
                 timeout: float = 10.0, clock=time.time) -> None:
        if not api_key:
            raise ValueError("TheRundown needs an API key (THERUNDOWN_API_KEY)")
        self.api_key = api_key
        self.sport_ids = [SPORT_IDS[lg] for lg in leagues if lg in SPORT_IDS]
        self.affiliate_ids = tuple(affiliate_ids)
        self.use_websocket = use_websocket
        self.rest_base = rest_base.rstrip("/")
        self.ws_url = ws_url
        self.days_ahead = days_ahead
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.clock = clock
        # state
        self.events: dict[str, dict] = {}        # event_id -> {league, home, away, home_id, away_id, status, start}
        self.prices: dict[tuple, dict] = {}      # (event_id, market_id, participant_id, line) -> {aff: {price, main}}
        self.last_snapshot = 0.0
        self.last_ws_signal = 0.0
        self.ws_messages = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws_task: Optional[asyncio.Task] = None
        mode = "therundown websocket + REST" if use_websocket else "therundown REST"
        # describe_state() reads these like a ProviderConfig
        self.config = SimpleNamespace(
            url=f"{self.rest_base}/sports/{{{','.join(map(str, self.sport_ids))}}}/events/{{date}}"
                + (f"  +  {self.ws_url}" if use_websocket else ""),
            mode=mode, odds_format="american", timestamp_format="receipt (liveness-checked)",
            market_map={str(k): v for k, v in MARKETS.items()}, market_overrides={}, timeout_seconds=timeout)

    # ------------------------------------------------------------------ fetch (called by SharpPoller)
    async def __call__(self) -> list[dict]:
        refresh = REST_REFRESH_WS_SECONDS if self.use_websocket else REST_REFRESH_POLL_SECONDS
        if self.clock() - self.last_snapshot >= refresh or not self.events:
            await self.snapshot()
        if self.use_websocket and (self._ws_task is None or self._ws_task.done()):
            self._ws_task = asyncio.create_task(self._ws_loop(), name="therundown_ws")
        if not self.alive():
            raise RuntimeError("TheRundown data not confirmed recently (no WS heartbeat / snapshot): "
                               "withholding prices so the engine's freshness rule expires them")
        return self.lines()

    def alive(self) -> bool:
        now = self.clock()
        if self.use_websocket:
            return now - self.last_ws_signal <= WS_ALIVE_SECONDS or now - self.last_snapshot <= WS_ALIVE_SECONDS
        return now - self.last_snapshot <= REST_REFRESH_POLL_SECONDS * 2

    # ------------------------------------------------------------------ REST snapshot
    def _dates(self) -> list[str]:
        today = datetime.fromtimestamp(self.clock(), timezone.utc).date()
        # US evening games fall on the next UTC date: always include today + days_ahead
        return [(today + timedelta(days=d)).isoformat() for d in range(0, self.days_ahead + 1)]

    async def snapshot(self) -> int:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout, headers={"X-TheRundown-Key": self.api_key})
        events: list[dict] = []
        for sport in self.sport_ids:
            for date in self._dates():
                params = {"market_ids": ",".join(map(str, MARKETS)), "main_line": "true",
                          "affiliate_ids": ",".join(map(str, self.affiliate_ids))}
                async with self._session.get(f"{self.rest_base}/sports/{sport}/events/{date}", params=params) as r:
                    r.raise_for_status()
                    payload = await r.json(content_type=None)
                if isinstance(payload, dict):
                    events += payload.get("events") or []
        self.load_events(events)
        self.last_snapshot = self.clock()
        log.info("THERUNDOWN snapshot: %d events, %d price rows", len(self.events), len(self.prices))
        return len(events)

    def load_events(self, events: list[dict]) -> None:
        """Replace state with a full snapshot (events -> markets -> participants -> lines -> prices)."""
        new_events, new_prices = {}, {}
        for ev in events:
            eid = ev.get("event_id")
            league = LEAGUE_BY_SPORT.get(ev.get("sport_id"))
            teams = ev.get("teams") or []
            home = next((t for t in teams if t.get("is_home")), None)
            away = next((t for t in teams if t.get("is_away")), None)
            if not (eid and league and home and away):
                continue
            new_events[eid] = dict(
                league=league, home=f"{home.get('name', '')} {home.get('mascot', '')}".strip(),
                away=f"{away.get('name', '')} {away.get('mascot', '')}".strip(),
                home_id=home.get("team_id"), away_id=away.get("team_id"),
                status=((ev.get("score") or {}).get("event_status") or "STATUS_SCHEDULED"),
                start=_epoch(ev.get("event_date")))
            for m in ev.get("markets") or []:
                if m.get("market_id") not in MARKETS or (m.get("period_id") or 0) != 0:
                    continue
                for part in m.get("participants") or []:
                    for ln in part.get("lines") or []:
                        key = (eid, m["market_id"], part.get("id"), str(ln.get("value") or ""))
                        books = {}
                        for aff, pr in (ln.get("prices") or {}).items():
                            price = _num((pr or {}).get("price"))
                            if price is None or abs(price - OFF_BOARD) < 1e-9 or (pr or {}).get("closed_at"):
                                continue
                            books[int(aff)] = dict(price=price, main=bool(pr.get("is_main_line", True)),
                                                   name=part.get("name"))
                        if books:
                            new_prices[key] = books
        self.events, self.prices = new_events, new_prices

    # ------------------------------------------------------------------ WebSocket deltas
    def apply_row(self, row: dict) -> bool:
        """One price change. Returns True if it changed the table."""
        eid, mid, aff = row.get("event_id"), row.get("market_id"), row.get("affiliate_id")
        if eid not in self.events or mid not in MARKETS or aff is None:
            return False
        if self.affiliate_ids and int(aff) not in self.affiliate_ids:
            return False
        key = (eid, mid, row.get("participant_id"), str(row.get("line") or ""))
        books = self.prices.setdefault(key, {})
        price = _num(row.get("price"))
        if str(row.get("change_type", "")).lower() == "close" or price is None or abs(price - OFF_BOARD) < 1e-9:
            removed = books.pop(int(aff), None) is not None
            if not books:
                self.prices.pop(key, None)
            return removed
        prev = books.get(int(aff))
        books[int(aff)] = dict(price=price, main=row.get("is_main_line", prev["main"] if prev else True),
                               name=row.get("participant_name") or (prev or {}).get("name"))
        return True

    def handle_message(self, raw: str | bytes) -> int:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("THERUNDOWN non-JSON WS frame skipped")
            return 0
        self.last_ws_signal = self.clock()
        self.ws_messages += 1
        if ((msg.get("meta") or {}).get("type") or "") == "heartbeat":
            return 0
        data = msg.get("data")
        rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        return sum(self.apply_row(r) for r in rows if isinstance(r, dict))

    async def _ws_loop(self) -> None:
        import websockets
        params = (f"?key={self.api_key}&affiliate_ids={','.join(map(str, self.affiliate_ids))}"
                  f"&market_ids={','.join(map(str, MARKETS))}&sport_ids={','.join(map(str, self.sport_ids))}")
        while True:
            try:
                async with websockets.connect(self.ws_url + params, ping_interval=20, ping_timeout=20,
                                              max_size=2 ** 22) as ws:
                    log.info("THERUNDOWN websocket connected")
                    async for raw in ws:
                        self.handle_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect forever; freshness guards the engine
                log.warning("THERUNDOWN websocket dropped (%s: %s); reconnecting in 2.5s", type(exc).__name__, exc)
            await asyncio.sleep(2.5)

    # ------------------------------------------------------------------ table -> SharpLine dicts
    def lines(self) -> list[dict]:
        out = []
        by_market: dict[tuple, list] = {}
        for (eid, mid, pid, line), books in self.prices.items():
            by_market.setdefault((eid, mid), []).append((pid, line, books))
        for (eid, mid), rows in by_market.items():
            ev = self.events.get(eid)
            if ev is None:
                continue
            live = ev["status"] not in PREGAME_STATUSES
            base = dict(league=ev["league"], home_team=ev["home"], away_team=ev["away"],
                        market_type=MARKETS[mid], is_live=live)
            for aff in {a for _, _, books in rows for a in books}:
                src = AFFILIATE_NAMES.get(aff, f"affiliate-{aff}")
                out += self._pair(ev, mid, rows, aff, base, src)
        return out

    def _pair(self, ev: dict, mid: int, rows: list, aff: int, base: dict, src: str) -> list[dict]:
        def side(pid, name, want_home: bool) -> bool:
            if pid is not None and pid in (ev["home_id"], ev["away_id"]):
                return (pid == ev["home_id"]) == want_home
            team = ev["home"] if want_home else ev["away"]
            return bool(name) and (str(name).lower() in team.lower() or team.lower() in str(name).lower())

        quotes = [(pid, line, books[aff]) for pid, line, books in rows if aff in books]
        out = []
        if mid == 3:
            overs = [(line, q) for pid, line, q in quotes if str(q.get("name") or "").lower().startswith("o")]
            unders = {line: q for pid, line, q in quotes if str(q.get("name") or "").lower().startswith("u")}
            for line, q in overs:
                u = unders.get(line)
                if u is not None and _num(line) is not None:
                    out.append(dict(base, side="Over", line=_num(line), odds_for=q["price"], odds_against=u["price"],
                                    source=src, is_main=q["main"] and u["main"]))
            return out
        homes = [(line, q) for pid, line, q in quotes if side(pid, q.get("name"), True)]
        aways = [(line, q) for pid, line, q in quotes if side(pid, q.get("name"), False)]
        for hline, hq in homes:
            for aline, aq in aways:
                if mid == 1:
                    ok, value = True, None
                else:
                    h, a = _num(hline), _num(aline)
                    ok, value = h is not None and a is not None and abs(h + a) < 1e-9, h
                if ok:
                    out.append(dict(base, side=ev["home"], line=value, odds_for=hq["price"],
                                    odds_against=aq["price"], source=src, is_main=hq["main"] and aq["main"]))
        return out

    async def close(self) -> None:
        if self._ws_task is not None:
            self._ws_task.cancel()
        if self._session is not None:
            await self._session.close()
