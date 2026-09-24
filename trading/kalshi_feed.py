"""
kalshi_feed.py — Kalshi order-book ingestion (second venue).

WHAT THIS MODULE DOES
    * Connects to Kalshi's WebSocket (DEMO by default) with RSA-PSS signed headers.
    * Subscribes to the `orderbook_delta` channel for every registered market ticker.
    * Rebuilds each book from `orderbook_snapshot` + ordered `orderbook_delta` messages.
    * Emits the same MarketUpdate objects as the Novig feed (venue="kalshi"), so the
      supervisor treats both venues identically.
    * Reconnect / heartbeat / watchdog behaviour comes from ws_base.py.

KALSHI BOOK MATHS
    Kalshi books hold only BIDS: YES bids and NO bids. A YES contract pays 100c.
    Buying YES means matching the best NO bid, so:
        YES ask (what we pay) = 100c - best NO bid
        YES bid (what we get) = best YES bid
    Each Kalshi game ticker is "Team X wins" -> one outcome; the other team's
    ticker in the same event is its sibling.

BRIDGE TRANSLATOR
    kalshi_cents_to_probability(53) -> 0.53 (= Novig implied probability)
    kalshi_cents_to_american(53)    -> -112.77 (~ -113)

VERIFIED vs ASSUMED (docs.kalshi.com is unreachable from the build sandbox;
source: a public reference of the v2 WebSocket API)
    Verified: URLs, auth header names and signing string "{ts_ms}GET/trade-api/ws/v2",
        subscribe command shape, snapshot fields yes_dollars_fp / no_dollars_fp,
        delta fields price_dollars / delta_fp / side / seq.
    ASSUMED: numeric values may arrive as strings (both accepted); legacy cent
        fields (yes / no / price / delta) are accepted as a fallback; REST market
        fields ticker / event_ticker / title / yes_sub_title; default series
        tickers KXNBAGAME / KXNFLGAME; only game-winner (moneyline) markets.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional, Union

import aiohttp

from execution import cents_to_american, cents_to_probability
from novig_feed import MAX_BOOK_LEVELS, MarketInfo, MarketRegistry, MarketUpdate, UpdateCallback
from novig_rest import parse_start_time
from team_normalizer import normalize_team_name
from ws_base import (
    OPEN_TIMEOUT_SECONDS,
    PING_INTERVAL_SECONDS,
    PING_TIMEOUT_SECONDS,
    RECONNECT_DELAY_SECONDS,
    STALE_STREAM_SECONDS,
    ResilientWebSocketFeed,
    StateCallback,
    safe_call,
)

DEFAULT_KALSHI_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"      # DEMO — safe default
KALSHI_PROD_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
DEFAULT_KALSHI_REST_BASE = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_PROD_REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"
WS_SIGN_PATH = "/trade-api/ws/v2"
DEFAULT_SERIES = {"KXNBAGAME": "NBA", "KXNFLGAME": "NFL"}

log = logging.getLogger("trading.kalshi")


# ==========================================================================
# Bridge translator
# ==========================================================================
def kalshi_cents_to_probability(price_cents: float) -> float:
    """Kalshi cents-per-contract -> implied probability on Novig's scale (53c -> 0.53)."""
    return cents_to_probability(price_cents)


def kalshi_cents_to_american(price_cents: float) -> float:
    """53c -> -112.77 (~ -113 American)."""
    return cents_to_american(price_cents)


# ==========================================================================
# Authentication (RSA-PSS, SHA-256)
# ==========================================================================
def load_private_key(path: Union[str, Path]):
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def sign_message(private_key, message: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    signature = private_key.sign(
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def auth_headers(key_id: str, private_key, method: str, path: str, now_ms: Optional[int] = None) -> dict[str, str]:
    ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sign_message(private_key, f"{ts}{method.upper()}{path}"),
    }


# ==========================================================================
# Order book
# ==========================================================================
def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _levels(msg: dict, side: str) -> list[tuple[float, float]]:
    """[(price_cents, qty)] from a snapshot; prefers *_dollars fields, falls back to legacy cents."""
    out = []
    for key, scale in ((f"{side}_dollars_fp", 100.0), (f"{side}_dollars", 100.0), (side, 1.0)):
        raw = msg.get(key)
        if isinstance(raw, list):
            for level in raw:
                if isinstance(level, (list, tuple)) and len(level) >= 2:
                    price, qty = _num(level[0]), _num(level[1])
                    if price is not None and qty is not None:
                        out.append((round(price * scale, 4), qty))
            return out
    return out


class KalshiBook:
    def __init__(self) -> None:
        self.yes: dict[float, float] = {}   # YES bids: cents -> contracts
        self.no: dict[float, float] = {}    # NO bids:  cents -> contracts

    def apply_snapshot(self, msg: dict) -> None:
        self.yes = {p: q for p, q in _levels(msg, "yes") if q > 0}
        self.no = {p: q for p, q in _levels(msg, "no") if q > 0}

    def apply_delta(self, msg: dict) -> None:
        side = str(msg.get("side", "")).lower()
        if side not in {"yes", "no"}:
            raise ValueError(f"bad delta side {msg.get('side')!r}")
        price = _num(msg.get("price_dollars"))
        price = price * 100 if price is not None else _num(msg.get("price"))
        delta = _num(msg.get("delta_fp"))
        delta = delta if delta is not None else _num(msg.get("delta"))
        if price is None or delta is None:
            raise ValueError("delta without price/quantity")
        levels = self.yes if side == "yes" else self.no
        price = round(price, 4)
        new = levels.get(price, 0.0) + delta
        if new > 1e-9:
            levels[price] = new
        else:
            levels.pop(price, None)

    def yes_ask(self) -> Optional[tuple[float, float]]:
        """(cents, contracts) to BUY YES: crosses the best NO bid."""
        if not self.no:
            return None
        best_no = max(self.no)
        return round(100 - best_no, 4), self.no[best_no]

    def yes_ask_levels(self, n: int = MAX_BOOK_LEVELS) -> list[tuple[float, float]]:
        """Cheapest n YES asks as (price as probability, contracts): each NO bid at c = a YES ask at 100 - c."""
        return [(round(100 - p, 4) / 100, self.no[p]) for p in sorted(self.no, reverse=True)[:n] if 0 < p < 100]

    def yes_bid(self) -> Optional[tuple[float, float]]:
        if not self.yes:
            return None
        best = max(self.yes)
        return best, self.yes[best]


class SequenceGapError(Exception):
    """A missing delta means the local book can no longer be trusted: reconnect + resnapshot."""


# ==========================================================================
# Feed
# ==========================================================================
class KalshiFeed(ResilientWebSocketFeed):
    venue = "kalshi"

    def __init__(
        self,
        url: Optional[str] = None,
        key_id: Optional[str] = None,
        private_key=None,
        registry: Optional[MarketRegistry] = None,
        on_update: Optional[UpdateCallback] = None,
        on_state_change: Optional[StateCallback] = None,
        reconnect_delay: float = RECONNECT_DELAY_SECONDS,
        ping_interval: float = PING_INTERVAL_SECONDS,
        ping_timeout: float = PING_TIMEOUT_SECONDS,
        stale_after: float = STALE_STREAM_SECONDS,
        open_timeout: float = OPEN_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(url or DEFAULT_KALSHI_WS_URL, on_state_change, reconnect_delay, ping_interval,
                         ping_timeout, stale_after, open_timeout, logger=log)
        self.key_id = key_id
        self.private_key = private_key
        self.registry = registry if registry is not None else MarketRegistry()
        self.on_update = on_update
        self.books: dict[str, KalshiBook] = {}
        self.latest: dict[str, MarketUpdate] = {}
        self._seq: dict[Any, int] = {}
        if not (key_id and private_key):
            log.warning("CONN kalshi has no API key; connecting WITHOUT authentication (only valid for mocks)")

    def _headers(self) -> Optional[dict[str, str]]:
        if self.key_id and self.private_key is not None:
            return auth_headers(self.key_id, self.private_key, "GET", WS_SIGN_PATH)
        return None

    def tickers(self) -> list[str]:
        return sorted(i.outcome_id for i in self.registry.all() if i.venue == "kalshi")

    async def _on_open(self, ws) -> None:
        cmd = {"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"],
                                                        "market_tickers": self.tickers()}}
        await ws.send(json.dumps(cmd))
        log.info("CONN kalshi subscribed orderbook_delta for %d tickers", len(cmd["params"]["market_tickers"]))

    def _clear_state(self) -> dict[str, int]:
        counts = {"stale_price_frames": len(self.latest), "order_books": len(self.books)}
        self.latest.clear()
        self.books.clear()
        self._seq.clear()
        return counts

    def _heartbeat_extra(self) -> str:
        return f" books={len(self.books)}"

    async def _handle_raw(self, raw) -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("KALSHI_PARSE_ERROR non-JSON message skipped")
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else {}
        if kind == "error":
            log.error("KALSHI server error: %s", msg or payload)
            return
        if kind not in {"orderbook_snapshot", "orderbook_delta"}:
            return  # subscription acks, heartbeats, other channels

        sid, seq = payload.get("sid"), payload.get("seq", msg.get("seq"))
        if seq is not None:
            last = self._seq.get(sid)
            if kind == "orderbook_delta" and last is not None and int(seq) != last + 1:
                raise SequenceGapError(f"sid {sid}: expected seq {last + 1}, got {seq}")
            self._seq[sid] = int(seq)

        ticker = msg.get("market_ticker")
        if not ticker:
            return
        book = self.books.setdefault(ticker, KalshiBook())
        try:
            if kind == "orderbook_snapshot":
                book.apply_snapshot(msg)
            else:
                book.apply_delta(msg)
        except ValueError as exc:
            log.warning("KALSHI_PARSE_ERROR %s: %s", ticker, exc)
            return
        await self._emit(ticker, book)

    async def _emit(self, ticker: str, book: KalshiBook) -> None:
        info = self.registry.get(ticker)
        if info is None or info.venue != "kalshi":
            return
        ask, bid = book.yes_ask(), book.yes_bid()
        top = dict(price=None if ask is None or not 0 < ask[0] < 100 else ask[0] / 100,
                   available_volume=0.0 if ask is None else ask[1],
                   best_bid=None if bid is None or not 0 < bid[0] < 100 else bid[0] / 100,
                   bid_volume=0.0 if bid is None else bid[1], ask_levels=book.yes_ask_levels())
        previous = self.latest.get(ticker)
        if previous is not None and all(getattr(previous, k) == v for k, v in top.items()):
            return
        update = MarketUpdate.from_info(info, **top)
        self.latest[ticker] = update
        if previous is not None and previous.price != update.price:
            log.info("LINE_SHIFT kalshi %s %s ask %s -> %s (%s American)", ticker, info.outcome, previous.price,
                     update.price, "n/a" if update.price is None else kalshi_cents_to_american(update.price * 100))
        await safe_call(self.on_update, update, previous, logger=log)


# ==========================================================================
# REST bootstrap (open game-winner markets -> registry rows)
# ==========================================================================
_MATCHUP = re.compile(r"^\s*(?P<away>.+?)\s+(?:at|@)\s+(?P<home>.+?)(?:\s+(?:winner|game)\b.*)?\??\s*$", re.I)
_VERSUS = re.compile(r"^\s*(?P<home>.+?)\s+(?:vs\.?|v)\s+(?P<away>.+?)(?:\s+(?:winner|game)\b.*)?\??\s*$", re.I)


def _matchup(title: str) -> Optional[tuple[str, str]]:
    """'Boston at New York Winner?' -> (home='New York', away='Boston')."""
    for rx in (_MATCHUP, _VERSUS):
        m = rx.match(title or "")
        if m:
            return m.group("home").strip(), m.group("away").strip()
    return None


def parse_kalshi_markets(payload: Any, league: str) -> list[MarketInfo]:
    markets = payload.get("markets", []) if isinstance(payload, dict) else payload if isinstance(payload, list) else []
    by_event: dict[str, list[dict]] = {}
    for m in markets:
        if isinstance(m, dict) and m.get("ticker") and m.get("event_ticker"):
            if str(m.get("status", "open")).lower() in {"open", "active", "initialized"}:
                by_event.setdefault(m["event_ticker"], []).append(m)
    rows: list[MarketInfo] = []
    for event_ticker, ms in by_event.items():
        teams = _matchup(ms[0].get("title", ""))
        if teams is None:
            log.info("BOOTSTRAP kalshi %s: cannot read teams from %r", event_ticker, ms[0].get("title"))
            continue
        home = normalize_team_name(teams[0], league)
        away = normalize_team_name(teams[1], league)
        if home is None or away is None:
            log.info("BOOTSTRAP kalshi %s: unmapped teams %r", event_ticker, teams)
            continue
        tickers = [m["ticker"] for m in ms]
        for m in ms:
            team = normalize_team_name(m.get("yes_sub_title") or m.get("subtitle") or "", league)
            if team not in {home, away}:
                continue
            sibling = next((t for t in tickers if t != m["ticker"]), None) if len(tickers) == 2 else None
            # ASSUMED field: Kalshi's scheduled occurrence time. Novig's start time for the same game is used when
            # this is missing (the supervisor keys the cutoff on the canonical game, across venues).
            start = parse_start_time(m.get("occurrence_datetime") or m.get("expected_start_time"))
            rows.append(MarketInfo(venue="kalshi", outcome_id=m["ticker"], market_id=event_ticker,
                                   sibling_outcome_id=sibling, league=league, market_type="moneyline",
                                   event_id=event_ticker, home_team=home, away_team=away, outcome=team,
                                   start_time=start))
    return rows


class KalshiRestClient:
    def __init__(self, base_url: str = DEFAULT_KALSHI_REST_BASE, key_id: Optional[str] = None,
                 private_key=None, series: Optional[dict[str, str]] = None, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.key_id, self.private_key = key_id, private_key
        self.series = series or DEFAULT_SERIES
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def fetch_open_markets(self) -> list[MarketInfo]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        rows: list[MarketInfo] = []
        path = "/markets"
        sign_path = "/trade-api/v2/markets"
        for series, league in self.series.items():
            cursor = None
            for _ in range(20):   # pagination guard
                params = {"series_ticker": series, "status": "open", "limit": "1000"}
                if cursor:
                    params["cursor"] = cursor
                headers = (auth_headers(self.key_id, self.private_key, "GET", sign_path)
                           if self.key_id and self.private_key is not None else {})
                async with self._session.get(self.base_url + path, params=params, headers=headers) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                rows.extend(parse_kalshi_markets(data, league))
                cursor = data.get("cursor") if isinstance(data, dict) else None
                if not cursor:
                    break
        log.info("BOOTSTRAP kalshi loaded %d game-winner outcomes", len(rows))
        return rows

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def series_from_env(value: Optional[str]) -> dict[str, str]:
    """'KXNBAGAME:NBA,KXNFLGAME:NFL' -> {'KXNBAGAME': 'NBA', 'KXNFLGAME': 'NFL'}."""
    if not value:
        return dict(DEFAULT_SERIES)
    out = {}
    for part in value.split(","):
        if ":" in part:
            s, lg = part.split(":", 1)
            out[s.strip()] = lg.strip().upper()
    return out
