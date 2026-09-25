"""
main_supervisor.py — The orchestrator for the dual-venue (Novig + Kalshi) engine.

WHAT RUNS CONCURRENTLY
    novig_feed    Novig tape WebSocket (novig_feed.py)
    kalshi_feed   Kalshi order-book WebSocket (kalshi_feed.py) — optional
    sharp_poller  sharp-odds provider polling (sharp_feed.py)
    maker         passive two-sided quoting on Novig (execution.MakerEngine)
    bootstrap     refreshes both venues' open markets from REST every 5 minutes
    heartbeat     system health line
    Any task that dies unexpectedly is logged and restarted.

STARTUP
    1. REST bootstrap: Novig events -> markets -> outcomes, Kalshi game markets,
       loaded into in-memory registries (no offline file needed).
    2. All tasks start.

FOR EVERY TOP-OF-BOOK UPDATE (either venue)
    names normalised -> (Novig) simulate paper fills of our resting quotes
    -> POSITION LOCK keyed on the canonical game, ACROSS venues:
         same side held      -> ignored
         opposite side held  -> only an ARBITRAGE hedge that profits in EVERY
                                settlement scenario (incl. NFL tie dead-heat)
    -> fresh sharp line -> edge (Kalshi: net of taker fee) -> liquidity cap
    -> GLOBAL EXPOSURE KILL-SWITCH ($15,000) -> PAPER taker order

ARBITRAGE SCENARIOS
    Each leg pays $1 per contract if its side wins, $0 if it loses. For NFL
    moneylines a tie is a third scenario: legs on a venue in DEAD_HEAT_VENUES
    settle at 50c (Novig's rule per the brief); any other venue is assumed to
    pay 0 on a tie, so a cross-venue NFL moneyline hedge is refused unless the
    tie scenario still profits. Spreads/totals must be half-point lines (no push).

MAKER KILL-SWITCH (one bulk cancel of every resting quote, timed vs 200ms)
    * Novig WebSocket drop
    * sharp spread/total line move > 0.5 points, or fair-probability move > 2c
    * global exposure kill-switch engaged
    * shutdown

TRADING MODE
    paper (default)  every order is simulated; fills are simulated from the tape.
    live             Novig orders go to the exchange through NovigOrderGateway.
        * Public prices and our private executions arrive on ONE Novig socket
          (channels "tape" and "orders").
        * A taker order RESERVES its full stake against the exposure limit and
          locks the game BEFORE it is sent; execution slips then set the
          position and exposure to what really filled (filled_volume is a
          cumulative total per order id: delta = new - previous). Any remainder still open after TAKER_FILL_TIMEOUT_SECONDS is
          cancelled, releasing the unused reservation (and the lock if nothing filled).
        * Maker quotes are registered as they are posted; their fills arrive as
          slips, become positions, and pull every other quote in that game.
        * Until the first execution slip of the session proves the "orders"
          channel works, an order that ends with NO observed fill keeps its lock
          and reservation (it may have filled unseen) and is logged CRITICAL.
        * If the socket is down, no new orders or quotes are sent and all resting
          quotes are bulk-cancelled: fills we cannot see would corrupt exposure.
        * Every live order and fill is appended to live_ledger.jsonl for
          line-by-line reconciliation with Novig's order history.
        * Kalshi is DATA-ONLY in live mode: there is no Kalshi order routing or
          Kalshi fill channel yet, and a hedge whose second leg is only simulated
          would be reported as risk-free while it is not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from execution import (
    MAKER_CANCEL_BUDGET_MS,
    MAKER_LINE_MOVE_POINTS,
    MAKER_ML_FAIR_MOVE,
    MAX_STAKE_USD,
    MIN_EDGE,
    GLOBAL_EXPOSURE_LIMIT_USD,
    ExposureMonitor,
    MakerEngine,
    MakerTarget,
    NovigQuote,
    SharpQuote,
    american_to_decimal,
    fair_devig,
    set_devig_method,
    DEVIG_METHODS,
    devig,
    evaluate_kalshi_edge,
    evaluate_market_edge,
    kalshi_fee_per_contract,
    kalshi_taker_fee,
)
from kalshi_feed import (
    DEFAULT_KALSHI_REST_BASE,
    DEFAULT_KALSHI_WS_URL,
    KALSHI_PROD_REST_BASE,
    KALSHI_PROD_WS_URL,
    KalshiFeed,
    KalshiRestClient,
    load_private_key,
    series_from_env,
)
from novig_feed import (
    DEFAULT_NOVIG_WS_URL,
    NOVIG_PROD_WS_URL,
    ORDERS_SUBSCRIBE,
    TAPE_SUBSCRIBE,
    MarketRegistry,
    MarketUpdate,
    NovigFeed,
)
from novig_rest import NovigOrderGateway, NovigRestClient, PaperOrderGateway, order_body
from sharp_feed import (
    MockSharpSource,
    NoSharpSource,
    ProviderConfig,
    ProviderSharpSource,
    SharpBook,
    SharpFetch,
    SharpLine,
    SharpPoller,
)
from alerts import AlertHandler
from kalshi_trading import session_tag
from novig_private import FillSlip
from research import DEPTH_SAMPLE_SECONDS, MARKOUT_DELAYS, GapTracker, ResearchRecorder
from settlement import (
    BASELINE_CAPITAL_USD,
    DEFAULT_POSITIONS_PATH,
    SETTLEMENT_SWEEP_SECONDS,
    PositionsClient,
    load_ledger_settlements,
    settlement_pnl,
)
from team_normalizer import DYNAMIC_LEAGUES, normalize_outcome, normalize_team_name, orient, register_game

LOG_FILE_NAME = "trading_engine.log"
LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)-20s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

ARB_MIN_PROFIT_PER_CONTRACT = 0.01     # 1c per contract locked in, in the WORST scenario
# Venues settling NFL moneyline ties at 50c per team contract:
#   novig  — push/dead-heat rule (per the brief)
#   kalshi — "$1 divided by the number of tied teams" (per the brief; matches public
#            Kalshi help/market-FAQ descriptions: both team markets settle at 50c)
DEAD_HEAT_VENUES = frozenset({"novig", "kalshi"})
DEAD_HEAT_PAYOUT = 0.50
BOOTSTRAP_REFRESH_SECONDS = 300.0
TAKER_FILL_TIMEOUT_SECONDS = 2.0       # live: cancel any unfilled taker remainder after this
TAKER_CUTOFF_SECONDS = 60.0            # no new orders/hedges on a game this long before its scheduled start
MAKER_CUTOFF_SECONDS = 180.0           # resting quotes are pulled earlier: late news picks off stale quotes
NEAR_START_REFRESH_SECONDS = 30.0      # re-read Novig's pregame list this often while a game is close to start
NEAR_START_WINDOW_SECONDS = 15 * 60    # ... "close" = within this long of its scheduled start
VANISHED_FLAG_WINDOW_SECONDS = 60 * 60 # a game gone from Novig's pregame list within 1h of start = treat as live
MIN_PARTIAL_HEDGE_CONTRACTS = 10       # smaller partial hedges are not worth an order
# How to buy through several ask levels. "staggered": one order per level at that level's price (default:
# Novig is believed to fill a taker at its limit). "single": one order limited at the worst level (only right
# if the exchange fills cheaper levels first). "off": best level only.
MULTI_LEVEL_MODES = ("staggered", "single", "off")
# Leagues where venues' settlement rules differ in ways that break a "locked" pair across venues. Tennis: a
# retirement mid-match is void on some venues and a win for the opponent on others, so both legs could lose.
# Directional trades and same-venue hedges are still allowed.
NO_CROSS_VENUE_HEDGE_LEAGUES = frozenset({"TENNIS"})
# A game's moneyline, spread and total are strongly correlated: cap the UNHEDGED money per game across all
# of its markets (hedged slices are risk-free and do not count).
GAME_EXPOSURE_LIMIT_USD = 1_000.0
# Realised (settled) net loss in one UTC day that stops new takers and pulls quotes until the next day.
DAILY_LOSS_LIMIT_USD = 2_000.0

log = logging.getLogger("trading.supervisor")
bridge_log = logging.getLogger("trading.bridge")
order_log = logging.getLogger("trading.orders")


# ==========================================================================
# Logging
# ==========================================================================
def setup_logging(log_dir: str | Path = "logs", level: int = logging.INFO, console: bool = True,
                  backup_days: int = 30, alert_url: Optional[str] = None) -> Path:
    """Configure the 'trading' logger tree: daily-rolling file + optional console. Safe to call twice."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / LOG_FILE_NAME
    root = logging.getLogger("trading")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)
    root.propagate = False
    formatter = logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT)
    file_handler = TimedRotatingFileHandler(path, when="midnight", backupCount=backup_days, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
    if alert_url:
        root.addHandler(AlertHandler(alert_url))     # CRITICAL lines -> phone / chat (alerts.py)
    return path


# ==========================================================================
# Positions
# ==========================================================================
class PaperOrder(BaseModel):
    order_id: int
    kind: Literal["DIRECTIONAL", "ARB_HEDGE", "ARB_PAIR", "MAKER_FILL", "RESTORED"]
    venue: str
    outcome_id: str
    event_id: str
    league: str
    market_type: str
    side: str                 # canonical team or "over"/"under" we are LONG
    line: Optional[float]
    price: float              # per-contract price paid for `side` (excl. fees)
    contracts: int
    fee_usd: float = 0.0
    stake_usd: float          # total cash out incl. fees
    edge: Optional[float]
    capped: bool
    placed_at: float
    live: bool = False                      # True = real exchange order
    pending: bool = False                   # live taker still waiting for fills
    requested_contracts: int = 0            # live: size sent to the exchange
    exchange_order_id: Optional[str] = None

    @property
    def position_id(self) -> str:
        return f"order-{self.order_id}"


class LiveOrder(BaseModel):
    """Exchange order we are tracking fills for."""
    exchange_order_id: str
    kind: Literal["DIRECTIONAL", "ARB_HEDGE", "MAKER"]
    outcome_id: str
    key: tuple
    requested: int
    limit_price: float                      # $ per contract for the side we BUY (maker sell: 1 - quote)
    maker_side: Optional[Literal["buy", "sell"]] = None
    fair_prob: Optional[float] = None
    leg_id: Optional[int] = None            # PaperOrder.order_id of the position leg
    expected_levels: list[tuple[float, int]] = Field(default_factory=list)   # book we expected to sweep
    venue: str = "novig"
    fill_mode: str = "cumulative"           # Kalshi fills arrive as increments
    exchange_confirmed_zero: bool = False    # the exchange's own order record says nothing filled
    fees: float = 0.0                        # Kalshi taker fees included in fill_cost
    expected_price: Optional[float] = None   # best ask when we decided (slippage is measured against it)
    edge: Optional[float] = None
    locked_per_contract: Optional[float] = None   # pairs / hedges: profit per contract if both legs fill
    sent_at: Optional[float] = None
    acked_at: Optional[float] = None
    first_fill_at: Optional[float] = None
    filled: float = 0.0
    fill_cost: float = 0.0
    done: bool = False


class MarketPosition(BaseModel):
    legs: list[PaperOrder]
    hedged: bool = False      # True = no further hedge wanted (fully hedged, or a hedge order is in flight)

    @property
    def primary(self) -> PaperOrder:
        return self.legs[0]

    def hedged_contracts(self) -> float:
        """Contracts of the opposite side already bought (or being bought) against the first leg."""
        return sum(leg.requested_contracts if leg.pending else leg.contracts for leg in self.legs[1:])

    def unhedged(self) -> float:
        return self.primary.contracts - self.hedged_contracts()


GameKey = tuple[str, str, str, str]   # (league, home, away, market_type) — venue independent


def write_ledger_line(path: Path, event: str, **fields) -> None:
    """Append-only JSON line (opened in "a" mode, flushed per line). Never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": round(time.time(), 3), "event": event, **fields}, default=str) + "\n")
    except OSError as exc:
        log.error("LEDGER write failed (%s): %s", path, exc)


def _floor_cents(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100.0


def _is_half_point(line: Optional[float]) -> bool:
    return line is not None and abs((abs(line) % 1) - 0.5) < 1e-9


def order_cost(venue: str, contracts: int, price: float) -> tuple[float, float]:
    """(total cash out incl. fees, fee) for buying `contracts` at `price` on `venue`."""
    fee = kalshi_taker_fee(contracts, price * 100) if venue == "kalshi" else 0.0
    return round(contracts * price + fee, 2), fee


def _sweep_avg(levels: list, contracts: float) -> float:
    """Average price of buying `contracts` from `levels` (cheapest first) at each level's own price."""
    left, cost = contracts, 0.0
    for price, n in levels:
        take = min(n, left)
        cost += take * price
        left -= take
        if left <= 0:
            break
    done = contracts - max(left, 0)
    return round(cost / done, 6) if done else 0.0


PROPHETX_WIN_FEE = 0.02      # ProphetX: 2% of NET winnings on straights, charged only when the bet wins


def win_payout(venue: str, price: float) -> float:
    """What one winning contract bought at `price` pays after win-only fees (ProphetX 2% of net winnings)."""
    return 1.0 - PROPHETX_WIN_FEE * (1.0 - price) if venue == "prophetx" else 1.0


def arbitrage_scenarios(first: PaperOrder, hedge_venue: str, contracts: int, hedge_cost: float,
                        league: str, market_type: str, hedge_price: Optional[float] = None) -> dict[str, float]:
    """
    Net P&L of the hedged slice under every way the game can settle: `contracts` of the first leg
    (its cost pro-rated) plus the hedge. With a partial hedge the rest of the first leg stays a plain
    directional position, which was already a +EV bet on its own.
    """
    held = getattr(first, "contracts", 0) or 0
    first_cost = first.stake_usd * contracts / held if held > contracts else first.stake_usd
    total_cost = first_cost + hedge_cost
    first_pay = win_payout(getattr(first, "venue", "novig"), getattr(first, "price", 1.0))
    hedge_pay = win_payout(hedge_venue, hedge_price if hedge_price is not None else 1.0)
    out = {"first_side_wins": contracts * first_pay - total_cost,
           "other_side_wins": contracts * hedge_pay - total_cost}
    if league == "NFL" and market_type == "moneyline":
        tie_payout = sum(DEAD_HEAT_PAYOUT if v in DEAD_HEAT_VENUES else 0.0 for v in (first.venue, hedge_venue))
        out["tie"] = contracts * tie_payout - total_cost
    return {k: round(v, 2) for k, v in out.items()}


# ==========================================================================
# The supervisor
# ==========================================================================
class Supervisor:
    def __init__(
        self,
        feed_url: str = DEFAULT_NOVIG_WS_URL,
        registry: Optional[MarketRegistry] = None,
        sharp_fetch: Optional[SharpFetch] = None,
        sharp_poll_interval: float = 2.0,
        sharp_max_age: float = 30.0,
        heartbeat_interval: float = 5.0,
        token: Optional[str] = None,
        exposure: Optional[ExposureMonitor] = None,
        feed_kwargs: Optional[dict] = None,
        # --- optional components ---
        novig_rest: Optional[NovigRestClient] = None,
        kalshi_url: Optional[str] = None,
        kalshi_registry: Optional[MarketRegistry] = None,
        kalshi_rest: Optional[KalshiRestClient] = None,
        kalshi_auth: tuple = (None, None),
        kalshi_kwargs: Optional[dict] = None,
        maker_enabled: bool = True,
        maker_gateway=None,
        maker_kwargs: Optional[dict] = None,
        bootstrap_refresh: float = BOOTSTRAP_REFRESH_SECONDS,
        # --- live trading ---
        live: bool = False,
        order_gateway=None,
        subscribe_messages: Optional[list] = None,
        fill_volume_mode: Literal["cumulative", "incremental"] = "cumulative",
        max_stake: float = MAX_STAKE_USD,
        taker_fill_timeout: float = TAKER_FILL_TIMEOUT_SECONDS,
        ledger_path: Optional[str | Path] = None,
        live_plan: Optional["LivePlan"] = None,
        positions_client: Optional[PositionsClient] = None,
        settlement_interval: float = SETTLEMENT_SWEEP_SECONDS,
        sync_retries: int = 5,
        sync_retry_delay: float = 10.0,
        # --- pregame cutoff & research ---
        taker_cutoff_s: float = TAKER_CUTOFF_SECONDS,
        maker_cutoff_s: float = MAKER_CUTOFF_SECONDS,
        cutoff_overrides: Optional[dict[str, tuple[float, float]]] = None,
        require_start_time: Optional[bool] = None,
        research: Optional[ResearchRecorder] = None,
        depth_sample_s: float = DEPTH_SAMPLE_SECONDS,
        multi_level_mode: str = "staggered",
        game_exposure_limit: float = GAME_EXPOSURE_LIMIT_USD,
        daily_loss_limit: Optional[float] = DAILY_LOSS_LIMIT_USD,
        devig_method: str = "multiplicative",
        sharp_weights: Optional[dict[str, float]] = None,
        taker_require_sharp_moved_last: bool = False,
        taker_max_sharp_move_age_s: Optional[float] = None,
        kalshi_gateway=None,
        kalshi_positions_client=None,
        arb_pairs_enabled: bool = True,
        prophetx_feed=None,
        paper_execution: str = "instant",
        sim_latency_ms: float = 500.0,
        sim_depth_haircut: float = 0.5,
        combo_quoter=None,
    ) -> None:
        if live and order_gateway is None:
            raise ValueError("live mode needs an order_gateway")
        if subscribe_messages is None:
            subscribe_messages = [TAPE_SUBSCRIBE, ORDERS_SUBSCRIBE] if live else [TAPE_SUBSCRIBE]
        # No sharp source = MEASUREMENT mode: prices, gaps, depth and research rows are recorded, but nothing
        # needs a fair value (no directional takers, no maker quotes). Arbitrage research works on venue prices.
        self.sharp_enabled = sharp_fetch is not None
        if sharp_fetch is None:
            sharp_fetch = NoSharpSource()
        set_devig_method(devig_method)                  # engine-wide: edges, maker fair values, research
        self.devig_method = devig_method
        # "who moved first": optional taker filters (off by default until the research data says they pay)
        self.taker_require_sharp_moved_last = taker_require_sharp_moved_last
        self.taker_max_sharp_move_age_s = taker_max_sharp_move_age_s
        self.last_sharp_change: dict[tuple, float] = {}   # (league, home, away, market_type) -> last real change
        self.book = SharpBook(max_age_seconds=sharp_max_age, on_move=self._on_sharp_move,
                              on_live=self._on_sharp_live, weights=sharp_weights)
        self.registry = registry if registry is not None else MarketRegistry()
        self.feed = NovigFeed(url=feed_url, token=token, registry=self.registry, on_update=self.on_market_update,
                              on_state_change=self.on_feed_state, subscribe_messages=subscribe_messages,
                              on_slip=self.on_fill_slip if live else None, **(feed_kwargs or {}))
        self.kalshi_registry = kalshi_registry if kalshi_registry is not None else MarketRegistry()
        self.kalshi: Optional[KalshiFeed] = None
        if kalshi_url or kalshi_rest or len(self.kalshi_registry):
            self.kalshi = KalshiFeed(url=kalshi_url, key_id=kalshi_auth[0], private_key=kalshi_auth[1],
                                     registry=self.kalshi_registry, on_update=self.on_market_update,
                                     on_state_change=self.on_feed_state, **(kalshi_kwargs or {}))
        self.novig_rest, self.kalshi_rest = novig_rest, kalshi_rest
        # ---- live Kalshi execution (off unless a Kalshi gateway is given in live mode) ----
        self.kalshi_gateway = kalshi_gateway
        self.kalshi_live = bool(live and kalshi_gateway is not None)
        self.kalshi_positions_client = kalshi_positions_client
        self.arb_pairs_enabled = arb_pairs_enabled      # buy BOTH sides when they lock a profit (no sharp needed)
        # ---- realistic paper execution: the live order path against a simulated exchange ----
        if paper_execution not in {"instant", "simulated"}:
            raise ValueError("paper_execution must be 'instant' or 'simulated'")
        self.sim = paper_execution == "simulated" and not live
        self.sim_gateways: dict[str, Any] = {}
        if self.sim:
            from sim_exchange import SimulatedGateway
            self.sim_gateways = {v: SimulatedGateway(v, self._latest_for, self.on_fill_slip, sim_latency_ms,
                                                     sim_depth_haircut) for v in ("novig", "kalshi", "prophetx")}
        self.sim_latency_ms, self.sim_depth_haircut = sim_latency_ms, sim_depth_haircut
        self._edge_watch: dict[str, dict] = {}          # outcome_id -> open edge-survival measurement
        # ProphetX: price feed for paper trading and research only (never in live_venues until verified)
        self.prophetx = prophetx_feed
        if self.prophetx is not None:
            self.prophetx.on_update = self.on_market_update
            self.prophetx.on_state_change = self.on_feed_state
        self.kalshi_fills_confirmed = False            # set by the first Kalshi fill message of the session
        self.session_tag = session_tag()
        if self.kalshi_live and self.kalshi is not None:
            self.kalshi.on_fill = self.on_fill_slip
        self.poller = SharpPoller(sharp_fetch, self.book, sharp_poll_interval)
        self.exposure = exposure or ExposureMonitor()
        self.heartbeat_interval = heartbeat_interval
        self.bootstrap_refresh = bootstrap_refresh
        self.live = live
        self.order_gateway = order_gateway
        self.fill_volume_mode = fill_volume_mode
        self.max_stake = min(max_stake, MAX_STAKE_USD)          # may only LOWER the $1,000 ceiling
        self.taker_fill_timeout = taker_fill_timeout
        self.live_orders: dict[str, LiveOrder] = {}
        self.orders_channel_confirmed = False     # set by the first execution slip of the session
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self.live_plan = live_plan
        # ---- settlement / reconciliation (live) ----
        self.positions_client = positions_client
        self.settlement_interval = settlement_interval
        self.sync_retries, self.sync_retry_delay = sync_retries, sync_retry_delay
        self.processed_settlements, self.cumulative_pnl = load_ledger_settlements(self.ledger_path)
        self.synced = positions_client is None and kalshi_positions_client is None   # nothing to sync without one
        self.unconfirmed_legs: dict[int, float] = {}     # leg order_id -> time it became UNCONFIRMED
        # ---- pregame cutoff (never trade or quote into a live game) ----
        self.taker_cutoff_s = taker_cutoff_s
        self.maker_cutoff_s = max(maker_cutoff_s, taker_cutoff_s)
        self.cutoff_overrides = dict(cutoff_overrides or {})  # league -> (taker_s, maker_s)
        self.require_start_time = live if require_start_time is None else require_start_time
        self.game_start: dict[tuple, float] = {}          # (league, home, away) -> scheduled start (epoch)
        self.cutoff_phases: dict[tuple, set[str]] = {}     # gid -> {"maker", "taker", "start"} already run
        self._cutoff_noted: set[tuple] = set()
        self.live_games: dict[tuple, str] = {}             # gid -> why we believe it is in play (never trade)
        self.novig_games: set[tuple] = set()               # gids in Novig's pregame list at the last refresh
        self._last_bootstrap = 0.0
        # ---- research / measurement ----
        self.research = research
        self.gaps = GapTracker(research) if research is not None else None
        self.depth_sample_s = depth_sample_s
        self.last_price_change: dict[tuple[str, str], float] = {}   # (venue, outcome_id) -> ts of last ask change
        # Multi-level live orders rely on the exchange filling cheaper levels first (standard price-time
        # matching, unconfirmed for Novig). Switched off for the session if a fill proves otherwise.
        self.multi_level_live = True
        if multi_level_mode not in MULTI_LEVEL_MODES:
            raise ValueError(f"multi_level_mode must be one of {MULTI_LEVEL_MODES}")
        self.multi_level_mode = multi_level_mode
        # ---- correlation + loss controls ----
        self.game_exposure_limit = game_exposure_limit     # unhedged $ per GAME across all its markets
        self.daily_loss_limit = daily_loss_limit            # realised loss per UTC day that halts new risk
        self.daily_pnl: dict[str, float] = {}               # "YYYY-MM-DD" (UTC) -> settled net P&L
        self.loss_halted_day: Optional[str] = None
        self._last_hedge_levels: list[tuple[float, int]] = []
        self.maker_gateway = order_gateway if live else (maker_gateway or PaperOrderGateway())
        mk = dict(maker_kwargs or {})
        mk.setdefault("max_stake", self.max_stake)
        if live:
            mk.update(on_posted=self._register_quote, can_quote=self._live_ready,
                      on_cancelled=lambda ids, reason: self._ledger("CANCEL", exchange_order_ids=ids, reason=reason,
                                                                    source="maker"))
        self.maker: Optional[MakerEngine] = (
            MakerEngine(self.maker_gateway, self.exposure, self._maker_targets, **mk) if maker_enabled else None)
        self.positions: dict[GameKey, MarketPosition] = {}
        self.orders: list[PaperOrder] = []
        self.stats: Counter[str] = Counter()
        self.started_at = time.monotonic()
        self._background: set[asyncio.Task] = set()

        self._task_factories: dict[str, Callable[[], Awaitable[None]]] = {
            "novig_feed": self.feed.run, "heartbeat": self._heartbeat_loop}
        if self.sharp_enabled:
            self._task_factories["sharp_poller"] = self.poller.run
        if prophetx_feed is not None:
            self._task_factories["prophetx_feed"] = prophetx_feed.run
        self.combo = combo_quoter
        if combo_quoter is not None:
            combo_quoter.pricer.leg_fair = combo_quoter._leg_fair_with_fallback(self.combo_leg_fair)
            combo_quoter.allowed = self._combo_block_reason
            self._task_factories["combo_quoter"] = combo_quoter.run
            self._task_factories["combo_results"] = combo_quoter.results_loop
        if self.kalshi is not None:
            self._task_factories["kalshi_feed"] = self.kalshi.run
        if self.maker is not None:
            self._task_factories["maker"] = self.maker.run
        if novig_rest is not None or kalshi_rest is not None:
            self._task_factories["bootstrap"] = self._bootstrap_loop
        if positions_client is not None or kalshi_positions_client is not None:
            self._task_factories["settlement"] = self._settlement_loop
        self._task_factories["cutoff"] = self._cutoff_loop
        if research is not None:
            self._task_factories["research_depth"] = self._depth_loop
        self._index_start_times()
        self._halt_alerted = False
        self._load_today_pnl()
        if self.sim:                                    # the simulated exchange always reports its fills
            self.orders_channel_confirmed = self.kalshi_fills_confirmed = True

    # backwards-compatible alias used by older callers/tests
    @property
    def markets(self) -> dict[GameKey, MarketPosition]:
        return self.positions

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ---------------- bootstrap ----------------
    async def bootstrap(self) -> None:
        if self.novig_rest is not None:
            try:
                n = self.registry.replace_all(await self.novig_rest.fetch_open_markets())
                log.info("BOOTSTRAP novig registry now holds %d outcomes", n)
                self._index_start_times()
                self._check_vanished_games(n)
            except Exception as exc:  # noqa: BLE001 — retried by the refresh loop
                log.error("BOOTSTRAP novig failed (%s: %s); keeping previous registry", type(exc).__name__, exc)
        if self.kalshi_rest is not None and self.kalshi is not None:
            try:
                before = set(self.kalshi.tickers())
                n = self.kalshi_registry.replace_all(await self.kalshi_rest.fetch_open_markets())
                log.info("BOOTSTRAP kalshi registry now holds %d outcomes", n)
                self._index_start_times()
                if set(self.kalshi.tickers()) != before and self.kalshi._ws is not None:
                    log.info("BOOTSTRAP kalshi ticker set changed: reconnecting to resubscribe")
                    await self.kalshi._ws.close()
            except Exception as exc:  # noqa: BLE001
                log.error("BOOTSTRAP kalshi failed (%s: %s); keeping previous registry", type(exc).__name__, exc)

    async def _bootstrap_loop(self) -> None:
        """Refresh every bootstrap_refresh seconds; every 30s while any game is close to its start."""
        self._last_bootstrap = time.time()
        while True:
            await asyncio.sleep(min(5.0, self.bootstrap_refresh))
            interval = self.bootstrap_refresh
            if self._any_game_near_start():
                interval = min(interval, NEAR_START_REFRESH_SECONDS)
            if time.time() - self._last_bootstrap >= interval:
                self._last_bootstrap = time.time()
                await self.bootstrap()

    def _any_game_near_start(self) -> bool:
        now = time.time()
        return any(gid not in self.live_games and start - NEAR_START_WINDOW_SECONDS <= now <= start + 3600
                   for gid, start in self.game_start.items())

    def _novig_gids(self) -> set[tuple]:
        out = set()
        for info in self.registry.all():
            canon = self._canonical(MarketUpdate.from_info(info))
            if canon is not None:
                out.add(canon[0][:3])
        return out

    def _check_vanished_games(self, outcomes: int) -> None:
        """
        Novig's bootstrap lists only OPEN_PREGAME events. A game that drops out of that list close to its
        start has most likely gone live (or been suspended): stop trading it and pull its quotes.
        An empty response is treated as a glitch, not as "every game went live".
        """
        current = self._novig_gids()
        if outcomes == 0:
            log.warning("BOOTSTRAP novig returned no outcomes; live-game detection skipped this refresh")
            return
        now = time.time()
        for gid in self.novig_games - current:
            start = self.game_start.get(gid)
            if start is None or start - now <= VANISHED_FLAG_WINDOW_SECONDS:
                self.mark_game_live(gid, "no longer in Novig's pregame list")
        self.novig_games = current

    # ---------------- event handlers ----------------
    async def on_feed_state(self, state: str, details: dict) -> None:
        venue = details.get("venue", "novig")
        self.stats[f"conn_{venue}_{state.lower()}"] += 1
        log.info("CONN_STATE %s %s %s", venue, state, json.dumps(details, default=str))
        if state == "DISCONNECTED" and self.gaps is not None:
            self.gaps.drop_venue(venue)                 # stale prices must not count as cross-venue gaps
        if state == "DISCONNECTED" and venue == "novig" and self.maker is not None and self.maker.quotes:
            await self.maker.cancel_all("novig websocket drop")
        if state == "DISCONNECTED" and venue == "novig" and self.live:
            log.critical("LIVE novig socket down (prices AND executions): new orders halted; fills during the "
                         "outage may be missed — reconcile with the Novig order history if this persists")

    @property
    def executing(self) -> bool:
        """True when orders go through the live order path: live trading, or simulated paper execution."""
        return self.live or self.sim

    def _latest_for(self, venue: str, outcome_id: str):
        feed = {"novig": self.feed, "kalshi": self.kalshi, "prophetx": self.prophetx}.get(venue)
        return None if feed is None else feed.latest.get(outcome_id)

    def _live_ready(self, venue: str = "novig") -> bool:
        """Live orders only while that venue's socket (prices + our executions) is connected."""
        if self.sim:
            return True
        if venue == "kalshi":
            return (self.kalshi_live and self.synced and self.kalshi is not None
                    and self.kalshi.connected.is_set())
        return self.live and self.synced and self.feed.connected.is_set()

    def live_venues(self) -> set[str]:
        if self.sim:
            return {"novig", "kalshi", "prophetx"}
        return {"novig", "kalshi"} if self.kalshi_live else {"novig"}

    def _gateway(self, venue: str):
        if self.sim:
            return self.sim_gateways[venue]
        return self.kalshi_gateway if venue == "kalshi" else self.order_gateway

    def record_live_plan(self) -> None:
        """Written at LAUNCH (not by --check-config): which limits this live session runs under and who approved."""
        plan = self.live_plan
        if plan is None:
            return
        self._ledger("SCALE_UP_AUTHORIZED" if plan.scaled_up else "CANARY_LIMITS",
                     level="CRITICAL" if plan.scaled_up else "WARNING", approved_by=plan.approved_by,
                     max_stake_usd=plan.max_stake, exposure_limit_usd=plan.exposure_limit, maker=plan.maker_enabled,
                     canary={"max_stake_usd": CANARY_MAX_STAKE_USD, "exposure_limit_usd": CANARY_EXPOSURE_LIMIT_USD,
                             "maker": CANARY_MAKER_ENABLED})

    def _ledger(self, event: str, **fields) -> None:
        """Append one line per live order / cancel / slip / fill for reconciliation with Novig's order history."""
        if self.ledger_path is not None:
            write_ledger_line(self.ledger_path, event, **fields)

    def _on_sharp_move(self, key, old: SharpLine, new: SharpLine) -> None:
        """Called synchronously by SharpBook when a stored sharp line changes."""
        points_move = abs((new.line or 0.0) - (old.line or 0.0))
        fair_old, fair_new = self._fair(old), self._fair(new)
        fair_move = abs(fair_new - fair_old) if None not in (fair_old, fair_new) else 0.0
        log.info("SHARP_MOVE %s line %s -> %s (%.2f pts) fair %.4f -> %.4f", key, old.line, new.line, points_move,
                 fair_old or 0, fair_new or 0)
        market_key = tuple(key[:4])
        self.last_sharp_change[market_key] = time.time()
        # The classic stale-price edge: the sharp moved, the venue has not. Re-check every venue price of this
        # market NOW instead of waiting for the venue's next tick.
        self._spawn(self._reevaluate_market(market_key))
        if points_move > MAKER_LINE_MOVE_POINTS + 1e-9 or fair_move > MAKER_ML_FAIR_MOVE + 1e-9:
            if self.maker is not None and self.maker.quotes:
                self.stats["maker_line_kills"] += 1
                self._spawn(self.maker.cancel_all(
                    f"sharp move on {key}: {points_move:.2f} pts / {fair_move * 100:.2f}c fair"))

    async def _reevaluate_market(self, market_key: tuple) -> None:
        for upd in list(self._latest_updates()):
            canon = self._canonical(upd)
            if canon is not None and canon[0] == market_key and upd.price is not None:
                self.stats["sharp_move_reevaluations"] += 1
                await self.on_market_update(upd, upd)       # previous == current: the venue price did not move

    def moved_last(self, update: MarketUpdate, market_key: tuple) -> tuple[Optional[str], Optional[float]]:
        """
        (who changed price most recently: the venue or "sharp", seconds since the sharp's last real move).
        The venue side uses the last time THIS outcome's ask changed; the sharp side the last time the
        sharp line actually moved (not merely re-polled).
        """
        sharp_at = self.last_sharp_change.get(market_key)
        venue_at = self.last_price_change.get((update.venue, update.outcome_id))
        age = None if sharp_at is None else round(time.time() - sharp_at, 3)
        if sharp_at is None:
            return (update.venue if venue_at is not None else None), age
        if venue_at is None or sharp_at > venue_at:
            return "sharp", age
        return update.venue, age

    @staticmethod
    def _fair(line: SharpLine) -> Optional[float]:
        try:
            (p, _), _ = fair_devig([american_to_decimal(line.odds_for),
                                              american_to_decimal(line.odds_against)])
            return p
        except ValueError:
            return None

    def _canonical(self, update: MarketUpdate) -> Optional[tuple[GameKey, str]]:
        league = update.league
        home = normalize_team_name(update.home_team, league)
        away = normalize_team_name(update.away_team, league)
        side = normalize_outcome(update.outcome, league)
        if None in (home, away, side):
            return None
        home, away = orient(league, home, away)
        return (league, home, away, update.market_type), side

    async def on_market_update(self, update: MarketUpdate, previous: Optional[MarketUpdate]) -> None:
        self.stats["updates"] += 1
        self.stats[f"updates_{update.venue}"] += 1
        log.info("FEED_UPDATE %s %s %s %s %s %s line=%s ask=%s vol=%g bid=%s", update.venue, update.league,
                 update.outcome_id, update.event_id, update.market_type, update.outcome, update.line,
                 "none" if update.price is None else f"{update.price:.4f}", update.available_volume,
                 "none" if update.best_bid is None else f"{update.best_bid:.4f}")

        if update.venue == "prophetx" and update.league in DYNAMIC_LEAGUES:
            # college/tennis on ProphetX: only games Novig or Kalshi also list are worth matching
            register_game(update.league, update.home_team, update.away_team, update.start_time, create=False)
        canon = self._canonical(update)
        if canon is None:
            self.stats["unmapped"] += 1
            bridge_log.warning("BRIDGE unmapped %s outcome %s: home=%r away=%r outcome=%r -> skipped", update.venue,
                               update.outcome_id, update.home_team, update.away_team, update.outcome)
            return
        key, side = canon
        gid = key[:3]
        if update.start_time:
            self._note_start(gid, update.start_time)
        if previous is None or previous.price != update.price:
            self.last_price_change[(update.venue, update.outcome_id)] = time.time()
        if self.gaps is not None:
            self._research_gap(update, key, side)
        if self._edge_watch:
            self._check_edge_watch(update)

        if not self.live and update.venue == "novig" and self.maker is not None and self.maker.quotes:
            await self._simulate_maker_fills(update, key)

        blocked = self._trade_blocked(gid)
        shadow = False
        if blocked:
            self.stats["cutoff_blocked"] += 1
            if (gid, blocked) not in self._cutoff_noted:
                self._cutoff_noted.add((gid, blocked))
                log.info("CUTOFF no trading on %s: %s", gid, blocked)
            # Paper only: keep evaluating between the cutoff and the scheduled start (never once a game is
            # flagged live) and record what we WOULD have done, so the report shows what the cutoff costs.
            start = self.game_start.get(gid)
            shadow = (not self.live and self.research is not None and gid not in self.live_games
                      and start is not None and time.time() < start)
            if not shadow:
                return
        if update.price is None:
            return
        held = self.positions.get(key)
        if held is not None:
            if shadow:
                return
            if held.hedged:
                self.stats["lock_blocked"] += 1
                log.debug("POSITION_LOCK %s fully hedged; ignoring", key)
            elif side == held.primary.side:
                self.stats["lock_blocked"] += 1
                log.info("POSITION_LOCK already hold #%d on %s %s; ignoring same-side %s update",
                         held.primary.order_id, key, side, update.venue)
            else:
                await self._try_arbitrage(update, key, side, held)
            return

        # A locked pair needs no fair value at all: both sides across venues cost less than the payout.
        if self.arb_pairs_enabled and not shadow and await self._try_locked_pair(update, key, side):
            return

        sharp = self.book.lookup(key[0], key[1], key[2], key[3], side, line=update.line)
        if sharp is None:
            self.stats["no_sharp"] += 1
            log.info("DECISION PASS %s %s %s: no fresh sharp line (<=%.0fs)", update.venue, key, side,
                     self.book.max_age)
            return
        sharp_q = SharpQuote(odds_for=sharp.odds_for, odds_against=sharp.odds_against, line=sharp.line,
                             source=sharp.source)
        label = f"{update.venue}/{update.event_id}/{update.market_type}/{side}"
        decision = await self._evaluate(update.venue, update.price, sharp_q, update.line, label)
        if self.research is not None:
            self._research_decision(update, key, side, decision, blocked=blocked if shadow else None)
            if decision.action == "BET":
                self._watch_edge(update, decision)
        if shadow:
            self.stats["shadow_decisions"] += 1
            return
        self.stats[f"decision_{decision.action.lower()}"] += 1
        log.info("DECISION %s %s %s %s edge=%s stake=$%.2f fee=$%.2f reason=%s", decision.action, update.venue,
                 key, side, "n/a" if decision.edge is None else f"{decision.edge:+.4%}", decision.stake_usd,
                 decision.fee_usd, decision.reason)
        if decision.action != "BET":
            return
        log.info("EV_TRIGGER %s %s %s ask=%.4f fair=%.4f edge=%+.4f%% kelly=$%.2f stake=$%.2f capped=%s%s",
                 update.venue, key, side, update.price, decision.fair_prob, decision.edge * 100,
                 decision.kelly_stake_usd, decision.stake_usd, decision.capped,
                 " SUSPICIOUS-EDGE" if decision.suspicious else "")

        if self.live and update.venue not in self.live_venues():
            self.stats["kalshi_exec_disabled"] += 1
            log.info("EV_TRIGGER not executed: %s is data-only in live mode", update.venue)
            return
        why_not = self._moved_first_filter(update, key)
        if why_not:
            self.stats["moved_first_blocked"] += 1
            log.info("EV_TRIGGER not executed: %s", why_not)
            return
        if self._loss_halted():
            self.stats["daily_loss_blocked"] += 1
            log.info("EV_TRIGGER not executed: daily loss stop active until the next UTC day")
            return
        room = self.game_room(gid)
        if room < 1.0:
            self.stats["game_cap_blocked"] += 1
            log.info("EV_TRIGGER not executed: game %s already carries $%.2f unhedged (cap $%.0f per game)", gid,
                     self.game_unhedged(gid), self.game_exposure_limit)
            return
        contracts, stake, fee, avg_price, limit_price, used = await self._walk_asks(
            update, sharp_q, label, decision, room=room)
        if contracts <= 0:
            self.stats["no_liquidity"] += 1
            log.info("EV_TRIGGER skipped: no liquidity with edge for %s", update.outcome_id)
            return
        if len(used) > 1:
            self.stats["multi_level_takes"] += 1
            log.info("EV_TRIGGER walked %d ask levels %s -> %d contracts avg %.4f, limit %.4f", len(used),
                     json.dumps(used), contracts, avg_price, limit_price)
        if contracts < decision.contracts:
            log.info("EV_TRIGGER size limited by liquidity: %d of %d contracts ($%.2f)", contracts,
                     decision.contracts, stake)
        reserve = round(contracts * limit_price + fee, 2) if self._single_limit(used) else stake
        if not self._kill_switch_allows(reserve, update.outcome_id):
            return
        if self.executing:
            await self._send_live_taker("DIRECTIONAL", update, key, side, contracts, reserve, decision.edge,
                                        decision.capped, limit_price=limit_price, levels=used)
            return
        self._record("DIRECTIONAL", update, side, contracts, stake, fee, decision.edge, decision.capped,
                     price=avg_price)
        self.positions[key] = MarketPosition(legs=[self.orders[-1]])
        await self._after_taker(key)

    @staticmethod
    async def _evaluate(venue: str, price: float, sharp_q: SharpQuote, line: Optional[float], label: str):
        if venue == "kalshi":
            return await evaluate_kalshi_edge(price * 100, sharp_q, line=line, label=label)
        if venue == "prophetx":          # 2% of net winnings = a higher effective price per $1 of payout
            effective = price / win_payout("prophetx", price)
            return await evaluate_market_edge(NovigQuote(price=price, fee_per_contract=effective - price, line=line,
                                                         label=label), sharp_q)
        return await evaluate_market_edge(NovigQuote(price=price, line=line, label=label), sharp_q)

    @staticmethod
    def _ask_levels(update: MarketUpdate) -> list[tuple[float, float]]:
        """Ask levels cheapest first; falls back to the best ask alone if the depth is missing or inconsistent."""
        levels = [(p, q) for p, q in update.ask_levels if q > 0]
        if not levels or not math.isclose(levels[0][0], update.price):
            return [(update.price, update.available_volume)]
        return levels

    def _single_limit(self, used: list) -> bool:
        """True when one live order limited at the deepest level will be sent (reserve at that limit)."""
        return self.executing and len(used) > 1 and self.multi_level_mode == "single"

    @staticmethod
    def _fit_worst_case(venue: str, used: list[tuple[float, int]], cap: float) -> list[tuple[float, int]]:
        """
        Trim the deepest level until the WORST case fits the cap: if the cheaper levels vanish before our
        order lands, every contract fills at the limit (the deepest price), never above it.
        """
        used = list(used)
        while used:
            limit = used[-1][0]
            total = sum(n for _, n in used)
            if order_cost(venue, total, limit)[0] <= cap + 1e-9:
                return used
            unit = limit + (kalshi_fee_per_contract(limit * 100) if venue == "kalshi" else 0.0)
            allowed = int(math.floor(cap / unit + 1e-9))
            excess = max(1, total - allowed)
            price, n = used[-1]
            used[-1] = (price, n - excess)
            if used[-1][1] <= 0:
                used.pop()
        return used

    async def _walk_asks(self, update: MarketUpdate, sharp_q: SharpQuote, label: str, decision,
                         room: float = MAX_STAKE_USD):
        """
        Buy through several ask levels while EACH level still clears the edge threshold.
        Size never exceeds the 1/4-Kelly stake of the worst level used (Kelly shrinks as the price worsens),
        the $1,000 per-position cap, or the live canary stake.
        Returns (contracts, total cost incl. fees, fee, average price, worst price = limit, [(price, n), ...]).
        """
        cap = min(decision.stake_usd, room)                 # room = what the per-game cap still allows
        if self.max_stake < MAX_STAKE_USD:
            cap = min(cap, self.max_stake)
        used: list[tuple[float, int]] = []
        spent = 0.0
        for i, (price, size) in enumerate(self._ask_levels(update)):
            if i == 0:
                unit0 = price + (kalshi_fee_per_contract(price * 100) if update.venue == "kalshi" else 0.0)
                n = min(decision.contracts, int(size), int(math.floor(cap / unit0 + 1e-9)))
            elif self.multi_level_mode == "off" or (self.live and not self.multi_level_live):
                break
            else:
                level = await self._evaluate(update.venue, price, sharp_q, update.line, label)
                if level.action != "BET":
                    break
                cap = min(cap, level.stake_usd)
                unit = price + (kalshi_fee_per_contract(price * 100) if update.venue == "kalshi" else 0.0)
                n = min(int(size), int(math.floor((cap - spent) / unit + 1e-9)))
            if self.max_stake < MAX_STAKE_USD:
                n = min(n, int(math.floor((self.max_stake - spent) / price + 1e-9)))
            if n <= 0:
                break
            used.append((price, n))
            spent += n * price
            if n < int(size):
                break
        if len(used) > 1 and self.multi_level_mode == "single":
            # one order limited at the deepest level: size for everything filling at that limit
            used = self._fit_worst_case(update.venue, used, min(cap, self.max_stake, MAX_STAKE_USD))
        while used:
            contracts = sum(n for _, n in used)
            costs = [order_cost(update.venue, n, p) for p, n in used]
            stake, fee = round(sum(c for c, _ in costs), 2), round(sum(f for _, f in costs), 2)
            net = (contracts * decision.fair_prob - stake) / stake
            if update.venue != "kalshi" or net > MIN_EDGE + 1e-9 or len(used) == 1 and contracts == decision.contracts:
                avg = round(sum(p * n for p, n in used) / contracts, 6)
                return contracts, stake, fee, avg, used[-1][0], used
            log.info("EV_TRIGGER net edge %+.4f%% after fees too thin at %d levels; dropping the worst", net * 100,
                     len(used))
            used.pop()
        return 0, 0.0, 0.0, update.price, update.price, []

    async def _after_taker(self, key: GameKey) -> None:
        if self.maker is not None:
            self.maker.note_taker_activity()
            await self.maker.cancel_market(key, "position opened in this market")

    # ---------------- locked pairs (both legs at once) ----------------
    def _unit_cost(self, venue: str, price: float) -> float:
        return price + (kalshi_fee_per_contract(price * 100) if venue == "kalshi" else 0.0)

    def _pair_candidates(self, update: MarketUpdate, key: GameKey, side: str) -> list[MarketUpdate]:
        """Latest asks for the OPPOSITE side of the same market on any venue, with a complementary line."""
        league, _, _, mtype = key
        out = []
        for other in list(self._latest_updates()):
            if other.price is None or other.outcome_id == update.outcome_id:
                continue
            canon = self._canonical(other)
            if canon is None or canon[0] != key or canon[1] == side:
                continue
            if mtype == "spread" and (other.line is None or update.line is None
                                      or not math.isclose(other.line, -update.line)):
                continue
            if mtype == "total" and (other.line is None or update.line is None
                                     or not math.isclose(other.line, update.line)):
                continue
            if mtype in {"spread", "total"} and not _is_half_point(update.line):
                continue                                   # a whole-number line can push
            out.append(other)
        return out

    async def _try_locked_pair(self, update: MarketUpdate, key: GameKey, side: str) -> bool:
        """
        Buy both sides at once when, after fees, every settlement scenario (incl. an NFL tie) pays at least
        ARB_MIN_PROFIT_PER_CONTRACT. Size: the thinner side's best level, the $1,000 per-leg cap, the canary,
        and the per-game cap applied to the WORST case (one leg fills, the other does not).
        Returns True when a pair was executed (or sent).
        """
        if update.price is None or self._loss_halted():
            return False
        league, _, _, mtype = key
        if self.executing and (update.venue not in self.live_venues() or not self._live_ready(update.venue)):
            return False
        best = None
        for other in self._pair_candidates(update, key, side):
            if self.executing and (other.venue not in self.live_venues() or not self._live_ready(other.venue)):
                continue
            if league in NO_CROSS_VENUE_HEDGE_LEAGUES and other.venue != update.venue:
                continue
            worst_payout = min(win_payout(update.venue, update.price), win_payout(other.venue, other.price))
            if league == "NFL" and mtype == "moneyline":
                worst_payout = min(worst_payout, sum(DEAD_HEAT_PAYOUT if v in DEAD_HEAT_VENUES else 0.0
                                                     for v in (update.venue, other.venue)))
            profit = worst_payout - self._unit_cost(update.venue, update.price) - self._unit_cost(other.venue,
                                                                                                 other.price)
            if profit >= ARB_MIN_PROFIT_PER_CONTRACT - 1e-9 and (best is None or profit > best[1]):
                best = (other, profit, worst_payout)
        if best is None:
            return False
        other, profit, worst_payout = best
        ua, ub = self._unit_cost(update.venue, update.price), self._unit_cost(other.venue, other.price)
        per_leg_cap = min(MAX_STAKE_USD, self.max_stake)
        n = int(min(update.available_volume, other.available_volume,
                    math.floor(per_leg_cap / ua + 1e-9), math.floor(per_leg_cap / ub + 1e-9),
                    math.floor(self.game_room(key[:3]) / max(ua, ub) + 1e-9)))
        while n >= MIN_PARTIAL_HEDGE_CONTRACTS:
            cost_a, fee_a = order_cost(update.venue, n, update.price)
            cost_b, fee_b = order_cost(other.venue, n, other.price)
            if n * worst_payout - cost_a - cost_b >= round(ARB_MIN_PROFIT_PER_CONTRACT * n, 2) - 1e-9:
                break
            n -= max(1, n // 20)                          # fee rounding ate the margin: shrink
        if n < MIN_PARTIAL_HEDGE_CONTRACTS:
            self.stats["arb_pair_too_small"] += 1
            return False
        if not self._kill_switch_allows(round(cost_a + cost_b, 2), update.outcome_id):
            return False
        other_side = self._canonical(other)[1]
        locked = round(n * worst_payout - cost_a - cost_b, 2)
        log.info("ARB_PAIR %s: buy %s %s @ %.4f + %s %s @ %.4f x%d -> cost $%.2f, locked profit $%.2f in every "
                 "outcome", key, update.venue, side, update.price, other.venue, other_side, other.price, n,
                 cost_a + cost_b, locked)
        self.stats["arb_pairs"] += 1
        if self.executing:
            self._ledger("ARB_PAIR", game=list(key), legs=[[update.venue, update.outcome_id, update.price],
                                                           [other.venue, other.outcome_id, other.price]],
                         contracts=n, locked_profit_usd=locked)
            await self._send_live_taker("DIRECTIONAL", update, key, side, n, cost_a, None, False,
                                        locked_per_contract=locked / n)
            held = self.positions.get(key)
            if held is None:                              # first leg rejected / not sent: nothing to pair
                return True
            held.hedged = True                            # the second leg is on its way
            await self._send_live_taker("ARB_HEDGE", other, key, other_side, n, cost_b, None, False, held=held,
                                        locked_per_contract=locked / n)
            return True
        leg_a = self._record("ARB_PAIR", update, side, n, cost_a, fee_a, None, False)
        leg_b = self._record("ARB_PAIR", other, other_side, n, cost_b, fee_b, None, False)
        self.positions[key] = MarketPosition(legs=[leg_a, leg_b], hedged=True)
        await self._after_taker(key)
        return True

    # ---------------- arbitrage ----------------
    async def _try_arbitrage(self, update: MarketUpdate, key: GameKey, side: str, held: MarketPosition) -> None:
        first = held.primary
        venues = self.live_venues()
        if self.executing and (update.venue not in venues or first.pending or first.venue not in venues):
            self.stats["lock_blocked"] += 1
            log.info("POSITION_LOCK live: no hedge on %s (%s)", key,
                     "first leg still filling" if first.pending else "Kalshi is data-only in live mode")
            return
        if key[0] in NO_CROSS_VENUE_HEDGE_LEAGUES and update.venue != first.venue:
            self.stats["lock_blocked"] += 1
            log.info("POSITION_LOCK %s: no cross-venue hedge in %s (settlement rules differ, e.g. retirements)",
                     key, key[0])
            return
        ok, reason, contracts, cost, fee, scenarios, avg_price, limit_price = self._arb_check(held, update, key)
        if not ok:
            self.stats["lock_blocked"] += 1
            log.info("POSITION_LOCK blocked opposite side %s on %s of %s (holding #%d %s %s @ %.4f): %s",
                     side, update.venue, key, first.order_id, first.venue, first.side, first.price, reason)
            return
        partial = contracts < held.unhedged()
        log.info("ARB_TRIGGER %s: held %s %s @ %.4f + buy %s %s avg %.4f (limit %.4f) -> %d contracts%s, hedge $%.2f "
                 "(fee $%.2f), scenarios %s, guaranteed profit $%.2f", key, first.venue, first.side, first.price,
                 update.venue, side, avg_price, limit_price, contracts,
                 f" (PARTIAL: {held.unhedged() - contracts:g} stay unhedged)" if partial else "", cost, fee,
                 json.dumps(scenarios), min(scenarios.values()))
        reserve = round(contracts * limit_price + fee, 2) if self._single_limit(self._last_hedge_levels) else cost
        if not self._kill_switch_allows(reserve, update.outcome_id):
            return
        if self.executing:
            held.hedged = True        # blocks further hedges while this one fills
            await self._send_live_taker("ARB_HEDGE", update, key, side, contracts, reserve, None, False, held=held,
                                        limit_price=limit_price, levels=self._last_hedge_levels,
                                        locked_per_contract=round(min(scenarios.values()) / contracts, 6))
            return
        self._record("ARB_HEDGE", update, side, contracts, cost, fee, None, False, price=avg_price)
        held.legs.append(self.orders[-1])
        held.hedged = held.unhedged() <= 0
        self.stats["arbs"] += 1
        if partial:
            self.stats["partial_hedges"] += 1
        await self._after_taker(key)

    def _arb_check(self, held: MarketPosition, update: MarketUpdate, key: GameKey):
        """
        How many contracts of the opposite side can be bought, across ask levels, so that EVERY settlement
        scenario still locks >= ARB_MIN_PROFIT_PER_CONTRACT per contract. Partial hedges are allowed
        (at least MIN_PARTIAL_HEDGE_CONTRACTS); the rest of the first leg stays directional.
        """
        first = held.primary
        league, _, _, mtype = key
        fail = lambda why: (False, why, 0, 0.0, 0.0, {}, 0.0, 0.0)  # noqa: E731
        if mtype == "spread":
            if first.line is None or update.line is None or not math.isclose(update.line, -first.line):
                return fail(f"lines not complementary ({first.line} vs {update.line})")
        elif mtype == "total":
            if first.line is None or update.line is None or not math.isclose(update.line, first.line):
                return fail(f"totals differ ({first.line} vs {update.line})")
        if mtype in {"spread", "total"} and not _is_half_point(update.line):
            return fail(f"whole-number line {update.line} can push")
        remaining = int(held.unhedged())
        if remaining <= 0 or first.contracts <= 0:
            return fail("nothing left to hedge")
        c1 = first.stake_usd / first.contracts
        worst_payout = 1.0
        if league == "NFL" and mtype == "moneyline":
            worst_payout = min(1.0, sum(DEAD_HEAT_PAYOUT if v in DEAD_HEAT_VENUES else 0.0
                                        for v in (first.venue, update.venue)))
        used: list[tuple[float, int]] = []
        spent = 0.0
        for i, (price, size) in enumerate(self._ask_levels(update)):
            if i > 0 and (self.multi_level_mode == "off" or (self.live and not self.multi_level_live)):
                break
            unit = price + (kalshi_fee_per_contract(price * 100) if update.venue == "kalshi" else 0.0)
            level_worst = min(worst_payout, win_payout(first.venue, first.price), win_payout(update.venue, price))
            if level_worst - c1 - unit < ARB_MIN_PROFIT_PER_CONTRACT - 1e-9:
                break
            n = min(int(size), remaining - sum(k for _, k in used),
                    int(math.floor((MAX_STAKE_USD - spent) / unit + 1e-9)))
            if n <= 0:
                break
            used.append((price, n))
            spent += n * unit
        if self.multi_level_mode == "single":
            used = self._fit_worst_case(update.venue, used, MAX_STAKE_USD)
        while used:
            contracts = sum(n for _, n in used)
            if contracts < remaining and contracts < MIN_PARTIAL_HEDGE_CONTRACTS:
                return fail(f"only {contracts} contracts hedgeable at a locked profit (need all {remaining} or "
                            f">= {MIN_PARTIAL_HEDGE_CONTRACTS} for a partial hedge)")
            costs = [order_cost(update.venue, n, p) for p, n in used]
            cost, fee = round(sum(c for c, _ in costs), 2), round(sum(f for _, f in costs), 2)
            scenarios = arbitrage_scenarios(first, update.venue, contracts, cost, league, mtype,
                                            hedge_price=used[0][0])      # cheapest level = largest win fee
            need = round(ARB_MIN_PROFIT_PER_CONTRACT * contracts, 2)
            if min(scenarios.values()) >= need - 1e-9:
                avg = round(sum(p * n for p, n in used) / contracts, 6)
                self._last_hedge_levels = list(used)
                return True, "ok", contracts, cost, fee, scenarios, avg, used[-1][0]
            used.pop()                                     # fee rounding ate the margin: drop the worst level
        best = update.price + (kalshi_fee_per_contract(update.price * 100) if update.venue == "kalshi" else 0.0)
        return fail(f"no arbitrage: worst case nets {worst_payout - c1 - best:+.4f}/contract at the best ask "
                    f"(need >= {ARB_MIN_PROFIT_PER_CONTRACT:.4f})")

    # ---------------- orders / exposure ----------------
    def _kill_switch_allows(self, stake: float, outcome_id: str) -> bool:
        ok, reason = self.exposure.check_taker(stake)
        if ok:
            self._halt_alerted = False
        else:
            if self.exposure.taker_halted and not self._halt_alerted:
                self._halt_alerted = True
                log.critical("KILL_SWITCH engaged: open exposure $%.2f of $%.2f — no new taker orders until "
                             "settlements free room", self.exposure.open_exposure, self.exposure.limit)
            self.stats["kill_switch_blocked"] += 1
            log.warning("KILL_SWITCH blocked taker order on %s ($%.2f): %s", outcome_id, stake, reason)
            if self.maker is not None and self.maker.quotes and self.exposure.taker_halted:
                self._spawn(self.maker.cancel_all("exposure kill-switch engaged"))
        return ok

    def _record(self, kind: str, update: MarketUpdate, side: str, contracts: int, stake: float, fee: float,
                edge: Optional[float], capped: bool, price: Optional[float] = None,
                force: bool = False) -> PaperOrder:
        """Record a SIMULATED fill. Sends nothing to any exchange."""
        order = PaperOrder(
            order_id=len(self.orders) + 1, kind=kind, venue=update.venue, outcome_id=update.outcome_id,
            event_id=update.event_id, league=update.league, market_type=update.market_type, side=side,
            line=update.line, price=update.price if price is None else price, contracts=contracts, fee_usd=fee,
            stake_usd=stake, edge=edge, capped=capped, placed_at=time.time(),
        )
        if force:
            self.exposure.record_fill(order.position_id, stake)
        else:
            self.exposure.record_open(order.position_id, stake)
        self.orders.append(order)
        self.stats["paper_orders"] += 1
        order_log.info("ORDER PAPER %s", order.model_dump_json())
        if self.research is not None and (not self.live or kind == "MAKER_FILL"):
            canon = self._canonical(update)
            self._research_entry(order, canon[0] if canon else None)
        return order

    # ---------------- live execution ----------------
    async def _send_live_taker(self, kind: str, update: MarketUpdate, key: GameKey, side: str, contracts: int,
                               stake: float, edge: Optional[float], capped: bool,
                               held: Optional[MarketPosition] = None, limit_price: Optional[float] = None,
                               levels: Optional[list[tuple[float, int]]] = None,
                               locked_per_contract: Optional[float] = None) -> None:
        """
        Reserve exposure + lock the game, then send marketable LIMIT order(s) for one position leg.

        multi_level_mode "staggered" (default; Novig is believed to fill a taker at its LIMIT, not at each
        resting price): one order per ask level, each priced at that level, all sent at once. No contract can
        cost more than its own level; a tranche whose level vanished rests at its (still profitable) price
        until the fill timeout cancels it.
        "single": one order limited at the worst level (only right if the exchange fills cheaper levels first).
        The reservation (`stake`) covers the worst case, so it is never low.
        """
        price = update.price if limit_price is None else limit_price
        venue = update.venue
        gateway = self._gateway(venue)
        if not self._live_ready(venue):
            self.stats["live_not_ready"] += 1
            log.warning("LIVE order not sent on %s: %s socket is down", update.outcome_id, venue)
            if held is not None:
                held.hedged = False
            return
        staggered = self.multi_level_mode == "staggered" and levels is not None and len(levels) > 1
        tranches = [(p, n) for p, n in levels] if staggered else [(price, contracts)]
        leg = PaperOrder(order_id=len(self.orders) + 1, kind=kind, venue=venue, outcome_id=update.outcome_id,
                         event_id=update.event_id, league=update.league, market_type=update.market_type, side=side,
                         line=update.line, price=price, contracts=0, stake_usd=0.0, edge=edge, capped=capped,
                         placed_at=time.time(), live=True, pending=True, requested_contracts=contracts)
        self.exposure.record_open(leg.position_id, stake)               # reservation (re-checks the limit)
        self.orders.append(leg)
        if held is None:
            self.positions[key] = MarketPosition(legs=[leg])
        else:
            held.legs.append(leg)
        prefix = f"tk-{self.session_tag}-" if venue == "kalshi" else "tk-"     # Kalshi ids must never repeat
        cids = [f"{prefix}{leg.order_id}" if len(tranches) == 1 else f"{prefix}{leg.order_id}-{i + 1}"
                for i in range(len(tranches))]
        if venue == "kalshi":
            payloads = [gateway.order_body(update.outcome_id, round(p * 100, 4), n, cid, gateway.time_in_force)
                        for (p, n), cid in zip(tranches, cids)]
        else:
            payloads = [order_body(update.outcome_id, "buy", round(p * 100, 4), n, cid)
                        for (p, n), cid in zip(tranches, cids)]
        sent_at = time.time()
        results = await asyncio.gather(*(gateway.place_limit(update.outcome_id, "buy", round(p * 100, 4), n, cid)
                                         for (p, n), cid in zip(tranches, cids)), return_exceptions=True)
        acked_at = time.time()
        placed = []
        for (p, n), payload, res in zip(tranches, payloads, results):
            if isinstance(res, BaseException):   # a rejected order must release everything it reserved
                log.error("LIVE_ORDER rejected %s %s x%d @ %.4f: %s", update.outcome_id, side, n, p, res)
                self._ledger("REJECTED", kind=kind, payload=payload, error=f"{type(res).__name__}: {res}")
                self.stats["live_rejected"] += 1
                continue
            placed.append((res, p, n, payload))
        if not placed:
            self.exposure.adjust(leg.position_id, 0)
            self._drop_leg(key, leg)
            if held is not None:
                held.hedged = False
            return
        if len(placed) < len(tranches):                # some tranches rejected: shrink the reservation to the rest
            leg.requested_contracts = sum(n for _, _, n, _ in placed)
            self.exposure.adjust(leg.position_id, round(sum(p * n for _, p, n, _ in placed), 2))
        leg.exchange_order_id = ",".join(oid for oid, *_ in placed)
        for oid, p, n, payload in placed:
            self.live_orders[oid] = LiveOrder(exchange_order_id=oid, kind=kind, outcome_id=update.outcome_id,
                                              key=key, requested=n, limit_price=p, leg_id=leg.order_id,
                                              expected_levels=[] if staggered else (levels or []), venue=venue,
                                              fill_mode="incremental" if venue == "kalshi" else self.fill_volume_mode,
                                              expected_price=update.price, edge=edge,
                                              locked_per_contract=locked_per_contract, sent_at=sent_at,
                                              acked_at=acked_at)
            self.stats["live_orders"] += 1
            order_log.info("ORDER LIVE %s id=%s %s %s x%d @ %.4f%s", kind, oid, update.outcome_id, side, n, p,
                           f" (tranche of leg #{leg.order_id})" if len(tranches) > 1 else f" reserved=${stake:.2f}")
            self._ledger("ORDER", exchange_order_id=oid, kind=kind, canonical_side=side, venue=venue,
                         reserved_usd=round(p * n, 2) if len(tranches) > 1 else stake, payload=payload,
                         leg=leg.order_id, tranches=len(tranches),
                         expected_levels=[[p, n]] if staggered else (levels or [[p, n]]),
                         expected_avg_price=p if staggered or not levels else _sweep_avg(levels, n))
            self._spawn(self._taker_timeout(oid))
        if staggered:
            self.stats["staggered_orders"] += 1
        await self._after_taker(key)

    def _children(self, leg_id: Optional[int]) -> list[LiveOrder]:
        """Every exchange order (tranche) feeding one taker position leg."""
        return [lo for lo in self.live_orders.values() if lo.leg_id == leg_id and lo.kind != "MAKER"]

    @staticmethod
    def _leg_exposure(kids: list[LiveOrder]) -> float:
        """What a leg can cost: finished tranches at their fills, working ones at their full reservation."""
        return round(sum(k.fill_cost if k.done else max(k.fill_cost, k.requested * k.limit_price) for k in kids), 2)

    async def _taker_timeout(self, oid: str) -> None:
        await asyncio.sleep(self.taker_fill_timeout)
        lo = self.live_orders.get(oid)
        if lo is None or lo.done:
            return
        gateway = self._gateway(lo.venue)
        try:
            await gateway.cancel_orders([oid])
            reason = "unfilled remainder cancelled"
            self._ledger("CANCEL", exchange_order_ids=[oid], reason=f"taker timeout {self.taker_fill_timeout}s",
                         source="taker", filled_so_far=lo.filled)
        except Exception as exc:  # noqa: BLE001
            log.critical("LIVE_ORDER could not cancel remainder of %s (%s); reservation KEPT", oid, exc)
            self._ledger("CANCEL_FAILED", exchange_order_ids=[oid], error=str(exc))
            return
        if lo.venue == "kalshi" and hasattr(gateway, "get_order"):
            await self._reconcile_kalshi_order(lo, gateway)
        if not lo.done:
            self._finalize(lo, reason)

    async def _reconcile_kalshi_order(self, lo: LiveOrder, gateway) -> None:
        """Kalshi keeps an order record: use it to confirm a zero fill, or book fills the socket missed."""
        try:
            order = await gateway.get_order(lo.exchange_order_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("KALSHI order %s status check failed: %s", lo.exchange_order_id, exc)
            return
        filled = gateway.filled_count(order) if order else None
        if filled is None:
            return
        if filled > lo.filled + 1e-9:
            missed = filled - lo.filled
            log.critical("KALSHI order %s: exchange reports %g filled, fill channel showed %g — booking %g at the "
                         "limit %.4f", lo.exchange_order_id, filled, lo.filled, missed, lo.limit_price)
            await self.on_fill_slip(FillSlip(order_id=lo.exchange_order_id, status="PARTIAL", filled_volume=missed,
                                             price_cents=lo.limit_price * 100, venue="kalshi"))
        lo.exchange_confirmed_zero = filled <= 1e-9

    def _leg(self, leg_id: Optional[int]) -> Optional[PaperOrder]:
        return self.orders[leg_id - 1] if leg_id and 0 < leg_id <= len(self.orders) else None

    def _drop_leg(self, key: GameKey, leg: PaperOrder) -> None:
        pos = self.positions.get(key)
        if pos is not None and leg in pos.legs:
            pos.legs.remove(leg)
            if not pos.legs:
                del self.positions[key]

    def _finalize(self, lo: LiveOrder, reason: str) -> None:
        """Order finished: exposure = what actually filled; nothing filled -> release the lock."""
        lo.done = True
        leg = self._leg(lo.leg_id)
        if leg is None:
            return
        kids = self._children(leg.order_id)
        if len(kids) > 1:
            self._ledger("TRANCHE_DONE", exchange_order_id=lo.exchange_order_id, leg=leg.order_id,
                         filled=lo.filled, requested=lo.requested, price=lo.limit_price,
                         cost_usd=round(lo.fill_cost, 2), reason=reason)
            if any(not k.done for k in kids):          # other tranches still working
                self.exposure.adjust(leg.position_id, self._leg_exposure(kids))
                return
            lo = lo.model_copy(update=dict(          # the whole leg, seen as one order from here on
                exchange_confirmed_zero=all(k.exchange_confirmed_zero for k in kids),
                sent_at=min((k.sent_at for k in kids if k.sent_at), default=None),
                acked_at=max((k.acked_at for k in kids if k.acked_at), default=None),
                first_fill_at=min((k.first_fill_at for k in kids if k.first_fill_at), default=None),
                fees=sum(k.fees for k in kids),
                exchange_order_id=",".join(k.exchange_order_id for k in kids),
                requested=sum(k.requested for k in kids), filled=sum(k.filled for k in kids),
                fill_cost=sum(k.fill_cost for k in kids), limit_price=max(k.limit_price for k in kids),
                expected_levels=[]))
        leg.pending = False
        pos = self.positions.get(lo.key)
        channel_ok = self.orders_channel_confirmed if lo.venue == "novig" else self.kalshi_fills_confirmed
        if lo.filled <= 0 and not (channel_ok or lo.exchange_confirmed_zero):
            self.stats["unconfirmed_zero_fill"] += 1
            log.critical("LIVE_UNCONFIRMED %s %s ended with no observed fill, but no execution slip has been seen "
                         "this session: it may have filled unseen. Lock and $%.2f reservation KEPT — check the "
                         "%s order history", lo.kind, lo.exchange_order_id,
                         sum(k.requested * k.limit_price for k in kids), lo.venue.capitalize())
            self.exposure.adjust(leg.position_id, round(sum(k.requested * k.limit_price for k in kids), 2))
            self._ledger("UNCONFIRMED", exchange_order_id=lo.exchange_order_id, reason=reason)
            self.unconfirmed_legs[leg.order_id] = time.time()   # the settlement sweep reconciles it
            return
        self.exposure.adjust(leg.position_id, round(lo.fill_cost, 2))
        if lo.filled <= 0:
            self._drop_leg(lo.key, leg)
            pos = self.positions.get(lo.key)
            if pos is not None and pos.legs:            # any leg of a pair can be the missing one
                pos.hedged = pos.unhedged() <= 0 and len(pos.legs) > 1
            log.info("LIVE_DONE %s %s: nothing filled (%s); lock/reservation released", lo.kind,
                     lo.exchange_order_id, reason)
            self._research_execution(lo, leg, None)
            return
        if lo.kind == "ARB_HEDGE" and pos is not None:
            pos.hedged = pos.unhedged() <= 0           # a partial fill leaves room for another hedge
            if not pos.hedged:
                log.warning("POSITION_RESIDUAL hedge %s filled %g of %d: %g contracts of the first leg remain "
                            "unhedged (another hedge may follow)", lo.exchange_order_id, lo.filled, lo.requested,
                            pos.unhedged())
        log.info("LIVE_DONE %s %s filled %g/%d cost=$%.2f (%s)", lo.kind, lo.exchange_order_id, lo.filled,
                 lo.requested, lo.fill_cost, reason)
        if pos is not None and len(pos.legs) > 1 and not any(l.pending for l in pos.legs) and pos.unhedged() < 0:
            log.warning("POSITION_RESIDUAL %s over-hedged by %g contracts (the second leg filled more than the "
                        "first): that excess is a directional position", lo.key, -pos.unhedged())
            self._ledger("POSITION_RESIDUAL", game=list(lo.key), over_hedged_contracts=-pos.unhedged())
        if self.research is not None:
            self._research_entry(leg, lo.key)
        self._research_execution(lo, leg, pos)
        self._ledger("DONE", exchange_order_id=lo.exchange_order_id, filled=lo.filled, cost_usd=round(lo.fill_cost, 2),
                     reason=reason, avg_fill_price=round(lo.fill_cost / lo.filled, 6),
                     expected_avg_price=_sweep_avg(lo.expected_levels, lo.filled) if lo.expected_levels else None)
        self._check_price_improvement(lo)

    def _check_price_improvement(self, lo: LiveOrder) -> None:
        """
        A multi-level order should fill the cheaper levels at THEIR prices. If everything came back at the
        limit while cheaper levels were showing, either Novig fills takers at their limit or the cheap levels
        vanished first. Either way: stop multi-level orders for the session (best level only) and flag it.
        """
        if len(lo.expected_levels) < 2 or lo.filled <= 0 or not self.multi_level_live:
            return
        actual = lo.fill_cost / lo.filled
        expected = _sweep_avg(lo.expected_levels, lo.filled)
        if actual >= lo.limit_price - 1e-6 and expected < lo.limit_price - 0.004:
            self.multi_level_live = False
            self.stats["price_improvement_missing"] += 1
            log.critical("PRICE_IMPROVEMENT_MISSING order %s filled %g all at the limit %.4f although cheaper levels "
                         "(expected avg %.4f) were showing: multi-level orders OFF for this session (best level "
                         "only). Check Novig's matching rules before re-enabling.", lo.exchange_order_id, lo.filled,
                         lo.limit_price, expected)
            self._ledger("PRICE_IMPROVEMENT_MISSING", exchange_order_id=lo.exchange_order_id,
                         avg_fill_price=round(actual, 6), expected_avg_price=round(expected, 6),
                         limit_price=lo.limit_price, action="multi-level orders disabled for this session")

    def _register_quote(self, q) -> None:
        """MakerEngine hook: remember every live quote so its fills can be booked."""
        buy_price = q.price_cents / 100 if q.side == "buy" else 1 - q.price_cents / 100
        self._ledger("ORDER", exchange_order_id=q.order_id, kind="MAKER", market=list(q.market_key),
                     fair_prob=round(q.fair_prob, 6),
                     payload=order_body(q.outcome_id, q.side, q.price_cents, q.contracts, q.order_id))
        self.live_orders[q.order_id] = LiveOrder(exchange_order_id=q.order_id, kind="MAKER", outcome_id=q.outcome_id,
                                                 key=q.market_key, requested=q.contracts, limit_price=buy_price,
                                                 maker_side=q.side, fair_prob=q.fair_prob)

    async def on_fill_slip(self, slip) -> None:
        """Execution slips: book real fills into positions and the exposure count."""
        if getattr(slip, "venue", "novig") == "kalshi":
            if not self.kalshi_fills_confirmed:
                self.kalshi_fills_confirmed = True
                log.info("LIVE Kalshi fill channel confirmed by first fill")
        elif not self.orders_channel_confirmed:
            self.orders_channel_confirmed = True
            log.info("LIVE orders channel confirmed by first execution slip")
        self._ledger("SLIP", **slip.model_dump())
        lo = self.live_orders.get(slip.order_id)
        if lo is None:
            self.stats["unknown_fills"] += 1
            log.critical("FILL_UNKNOWN slip for order %s we did not place this session: %s — reconcile manually",
                         slip.order_id, slip.model_dump_json())
            return
        if lo.fill_mode == "cumulative":
            delta = slip.filled_volume - lo.filled
        else:
            delta = slip.filled_volume
        if delta > 1e-9:
            if slip.price_cents is not None:
                price = slip.price_cents / 100 if lo.maker_side != "sell" else 1 - slip.price_cents / 100
            else:
                price = lo.limit_price
            if lo.first_fill_at is None:
                lo.first_fill_at = time.time()
            lo.filled += delta
            lo.fill_cost += delta * price
            if lo.venue == "kalshi":                   # taker fee per fill, rounded up: never understated
                fee = kalshi_taker_fee(int(math.ceil(delta - 1e-9)), price * 100)
                lo.fees += fee
                lo.fill_cost += fee
            self.stats["fills"] += 1
            if lo.kind == "MAKER":
                await self._book_maker_fill(lo, delta, price)
            else:
                leg = self._leg(lo.leg_id)
                if leg is not None:
                    kids = self._children(leg.order_id)       # one order, or several staggered tranches
                    filled, cost = sum(k.filled for k in kids), sum(k.fill_cost for k in kids)
                    leg.contracts = int(round(filled))
                    leg.stake_usd = round(cost, 2)
                    leg.price = round(cost / filled, 6)
                    if lo.done:   # late fill after we cancelled the remainder: still real
                        log.warning("LIVE late fill on %s after cancel: +%g", lo.exchange_order_id, delta)
                        self.exposure.adjust(leg.position_id, self._leg_exposure(kids))
            log.info("FILL %s %s +%g (total %g/%d) @ %.4f cost=$%.2f", lo.kind, lo.exchange_order_id, delta,
                     lo.filled, lo.requested, price, lo.fill_cost)
            self._ledger("FILL", exchange_order_id=lo.exchange_order_id, kind=lo.kind, delta=delta,
                         filled_total=lo.filled, requested=lo.requested, price=round(price, 6),
                         cost_total_usd=round(lo.fill_cost, 2), open_exposure_usd=self.exposure.open_exposure)
        elif slip.filled_volume > 0:
            log.debug("FILL duplicate/replayed slip for %s ignored", slip.order_id)
        if lo.kind != "MAKER" and not lo.done and (slip.terminal or lo.filled >= lo.requested - 1e-9):
            self._finalize(lo, f"status {slip.status}")

    async def _book_maker_fill(self, lo: LiveOrder, delta: float, price: float) -> None:
        info = self.registry.get(lo.outcome_id)
        if lo.maker_side == "sell" and info is not None and info.sibling_outcome_id:
            info = self.registry.sibling(lo.outcome_id) or info
        canon = self._canonical(MarketUpdate.from_info(info)) if info is not None else None
        if canon is None:
            log.critical("MAKER_FILL %s on unknown/unmapped outcome %s; exposure still recorded",
                         lo.exchange_order_id, lo.outcome_id)
            self.exposure.record_fill(f"maker-{lo.exchange_order_id}-{self.stats['fills']}", round(delta * price, 2))
            return
        key, side = canon
        leg = self._leg(lo.leg_id)
        if leg is None:
            upd = MarketUpdate.from_info(info, price=min(max(price, 0.0001), 0.9999))
            edge = None if lo.fair_prob is None else (
                lo.fair_prob / price - 1 if lo.maker_side == "buy" else (1 - lo.fair_prob) / price - 1)
            leg = self._record("MAKER_FILL", upd, side, int(round(delta)), round(delta * price, 2), 0.0, edge, False,
                               price=price, force=True)
            leg.live = True
            leg.exchange_order_id = lo.exchange_order_id
            lo.leg_id = leg.order_id
            self._schedule_markouts(key, side, info.line, price, int(round(delta)))
            if key in self.positions:
                self.positions[key].legs.append(leg)
            else:
                self.positions[key] = MarketPosition(legs=[leg])
        else:
            leg.contracts = int(round(lo.filled))
            leg.stake_usd = round(lo.fill_cost, 2)
            self.exposure.adjust(leg.position_id, leg.stake_usd)
        if self.maker is not None:
            self.maker.on_fill(lo.exchange_order_id)
            await self.maker.cancel_market(lo.key, "maker fill: position lock")

    def _moved_first_filter(self, update: MarketUpdate, key: tuple) -> Optional[str]:
        """Optional: only take edges where the SHARP moved last (the venue is stale), and recently."""
        if not self.taker_require_sharp_moved_last and self.taker_max_sharp_move_age_s is None:
            return None
        mover, age = self.moved_last(update, tuple(key))
        if self.taker_require_sharp_moved_last and mover != "sharp":
            return f"the venue moved last ({mover}): likely informed flow, not a stale price"
        if self.taker_max_sharp_move_age_s is not None and (age is None or age > self.taker_max_sharp_move_age_s):
            return (f"sharp move is {'unknown' if age is None else f'{age:.1f}s'} old "
                    f"(> {self.taker_max_sharp_move_age_s:g}s freshness window)")
        return None

    # ---------------- combo (parlay) quoting support ----------------
    def combo_leg_fair(self, leg):
        """Fair YES probability of a Kalshi leg from the sharp line, when the leg is a game market we track."""
        from combo_quoter import LegFair
        info = self.kalshi_registry.get(leg.market_ticker)
        if info is None:
            return None
        canon = self._canonical(MarketUpdate.from_info(info))
        if canon is None:
            return None
        key, side = canon
        sharp = self.book.lookup(key[0], key[1], key[2], key[3], side, line=info.line)
        fair = self._fair(sharp) if sharp is not None else None
        if fair is None:
            return None
        age = self.book.age_of(key[0], key[1], key[2], key[3], side) or 0.0
        return LegFair(prob_yes=fair, source="sharp", age_s=age, game=key[:3])

    def _combo_block_reason(self) -> Optional[str]:
        if self._loss_halted():
            return "daily loss stop"
        if self.exposure.taker_halted:
            return "exposure kill-switch"
        return None

    # ---------------- correlation + loss controls ----------------
    @staticmethod
    def _position_unhedged_usd(pos: MarketPosition) -> float:
        first = pos.primary
        n = first.requested_contracts if first.pending else first.contracts
        if n <= 0:
            return 0.0
        risk = first.requested_contracts * first.price if first.pending else first.stake_usd
        return risk * max(0.0, n - pos.hedged_contracts()) / n

    def game_unhedged(self, gid: tuple) -> float:
        """Unhedged $ across every market (moneyline, spread, total) of one game."""
        return round(sum(self._position_unhedged_usd(pos) for key, pos in self.positions.items() if key[:3] == gid), 2)

    def game_room(self, gid: tuple) -> float:
        return max(0.0, self.game_exposure_limit - self.game_unhedged(gid))

    @staticmethod
    def _utc_day(ts: Optional[float] = None) -> str:
        return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).strftime("%Y-%m-%d")

    def _loss_halted(self) -> bool:
        return self.loss_halted_day is not None and self.loss_halted_day == self._utc_day()

    def _record_daily_pnl(self, net: Optional[float], ts: Optional[float] = None, replay: bool = False) -> None:
        if net is None:
            return
        day = self._utc_day(ts)
        self.daily_pnl[day] = round(self.daily_pnl.get(day, 0.0) + net, 2)
        if (self.daily_loss_limit is not None and self.daily_pnl[day] <= -self.daily_loss_limit
                and self.loss_halted_day != day):
            self.loss_halted_day = day
            log.critical("DAILY_LOSS_STOP settled net P&L today $%.2f <= -$%.0f: no new positions or quotes until "
                         "00:00 UTC (hedges still allowed)", self.daily_pnl[day], self.daily_loss_limit)
            if not replay:
                self._ledger("DAILY_LOSS_STOP", day=day, net_pnl_usd=self.daily_pnl[day],
                             limit_usd=self.daily_loss_limit)
                if self.maker is not None and self.maker.quotes:
                    self._spawn(self.maker.cancel_all("daily loss stop"))

    def _load_today_pnl(self) -> None:
        """After a restart, today's settled P&L (and a loss stop already hit) come back from the ledger."""
        if self.ledger_path is None or not self.ledger_path.exists():
            return
        today = self._utc_day()
        with self.ledger_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") == "SETTLE" and self._utc_day(row.get("ts", 0)) == today:
                    self._record_daily_pnl(row.get("net_profit_usd"), row.get("ts"), replay=True)

    # ---------------- pregame cutoff ----------------
    def _note_start(self, gid: tuple, start: float) -> None:
        prev = self.game_start.get(gid)
        self.game_start[gid] = start if prev is None else min(prev, start)

    def _register_dynamic_games(self) -> None:
        """College leagues: register every listed game (Novig first, so its spellings become canonical)."""
        for reg in (self.registry, self.kalshi_registry):
            for info in sorted(reg.all(), key=lambda i: i.outcome_id):
                if info.league in DYNAMIC_LEAGUES:
                    if register_game(info.league, info.home_team, info.away_team, info.start_time) is None:
                        self.stats["dynamic_game_unmatched"] += 1

    def _index_start_times(self) -> None:
        """Learn scheduled start times from every registry (any venue) for every canonical game."""
        self._register_dynamic_games()
        for reg in (self.registry, self.kalshi_registry):
            for info in reg.all():
                if info.start_time:
                    canon = self._canonical(MarketUpdate.from_info(info))
                    if canon is not None:
                        self._note_start(canon[0][:3], info.start_time)

    def minutes_to_start(self, gid: tuple) -> Optional[float]:
        start = self.game_start.get(gid)
        return None if start is None else round((start - time.time()) / 60.0, 2)

    def cutoffs_for(self, league: str) -> tuple[float, float]:
        """(taker, maker) cutoff in seconds before the scheduled start for this league."""
        taker, maker = self.cutoff_overrides.get(league, (self.taker_cutoff_s, self.maker_cutoff_s))
        return taker, max(maker, taker)

    def _trade_blocked(self, gid: tuple, kind: str = "taker") -> Optional[str]:
        """
        Why this game may not be traded (kind="taker": takers + hedges) or quoted (kind="maker") right now.
        None = allowed.
        """
        if gid in self.live_games:
            return f"game is live ({self.live_games[gid]})"
        start = self.game_start.get(gid)
        if start is None:
            return "no scheduled start time (required in live mode)" if self.require_start_time else None
        taker_s, maker_s = self.cutoffs_for(gid[0])
        window = maker_s if kind == "maker" else taker_s
        if time.time() >= start - window:
            return f"{kind} cutoff ({window / 60:g} min before start)"
        return None

    def mark_game_live(self, gid: tuple, reason: str) -> None:
        """A venue or the sharp feed says this game is in play: never trade it again this session."""
        if gid in self.live_games:
            return
        self.live_games[gid] = reason
        self.stats["games_flagged_live"] += 1
        log.warning("GAME_LIVE %s: %s -> no orders, quotes pulled", gid, reason)
        self._ledger("GAME_LIVE", game=list(gid), reason=reason, minutes_to_start=self.minutes_to_start(gid))
        self._spawn(self._pull_game_quotes(gid, f"game live: {reason}"))

    def _on_sharp_live(self, league: str, home: str, away: str) -> None:
        self.mark_game_live((league, home, away), "sharp feed reports it in play")

    async def _pull_game_quotes(self, gid: tuple, reason: str) -> int:
        pulled = 0
        if self.maker is not None:
            for mk in {q.market_key for q in self.maker.quotes.values() if tuple(q.market_key)[:3] == gid}:
                before = len(self.maker.quotes)
                await self.maker.cancel_market(mk, reason)
                pulled += before - len(self.maker.quotes)
        return pulled

    async def _cutoff_loop(self) -> None:
        """Every second: maker cutoff pulls quotes, taker cutoff ends trading, both record the closing line."""
        while True:
            try:
                await self.run_cutoffs()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("CUTOFF loop error (continuing)")
            await asyncio.sleep(1.0)

    async def run_cutoffs(self) -> list[tuple[tuple, str]]:
        """
        Per game, in order, once each:
          maker  (default 3 min before start): pull every resting quote on the game
          taker  (default 1 min before start): no more takers/hedges; closing-line snapshot (research)
          start  (scheduled start): second closing-line snapshot, the latest pregame price (research)
        """
        now = time.time()
        reached = []
        for gid, start in list(self.game_start.items()):
            taker_s, maker_s = self.cutoffs_for(gid[0])
            done = self.cutoff_phases.setdefault(gid, set())
            for phase, at in (("maker", start - maker_s), ("taker", start - taker_s), ("start", start)):
                if phase in done or now < at:
                    continue
                done.add(phase)
                reached.append((gid, phase))
                if phase == "start":
                    if self.research is not None and gid not in self.live_games:
                        self._research_close(gid, "start")
                    continue
                pulled = await self._pull_game_quotes(gid, f"{phase} cutoff")
                log.warning("CUTOFF %s %s cutoff reached (%.1f min to start): %d quote(s) pulled%s", gid, phase,
                            (start - now) / 60, pulled, ", no new orders" if phase == "taker" else "")
                self._ledger("CUTOFF", game=list(gid), phase=phase, quotes_pulled=pulled,
                             minutes_to_start=self.minutes_to_start(gid))
                if phase == "taker" and self.research is not None and gid not in self.live_games:
                    self._research_close(gid, "cutoff")
        return reached

    # ---------------- research hooks ----------------
    def _sides(self, key: tuple) -> tuple[str, str]:
        return ("over", "under") if key[3] == "total" else (key[1], key[2])

    def _latest_updates(self):
        yield from self.feed.latest.values()
        if self.kalshi is not None:
            yield from self.kalshi.latest.values()
        if self.prophetx is not None:
            yield from self.prophetx.latest.values()

    def _research_gap(self, update: MarketUpdate, key: tuple, side: str) -> None:
        tie = DEAD_HEAT_PAYOUT if key[0] == "NFL" and key[3] == "moneyline" else None
        self.gaps.update(key, update.venue, side, update.price, update.available_volume, update.line, tie,
                         self._sides(key), self.minutes_to_start(key[:3]))

    def _research_decision(self, update: MarketUpdate, key: tuple, side: str, decision,
                           blocked: Optional[str] = None) -> None:
        age = self.book.age_of(key[0], key[1], key[2], key[3], side)
        mover, move_age = self.moved_last(update, tuple(key))
        fair_by_method = {}
        sharp = self.book.lookup(key[0], key[1], key[2], key[3], side, line=update.line)
        if sharp is not None:
            try:
                odds = [american_to_decimal(sharp.odds_for), american_to_decimal(sharp.odds_against)]
                fair_by_method = {m: round(devig(odds, m)[0][0], 6) for m in DEVIG_METHODS}
            except ValueError:
                pass
        self.research.write(
            "DECISION", venue=update.venue, outcome_id=update.outcome_id, game=list(key), side=side,
            line=update.line, price=update.price, depth=update.available_volume, fair_prob=decision.fair_prob,
            edge=decision.edge, action=decision.action, reason=decision.reason, fee_usd=decision.fee_usd,
            sharp_age_s=None if age is None else round(age, 3),
            moved_last=mover, sharp_move_age_s=move_age,
            minutes_to_start=self.minutes_to_start(key[:3]), blocked=blocked, fair_by_method=fair_by_method,
            sharp_source=None if sharp is None else sharp.source)

    def _research_execution(self, lo: LiveOrder, leg: PaperOrder, pos: Optional[MarketPosition]) -> None:
        """
        One EXECUTION row per finished order (live or simulated): how much of what we asked for we actually got,
        at what price versus the price we saw, how fast, and the expected profit of what filled.
        """
        if self.research is None:
            return
        filled = lo.filled
        avg = (lo.fill_cost - lo.fees) / filled if filled else None
        requested_usd = round(lo.requested * (lo.expected_price or lo.limit_price), 2)
        if lo.kind == "DIRECTIONAL" and lo.edge is not None:
            expected = round(lo.edge * lo.fill_cost, 2) if lo.locked_per_contract is None else 0.0
        elif lo.kind == "ARB_HEDGE" and lo.locked_per_contract is not None and pos is not None:
            expected = round(lo.locked_per_contract * min(filled, pos.primary.contracts), 2)
        else:
            expected = 0.0
        ms = lambda a, b: None if a is None or b is None else round((b - a) * 1000, 1)  # noqa: E731
        self.research.write(
            "EXECUTION", simulated=self.sim, venue=lo.venue, kind=lo.kind, outcome_id=lo.outcome_id,
            game=list(lo.key), requested=lo.requested, filled=filled,
            fill_ratio=round(filled / lo.requested, 4) if lo.requested else None, requested_usd=requested_usd,
            filled_usd=round(lo.fill_cost, 2), expected_price=lo.expected_price, limit_price=lo.limit_price,
            avg_price=None if avg is None else round(avg, 6),
            slippage_cents=None if avg is None or lo.expected_price is None else round((avg - lo.expected_price) * 100, 3),
            ack_ms=ms(lo.sent_at, lo.acked_at), fill_ms=ms(lo.sent_at, lo.first_fill_at), edge=lo.edge,
            expected_profit_usd=expected, minutes_to_start=self.minutes_to_start(tuple(lo.key)[:3]))

    # ---- edge survival: how long does an opportunity stay available? ----
    EDGE_WATCH_MAX_S = 120.0

    def _watch_edge(self, update: MarketUpdate, decision) -> None:
        if self.research is None or update.price is None or update.outcome_id in self._edge_watch:
            return
        self._edge_watch[update.outcome_id] = dict(
            t0=time.time(), price=update.price, depth=self._depth_at(update, update.price), venue=update.venue,
            edge=decision.edge, game=list(self._canonical(update)[0]) if self._canonical(update) else None)

    @staticmethod
    def _depth_at(update: MarketUpdate, price: float) -> float:
        levels = update.ask_levels or ([(update.price, update.available_volume)] if update.price else [])
        return round(sum(q for p, q in levels if p <= price + 1e-9), 2)

    def _check_edge_watch(self, update: MarketUpdate) -> None:
        w = self._edge_watch.get(update.outcome_id)
        if w is None:
            return
        now = time.time()
        depth = 0.0 if update.price is None else self._depth_at(update, w["price"])
        if update.price is not None and update.price > w["price"] + 1e-9:
            reason = "price moved away"
        elif depth < 0.5 * w["depth"]:
            reason = "more than half the size gone"
        elif now - w["t0"] > self.EDGE_WATCH_MAX_S:
            reason = "still there"
        else:
            return
        del self._edge_watch[update.outcome_id]
        self.research.write("EDGE_SURVIVAL", venue=w["venue"], outcome_id=update.outcome_id, game=w["game"],
                            price=w["price"], start_depth=w["depth"], end_depth=depth, edge=w["edge"],
                            survival_ms=round((now - w["t0"]) * 1000, 1), reason=reason,
                            censored=reason == "still there")

    def _research_entry(self, leg: PaperOrder, canon_key: Optional[tuple]) -> None:
        self.research.write("ENTRY", order_id=leg.order_id, order_kind=leg.kind, venue=leg.venue, live=leg.live,
                            outcome_id=leg.outcome_id, game=list(canon_key) if canon_key else None, side=leg.side,
                            line=leg.line, price=leg.price, contracts=leg.contracts, stake_usd=leg.stake_usd,
                            edge=leg.edge, minutes_to_start=self.minutes_to_start(canon_key[:3]) if canon_key else None)

    def _research_close(self, gid: tuple, point: str = "cutoff") -> None:
        """Closing snapshot at the cutoff: sharp fair value and venue prices for every side of every market."""
        for upd in list(self._latest_updates()):
            canon = self._canonical(upd)
            if canon is None or canon[0][:3] != gid:
                continue
            key, side = canon
            sharp = self.book.lookup(key[0], key[1], key[2], key[3], side, line=upd.line)
            fair = self._fair(sharp) if sharp is not None and (
                upd.line is None or sharp.line is None or math.isclose(upd.line, sharp.line)) else None
            self.research.write("CLOSE", point=point, game=list(key), side=side, line=upd.line, venue=upd.venue,
                                outcome_id=upd.outcome_id, close_fair_prob=fair, ask=upd.price, bid=upd.best_bid,
                                depth=upd.available_volume, minutes_to_start=self.minutes_to_start(gid))

    def _schedule_markouts(self, key: tuple, side: str, line: Optional[float], price: float, contracts: int) -> None:
        if self.research is None:
            return

        minutes = self.minutes_to_start(key[:3])

        async def markouts() -> None:
            start = time.time()
            for delay in MARKOUT_DELAYS:
                await asyncio.sleep(max(0.0, start + delay - time.time()))
                sharp = self.book.lookup(key[0], key[1], key[2], key[3], side, line=line)
                fair = self._fair(sharp) if sharp is not None else None
                self.research.write("MARKOUT", game=list(key), side=side, line=line, fill_price=price,
                                    minutes_to_start=minutes,
                                    contracts=contracts, delay_s=delay, fair_prob=fair,
                                    markout_per_contract=None if fair is None else round(fair - price, 5))
        self._spawn(markouts())

    async def _depth_loop(self) -> None:
        """Once a minute: top of book for every outcome (the liquidity curve) + cheapest cross-venue combination."""
        while True:
            await asyncio.sleep(self.depth_sample_s)
            try:
                self.sample_depth()
            except Exception:  # noqa: BLE001
                log.exception("RESEARCH depth sample failed (continuing)")

    def sample_depth(self) -> int:
        n = 0
        for upd in list(self._latest_updates()):
            canon = self._canonical(upd)
            key = canon[0] if canon else None
            self.research.write("DEPTH", venue=upd.venue, outcome_id=upd.outcome_id, game=list(key) if key else None,
                                side=canon[1] if canon else upd.outcome, line=upd.line, ask=upd.price,
                                ask_size=upd.available_volume, bid=upd.best_bid, bid_size=upd.bid_volume,
                                minutes_to_start=self.minutes_to_start(key[:3]) if key else None)
            n += 1
        if self.gaps is not None:
            for mk in list(self.gaps.books):
                tie = DEAD_HEAT_PAYOUT if mk[0] == "NFL" and mk[3] == "moneyline" else None
                best = self.gaps.best_combo(mk, tie, self._sides(mk))
                if best is not None:
                    self.research.write("BEST_COMBO", game=list(mk), minutes_to_start=self.minutes_to_start(mk[:3]),
                                        **best)
        return n

    # ---------------- settlement & reconciliation ----------------
    @property
    def capital_pool(self) -> float:
        return round(BASELINE_CAPITAL_USD + self.cumulative_pnl, 2)

    def _legs_for_outcome(self, outcome_id: str) -> list[tuple[GameKey, PaperOrder]]:
        return [(key, leg) for key, pos in self.positions.items() for leg in pos.legs
                if leg.live and leg.outcome_id == outcome_id]

    def _position_clients(self) -> list[tuple[str, Any]]:
        return [(v, c) for v, c in (("novig", self.positions_client), ("kalshi", self.kalshi_positions_client))
                if c is not None]

    def _restore(self, pos, source: str, venue: str = "novig") -> PaperOrder:
        """Book an exchange position the engine did not know about as a real liability."""
        info = (self.kalshi_registry if venue == "kalshi" else self.registry).get(pos.outcome_id)
        canon = self._canonical(MarketUpdate.from_info(info)) if info is not None else None
        key, side = canon if canon else (("UNMAPPED", pos.outcome_id, "", ""), pos.outcome_id)
        # exposure = what the position cost; unknown cost -> contracts x $1 (the most it can lose is its cost <= $1)
        stake = round(pos.cost_usd if pos.cost_usd is not None else pos.contracts * 1.0, 2)
        price = (pos.cost_usd / pos.contracts) if pos.cost_usd and pos.contracts else 0.5
        leg = PaperOrder(order_id=len(self.orders) + 1, kind="RESTORED", venue=venue, outcome_id=pos.outcome_id,
                         event_id=(info.event_id if info else pos.event_id) or "", league=info.league if info else "",
                         market_type=info.market_type if info else "", side=side, line=info.line if info else None,
                         price=min(max(price, 0.0001), 0.9999), contracts=int(round(pos.contracts)), stake_usd=stake,
                         edge=None, capped=False, placed_at=time.time(), live=True)
        self.exposure.record_fill(leg.position_id, stake)
        self.orders.append(leg)
        self.positions.setdefault(key, MarketPosition(legs=[])).legs.append(leg)
        self._ledger("RESTORE", source=source, venue=venue, restore_id=leg.position_id, outcome_id=pos.outcome_id,
                     game_id=leg.event_id, market_type=leg.market_type, side=side, contracts=leg.contracts,
                     exposure_usd=stake, mapped=canon is not None, open_exposure_usd=self.exposure.open_exposure)
        if canon is None:
            log.warning("RESTORE outcome %s is not in the market registry: exposure counted, game cannot be locked",
                        pos.outcome_id)
        return leg

    async def startup_sync(self) -> bool:
        """Live: load every OPEN exchange position (every venue we trade) before trading. False = could not."""
        restored = []
        for venue, client in self._position_clients():
            for attempt in range(1, self.sync_retries + 1):
                try:
                    open_positions = await client.open_positions()
                    break
                except Exception as exc:  # noqa: BLE001
                    log.error("SYNC %s attempt %d/%d failed (%s: %s)", venue, attempt, self.sync_retries,
                              type(exc).__name__, exc)
                    if attempt == self.sync_retries:
                        log.critical("SYNC could not load open positions from %s: live trading will NOT start",
                                     venue.capitalize())
                        self._ledger("SYNC_FAILED", venue=venue, attempts=attempt,
                                     error=f"{type(exc).__name__}: {exc}")
                        return False
                    await asyncio.sleep(self.sync_retry_delay)
            restored += [self._restore(p, "startup", venue) for p in open_positions if not p.is_settled]
        self.synced = True
        total = round(sum(l.stake_usd for l in restored), 2)
        log.warning("SYNC restored %d open exchange position(s) worth $%.2f; open exposure $%.2f of $%.2f%s",
                    len(restored), total, self.exposure.open_exposure, self.exposure.limit,
                    " — taker orders HALTED until settlements free room" if self.exposure.taker_halted else "")
        self._ledger("SYNC", restored=len(restored), restored_exposure_usd=total,
                     open_exposure_usd=self.exposure.open_exposure, capital_pool=self.capital_pool)
        return True

    def _apply_settlement(self, pos, legs: list[tuple[GameKey, PaperOrder]]) -> dict:
        """ATOMIC (no await): release exposure, drop the legs / lock, append the SETTLE row."""
        stake = round(sum(l.stake_usd for _, l in legs), 2)
        ours = sum(l.contracts for _, l in legs)
        if pos.contracts and abs(pos.contracts - ours) > 1e-6:
            log.warning("SETTLE %s: exchange reports %g contracts, engine tracked %d", pos.outcome_id,
                        pos.contracts, ours)
        if not pos.contracts:
            pos = pos.model_copy(update=dict(contracts=float(ours)))
        net, payout, method = settlement_pnl(pos, stake)
        released = 0.0
        for key, leg in legs:
            released += self.exposure.settle(leg.position_id)
            self.unconfirmed_legs.pop(leg.order_id, None)
            self._drop_leg(key, leg)
        self.processed_settlements.add(pos.settlement_id)
        self.cumulative_pnl = round(self.cumulative_pnl + (net or 0.0), 2)
        self._record_daily_pnl(net)
        first = legs[0][1]
        row = dict(timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                   settlement_id=pos.settlement_id, game_id=first.event_id or pos.event_id,
                   market_type=first.market_type, outcome_id=pos.outcome_id, side=first.side,
                   contracts=pos.contracts, stake_usd=stake, payout_usd=payout, net_profit_usd=net,
                   pnl_method=method, result=pos.result, released_exposure_usd=round(released, 2),
                   released_position_ids=[l.position_id for _, l in legs],
                   released_order_ids=[l.exchange_order_id for _, l in legs if l.exchange_order_id],
                   open_exposure_usd=self.exposure.open_exposure, resulting_capital_pool=self.capital_pool)
        self._ledger("SETTLE", **row)
        self.stats["settlements"] += 1
        if net is None:
            log.critical("SETTLE %s: exposure $%.2f released but P&L UNKNOWN (no pnl/payout/result from Novig)",
                         pos.outcome_id, released)
        else:
            log.info("SETTLE %s %s net %+.2f (stake $%.2f, payout $%.2f, %s); released $%.2f; exposure $%.2f; "
                     "capital $%.2f", row["game_id"], pos.outcome_id, net, stake, payout, method, released,
                     self.exposure.open_exposure, self.capital_pool)
        return row

    async def settlement_sweep(self) -> list[dict]:
        """Every venue: settle what it says settled; restore untracked open positions; resolve UNCONFIRMED orders."""
        rows = []
        for venue, client in self._position_clients():
            try:
                rows += await self._sweep_venue(venue, client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one venue failing must not stop the other's sweep
                log.error("SETTLEMENT %s sweep failed (%s: %s)", venue, type(exc).__name__, exc)
                if len(self._position_clients()) == 1:
                    raise
        return rows

    async def _sweep_venue(self, venue: str, client) -> list[dict]:
        settled = await client.settled_positions()
        open_positions = await client.open_positions()
        rows = []
        for pos in settled:
            if pos.settlement_id in self.processed_settlements or not pos.is_settled:
                continue
            legs = self._legs_for_outcome(pos.outcome_id)
            if legs:
                rows.append(self._apply_settlement(pos, legs))
        open_by_outcome = {p.outcome_id: p for p in open_positions if not p.is_settled}
        settled_outcomes = {p.outcome_id for p in settled}
        now = time.time()
        for leg_id, since in list(self.unconfirmed_legs.items()):
            leg = self._leg(leg_id)
            if leg is None:
                self.unconfirmed_legs.pop(leg_id, None)
                continue
            if leg.venue != venue:
                continue
            key = next((k for k, pos in self.positions.items() if leg in pos.legs), None)
            if leg.outcome_id in open_by_outcome:
                exch = open_by_outcome[leg.outcome_id]
                leg.contracts = int(round(exch.contracts))
                leg.stake_usd = round(exch.cost_usd if exch.cost_usd is not None else leg.contracts * leg.price, 2)
                self.exposure.adjust(leg.position_id, leg.stake_usd)
                self.unconfirmed_legs.pop(leg_id)
                self._ledger("UNCONFIRMED_RESOLVED", exchange_order_id=leg.exchange_order_id, filled=leg.contracts,
                             exposure_usd=leg.stake_usd)
                log.warning("UNCONFIRMED %s resolved from exchange positions: %d contracts, $%.2f",
                            leg.exchange_order_id, leg.contracts, leg.stake_usd)
            elif leg.outcome_id not in settled_outcomes and now - since >= 60 and key is not None:
                self.exposure.adjust(leg.position_id, 0)
                self._drop_leg(key, leg)
                self.unconfirmed_legs.pop(leg_id)
                self._ledger("UNCONFIRMED_RELEASED", exchange_order_id=leg.exchange_order_id,
                             reason="no open or settled exchange position for this outcome")
                log.warning("UNCONFIRMED %s released: the exchange holds no position for %s", leg.exchange_order_id,
                            leg.outcome_id)
        for outcome, exch in open_by_outcome.items():
            if outcome in settled_outcomes:
                continue          # the exchange's open list can lag its settlements: never resurrect a settled one
            tracked = self._legs_for_outcome(outcome)
            if not tracked:
                rows.append({"restored": self._restore(exch, "sweep", venue).position_id})
            else:
                ours = sum(l.contracts for _, l in tracked)
                if abs(ours - exch.contracts) > 1e-6 and not any(l.pending for _, l in tracked):
                    self._ledger("POSITION_MISMATCH", outcome_id=outcome, engine_contracts=ours,
                                 exchange_contracts=exch.contracts)
                    log.warning("POSITION_MISMATCH %s: engine %d vs exchange %g contracts", outcome, ours,
                                exch.contracts)
        return rows

    async def _settlement_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settlement_interval)
            try:
                await self.settlement_sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a failed sweep just waits for the next one
                log.error("SETTLEMENT sweep failed (%s: %s); retrying in %.0fs", type(exc).__name__, exc,
                          self.settlement_interval)

    def settle_event(self, event_id: str) -> float:
        """Mark positions containing this event id as settled: releases exposure and the lock."""
        released = 0.0
        for key in [k for k, pos in self.positions.items() if any(l.event_id == event_id for l in pos.legs)]:
            for leg in self.positions.pop(key).legs:
                released += self.exposure.settle(leg.position_id)
        log.info("SETTLE %s released $%.2f; open exposure now $%.2f", event_id, released, self.exposure.open_exposure)
        return round(released, 2)

    # ---------------- maker integration ----------------
    def _maker_targets(self) -> list[MakerTarget]:
        """Novig outcomes eligible for passive quotes right now."""
        if not self.feed.connected.is_set():
            return []
        targets, seen_markets = [], set()
        for info in sorted(self.registry.all(), key=lambda i: i.outcome_id):
            if info.venue != "novig" or not info.sibling_outcome_id or info.market_id in seen_markets:
                continue
            seen_markets.add(info.market_id)          # quote ONE outcome per two-outcome market
            upd = MarketUpdate.from_info(info)
            canon = self._canonical(upd)
            if canon is None:
                continue
            key, side = canon
            if (key in self.positions or self._trade_blocked(key[:3], "maker") or self._loss_halted()
                    or self.game_unhedged(key[:3]) > 0):      # correlated: only quote games we hold nothing in
                continue
            sharp = self.book.lookup(key[0], key[1], key[2], key[3], side, line=info.line)
            if sharp is None or (info.line is not None and sharp.line is not None
                                 and not math.isclose(info.line, sharp.line)):
                continue
            fair = self._fair(sharp)
            if fair is not None:
                targets.append(MakerTarget(outcome_id=info.outcome_id, market_key=key, fair_prob=fair,
                                           label=f"{key[0]} {key[3]} {side}"))
        return targets

    async def _simulate_maker_fills(self, update: MarketUpdate, key: GameKey) -> None:
        """PAPER fills: a resting bid fills if the ask trades through it; a resting ask if the bid does."""
        for q in list(self.maker.quotes_for(update.outcome_id)):
            crossed = ((q.side == "buy" and update.price is not None and update.price * 100 <= q.price_cents)
                       or (q.side == "sell" and update.best_bid is not None and update.best_bid * 100 >= q.price_cents))
            if not crossed or self.maker.on_fill(q.order_id) is None:
                continue
            if q.side == "buy":
                side, price = self._canonical(update)[1], q.price_cents / 100
            else:   # selling this outcome == owning its sibling at (1 - price)
                sib = self.registry.sibling(update.outcome_id)
                sib_canon = self._canonical(MarketUpdate.from_info(sib)) if sib else None
                if sib_canon is None:
                    log.error("MAKER_FILL sell on %s but sibling unknown; recording on this outcome", update.outcome_id)
                    side = self._canonical(update)[1]
                else:
                    side = sib_canon[1]
                price = 1 - q.price_cents / 100
            stake = round(q.contracts * price, 2)
            edge = (q.fair_prob / price - 1) if q.side == "buy" else ((1 - q.fair_prob) / price - 1)
            log.info("MAKER_FILL %s %s %d @ %dc -> long %s @ %.4f edge=%+.2f%%", update.outcome_id, q.side,
                     q.contracts, q.price_cents, side, price, edge * 100)
            self.stats["maker_fills"] += 1
            order = self._record("MAKER_FILL", update, side, q.contracts, stake, 0.0, edge, False, price=price,
                                 force=True)
            self._schedule_markouts(key, side, update.line, price, q.contracts)
            if key not in self.positions:
                self.positions[key] = MarketPosition(legs=[order])
            else:
                self.positions[key].legs.append(order)
            await self.maker.cancel_market(key, "maker fill: position lock")

    # ---------------- lifecycle ----------------
    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            log.info("HEARTBEAT uptime=%.1fs novig=%s kalshi=%s sharp_lines=%d updates=%d open_games=%d "
                     "resting_quotes=%d exposure=$%.2f/$%.0f taker_halted=%s",
                     time.monotonic() - self.started_at, self.feed.connected.is_set(),
                     "off" if self.kalshi is None else self.kalshi.connected.is_set(), len(self.book),
                     self.stats["updates"], len(self.positions), 0 if self.maker is None else len(self.maker.quotes),
                     self.exposure.open_exposure, self.exposure.limit, self.exposure.taker_halted)

    def total_exposure(self) -> float:
        return self.exposure.open_exposure

    async def run(self, duration: Optional[float] = None) -> None:
        log.info("SUPERVISOR starting (novig=%s, kalshi=%s, maker=%s, paper trading only)", self.feed.url,
                 "off" if self.kalshi is None else self.kalshi.url, self.maker is not None)
        if self.live:
            self.record_live_plan()
            self._ledger("SESSION_START", novig_socket=self.feed.url, max_stake_usd=self.max_stake,
                         exposure_limit_usd=self.exposure.limit, maker=self.maker is not None,
                         fill_volume_mode=self.fill_volume_mode)
            log.warning("LIVE TRADING ENABLED: real orders will be sent to %s. Orders resting from earlier sessions "
                        "are unknown to this engine — cancel them in the Novig UI first.",
                        self.order_gateway.api_base if hasattr(self.order_gateway, "api_base") else "the gateway")
        await self.bootstrap()
        if self.live and self._position_clients() and not await self.startup_sync():
            log.critical("SUPERVISOR stopping: startup position sync failed, refusing to trade blind")
            await self.shutdown({})
            return
        tasks = {name: asyncio.create_task(factory(), name=name) for name, factory in self._task_factories.items()}
        deadline = None if duration is None else time.monotonic() + duration
        try:
            while True:
                timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
                if timeout == 0.0:
                    break
                done, _ = await asyncio.wait(tasks.values(), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    name = task.get_name()
                    exc = None if task.cancelled() else task.exception()
                    self.stats["task_restarts"] += 1
                    log.error("SUPERVISOR task %s exited unexpectedly (%r); restarting", name, exc)
                    tasks[name] = asyncio.create_task(self._task_factories[name](), name=name)
        finally:
            await self.shutdown(tasks)

    async def shutdown(self, tasks: dict[str, asyncio.Task]) -> None:
        log.info("SUPERVISOR shutting down")
        if self.maker is not None and self.maker.quotes:
            await self.maker.cancel_all("shutdown")
        await self.feed.stop()
        if self.kalshi is not None:
            await self.kalshi.stop()
        for name, task in tasks.items():
            if name not in {"novig_feed", "kalshi_feed"}:
                task.cancel()
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks, results):
            if isinstance(result, Exception):
                log.error("SUPERVISOR task %s raised during shutdown: %r", name, result)
        closeables = {id(c): c for c in (self.poller.fetch, self.novig_rest, self.kalshi_rest, self.maker_gateway,
                                         self.order_gateway, self.positions_client, self.kalshi_gateway,
                                         self.kalshi_positions_client,
                                         self.prophetx.client if self.prophetx is not None else None,
                                         *self.sim_gateways.values())
                      if c is not None}
        for closeable in closeables.values():
            closer = getattr(closeable, "close", None)
            if closer is not None:
                await closer()
        log.info("SUPERVISOR stopped. stats=%s exposure=$%.2f", json.dumps(dict(self.stats)), self.total_exposure())


# ==========================================================================
# Live configuration
# ==========================================================================
class ConfigError(RuntimeError):
    pass


# ---- production endpoints -------------------------------------------------
# Tape: wss://api.novig.com/tape (Novig docs). The brief's "wss://://novig.com" has no host and is rejected.
# REST: Novig's docs example serves /nbx/v2/emm/* from https://api.novig.us (the brief asks for the docs'
#       host; "novig.us" without "api." does not appear in the docs). Override with NOVIG_API_BASE.
NOVIG_PROD_API_BASE = "https://api.novig.us"
NOVIG_EVENTS_PATH = "/nbx/v2/emm/events?status=OPEN_PREGAME&limit=100"
NOVIG_PROD_EVENTS_URL = NOVIG_PROD_API_BASE + NOVIG_EVENTS_PATH
LIVE_ACK_VALUE = "yes"

# ---- guarded rollout ---------------------------------------------------------
# Live trading ALWAYS starts at these caps. Anything larger (up to the hard
# $1,000 / $15,000 ceilings) or turning the maker on requires LIVE_SCALE_APPROVED_BY,
# which is written to the log as a CRITICAL audit line on every start.
CANARY_MAX_STAKE_USD = 10.0
CANARY_EXPOSURE_LIMIT_USD = 100.0
CANARY_MAKER_ENABLED = False


def validate_ws_url(url: Optional[str], name: str) -> str:
    """Reject malformed WebSocket URLs such as 'wss://://novig.com' (empty host)."""
    parsed = urlparse(url or "")
    host = parsed.hostname or ""
    if parsed.scheme not in {"ws", "wss"} or not host or ("." not in host and host != "localhost"):
        raise ConfigError(f"{name}={url!r} is not a valid WebSocket URL (expected e.g. wss://api.novig.com/tape)")
    if parsed.scheme == "ws" and host not in {"localhost", "127.0.0.1"}:
        raise ConfigError(f"{name} must use wss:// (encrypted) for a remote host")
    return url


def _cap(env: dict, name: str, ceiling: float, default: float) -> float:
    raw = env.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number") from None
    if not 0 < value <= ceiling:
        raise ConfigError(f"{name} must be > 0 and at most the hard ceiling ${ceiling:,.0f}")
    return value


class LivePlan(BaseModel):
    """Resolved live-trading limits (what --check-config prints)."""
    max_stake: float
    exposure_limit: float
    maker_enabled: bool
    scaled_up: bool
    approved_by: Optional[str] = None


def resolve_live_plan(env: dict) -> LivePlan:
    """Canary caps by default; any increase requires LIVE_SCALE_APPROVED_BY."""
    stake = _cap(env, "LIVE_MAX_STAKE_USD", MAX_STAKE_USD, CANARY_MAX_STAKE_USD)
    exposure = _cap(env, "LIVE_EXPOSURE_LIMIT_USD", GLOBAL_EXPOSURE_LIMIT_USD, CANARY_EXPOSURE_LIMIT_USD)
    maker = env.get("MAKER_MODE", env.get("MAKER_ENABLED", "0")).strip().lower() in {"1", "true", "yes", "on"}
    scaled = stake > CANARY_MAX_STAKE_USD or exposure > CANARY_EXPOSURE_LIMIT_USD or (maker and not CANARY_MAKER_ENABLED)
    approver = (env.get("LIVE_SCALE_APPROVED_BY") or "").strip() or None
    if scaled and not approver:
        raise ConfigError(
            f"live limits above the canary (${CANARY_MAX_STAKE_USD:,.0f} stake / ${CANARY_EXPOSURE_LIMIT_USD:,.0f} "
            f"exposure / maker off) require LIVE_SCALE_APPROVED_BY='<name, date, reconciliation reference>' "
            f"— set it only after live_ledger.jsonl has been reconciled against Novig's order history")
    return LivePlan(max_stake=stake, exposure_limit=exposure, maker_enabled=maker, scaled_up=scaled,
                    approved_by=approver if scaled else None)


def _minutes(name: str, raw: str) -> float:
    try:
        minutes = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number") from None
    if not 0.5 <= minutes <= 24 * 60:
        raise ConfigError(f"{name} must be between 0.5 and 1440 minutes")
    return minutes


def cutoffs_from_env(env: dict) -> dict:
    """
    TAKER_CUTOFF_MINUTES (default 1): no new orders or hedges on a game this long before its scheduled start.
    MAKER_CUTOFF_MINUTES (default 3): resting quotes pulled this long before. Never less than the taker cutoff.
    CUTOFF_OVERRIDES: per league "taker/maker" minutes, e.g. "NBA=2/5,NFL=1/3".
    """
    taker = _minutes("TAKER_CUTOFF_MINUTES", env["TAKER_CUTOFF_MINUTES"]) if env.get("TAKER_CUTOFF_MINUTES") \
        else TAKER_CUTOFF_SECONDS / 60
    maker = _minutes("MAKER_CUTOFF_MINUTES", env["MAKER_CUTOFF_MINUTES"]) if env.get("MAKER_CUTOFF_MINUTES") \
        else MAKER_CUTOFF_SECONDS / 60
    if maker < taker:
        raise ConfigError("MAKER_CUTOFF_MINUTES must be >= TAKER_CUTOFF_MINUTES (quotes come off first)")
    overrides = {}
    for item in filter(None, (x.strip() for x in (env.get("CUTOFF_OVERRIDES") or "").split(","))):
        try:
            league, pair = item.split("=")
            t_raw, m_raw = pair.split("/")
        except ValueError:
            raise ConfigError(f"CUTOFF_OVERRIDES entry {item!r} must look like NBA=2/5 (taker/maker minutes)") \
                from None
        t, m = _minutes("CUTOFF_OVERRIDES", t_raw), _minutes("CUTOFF_OVERRIDES", m_raw)
        if m < t:
            raise ConfigError(f"CUTOFF_OVERRIDES {item!r}: maker minutes must be >= taker minutes")
        overrides[league.strip().upper()] = (t * 60, m * 60)
    return dict(taker_cutoff_s=taker * 60, maker_cutoff_s=maker * 60, cutoff_overrides=overrides)


def _usd(env: dict, name: str, default: Optional[float], ceiling: float) -> Optional[float]:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    if raw.lower() in {"off", "none", "0"} and name == "DAILY_LOSS_LIMIT_USD":
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number") from None
    if not 0 < value <= ceiling:
        raise ConfigError(f"{name} must be > 0 and at most ${ceiling:,.0f}")
    return value


def risk_controls_from_env(env: dict) -> dict:
    """
    GAME_EXPOSURE_LIMIT_USD (default 1000): unhedged $ per game across its moneyline/spread/total.
    DAILY_LOSS_LIMIT_USD (default 2000, "off" to disable): settled loss per UTC day that stops new risk.
    """
    return dict(game_exposure_limit=_usd(env, "GAME_EXPOSURE_LIMIT_USD", GAME_EXPOSURE_LIMIT_USD,
                                         GLOBAL_EXPOSURE_LIMIT_USD),
                daily_loss_limit=_usd(env, "DAILY_LOSS_LIMIT_USD", DAILY_LOSS_LIMIT_USD, BASELINE_CAPITAL_USD))


def fair_value_from_env(env: dict) -> dict:
    """
    DEVIG_METHOD: multiplicative (default) | power | shin.
    SHARP_BOOK_WEIGHTS: consensus weights per sportsbook in the feed, e.g. "pinnacle:2,circa sports:1"
    (books not listed are ignored; "*:1" includes every other book). Empty = every book equally.
    """
    method = (env.get("DEVIG_METHOD") or "multiplicative").strip().lower()
    if method not in DEVIG_METHODS:
        raise ConfigError(f"DEVIG_METHOD={method!r} must be one of {', '.join(DEVIG_METHODS)}")
    weights = {}
    for item in filter(None, (x.strip() for x in (env.get("SHARP_BOOK_WEIGHTS") or "").split(","))):
        book, _, raw = item.rpartition(":")
        try:
            w = float(raw)
        except ValueError:
            raise ConfigError(f"SHARP_BOOK_WEIGHTS entry {item!r} must look like pinnacle:2") from None
        if not book or w < 0:
            raise ConfigError(f"SHARP_BOOK_WEIGHTS entry {item!r} must look like pinnacle:2 (weight >= 0)")
        weights[book.strip().lower()] = w
    return dict(devig_method=method, sharp_weights=weights or None)


def taker_filters_from_env(env: dict) -> dict:
    """
    TAKER_REQUIRE_SHARP_MOVED_LAST=1: only take an edge when the sharp moved after the venue's price
    (the venue is stale). TAKER_MAX_SHARP_MOVE_AGE_SECONDS=5: ... and only within 5s of that move.
    Both default off; turn on only when research_report shows they separate good edges from bad.
    """
    moved = (env.get("TAKER_REQUIRE_SHARP_MOVED_LAST") or "0").strip().lower() in {"1", "true", "yes", "on"}
    raw = (env.get("TAKER_MAX_SHARP_MOVE_AGE_SECONDS") or "").strip()
    age = None
    if raw:
        try:
            age = float(raw)
        except ValueError:
            raise ConfigError(f"TAKER_MAX_SHARP_MOVE_AGE_SECONDS={raw!r} is not a number") from None
        if not 0 < age <= 3600:
            raise ConfigError("TAKER_MAX_SHARP_MOVE_AGE_SECONDS must be > 0 and <= 3600")
    return dict(taker_require_sharp_moved_last=moved, taker_max_sharp_move_age_s=age,
                arb_pairs_enabled=(env.get("ARB_PAIRS_ENABLED") or "1").strip().lower() not in {"0", "false", "off"})


def research_from_env(env: dict) -> Optional[ResearchRecorder]:
    """RESEARCH_ENABLED (default on) writes measurement rows to RESEARCH_DIR (default ./research)."""
    if env.get("RESEARCH_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    return ResearchRecorder(env.get("RESEARCH_DIR", "research"))


def sharp_source_from_env(env: dict):
    """
    SHARP_PROVIDER=therundown  -> TheRundown v2 (THERUNDOWN_API_KEY; THERUNDOWN_AFFILIATE_IDS default 3 =
                                  Pinnacle; THERUNDOWN_WEBSOCKET=1 default, needs the Ultra plan or above)
    SHARP_PROVIDER_CONFIG=path -> generic JSON-mapped provider (OpticOdds / OddsJam examples in config/)
    neither                    -> None: measurement mode
    """
    if (env.get("SHARP_PROVIDER") or "").strip().lower() == "therundown":
        from therundown_feed import TheRundownSource
        raw_ids = env.get("THERUNDOWN_AFFILIATE_IDS") or "3"
        try:
            affiliates = tuple(int(x) for x in raw_ids.split(",") if x.strip())
        except ValueError:
            raise ConfigError(f"THERUNDOWN_AFFILIATE_IDS={raw_ids!r} must be comma-separated numbers (3 = Pinnacle)") \
                from None
        if not env.get("THERUNDOWN_API_KEY"):
            raise ConfigError("SHARP_PROVIDER=therundown needs THERUNDOWN_API_KEY")
        return TheRundownSource(env["THERUNDOWN_API_KEY"], affiliate_ids=affiliates,
                                use_websocket=(env.get("THERUNDOWN_WEBSOCKET") or "1").strip() not in {"0", "false"})
    if env.get("SHARP_PROVIDER_CONFIG"):
        return ProviderSharpSource(ProviderConfig.from_file(env["SHARP_PROVIDER_CONFIG"]))
    return None


def load_env_file(path: str, environ=None) -> list[str]:
    """
    Read KEY=VALUE lines (blank lines, '#' comments and trailing ' # comments' ignored; optional quotes and
    'export ' prefix allowed) into the environment. Values already set in the real environment win.
    Returns the keys it set. Values are never logged.
    """
    environ = os.environ if environ is None else environ
    loaded = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key = key.strip()
        value = value.strip()
        if value[:1] in {'"', "'"} and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if key and key not in environ:
            environ[key] = value
            loaded.append(key)
    return loaded


def build_live_supervisor(env: Optional[dict] = None, url: Optional[str] = None) -> Supervisor:
    """
    Build the production supervisor from environment variables.

    Paper (default): SHARP_PROVIDER_CONFIG, NOVIG_BEARER_TOKEN (bootstrap auth).
    Live (TRADING_MODE=live) additionally REQUIRES LIVE_TRADING_ACKNOWLEDGED=yes and the token.
    Live limits: canary $10 / $100 / maker off unless LIVE_SCALE_APPROVED_BY is set (see resolve_live_plan).
    Optional: NOVIG_WS_URL, NOVIG_API_BASE, NOVIG_EVENTS_URL, NOVIG_SUBSCRIBE_MESSAGES (JSON list),
              NOVIG_FILL_VOLUME_MODE (default cumulative, per Novig), TRADING_LOG_DIR,
              NOVIG_MULTI_LEVEL_MODE (staggered | single | off; default staggered),
              TAKER_CUTOFF_MINUTES (default 1), MAKER_CUTOFF_MINUTES (default 3), CUTOFF_OVERRIDES,
              RESEARCH_ENABLED (default 1), RESEARCH_DIR (default research).
    """
    env = os.environ if env is None else env
    live = env.get("TRADING_MODE", "paper").lower() == "live"
    if not env.get("SHARP_PROVIDER_CONFIG") and (env.get("SHARP_PROVIDER") or "").lower() != "therundown":
        log.warning("SUPERVISOR no SHARP_PROVIDER_CONFIG: MEASUREMENT mode — venue prices, cross-venue gaps and "
                    "liquidity are recorded; nothing that needs a fair value (directional takers, maker quotes) runs")
    token = env.get("NOVIG_BEARER_TOKEN")
    api_base = (env.get("NOVIG_API_BASE") or NOVIG_PROD_API_BASE).rstrip("/")
    events_url = env.get("NOVIG_EVENTS_URL") or api_base + NOVIG_EVENTS_PATH
    fill_mode = env.get("NOVIG_FILL_VOLUME_MODE", "cumulative")
    if fill_mode not in {"cumulative", "incremental"}:
        raise ConfigError(f"NOVIG_FILL_VOLUME_MODE={fill_mode!r} must be cumulative or incremental")
    subscribe = None
    if env.get("NOVIG_SUBSCRIBE_MESSAGES"):
        try:
            subscribe = json.loads(env["NOVIG_SUBSCRIBE_MESSAGES"])
        except json.JSONDecodeError as exc:
            raise ConfigError(f"NOVIG_SUBSCRIBE_MESSAGES is not valid JSON: {exc}") from None
        if not isinstance(subscribe, list) or not all(isinstance(m, dict) for m in subscribe):
            raise ConfigError("NOVIG_SUBSCRIBE_MESSAGES must be a JSON list of objects")
    if env.get("NOVIG_PRIVATE_WS_URL"):
        log.warning("NOVIG_PRIVATE_WS_URL is no longer used: executions arrive on the main Novig socket")

    feed_url = url or env.get("NOVIG_WS_URL") or (NOVIG_PROD_WS_URL if live else DEFAULT_NOVIG_WS_URL)
    live_kw: dict = {}
    exposure = None
    maker_enabled = env.get("MAKER_MODE", env.get("MAKER_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}
    if live:
        problems = []
        if env.get("LIVE_TRADING_ACKNOWLEDGED", "").lower() != LIVE_ACK_VALUE:
            problems.append("LIVE_TRADING_ACKNOWLEDGED=yes (explicit sign-off that real money will trade)")
        if not token:
            problems.append("NOVIG_BEARER_TOKEN")
        try:
            validate_ws_url(feed_url, "NOVIG_WS_URL")
        except ConfigError as exc:
            problems.append(str(exc))
        try:
            plan = resolve_live_plan(env)
        except ConfigError as exc:
            problems.append(str(exc))
            plan = None
        if problems:
            raise ConfigError("TRADING_MODE=live refused; missing/invalid: " + "; ".join(problems))
        if plan.scaled_up:
            log.critical("LIVE SCALE-UP AUTHORIZED by %r: max stake $%.2f, exposure limit $%.2f, maker %s "
                         "(canary is $%.0f / $%.0f / maker off)", plan.approved_by, plan.max_stake,
                         plan.exposure_limit, "ON" if plan.maker_enabled else "off", CANARY_MAX_STAKE_USD,
                         CANARY_EXPOSURE_LIMIT_USD)
        else:
            log.warning("LIVE CANARY limits in force: max stake $%.0f, exposure limit $%.0f, maker off. Scaling "
                        "up requires LIVE_SCALE_APPROVED_BY after reconciling live_ledger.jsonl.",
                        plan.max_stake, plan.exposure_limit)
        exposure = ExposureMonitor(plan.exposure_limit)
        maker_enabled = plan.maker_enabled
        ledger = Path(env.get("TRADING_LOG_DIR", "logs")) / "live_ledger.jsonl"
        positions = PositionsClient(
            api_base, token, path=env.get("NOVIG_POSITIONS_PATH", DEFAULT_POSITIONS_PATH),
            status_param=env.get("NOVIG_POSITIONS_STATUS_PARAM", "status"),
            open_status=env.get("NOVIG_OPEN_STATUS", "OPEN"), settled_status=env.get("NOVIG_SETTLED_STATUS", "SETTLED"))
        live_kw = dict(live=True, order_gateway=NovigOrderGateway(api_base, token), fill_volume_mode=fill_mode,
                       max_stake=plan.max_stake, ledger_path=ledger, live_plan=plan, positions_client=positions,
                       settlement_interval=float(env.get("SETTLEMENT_SWEEP_SECONDS", SETTLEMENT_SWEEP_SECONDS)))
    else:
        validate_ws_url(feed_url, "NOVIG_WS_URL")
    registry = MarketRegistry.from_json_file(env["NOVIG_MARKETS_FILE"]) if env.get("NOVIG_MARKETS_FILE") else None
    kw: dict = {}
    if env.get("KALSHI_ENABLED", "0") == "1":
        prod = env.get("KALSHI_ENV", "demo").lower() == "prod"
        key_id, key_path = env.get("KALSHI_KEY_ID"), env.get("KALSHI_PRIVATE_KEY_PATH")
        pk = load_private_key(key_path) if key_path else None
        rest_base = env.get("KALSHI_REST_BASE") or (KALSHI_PROD_REST_BASE if prod else DEFAULT_KALSHI_REST_BASE)
        kw = dict(kalshi_url=env.get("KALSHI_WS_URL") or (KALSHI_PROD_WS_URL if prod else DEFAULT_KALSHI_WS_URL),
                  kalshi_auth=(key_id, pk),
                  kalshi_rest=KalshiRestClient(rest_base, key_id, pk, series_from_env(env.get("KALSHI_SERIES"))))
        if (env.get("KALSHI_LIVE_TRADING") or "0").strip().lower() in {"1", "true", "yes", "on"}:
            if not live:
                raise ConfigError("KALSHI_LIVE_TRADING=1 only applies with TRADING_MODE=live (paper already "
                                  "simulates Kalshi trades)")
            if not (key_id and pk is not None):
                raise ConfigError("KALSHI_LIVE_TRADING=1 needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
            from kalshi_trading import KalshiOrderGateway, KalshiPositionsClient
            tif = (env.get("KALSHI_TIME_IN_FORCE") or "immediate_or_cancel").strip()
            kw.update(kalshi_gateway=KalshiOrderGateway(rest_base, key_id, pk,
                                                        time_in_force=None if tif.lower() == "none" else tif),
                      kalshi_positions_client=KalshiPositionsClient(rest_base, key_id, pk))
            log.critical("LIVE Kalshi execution ENABLED (%s): same canary stake/exposure limits as Novig, "
                         "immediate-or-cancel orders", "PROD" if prod else "DEMO")
    elif (env.get("KALSHI_LIVE_TRADING") or "0").strip().lower() in {"1", "true", "yes", "on"}:
        raise ConfigError("KALSHI_LIVE_TRADING=1 needs KALSHI_ENABLED=1 (the Kalshi feed carries our fills)")
    if (env.get("PROPHETX_ENABLED") or "0").strip().lower() in {"1", "true", "yes", "on"}:
        from prophetx_feed import PROD_BASE, ProphetXClient, ProphetXFeed
        if not (env.get("PROPHETX_ACCESS_KEY") and env.get("PROPHETX_SECRET_KEY")):
            raise ConfigError("PROPHETX_ENABLED=1 needs PROPHETX_ACCESS_KEY and PROPHETX_SECRET_KEY")
        kw["prophetx_feed"] = ProphetXFeed(ProphetXClient(env["PROPHETX_ACCESS_KEY"], env["PROPHETX_SECRET_KEY"],
                                                          env.get("PROPHETX_API_BASE") or PROD_BASE))
    combo_mode = (env.get("COMBO_QUOTER") or "off").strip().lower()
    if combo_mode not in {"off", "shadow", "demo", "live"}:
        raise ConfigError("COMBO_QUOTER must be off, shadow, demo or live")
    if combo_mode != "off":
        from combo_quoter import ComboConfig, ComboQuoter, KalshiComms
        from kalshi_trading import KalshiOrderGateway
        if combo_mode == "live" and not live:
            raise ConfigError("COMBO_QUOTER=live needs TRADING_MODE=live (use shadow or demo otherwise)")
        prod = env.get("KALSHI_ENV", "demo").lower() == "prod"
        if combo_mode == "demo" and prod:
            raise ConfigError("COMBO_QUOTER=demo needs KALSHI_ENV=demo (Kalshi's demo exchange)")
        key_id, key_path = env.get("KALSHI_KEY_ID"), env.get("KALSHI_PRIVATE_KEY_PATH")
        if not (key_id and key_path):
            raise ConfigError("COMBO_QUOTER needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH (RFQs require a signed "
                              "account)")
        base = env.get("KALSHI_REST_BASE") or (KALSHI_PROD_REST_BASE if prod else DEFAULT_KALSHI_REST_BASE)

        def f(name, default):
            try:
                return float(env.get(name) or default)
            except ValueError:
                raise ConfigError(f"{name} must be a number") from None
        cfg = ComboConfig(mode=combo_mode, max_legs=int(f("COMBO_MAX_LEGS", 4)), base_margin=f("COMBO_BASE_MARGIN", 0.04),
                          per_leg_margin=f("COMBO_PER_LEG_MARGIN", 0.02), min_roc=f("COMBO_MIN_ROC", 0.01),
                          max_loss_per_combo=f("COMBO_MAX_LOSS_PER_COMBO", 25),
                          max_leg_exposure=f("COMBO_MAX_LEG_EXPOSURE", 150),
                          max_total_liability=f("COMBO_MAX_TOTAL_LIABILITY", 1000))
        kw["combo_quoter"] = ComboQuoter(cfg, KalshiComms(KalshiOrderGateway(base, key_id, load_private_key(key_path))),
                                         leg_fair=lambda leg: None, research=kw.get("research"))
    mode = (env.get("NOVIG_MULTI_LEVEL_MODE") or "staggered").strip().lower()
    if mode not in MULTI_LEVEL_MODES:
        raise ConfigError(f"NOVIG_MULTI_LEVEL_MODE={mode!r} must be one of {', '.join(MULTI_LEVEL_MODES)}")
    pe = (env.get("PAPER_EXECUTION") or "simulated").strip().lower()
    if pe not in {"simulated", "instant"}:
        raise ConfigError("PAPER_EXECUTION must be simulated (default: realistic fills) or instant")
    try:
        kw.update(paper_execution=pe, sim_latency_ms=float(env.get("SIM_LATENCY_MS") or 500),
                  sim_depth_haircut=float(env.get("SIM_DEPTH_HAIRCUT") or 0.5))
    except ValueError:
        raise ConfigError("SIM_LATENCY_MS and SIM_DEPTH_HAIRCUT must be numbers") from None
    if not 0 < kw["sim_depth_haircut"] <= 1 or kw["sim_latency_ms"] < 0:
        raise ConfigError("SIM_DEPTH_HAIRCUT must be in (0, 1] and SIM_LATENCY_MS >= 0")
    kw.update(research=research_from_env(env), multi_level_mode=mode, **cutoffs_from_env(env),
              **risk_controls_from_env(env), **fair_value_from_env(env), **taker_filters_from_env(env))
    if kw.get("combo_quoter") is not None:
        kw["combo_quoter"].research = kw["research"]
    return Supervisor(feed_url=feed_url, registry=registry, token=token,
                      novig_rest=None if registry is not None else NovigRestClient(events_url, token),
                      sharp_fetch=sharp_source_from_env(env),
                      maker_enabled=maker_enabled, exposure=exposure, subscribe_messages=subscribe,
                      **kw, **live_kw)


def describe_state(sup: Supervisor) -> dict:
    """Structured summary of what the engine WILL do if started (no network access)."""
    cfg = getattr(sup.poller.fetch, "config", None)
    return {
        "mode": "LIVE" if sup.live else "paper",
        "novig_socket": sup.feed.url,
        "subscriptions": sup.feed.subscribe_messages,
        "fill_volume_mode": sup.fill_volume_mode if sup.live else "n/a (paper)",
        "bootstrap_url": sup.novig_rest.events_url if sup.novig_rest else "NOVIG_MARKETS_FILE",
        "bootstrap_refresh_s": sup.bootstrap_refresh,
        "orders_endpoint": f"{sup.order_gateway.api_base}/v1/orders" if sup.live else "paper (nothing sent)",
        "positions_endpoint": (f"{sup.positions_client.url}?{sup.positions_client.status_param}="
                               f"{sup.positions_client.open_status}|{sup.positions_client.settled_status}"
                               if sup.positions_client else "n/a (paper)"),
        "settlement_sweep": (f"every {sup.settlement_interval:.0f}s; startup sync before trading"
                             if sup.positions_client else "n/a (paper)"),
        "max_stake_usd": sup.max_stake,
        "exposure_limit_usd": sup.exposure.limit,
        "min_edge": MIN_EDGE,
        "maker": "on" if sup.maker is not None else "off",
        "maker_cancel_budget_ms": MAKER_CANCEL_BUDGET_MS,
        "taker_fill_timeout_s": sup.taker_fill_timeout,
        "reconnect_delay_s": sup.feed.reconnect_delay,
        "ping_interval_s": sup.feed.ping_interval,
        "ping_timeout_s": sup.feed.ping_timeout,
        "stale_stream_s": sup.feed.stale_after,
        "kalshi": "off" if sup.kalshi is None else (
            ("LIVE execution (IOC orders, fill channel, positions sync)" if sup.kalshi_live else "data-only")
            if sup.live else "paper execution"),
        "kalshi_socket": None if sup.kalshi is None else sup.kalshi.url,
        "prophetx": "off" if sup.prophetx is None else f"price feed (paper + research only) {sup.prophetx.url}",
        "combo": "off" if sup.combo is None else (
            f"{sup.combo.cfg.mode.upper()}: <= {sup.combo.cfg.max_legs} legs, margin {sup.combo.cfg.base_margin:.0%} + "
            f"{sup.combo.cfg.per_leg_margin:.0%}/leg, min return {sup.combo.cfg.min_roc:.0%}, caps "
            f"${sup.combo.cfg.max_loss_per_combo:,.0f}/combo ${sup.combo.cfg.max_leg_exposure:,.0f}/leg "
            f"${sup.combo.cfg.max_total_liability:,.0f} total"),
        "paper_execution": ("n/a (live)" if sup.live else
                            f"SIMULATED: {sup.sim_latency_ms:g}ms delay, {sup.sim_depth_haircut:.0%} of remaining depth"
                            if sup.sim else "instant (optimistic: full displayed size at the displayed price)"),
        "kalshi_rest": None if sup.kalshi_rest is None else sup.kalshi_rest.base_url,
        "dead_heat_venues": sorted(DEAD_HEAT_VENUES),
        "ledger": str(sup.ledger_path) if sup.ledger_path else None,
        "sharp_url": cfg.url if cfg else ("mock" if sup.sharp_enabled else "NONE: measurement mode"),
        "sharp_mode": cfg.mode if cfg else ("mock" if sup.sharp_enabled else "-"),
        "devig_method": sup.devig_method,
        "moved_first": ("sharp must move last" if sup.taker_require_sharp_moved_last else "off")
        + ("" if sup.taker_max_sharp_move_age_s is None else f", within {sup.taker_max_sharp_move_age_s:g}s"),
        "sharp_weights": sup.book.weights,
        "game_exposure_limit": sup.game_exposure_limit,
        "daily_loss_limit": sup.daily_loss_limit,
        "sharp_odds_format": cfg.odds_format if cfg else "-",
        "sharp_timestamp_format": cfg.timestamp_format if cfg else "-",
        "sharp_markets": sorted(set(cfg.market_map.values())) if cfg else [],
        "sharp_overrides": sorted(cfg.market_overrides) if cfg else [],
        "sharp_http_timeout_s": cfg.timeout_seconds if cfg else None,
        "sharp_poll_s": sup.poller.interval,
        "sharp_max_age_s": sup.book.max_age,
        "multi_level_mode": sup.multi_level_mode,
        "taker_cutoff_min": sup.taker_cutoff_s / 60,
        "maker_cutoff_min": sup.maker_cutoff_s / 60,
        "cutoff_overrides": {k: f"{t / 60:g}/{m / 60:g}" for k, (t, m) in sorted(sup.cutoff_overrides.items())},
        "require_start_time": sup.require_start_time,
        "research_dir": str(sup.research.dir) if sup.research is not None else None,
    }


def format_state_report(sup: Supervisor) -> str:
    """Plain-text configuration report for --check-config."""
    st = describe_state(sup)
    rows = [
        ("MODE", None),
        ("Trading mode", st["mode"]),
        ("Paper execution", st["paper_execution"]),
        ("NOVIG", None),
        ("Socket (prices + executions)", st["novig_socket"]),
        ("Subscriptions", " + ".join(json.dumps(m) for m in st["subscriptions"])),
        ("Fill volume mode", st["fill_volume_mode"]),
        ("Bootstrap (REST)", st["bootstrap_url"]),
        ("Bootstrap refresh", f"{st['bootstrap_refresh_s']:.0f}s"),
        ("Orders endpoint", st["orders_endpoint"]),
        ("Positions endpoint (sync/settle)", st["positions_endpoint"]),
        ("Settlement sweep", st["settlement_sweep"]),
        ("RISK", None),
        ("Max stake per position", f"${st['max_stake_usd']:,.2f}   (hard ceiling ${MAX_STAKE_USD:,.0f})"),
        ("Exposure limit", f"${st['exposure_limit_usd']:,.2f}   (hard ceiling ${GLOBAL_EXPOSURE_LIMIT_USD:,.0f})"),
        ("Minimum edge", f"> {st['min_edge']:.1%}"),
        ("Maker", st["maker"]),
        ("Dead-heat venues (NFL ML ties = 50c)", ", ".join(st["dead_heat_venues"])),
        ("Buying through the book", {
            "staggered": "staggered: one order per ask level at that level's price",
            "single": "single order limited at the worst level (needs price improvement)",
            "off": "best ask level only"}[st["multi_level_mode"]]),
        ("Unhedged $ per game (all markets)", f"${st['game_exposure_limit']:,.0f}"),
        ("Daily loss stop (settled, UTC day)", "off" if st["daily_loss_limit"] is None
         else f"-${st['daily_loss_limit']:,.0f} -> no new positions/quotes until 00:00 UTC"),
        ("Taker/hedge cutoff", f"no new orders {st['taker_cutoff_min']:g} min before scheduled start"),
        ("Maker cutoff", f"quotes pulled {st['maker_cutoff_min']:g} min before scheduled start"),
        ("Per-league cutoffs (taker/maker min)", ", ".join(f"{k} {v}" for k, v in st["cutoff_overrides"].items())
         or "none"),
        ("Live-game stop", "sharp feed in-play flag; game gone from Novig's pregame list (30s refresh near start)"),
        ("Games with no start time", "NEVER traded" if st["require_start_time"] else "traded (paper only)"),
        ("Research data", st["research_dir"] or "off"),
        ("Ledger", st["ledger"] or "(paper: none)"),
        ("TIMEOUTS", None),
        ("Reconnect delay", f"{st['reconnect_delay_s']}s"),
        ("Ping interval / timeout", f"{st['ping_interval_s']}s / {st['ping_timeout_s']}s"),
        ("Silent-stream watchdog", f"{st['stale_stream_s']}s"),
        ("Taker fill timeout", f"{st['taker_fill_timeout_s']}s"),
        ("Maker bulk-cancel budget", f"{st['maker_cancel_budget_ms']:.0f}ms"),
        ("SHARP DATA", None),
        ("Provider URL", st["sharp_url"]),
        ("Mode / odds / timestamps", f"{st['sharp_mode']} / {st['sharp_odds_format']} / {st['sharp_timestamp_format']}"),
        ("Markets", ", ".join(st["sharp_markets"]) or "-"),
        ("Margin removal (DEVIG_METHOD)", st["devig_method"]),
        ("Who-moved-first taker filter", st["moved_first"]),
        ("Book weights (consensus fair value)", ", ".join(f"{k}:{v:g}" for k, v in st["sharp_weights"].items())
         or "every book in the feed, equally"),
        ("Per-market overrides", ", ".join(st["sharp_overrides"]) or "none"),
        ("HTTP timeout / poll / freshness", f"{st['sharp_http_timeout_s']}s / {st['sharp_poll_s']}s / {st['sharp_max_age_s']}s"),
        ("COMBOS (parlay quoting)", None),
        ("Mode", st["combo"]),
        ("PROPHETX", None),
        ("Status", st["prophetx"]),
        ("KALSHI", None),
        ("Status", st["kalshi"]),
        ("Socket", st["kalshi_socket"] or "-"),
        ("REST", st["kalshi_rest"] or "-"),
    ]
    lines = ["=" * 78, "TRADING ENGINE CONFIGURATION REPORT (no network connections were opened)", "=" * 78]
    for label, value in rows:
        if value is None:
            lines.append(f"\n[{label}]")
        else:
            lines.append(f"  {label:<38} {value}")
    lines.append("\n[UNVERIFIED AGAINST A LIVE EXCHANGE]")
    for item in ("Novig order body keys + DELETE body key (novig_rest.ORDER_BODY_KEYS / BULK_CANCEL_KEY)",
                 "Novig 'orders' channel slip shape (confirmed only once the first slip arrives)",
                 "Novig REST host api.novig.us for /v1/orders; whether events embed markets; pagination",
                 "Novig positions endpoint path, status values and field names (settlement.py)",
                 "Novig event start-time field name (novig_rest.START_TIME_KEYS; no start = no live trading)",
                 "OpticOdds record paths in the sharp provider config"):
        lines.append(f"  - {item}")
    lines.append("=" * 78)
    return "\n".join(lines)


# ==========================================================================
# Demo / simulation data (Novig outcome model + one Kalshi game)
# ==========================================================================
DEMO_SHARP_LINES = [
    dict(league="NBA", home_team="NY Knicks", away_team="Boston", market_type="moneyline",
         side="NY Knicks", odds_for=-120, odds_against=100, source="mock-pinnacle"),
    dict(league="NBA", home_team="GS Warriors", away_team="LA Lakers", market_type="spread",
         side="GS Warriors", line=-4.5, odds_for=-110, odds_against=-110, source="mock-pinnacle"),
    dict(league="NFL", home_team="KC Chiefs", away_team="Buffalo", market_type="total",
         side="Over", line=47.5, odds_for=-105, odds_against=-115, source="mock-circa"),
    dict(league="NFL", home_team="NY Giants", away_team="NY Jets", market_type="moneyline",
         side="NY Giants", odds_for=150, odds_against=-170, source="mock-circa"),
]


# Demo games start 24h after the process starts, so the pregame cutoff never interferes with a demo run.
DEMO_START = time.time() + 24 * 3600


def _pair(market_id, event_id, league, mtype, home, away, a, b, line_a=None, line_b=None):
    base = dict(venue="novig", market_id=market_id, event_id=event_id, league=league, market_type=mtype,
                home_team=home, away_team=away, start_time=DEMO_START)
    return [dict(base, outcome_id=a[0], sibling_outcome_id=b[0], outcome=a[1], line=line_a),
            dict(base, outcome_id=b[0], sibling_outcome_id=a[0], outcome=b[1], line=line_b)]


DEMO_MARKETS = (
    _pair("MK-NYK-ML", "NBA-BOS-NYK", "NBA", "moneyline", "New York Knicks", "Boston Celtics",
          ("O-NYK", "New York Knicks"), ("O-BOS", "Boston Celtics"))
    + _pair("MK-GSW-SPR", "NBA-LAL-GSW", "NBA", "spread", "Golden State Warriors", "Los Angeles Lakers",
            ("O-GSW", "Golden State Warriors"), ("O-LAL", "Los Angeles Lakers"), -4.5, 4.5)
    + _pair("MK-KC-TOT", "NFL-BUF-KC", "NFL", "total", "Kansas City Chiefs", "Buffalo Bills",
            ("O-OVER", "Over"), ("O-UNDER", "Under"), 47.5, 47.5)
    + _pair("MK-NYG-ML", "NFL-NYJ-NYG", "NFL", "moneyline", "New York Giants", "New York Jets",
            ("O-NYG", "New York Giants"), ("O-NYJ", "New York Jets"))
)
DEMO_KALSHI_MARKETS = [
    dict(venue="kalshi", outcome_id="KXNBAGAME-BOSNYK-NYK", market_id="KXNBAGAME-BOSNYK",
         sibling_outcome_id="KXNBAGAME-BOSNYK-BOS", event_id="KXNBAGAME-BOSNYK", league="NBA",
         market_type="moneyline", home_team="New York Knicks", away_team="Boston Celtics", outcome="New York Knicks",
         start_time=DEMO_START),
    dict(venue="kalshi", outcome_id="KXNBAGAME-BOSNYK-BOS", market_id="KXNBAGAME-BOSNYK",
         sibling_outcome_id="KXNBAGAME-BOSNYK-NYK", event_id="KXNBAGAME-BOSNYK", league="NBA",
         market_type="moneyline", home_team="New York Knicks", away_team="Boston Celtics", outcome="Boston Celtics",
         start_time=DEMO_START),
]
DEMO_FAIR = {"O-NYK": 0.522, "O-BOS": 0.478, "O-GSW": 0.50, "O-LAL": 0.50, "O-OVER": 0.489, "O-UNDER": 0.511,
             "O-NYG": 0.387, "O-NYJ": 0.613}


async def _simulated_exchanges(novig, kalshi, duration: float, tick: float = 0.2) -> None:
    """Random-walk both venues around fair value; force one harsh Novig drop mid-run."""
    end = time.monotonic() + duration
    dropped, seq = False, 0
    await kalshi.broadcast({"type": "orderbook_snapshot", "sid": 1, "seq": 0,
                            "msg": {"market_ticker": "KXNBAGAME-BOSNYK-NYK",
                                    "yes_dollars_fp": [["0.50", "3000"]], "no_dollars_fp": [["0.46", "3000"]]}})
    while time.monotonic() < end:
        oid = random.choice(list(DEMO_FAIR))
        cents = round(min(97, max(3, DEMO_FAIR[oid] * 100 * random.uniform(0.92, 1.04))))
        await novig.broadcast({"event": "tape", "data": {"outcomeId": oid, "price_cents": cents, "side": "sell",
                                                         "volume": random.choice([500, 2000, 5000])}})
        if random.random() < 0.3:
            seq += 1
            await kalshi.broadcast({"type": "orderbook_delta", "sid": 1, "seq": seq,
                                    "msg": {"market_ticker": "KXNBAGAME-BOSNYK-NYK", "side": "no",
                                            "price_dollars": f"{random.choice([0.46, 0.47, 0.48, 0.49, 0.50]):.2f}",
                                            "delta_fp": "500"}})
        if not dropped and time.monotonic() > end - duration / 2:
            dropped = True
            logging.getLogger("trading.sim").warning("SIM forcing a harsh Novig connection drop")
            novig.drop_all_clients()
        await asyncio.sleep(tick)


async def run_simulation(duration: float = 20.0, research_dir: Optional[str] = None) -> Supervisor:
    from mock_novig_server import MockNovigServer  # the mock is a generic WebSocket server

    novig, kalshi = await MockNovigServer().start(), await MockNovigServer().start()
    sup = Supervisor(feed_url=novig.url, token="simulation", registry=MarketRegistry(DEMO_MARKETS),
                     kalshi_url=kalshi.url, kalshi_registry=MarketRegistry(DEMO_KALSHI_MARKETS),
                     sharp_fetch=MockSharpSource(DEMO_SHARP_LINES, jitter_cents=3, latency=0.02),
                     sharp_poll_interval=1.0, heartbeat_interval=2.0,
                     maker_kwargs=dict(refresh_interval=0.5, quiet_seconds=1.0),
                     research=ResearchRecorder(research_dir) if research_dir else None, depth_sample_s=2.0,
                     paper_execution="simulated")
    sim = asyncio.create_task(_simulated_exchanges(novig, kalshi, duration))
    try:
        await sup.run(duration=duration + 1.0)
    finally:
        sim.cancel()
        await asyncio.gather(sim, return_exceptions=True)
        await novig.stop()
        await kalshi.stop()
    return sup


def main() -> None:
    parser = argparse.ArgumentParser(description="Novig + Kalshi trading engine supervisor (PAPER TRADING ONLY)")
    parser.add_argument("--simulate", type=float, metavar="SECONDS", help="self-contained local simulation")
    parser.add_argument("--log-dir", default=os.environ.get("TRADING_LOG_DIR", "logs"))
    parser.add_argument("--url", default=None, help="override the Novig WebSocket URL")
    parser.add_argument("--env-file", metavar="PATH",
                        help="load settings from a KEY=VALUE file such as live.env (real environment wins)")
    parser.add_argument("--check-config", action="store_true",
                        help="build from the environment, print the system state, connect to nothing")
    args = parser.parse_args()

    if args.env_file:
        try:
            loaded = load_env_file(args.env_file)
        except OSError as exc:
            print(f"cannot read --env-file {args.env_file}: {exc}", file=sys.stderr)
            sys.exit(2)
        print(f"loaded {len(loaded)} setting(s) from {args.env_file}")
    path = setup_logging(args.log_dir, alert_url=os.environ.get("ALERT_WEBHOOK_URL") or None)
    log.info("SUPERVISOR logging to %s", path.resolve())
    try:
        if args.simulate:
            sup = asyncio.run(run_simulation(args.simulate, research_dir=str(Path(args.log_dir) / "research")))
            print(f"\nSimulation finished. stats={dict(sup.stats)}  open exposure=${sup.total_exposure():,.2f}")
            return
        try:
            sup = build_live_supervisor(url=args.url)
        except (ConfigError, ValueError, OSError) as exc:
            log.error("SUPERVISOR cannot start: %s", exc)
            sys.exit(2)
        if args.check_config:
            print(format_state_report(sup))
            return
        asyncio.run(sup.run())
    except KeyboardInterrupt:
        log.info("SUPERVISOR interrupted by user")


if __name__ == "__main__":
    main()
