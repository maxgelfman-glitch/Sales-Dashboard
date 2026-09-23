"""
novig_feed.py — Real-time market ingestion engine for the Novig exchange.

WHAT THIS MODULE DOES
    1. Opens a WebSocket connection to Novig's tape (QA/staging by default).
    2. Authenticates with the bearer token in the NOVIG_BEARER_TOKEN env var.
    3. Parses order-book ticks (market_id, price_cents, side, volume), keeps a
       live order book per market, and reports the price WE could buy at
       (the best ask) whenever it changes.
    4. Survives any connection failure: logs the error, wipes all local books
       and prices (so we never trade on stale data), waits a short cool-off
       and reconnects — forever, without crashing the parent process.

WHAT IS VERIFIED vs ASSUMED
    Verified from docs.novig.com (via search; the docs site itself is not
    reachable from the build sandbox):
        * QA URL   wss://api-qa.novig.us/tape      (default here)
        * PROD URL wss://api.novig.com/tape        (NOVIG_PROD_WS_URL; opt-in only)
        * public order-book ticks are PLACE / CANCEL events
        * the server pings every 15s and disconnects clients that miss a pong
          (the websockets library answers pings automatically)
    ASSUMED — confirm against the docs before trading real money:
        * tick field names: market_id, price_cents, side ("buy"/"sell"), volume,
          plus an optional action field (PLACE / CANCEL / FILL / TRADE)
        * `volume` on PLACE/CANCEL/FILL is a change in contracts at that price;
          a tick with no action is a snapshot that SETS the level's volume
        * one market_id = one outcome contract paying $1 if it wins
        * no subscription message is required (pass `subscribe_messages` if it is)
        * ticks do not carry team/league info, so market metadata comes from
          a MarketRegistry (loaded from Novig's REST markets endpoint or a file)
    Every assumption lives in TapeTick / OrderBook.apply / parse_message below.

HEALTH CHECKS ("heartbeat")
    * Protocol ping/pong in both directions (ours every PING_INTERVAL_SECONDS).
    * Stale-stream watchdog: no message for STALE_STREAM_SECONDS => reconnect.
    * Heartbeat log line with measured latency every PING_INTERVAL_SECONDS.

RECOVERY GUARANTEE
    The next handshake starts RECONNECT_DELAY_SECONDS (2.5s) after a drop, so a
    healthy server is reconnected inside the 3-second budget (tests/test_feed.py).

Run standalone (connects to QA, prints best-ask moves for registered markets):
    export NOVIG_BEARER_TOKEN=...          # never commit this value
    export NOVIG_MARKETS_FILE=markets.json # optional market metadata
    python novig_feed.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Literal, Optional, Union
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, Field, ValidationError, field_validator
from websockets.asyncio.client import connect

# --------------------------------------------------------------------------
# Configuration constants (change here, not deep in the code)
# --------------------------------------------------------------------------
DEFAULT_NOVIG_WS_URL = "wss://api-qa.novig.us/tape"  # QA / staging — safe default
NOVIG_PROD_WS_URL = "wss://api.novig.com/tape"       # production — use deliberately
TOKEN_ENV_VAR = "NOVIG_BEARER_TOKEN"

RECONNECT_DELAY_SECONDS = 2.5   # cool-off before a re-handshake (keeps recovery < 3s)
PING_INTERVAL_SECONDS = 10.0    # our pings (Novig's server also pings us every 15s)
PING_TIMEOUT_SECONDS = 10.0     # how long we wait for a pong before declaring death
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

ADD_ACTIONS = {"PLACE", "ADD", "NEW"}
REMOVE_ACTIONS = {"CANCEL", "FILL", "REMOVE", "DELETE"}
PRINT_ACTIONS = {"TRADE", "MATCH"}  # informational trade prints: do not change resting volume

log = logging.getLogger("trading.feed")


# --------------------------------------------------------------------------
# Data models
# --------------------------------------------------------------------------
class TapeTick(BaseModel):
    """One order-book tick from the Novig tape (field names per the brief; see ASSUMED above)."""

    market_id: str = Field(validation_alias=AliasChoices("market_id", "marketId"))
    price_cents: float = Field(validation_alias=AliasChoices("price_cents", "priceCents"))
    side: Literal["buy", "sell"]
    volume: float = Field(ge=0)
    action: Optional[str] = Field(default=None, validation_alias=AliasChoices("action", "type", "event"))

    @field_validator("market_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> Any:
        return str(v) if isinstance(v, int) else v

    @field_validator("side", mode="before")
    @classmethod
    def _lower_side(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("action", mode="before")
    @classmethod
    def _upper_action(cls, v: Any) -> Any:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("price_cents")
    @classmethod
    def _cents_in_range(cls, v: float) -> float:
        if not 0 < v < 100:
            raise ValueError(f"price_cents must be strictly between 0 and 100, got {v}")
        return v


class MarketInfo(BaseModel):
    """Metadata for one market_id (one outcome contract). Ticks do not carry this."""

    market_id: str
    league: str
    market_type: str
    event_id: str
    home_team: str
    away_team: str
    outcome: str                   # team name, or "over"/"under"
    line: Optional[float] = None   # spread/total points; None for moneylines

    @field_validator("market_id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> Any:
        return str(v) if isinstance(v, int) else v

    @field_validator("league")
    @classmethod
    def _upper_league(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("market_type")
    @classmethod
    def _canonical_market(cls, v: str) -> str:
        key = v.strip().lower().replace(" ", "_").replace("-", "_")
        return MARKET_TYPE_ALIASES.get(key, key)

    def is_tracked(self) -> bool:
        return self.league in TRACKED_LEAGUES and self.market_type in {"spread", "moneyline", "total"}


class NovigMarketUpdate(BaseModel):
    """What the rest of the engine sees: the best price we could BUY this contract at."""

    market_id: str
    league: str
    market_type: str
    event_id: str
    home_team: str
    away_team: str
    outcome: str
    line: Optional[float] = None
    price: float = Field(gt=0, lt=1)      # best ask in dollars per $1 payout (= implied probability)
    available_volume: float = 0.0         # contracts resting at that best ask
    received_at: float = Field(default_factory=time.time)

    @property
    def market_key(self) -> str:
        return self.market_id


class MarketRegistry:
    """market_id -> MarketInfo. Only tracked NFL/NBA spreads, moneylines and totals are kept."""

    def __init__(self, markets: Iterable[Union[MarketInfo, dict]] = ()) -> None:
        self._markets: dict[str, MarketInfo] = {}
        for m in markets:
            self.register(m)

    def register(self, market: Union[MarketInfo, dict]) -> bool:
        try:
            info = market if isinstance(market, MarketInfo) else MarketInfo.model_validate(market)
        except ValidationError as exc:
            log.warning("REGISTRY invalid market skipped (%d errors): %s", exc.error_count(), market)
            return False
        if not info.is_tracked():
            return False
        self._markets[info.market_id] = info
        return True

    def get(self, market_id: str) -> Optional[MarketInfo]:
        return self._markets.get(market_id)

    def __len__(self) -> int:
        return len(self._markets)

    @classmethod
    def from_json_file(cls, path: Union[str, Path]) -> "MarketRegistry":
        data = json.loads(Path(path).read_text())
        return cls(data if isinstance(data, list) else data.get("markets", []))


class OrderBook:
    """Resting volume by price (in cents) for one market. 'sell' orders are what we can buy from."""

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}   # buy orders:  price_cents -> contracts
        self.asks: dict[float, float] = {}   # sell orders: price_cents -> contracts
        self.last_trade_cents: Optional[float] = None

    def apply(self, tick: TapeTick) -> None:
        if tick.action in PRINT_ACTIONS:
            self.last_trade_cents = tick.price_cents
            return
        levels = self.bids if tick.side == "buy" else self.asks
        current = levels.get(tick.price_cents, 0.0)
        if tick.action in ADD_ACTIONS:
            new = current + tick.volume
        elif tick.action in REMOVE_ACTIONS:
            new = current - tick.volume
        else:  # no/unknown action: treat as a snapshot of the level
            new = tick.volume
        if new > 1e-9:
            levels[tick.price_cents] = new
        else:
            levels.pop(tick.price_cents, None)

    def best_ask(self) -> Optional[tuple[float, float]]:
        """(price_cents, volume) of the cheapest resting sell order, or None."""
        if not self.asks:
            return None
        p = min(self.asks)
        return p, self.asks[p]

    def best_bid(self) -> Optional[tuple[float, float]]:
        if not self.bids:
            return None
        p = max(self.bids)
        return p, self.bids[p]


def parse_message(raw: Union[str, bytes]) -> list[TapeTick]:
    """
    Turn one raw WebSocket message into a list of TapeTicks.

    Accepts a single tick, a list of ticks, or an envelope like
    {"type": "PLACE", "data": [...]}; an envelope's type becomes the action of
    ticks that do not carry their own. Never raises: bad input is logged and skipped.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.warning("FEED_PARSE_ERROR non-JSON message skipped: %s", exc)
        return []

    envelope_action = None
    if isinstance(payload, dict) and isinstance(payload.get("data"), (list, dict)):
        envelope_action = payload.get("type") or payload.get("action")
        payload = payload["data"]
    items = payload if isinstance(payload, list) else [payload]

    ticks: list[TapeTick] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if envelope_action and not any(k in item for k in ("action", "type", "event")):
            item = {**item, "action": envelope_action}
        try:
            ticks.append(TapeTick.model_validate(item))
        except ValidationError as exc:
            log.debug("FEED_SKIP unrecognised item (%d errors): %s", exc.error_count(), item)
    return ticks


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
        feed = NovigFeed(registry=MarketRegistry([...]), on_update=my_handler)
        task = asyncio.create_task(feed.run())
        ...
        await feed.stop()

    on_update(update, previous) fires whenever the best ask (price or volume)
        of a registered market changes. `previous` is None the first time.
    on_state_change(state, details) fires on CONNECTING / CONNECTED /
        DISCONNECTED / STOPPED transitions.
    """

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        registry: Optional[MarketRegistry] = None,
        on_update: Optional[UpdateCallback] = None,
        on_state_change: Optional[StateCallback] = None,
        subscribe_messages: Iterable[dict] = (),
        reconnect_delay: float = RECONNECT_DELAY_SECONDS,
        ping_interval: float = PING_INTERVAL_SECONDS,
        ping_timeout: float = PING_TIMEOUT_SECONDS,
        stale_after: float = STALE_STREAM_SECONDS,
        open_timeout: float = OPEN_TIMEOUT_SECONDS,
    ) -> None:
        self.url = url or DEFAULT_NOVIG_WS_URL
        self._token = token if token is not None else os.environ.get(TOKEN_ENV_VAR)
        self.registry = registry if registry is not None else MarketRegistry()
        self.on_update = on_update
        self.on_state_change = on_state_change
        self.subscribe_messages = list(subscribe_messages)
        self.reconnect_delay = reconnect_delay
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.stale_after = stale_after
        self.open_timeout = open_timeout

        # Local memory. Both are wiped on every drop.
        self.books: dict[str, OrderBook] = {}
        self.latest: dict[str, NovigMarketUpdate] = {}   # last reported best ask per market_id

        # Observability counters (read by tests and the supervisor).
        self.connected = asyncio.Event()
        self.connect_count = 0
        self.disconnect_count = 0
        self.messages_received = 0
        self.last_recovery_seconds: Optional[float] = None

        self._unknown_markets: set[str] = set()
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
        """One connection lifetime: handshake, subscribe, then read until something breaks."""
        await self._emit_state("CONNECTING", {"url": self.url})
        async with connect(self.url, **self._connect_kwargs()) as ws:
            self._ws = ws
            for msg in self.subscribe_messages:
                await ws.send(json.dumps(msg))
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
                    for tick in parse_message(raw):
                        await self._process(tick)
            finally:
                heartbeat.cancel()
                self._ws = None

    async def _heartbeat_logger(self, ws) -> None:
        """Log round-trip latency measured by the protocol ping/pong."""
        while True:
            await asyncio.sleep(self.ping_interval)
            log.info("HEARTBEAT feed ping latency=%.1fms msgs=%d books=%d",
                     ws.latency * 1000, self.messages_received, len(self.books))

    def _handle_drop(self, exc: BaseException) -> None:
        """Log the failure and wipe stale state. Must never raise."""
        was_connected = self.connected.is_set()
        self.connected.clear()
        stale_frames = len(self.latest)
        stale_books = len(self.books)
        self.latest.clear()   # never trade on prices from a dead connection
        self.books.clear()    # books must be rebuilt from fresh ticks after reconnect
        if was_connected:
            self.disconnect_count += 1
            self._dropped_at = time.monotonic()
        elif self._dropped_at is None:
            self._dropped_at = time.monotonic()
        log.warning(
            "CONN dropped (%s: %s); cleared %d stale price frames and %d order books",
            type(exc).__name__, exc, stale_frames, stale_books,
        )
        asyncio.get_running_loop().create_task(
            self._emit_state("DISCONNECTED", {"error": f"{type(exc).__name__}: {exc}",
                                              "cleared_frames": stale_frames, "cleared_books": stale_books})
        )

    async def _process(self, tick: TapeTick) -> None:
        book = self.books.setdefault(tick.market_id, OrderBook())
        book.apply(tick)

        info = self.registry.get(tick.market_id)
        if info is None:
            if tick.market_id not in self._unknown_markets:
                self._unknown_markets.add(tick.market_id)
                log.info("FEED_SKIP market %s not in registry (untracked or unknown); ignoring its ticks",
                         tick.market_id)
            return

        ask = book.best_ask()
        previous = self.latest.get(tick.market_id)
        if ask is None:
            if previous is not None:
                log.info("FEED_NO_ASK %s: no resting sell orders, market not buyable", tick.market_id)
                self.latest.pop(tick.market_id, None)
            return

        price_cents, volume = ask
        if previous is not None and previous.price == price_cents / 100 and previous.available_volume == volume:
            return  # best ask unchanged: nothing to report
        update = NovigMarketUpdate(
            market_id=info.market_id, league=info.league, market_type=info.market_type,
            event_id=info.event_id, home_team=info.home_team, away_team=info.away_team,
            outcome=info.outcome, line=info.line, price=price_cents / 100, available_volume=volume,
        )
        self.latest[tick.market_id] = update
        if previous is None:
            log.debug("FEED_SNAPSHOT %s", update.model_dump_json())
        elif previous.price != update.price:
            log.info("LINE_SHIFT %s", json.dumps(format_shift(update, previous)))
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
    """Clean, human-readable summary of a best-ask move."""
    return {
        "market_id": update.market_id,
        "league": update.league,
        "market": update.market_type,
        "event": f"{update.away_team} @ {update.home_team}",
        "outcome": update.outcome,
        "line": update.line,
        "best_ask": {"from": previous.price, "to": update.price},
        "volume": update.available_volume,
    }


# --------------------------------------------------------------------------
# Standalone entry point
# --------------------------------------------------------------------------
async def _main() -> None:
    path = os.environ.get("NOVIG_MARKETS_FILE")
    registry = MarketRegistry.from_json_file(path) if path else MarketRegistry()
    feed = NovigFeed(registry=registry, url=os.environ.get("NOVIG_WS_URL"))
    await feed.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
