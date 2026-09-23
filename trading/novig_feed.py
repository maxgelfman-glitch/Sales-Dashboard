"""
novig_feed.py — Real-time market ingestion engine for the Novig exchange.

WHAT THIS MODULE DOES
    1. Opens a WebSocket connection to Novig's tape (staging by default).
    2. Authenticates with the bearer token in the NOVIG_BEARER_TOKEN env var.
    3. Parses each incoming JSON message, keeps only NFL/NBA spreads,
       moneylines and main game totals, and reports every line/price shift.
    4. Survives any connection failure: it logs the error, wipes its local
       price cache (so we never trade on stale prices), waits a short
       cool-off and reconnects — forever, without crashing the parent process.

HEALTH CHECKS ("heartbeat")
    * Protocol ping/pong: every PING_INTERVAL_SECONDS the client pings the
      server. If no pong arrives within PING_TIMEOUT_SECONDS the connection
      is declared dead and the reconnect routine runs.
    * Stale-stream watchdog: if the socket stays "open" but no message arrives
      for STALE_STREAM_SECONDS, we assume a half-dead connection and reconnect.
    * Heartbeat log: every PING_INTERVAL_SECONDS we log the measured latency.

RECOVERY GUARANTEE
    After a drop, the next handshake attempt starts RECONNECT_DELAY_SECONDS
    (2.5s) later, so a healthy server is reconnected within the 3-second
    recovery budget. Proven by tests/test_feed.py.

!! ASSUMED MESSAGE SCHEMA !!
    Novig's exact tape payload format was not available when this was written.
    The parser accepts the field names listed in `NovigMarketUpdate` below
    (with common aliases). Once the official API docs are in hand, adjust ONLY
    that model and `MARKET_TYPE_ALIASES`; nothing else needs to change.

Run standalone (connects to staging, prints line shifts):
    export NOVIG_BEARER_TOKEN=...   # never commit this value
    python novig_feed.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional, Union
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, Field, ValidationError, field_validator
from websockets.asyncio.client import connect

# --------------------------------------------------------------------------
# Configuration constants (change here, not deep in the code)
# --------------------------------------------------------------------------
DEFAULT_NOVIG_WS_URL = "wss://api-qa.novig.us/tape"  # STAGING. Do not point at prod casually.
TOKEN_ENV_VAR = "NOVIG_BEARER_TOKEN"

RECONNECT_DELAY_SECONDS = 2.5   # cool-off before a re-handshake (keeps recovery < 3s)
PING_INTERVAL_SECONDS = 10.0    # how often we ping the server
PING_TIMEOUT_SECONDS = 10.0     # how long we wait for the pong before declaring death
STALE_STREAM_SECONDS = 30.0     # no messages for this long => reconnect
OPEN_TIMEOUT_SECONDS = 10.0     # max time allowed for the handshake itself

TRACKED_LEAGUES = frozenset({"NFL", "NBA"})

# Every spelling we accept for a market type, mapped to our three canonical names.
MARKET_TYPE_ALIASES = {
    "spread": "spread", "point_spread": "spread", "pointspread": "spread",
    "handicap": "spread", "ats": "spread",
    "moneyline": "moneyline", "money_line": "moneyline", "ml": "moneyline",
    "h2h": "moneyline", "winner": "moneyline",
    "total": "total", "totals": "total", "game_total": "total",
    "over_under": "total", "ou": "total",
}

log = logging.getLogger("trading.feed")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
class NovigMarketUpdate(BaseModel):
    """One price update for one side of one market (validated and normalised)."""

    league: str
    market_type: str = Field(validation_alias=AliasChoices("market_type", "marketType", "market", "type"))
    event_id: str = Field(validation_alias=AliasChoices("event_id", "eventId", "game_id", "gameId", "event"))
    home_team: str = Field(validation_alias=AliasChoices("home_team", "homeTeam", "home"))
    away_team: str = Field(validation_alias=AliasChoices("away_team", "awayTeam", "away"))
    # The side this price is for: a team name, or "over"/"under" for totals.
    outcome: str = Field(validation_alias=AliasChoices("outcome", "selection", "side"))
    # Spread points (e.g. -3.5) or total points (e.g. 47.5). None for moneylines.
    line: Optional[float] = Field(default=None, validation_alias=AliasChoices("line", "points", "handicap"))
    # Contract price in dollars per $1 payout, i.e. the implied probability (0 < p < 1).
    price: float = Field(validation_alias=AliasChoices("price", "last", "last_price", "probability"))
    received_at: float = Field(default_factory=time.time)

    @field_validator("league")
    @classmethod
    def _upper_league(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("market_type")
    @classmethod
    def _canonical_market(cls, v: str) -> str:
        key = v.strip().lower().replace(" ", "_").replace("-", "_")
        return MARKET_TYPE_ALIASES.get(key, key)

    @field_validator("price")
    @classmethod
    def _price_in_range(cls, v: float) -> float:
        if not 0.0 < v < 1.0:
            raise ValueError(f"contract price must be strictly between 0 and 1, got {v}")
        return v

    @property
    def market_key(self) -> tuple[str, str, str]:
        """Uniquely identifies the contract whose price we are tracking."""
        return (self.event_id, self.market_type, self.outcome.strip().lower())

    def is_tracked(self) -> bool:
        return self.league in TRACKED_LEAGUES and self.market_type in {"spread", "moneyline", "total"}


def parse_message(raw: Union[str, bytes]) -> list[NovigMarketUpdate]:
    """
    Turn one raw WebSocket message into a list of tracked market updates.

    Accepts a single update object, a list of them, or an envelope like
    {"type": "...", "data": [...]}. Never raises: bad input is logged and skipped.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.warning("FEED_PARSE_ERROR non-JSON message skipped: %s", exc)
        return []

    if isinstance(payload, dict) and isinstance(payload.get("data"), (list, dict)):
        payload = payload["data"]
    items = payload if isinstance(payload, list) else [payload]

    updates: list[NovigMarketUpdate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            update = NovigMarketUpdate.model_validate(item)
        except ValidationError as exc:
            log.debug("FEED_SKIP unrecognised item (%d errors): %s", exc.error_count(), item)
            continue
        if update.is_tracked():
            updates.append(update)
    return updates


# --------------------------------------------------------------------------
# The feed client
# --------------------------------------------------------------------------
UpdateCallback = Callable[[NovigMarketUpdate, Optional[NovigMarketUpdate]], Union[None, Awaitable[None]]]
StateCallback = Callable[[str, dict], Union[None, Awaitable[None]]]


class StaleStreamError(Exception):
    """Raised when the socket is open but silent for too long."""


class NovigFeed:
    """
    Long-running, self-healing WebSocket client.

    Usage:
        feed = NovigFeed(on_update=my_handler)
        task = asyncio.create_task(feed.run())
        ...
        await feed.stop()

    on_update(update, previous) is called for every tracked update.
        `previous` is None the first time we see a contract (snapshot),
        otherwise it is the prior update for the same contract.
    on_state_change(state, details) is called on CONNECTING / CONNECTED /
        DISCONNECTED / STOPPED transitions.
    """

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        on_update: Optional[UpdateCallback] = None,
        on_state_change: Optional[StateCallback] = None,
        reconnect_delay: float = RECONNECT_DELAY_SECONDS,
        ping_interval: float = PING_INTERVAL_SECONDS,
        ping_timeout: float = PING_TIMEOUT_SECONDS,
        stale_after: float = STALE_STREAM_SECONDS,
        open_timeout: float = OPEN_TIMEOUT_SECONDS,
    ) -> None:
        self.url = url or DEFAULT_NOVIG_WS_URL
        self._token = token if token is not None else os.environ.get(TOKEN_ENV_VAR)
        self.on_update = on_update
        self.on_state_change = on_state_change
        self.reconnect_delay = reconnect_delay
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.stale_after = stale_after
        self.open_timeout = open_timeout

        # Local memory: last known update per contract. Wiped on every drop.
        self.latest: dict[tuple[str, str, str], NovigMarketUpdate] = {}

        # Observability counters (read by tests and the supervisor).
        self.connected = asyncio.Event()
        self.connect_count = 0
        self.disconnect_count = 0
        self.messages_received = 0
        self.last_recovery_seconds: Optional[float] = None

        self._stopping = False
        self._ws = None
        self._dropped_at: Optional[float] = None

    # ---------------- public API ----------------
    async def run(self) -> None:
        """Connect, stream, and reconnect forever until stop() is called."""
        if not self._token:
            log.warning("CONN no %s set; connecting WITHOUT authentication (only valid for mocks)", TOKEN_ENV_VAR)
        while not self._stopping:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — by design: nothing may kill the loop
                if self._stopping:
                    break
                self._handle_drop(exc)
            else:
                if self._stopping:
                    break
                self._handle_drop(ConnectionError("server closed the stream"))
            if self._stopping:
                break
            log.info("CONN reconnecting in %.1fs", self.reconnect_delay)
            await asyncio.sleep(self.reconnect_delay)
        self.connected.clear()
        await self._emit_state("STOPPED", {})
        log.info("CONN feed stopped")

    async def stop(self) -> None:
        """Ask the feed to shut down cleanly."""
        self._stopping = True
        if self._ws is not None:
            await self._ws.close()

    # ---------------- internals ----------------
    def _connect_kwargs(self) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
        host = urlparse(self.url).hostname or ""
        is_local = host in {"localhost", "127.0.0.1", "::1"}
        return dict(
            additional_headers=headers,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
            open_timeout=self.open_timeout,
            # Never route local mock traffic through a corporate/sandbox proxy.
            proxy=None if is_local else True,
        )

    async def _session(self) -> None:
        """One connection lifetime: handshake, then read until something breaks."""
        await self._emit_state("CONNECTING", {"url": self.url})
        async with connect(self.url, **self._connect_kwargs()) as ws:
            self._ws = ws
            self.connect_count += 1
            self.connected.set()
            details: dict[str, Any] = {"url": self.url, "connect_count": self.connect_count}
            if self._dropped_at is not None:
                self.last_recovery_seconds = time.monotonic() - self._dropped_at
                details["recovery_seconds"] = round(self.last_recovery_seconds, 3)
                self._dropped_at = None
            log.info("CONN connected %s", json.dumps(details))
            await self._emit_state("CONNECTED", details)

            heartbeat = asyncio.create_task(self._heartbeat_logger(ws))
            try:
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_after)
                    except asyncio.TimeoutError:
                        raise StaleStreamError(f"no data for {self.stale_after:.1f}s")
                    self.messages_received += 1
                    for update in parse_message(raw):
                        await self._process(update)
            finally:
                heartbeat.cancel()
                self._ws = None

    async def _heartbeat_logger(self, ws) -> None:
        """Log round-trip latency measured by the protocol ping/pong."""
        while True:
            await asyncio.sleep(self.ping_interval)
            log.info("HEARTBEAT feed ping latency=%.1fms msgs=%d", ws.latency * 1000, self.messages_received)

    def _handle_drop(self, exc: BaseException) -> None:
        """Log the failure and wipe stale state. Must never raise."""
        was_connected = self.connected.is_set()
        self.connected.clear()
        stale_frames = len(self.latest)
        self.latest.clear()  # never trade on prices from a dead connection
        if was_connected:
            self.disconnect_count += 1
            self._dropped_at = time.monotonic()
        elif self._dropped_at is None:
            self._dropped_at = time.monotonic()
        log.warning(
            "CONN dropped (%s: %s); cleared %d stale price frames",
            type(exc).__name__, exc, stale_frames,
        )
        # State callback is sync-scheduled so this method stays exception-free.
        asyncio.get_running_loop().create_task(
            self._emit_state("DISCONNECTED", {"error": f"{type(exc).__name__}: {exc}", "cleared_frames": stale_frames})
        )

    async def _process(self, update: NovigMarketUpdate) -> None:
        previous = self.latest.get(update.market_key)
        self.latest[update.market_key] = update
        if previous is None:
            log.debug("FEED_SNAPSHOT %s", update.model_dump_json())
        elif previous.price != update.price or previous.line != update.line:
            log.info("LINE_SHIFT %s", json.dumps(format_shift(update, previous)))
        else:
            return  # identical re-broadcast: nothing to do
        await self._safe_call(self.on_update, update, previous)

    async def _emit_state(self, state: str, details: dict) -> None:
        await self._safe_call(self.on_state_change, state, details)

    @staticmethod
    async def _safe_call(fn, *args) -> None:
        """Run a user callback; a bug in it must never take the feed down."""
        if fn is None:
            return
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            log.exception("CALLBACK_ERROR in %s (feed continues)", getattr(fn, "__name__", fn))


def format_shift(update: NovigMarketUpdate, previous: NovigMarketUpdate) -> dict[str, Any]:
    """Clean, human-readable summary of a price/line move."""
    return {
        "league": update.league,
        "market": update.market_type,
        "event": f"{update.away_team} @ {update.home_team}",
        "outcome": update.outcome,
        "line": {"from": previous.line, "to": update.line},
        "price": {"from": previous.price, "to": update.price},
    }


# --------------------------------------------------------------------------
# Standalone entry point
# --------------------------------------------------------------------------
async def _main() -> None:
    feed = NovigFeed()
    await feed.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
