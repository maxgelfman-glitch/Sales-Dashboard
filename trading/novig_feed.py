"""
novig_feed.py — Real-time Novig tape ingestion.

WHAT THIS MODULE DOES
    1. Connects to Novig's tape (QA by default) with the NOVIG_BEARER_TOKEN.
    2. Immediately subscribes on the SAME socket to the public tape
       {"event": "subscribe", "channel": "tape"} and, in live mode, to our private
       executions {"event": "subscribe", "channel": "orders"}.
    3. Parses tape ticks keyed by outcomeId (outcomeId, price_cents, side, volume),
       keeps a live order book per outcome, and reports the best ask (what we
       can buy at) and best bid (what we can sell at) whenever either changes.
    4. Routes private execution slips (anything carrying an order_id) to on_slip;
       a slip is NEVER applied to the public order book.
    5. Reconnect / heartbeat / watchdog behaviour comes from ws_base.py.

NOVIG DATA MODEL (per the brief and docs.novig.com)
    Event (a game) -> Markets -> exactly two mutually exclusive Outcomes (outcomeId).
    Orders and ticks reference outcomeIds. The MarketRegistry below mirrors this:
    every outcome knows its market and its SIBLING outcome (the other side).
    The registry is filled at startup by the REST bootstrap (novig_rest.py).

VERIFIED vs ASSUMED
    Verified (brief + docs search): URLs, subscription payload, outcomeId model,
        tick fields outcomeId / price_cents / side / volume, 15s server pings.
    ASSUMED (docs.novig.com is unreachable from the build sandbox):
        * envelope shape {"event"|"type": ..., "data": tick | [ticks]}
        * optional per-tick action PLACE / CANCEL / FILL / TRADE; `volume` is a
          change in contracts for PLACE/CANCEL/FILL, a level snapshot when absent
    All assumptions live in TapeTick, OrderBook.apply and parse_message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Literal, Optional, Union

from pydantic import AliasChoices, BaseModel, Field, ValidationError, field_validator

from novig_private import parse_slips
from ws_base import (  # re-exported: tests and callers use novig_feed.RECONNECT_DELAY_SECONDS etc.
    OPEN_TIMEOUT_SECONDS,
    PING_INTERVAL_SECONDS,
    PING_TIMEOUT_SECONDS,
    RECONNECT_DELAY_SECONDS,
    STALE_STREAM_SECONDS,
    ResilientWebSocketFeed,
    StaleStreamError,
    StateCallback,
    safe_call,
)

__all__ = ["RECONNECT_DELAY_SECONDS", "StaleStreamError", "NovigFeed", "MarketRegistry", "MarketInfo",
           "MarketUpdate", "NovigMarketUpdate", "OrderBook", "TapeTick", "parse_message"]

DEFAULT_NOVIG_WS_URL = "wss://api-qa.novig.us/tape"  # QA / staging — safe default
NOVIG_PROD_WS_URL = "wss://api.novig.com/tape"       # production — use deliberately
TOKEN_ENV_VAR = "NOVIG_BEARER_TOKEN"
# One socket, two channels (per the brief): public prices + our private executions.
TAPE_SUBSCRIBE = {"event": "subscribe", "channel": "tape"}
ORDERS_SUBSCRIBE = {"event": "subscribe", "channel": "orders"}
SUBSCRIBE_PAYLOAD = TAPE_SUBSCRIBE        # paper mode needs only public prices

# Two-way markets only. Soccer is NOT here on purpose: its moneyline has a draw (3 outcomes), so buying both
# teams is not a hedge. College leagues need a team list built from the venues' own names first.
TRACKED_LEAGUES = frozenset({"NFL", "NBA", "MLB", "NHL", "WNBA"})
MAX_BOOK_LEVELS = 10                     # ask levels passed to the engine per update
MARKET_TYPE_ALIASES = {
    "spread": "spread", "point_spread": "spread", "pointspread": "spread", "handicap": "spread", "ats": "spread",
    "run_line": "spread", "runline": "spread", "puck_line": "spread", "puckline": "spread", "spreads": "spread",
    "moneyline": "moneyline", "money_line": "moneyline", "ml": "moneyline", "h2h": "moneyline",
    "winner": "moneyline", "game_winner": "moneyline",
    "total": "total", "totals": "total", "game_total": "total", "over_under": "total", "ou": "total",
}

ADD_ACTIONS = {"PLACE", "ADD", "NEW"}
REMOVE_ACTIONS = {"CANCEL", "FILL", "REMOVE", "DELETE"}
PRINT_ACTIONS = {"TRADE", "MATCH"}
KNOWN_ACTIONS = ADD_ACTIONS | REMOVE_ACTIONS | PRINT_ACTIONS

log = logging.getLogger("trading.feed")


def canonical_market_type(v: str) -> str:
    key = v.strip().lower().replace(" ", "_").replace("-", "_")
    return MARKET_TYPE_ALIASES.get(key, key)


# --------------------------------------------------------------------------
# Data models
# --------------------------------------------------------------------------
class TapeTick(BaseModel):
    """One tape tick for one outcome."""

    outcome_id: str = Field(validation_alias=AliasChoices("outcomeId", "outcome_id"))
    price_cents: float = Field(validation_alias=AliasChoices("price_cents", "priceCents"))
    side: Literal["buy", "sell"]
    volume: float = Field(ge=0)
    action: Optional[str] = Field(default=None, validation_alias=AliasChoices("action", "type"))

    @field_validator("outcome_id", mode="before")
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
    """Metadata for one tradable outcome on one venue."""

    venue: str = "novig"
    outcome_id: str                           # Novig outcomeId / Kalshi market ticker
    market_id: str = ""                       # Novig marketId (holds exactly two outcomes)
    sibling_outcome_id: Optional[str] = None  # the other outcome of the same market
    league: str
    market_type: str
    event_id: str
    home_team: str
    away_team: str
    outcome: str                              # team name, or "over"/"under"
    line: Optional[float] = None              # this outcome's spread, or the total
    start_time: Optional[float] = None        # scheduled game start (epoch seconds, UTC); drives the pregame cutoff

    @field_validator("outcome_id", "market_id", "event_id", mode="before")
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
        return canonical_market_type(v)

    def is_tracked(self) -> bool:
        return self.league in TRACKED_LEAGUES and self.market_type in {"spread", "moneyline", "total"}


class MarketUpdate(BaseModel):
    """What the engine sees for one outcome on one venue: top of book."""

    venue: str = "novig"
    outcome_id: str
    market_id: str = ""
    sibling_outcome_id: Optional[str] = None
    league: str
    market_type: str
    event_id: str
    home_team: str
    away_team: str
    outcome: str
    line: Optional[float] = None
    price: Optional[float] = Field(default=None, gt=0, lt=1)  # best ask ($ per $1 payout); None = nothing to buy
    available_volume: float = 0.0                             # contracts at the best ask
    best_bid: Optional[float] = Field(default=None, gt=0, lt=1)
    bid_volume: float = 0.0
    # every ask level, cheapest first: [(price, contracts), ...] (up to MAX_BOOK_LEVELS). Lets the engine buy
    # through several prices while each one still has edge. Empty = only the best ask is known.
    ask_levels: list[tuple[float, float]] = Field(default_factory=list)
    start_time: Optional[float] = None                        # scheduled game start (epoch seconds, UTC)
    received_at: float = Field(default_factory=time.time)

    @classmethod
    def from_info(cls, info: MarketInfo, **top_of_book: Any) -> "MarketUpdate":
        return cls(**info.model_dump(), **top_of_book)


NovigMarketUpdate = MarketUpdate  # backwards-compatible name


class MarketRegistry:
    """outcome_id -> MarketInfo. Only tracked NFL/NBA spreads, moneylines and totals are kept."""

    def __init__(self, markets: Iterable[Union[MarketInfo, dict]] = ()) -> None:
        self._outcomes: dict[str, MarketInfo] = {}
        for m in markets:
            self.register(m)

    def register(self, market: Union[MarketInfo, dict]) -> bool:
        try:
            info = market if isinstance(market, MarketInfo) else MarketInfo.model_validate(market)
        except ValidationError as exc:
            log.warning("REGISTRY invalid outcome skipped (%d errors): %s", exc.error_count(), market)
            return False
        if not info.is_tracked():
            return False
        self._outcomes[info.outcome_id] = info
        return True

    def replace_all(self, markets: Iterable[Union[MarketInfo, dict]]) -> int:
        """Atomically swap in a fresh snapshot (used by the periodic REST bootstrap)."""
        fresh = MarketRegistry(markets)
        self._outcomes = fresh._outcomes
        return len(self._outcomes)

    def get(self, outcome_id: str) -> Optional[MarketInfo]:
        return self._outcomes.get(outcome_id)

    def sibling(self, outcome_id: str) -> Optional[MarketInfo]:
        info = self.get(outcome_id)
        return self.get(info.sibling_outcome_id) if info and info.sibling_outcome_id else None

    def all(self) -> list[MarketInfo]:
        return list(self._outcomes.values())

    def __len__(self) -> int:
        return len(self._outcomes)

    @classmethod
    def from_json_file(cls, path: Union[str, Path]) -> "MarketRegistry":
        data = json.loads(Path(path).read_text())
        return cls(data if isinstance(data, list) else data.get("outcomes", []))


class OrderBook:
    """Resting volume by price (cents) for one outcome."""

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
        else:
            new = tick.volume
        if new > 1e-9:
            levels[tick.price_cents] = new
        else:
            levels.pop(tick.price_cents, None)

    def best_ask(self) -> Optional[tuple[float, float]]:
        if not self.asks:
            return None
        p = min(self.asks)
        return p, self.asks[p]

    def best_bid(self) -> Optional[tuple[float, float]]:
        if not self.bids:
            return None
        p = max(self.bids)
        return p, self.bids[p]

    def ask_levels(self, n: int = MAX_BOOK_LEVELS) -> list[tuple[float, float]]:
        """Cheapest n ask levels as (price as probability, contracts)."""
        return [(p / 100, self.asks[p]) for p in sorted(self.asks)[:n]]


def parse_message(raw: Union[str, bytes]) -> list[TapeTick]:
    """
    Turn one raw message into TapeTicks. Accepts a tick, a list of ticks, or an
    envelope {"event"|"type": ..., "data": tick | [ticks]}. An envelope value that
    is a known action (PLACE/CANCEL/...) is applied to ticks without their own.
    Control messages (e.g. subscription acks) yield nothing. Never raises.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.warning("FEED_PARSE_ERROR non-JSON message skipped: %s", exc)
        return []

    envelope_action = None
    if isinstance(payload, dict) and isinstance(payload.get("data"), (list, dict)):
        for key in ("action", "type", "event"):
            value = payload.get(key)
            if isinstance(value, str) and value.upper() in KNOWN_ACTIONS:
                envelope_action = value.upper()
                break
        payload = payload["data"]
    items = payload if isinstance(payload, list) else [payload]

    ticks: list[TapeTick] = []
    for item in items:
        if not isinstance(item, dict) or "order_id" in item or "orderId" in item:
            continue          # private execution slips are handled by novig_private.parse_slips
        if envelope_action and "action" not in item and "type" not in item:
            item = {**item, "action": envelope_action}
        try:
            ticks.append(TapeTick.model_validate(item))
        except ValidationError as exc:
            log.debug("FEED_SKIP unrecognised item (%d errors): %s", exc.error_count(), item)
    return ticks


UpdateCallback = Callable[[MarketUpdate, Optional[MarketUpdate]], Union[None, Awaitable[None]]]


class NovigFeed(ResilientWebSocketFeed):
    """
    on_update(update, previous) fires whenever the top of book (best ask or
    best bid, price or volume) of a registered outcome changes.
    """

    venue = "novig"

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        registry: Optional[MarketRegistry] = None,
        on_update: Optional[UpdateCallback] = None,
        on_state_change: Optional[StateCallback] = None,
        subscribe_messages: Optional[Iterable[dict]] = None,
        on_slip: Optional[Callable[[Any], Union[None, Awaitable[None]]]] = None,
        reconnect_delay: float = RECONNECT_DELAY_SECONDS,
        ping_interval: float = PING_INTERVAL_SECONDS,
        ping_timeout: float = PING_TIMEOUT_SECONDS,
        stale_after: float = STALE_STREAM_SECONDS,
        open_timeout: float = OPEN_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(url or DEFAULT_NOVIG_WS_URL, on_state_change, reconnect_delay, ping_interval,
                         ping_timeout, stale_after, open_timeout, logger=log)
        self._token = token if token is not None else os.environ.get(TOKEN_ENV_VAR)
        self.registry = registry if registry is not None else MarketRegistry()
        self.on_update = on_update
        self.on_slip = on_slip
        self.slips_received = 0
        self.subscribe_messages = [SUBSCRIBE_PAYLOAD] if subscribe_messages is None else list(subscribe_messages)
        self.books: dict[str, OrderBook] = {}
        self.latest: dict[str, MarketUpdate] = {}
        self._unknown: set[str] = set()
        if not self._token:
            log.warning("CONN no %s set; connecting WITHOUT authentication (only valid for mocks)", TOKEN_ENV_VAR)

    def _headers(self) -> Optional[dict[str, str]]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else None

    async def _on_open(self, ws) -> None:
        for msg in self.subscribe_messages:
            await ws.send(json.dumps(msg))
            log.info("CONN novig sent subscription %s", json.dumps(msg))

    async def _handle_raw(self, raw) -> None:
        for tick in parse_message(raw):
            await self._process(tick)
        if self.on_slip is not None:
            for slip in parse_slips(raw):
                self.slips_received += 1
                log.info("FILL_SLIP %s", slip.model_dump_json())
                await safe_call(self.on_slip, slip, logger=log)

    def _clear_state(self) -> dict[str, int]:
        counts = {"stale_price_frames": len(self.latest), "order_books": len(self.books)}
        self.latest.clear()
        self.books.clear()
        return counts

    def _heartbeat_extra(self) -> str:
        return f" books={len(self.books)}"

    async def _process(self, tick: TapeTick) -> None:
        book = self.books.setdefault(tick.outcome_id, OrderBook())
        book.apply(tick)

        info = self.registry.get(tick.outcome_id)
        if info is None:
            if tick.outcome_id not in self._unknown:
                self._unknown.add(tick.outcome_id)
                log.info("FEED_SKIP outcome %s not in registry (untracked or unknown); ignoring its ticks",
                         tick.outcome_id)
            return

        ask, bid = book.best_ask(), book.best_bid()
        top = dict(price=None if ask is None else ask[0] / 100, available_volume=0.0 if ask is None else ask[1],
                   best_bid=None if bid is None else bid[0] / 100, bid_volume=0.0 if bid is None else bid[1],
                   ask_levels=book.ask_levels())
        previous = self.latest.get(tick.outcome_id)
        if previous is not None and all(getattr(previous, k) == v for k, v in top.items()):
            return
        if ask is None and bid is None:
            self.latest.pop(tick.outcome_id, None)
            log.info("FEED_EMPTY %s: book empty", tick.outcome_id)
            return
        update = MarketUpdate.from_info(info, **top)
        self.latest[tick.outcome_id] = update
        if previous is None:
            log.debug("FEED_SNAPSHOT %s", update.model_dump_json())
        elif previous.price != update.price:
            log.info("LINE_SHIFT %s", json.dumps(format_shift(update, previous)))
        await safe_call(self.on_update, update, previous, logger=log)


def format_shift(update: MarketUpdate, previous: MarketUpdate) -> dict[str, Any]:
    return {
        "venue": update.venue, "outcome_id": update.outcome_id, "league": update.league,
        "market": update.market_type, "event": f"{update.away_team} @ {update.home_team}",
        "outcome": update.outcome, "line": update.line,
        "best_ask": {"from": previous.price, "to": update.price}, "volume": update.available_volume,
    }


async def _main() -> None:
    path = os.environ.get("NOVIG_MARKETS_FILE")
    registry = MarketRegistry.from_json_file(path) if path else MarketRegistry()
    await NovigFeed(registry=registry, url=os.environ.get("NOVIG_WS_URL")).run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
