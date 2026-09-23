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
    Paper only. TRADING_MODE=live is REFUSED until fill confirmations from
    Novig's private order channel are wired in: without them the engine cannot
    know its real positions, so the lock and the exposure limit would be blind.
    Kalshi orders are paper-only in every mode.
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
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional

from pydantic import BaseModel

from execution import (
    MAKER_LINE_MOVE_POINTS,
    MAKER_ML_FAIR_MOVE,
    MAX_STAKE_USD,
    MIN_EDGE,
    ExposureMonitor,
    MakerEngine,
    MakerTarget,
    NovigQuote,
    SharpQuote,
    american_to_decimal,
    devig_multiplicative,
    evaluate_kalshi_edge,
    evaluate_market_edge,
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
from novig_feed import DEFAULT_NOVIG_WS_URL, MarketRegistry, MarketUpdate, NovigFeed
from novig_rest import NovigRestClient, PaperOrderGateway
from sharp_feed import (
    MockSharpSource,
    ProviderConfig,
    ProviderSharpSource,
    SharpBook,
    SharpFetch,
    SharpLine,
    SharpPoller,
)
from team_normalizer import normalize_outcome, normalize_team_name

LOG_FILE_NAME = "trading_engine.log"
LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)-20s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

ARB_MIN_PROFIT_PER_CONTRACT = 0.01     # 1c per contract locked in, in the WORST scenario
DEAD_HEAT_VENUES = frozenset({"novig"})  # venues settling NFL moneyline ties at 50c
DEAD_HEAT_PAYOUT = 0.50
BOOTSTRAP_REFRESH_SECONDS = 300.0

log = logging.getLogger("trading.supervisor")
bridge_log = logging.getLogger("trading.bridge")
order_log = logging.getLogger("trading.orders")


# ==========================================================================
# Logging
# ==========================================================================
def setup_logging(log_dir: str | Path = "logs", level: int = logging.INFO, console: bool = True,
                  backup_days: int = 30) -> Path:
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
    return path


# ==========================================================================
# Positions
# ==========================================================================
class PaperOrder(BaseModel):
    order_id: int
    kind: Literal["DIRECTIONAL", "ARB_HEDGE", "MAKER_FILL"]
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

    @property
    def position_id(self) -> str:
        return f"order-{self.order_id}"


class MarketPosition(BaseModel):
    legs: list[PaperOrder]
    hedged: bool = False

    @property
    def primary(self) -> PaperOrder:
        return self.legs[0]


GameKey = tuple[str, str, str, str]   # (league, home, away, market_type) — venue independent


def _floor_cents(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100.0


def _is_half_point(line: Optional[float]) -> bool:
    return line is not None and abs((abs(line) % 1) - 0.5) < 1e-9


def order_cost(venue: str, contracts: int, price: float) -> tuple[float, float]:
    """(total cash out incl. fees, fee) for buying `contracts` at `price` on `venue`."""
    fee = kalshi_taker_fee(contracts, price * 100) if venue == "kalshi" else 0.0
    return round(contracts * price + fee, 2), fee


def arbitrage_scenarios(first: PaperOrder, hedge_venue: str, contracts: int, hedge_cost: float,
                        league: str, market_type: str) -> dict[str, float]:
    """Net P&L of holding both legs under every way the game can settle."""
    total_cost = first.stake_usd + hedge_cost
    out = {"first_side_wins": contracts * 1.0 - total_cost,
           "other_side_wins": contracts * 1.0 - total_cost}
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
    ) -> None:
        if sharp_fetch is None:
            raise ValueError("a sharp_fetch source is required (ProviderSharpSource live, MockSharpSource in tests)")
        self.book = SharpBook(max_age_seconds=sharp_max_age, on_move=self._on_sharp_move)
        self.registry = registry if registry is not None else MarketRegistry()
        self.feed = NovigFeed(url=feed_url, token=token, registry=self.registry, on_update=self.on_market_update,
                              on_state_change=self.on_feed_state, **(feed_kwargs or {}))
        self.kalshi_registry = kalshi_registry if kalshi_registry is not None else MarketRegistry()
        self.kalshi: Optional[KalshiFeed] = None
        if kalshi_url or kalshi_rest or len(self.kalshi_registry):
            self.kalshi = KalshiFeed(url=kalshi_url, key_id=kalshi_auth[0], private_key=kalshi_auth[1],
                                     registry=self.kalshi_registry, on_update=self.on_market_update,
                                     on_state_change=self.on_feed_state, **(kalshi_kwargs or {}))
        self.novig_rest, self.kalshi_rest = novig_rest, kalshi_rest
        self.poller = SharpPoller(sharp_fetch, self.book, sharp_poll_interval)
        self.exposure = exposure or ExposureMonitor()
        self.heartbeat_interval = heartbeat_interval
        self.bootstrap_refresh = bootstrap_refresh
        self.maker_gateway = maker_gateway or PaperOrderGateway()
        self.maker: Optional[MakerEngine] = (
            MakerEngine(self.maker_gateway, self.exposure, self._maker_targets, **(maker_kwargs or {}))
            if maker_enabled else None)
        self.positions: dict[GameKey, MarketPosition] = {}
        self.orders: list[PaperOrder] = []
        self.stats: Counter[str] = Counter()
        self.started_at = time.monotonic()
        self._background: set[asyncio.Task] = set()

        self._task_factories: dict[str, Callable[[], Awaitable[None]]] = {
            "novig_feed": self.feed.run, "sharp_poller": self.poller.run, "heartbeat": self._heartbeat_loop}
        if self.kalshi is not None:
            self._task_factories["kalshi_feed"] = self.kalshi.run
        if self.maker is not None:
            self._task_factories["maker"] = self.maker.run
        if novig_rest is not None or kalshi_rest is not None:
            self._task_factories["bootstrap"] = self._bootstrap_loop

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
            except Exception as exc:  # noqa: BLE001 — retried by the refresh loop
                log.error("BOOTSTRAP novig failed (%s: %s); keeping previous registry", type(exc).__name__, exc)
        if self.kalshi_rest is not None and self.kalshi is not None:
            try:
                before = set(self.kalshi.tickers())
                n = self.kalshi_registry.replace_all(await self.kalshi_rest.fetch_open_markets())
                log.info("BOOTSTRAP kalshi registry now holds %d outcomes", n)
                if set(self.kalshi.tickers()) != before and self.kalshi._ws is not None:
                    log.info("BOOTSTRAP kalshi ticker set changed: reconnecting to resubscribe")
                    await self.kalshi._ws.close()
            except Exception as exc:  # noqa: BLE001
                log.error("BOOTSTRAP kalshi failed (%s: %s); keeping previous registry", type(exc).__name__, exc)

    async def _bootstrap_loop(self) -> None:
        while True:
            await asyncio.sleep(self.bootstrap_refresh)
            await self.bootstrap()

    # ---------------- event handlers ----------------
    async def on_feed_state(self, state: str, details: dict) -> None:
        venue = details.get("venue", "novig")
        self.stats[f"conn_{venue}_{state.lower()}"] += 1
        log.info("CONN_STATE %s %s %s", venue, state, json.dumps(details, default=str))
        if state == "DISCONNECTED" and venue == "novig" and self.maker is not None and self.maker.quotes:
            await self.maker.cancel_all("novig websocket drop")

    def _on_sharp_move(self, key, old: SharpLine, new: SharpLine) -> None:
        """Called synchronously by SharpBook when a stored sharp line changes."""
        points_move = abs((new.line or 0.0) - (old.line or 0.0))
        fair_old, fair_new = self._fair(old), self._fair(new)
        fair_move = abs(fair_new - fair_old) if None not in (fair_old, fair_new) else 0.0
        log.info("SHARP_MOVE %s line %s -> %s (%.2f pts) fair %.4f -> %.4f", key, old.line, new.line, points_move,
                 fair_old or 0, fair_new or 0)
        if points_move > MAKER_LINE_MOVE_POINTS + 1e-9 or fair_move > MAKER_ML_FAIR_MOVE + 1e-9:
            if self.maker is not None and self.maker.quotes:
                self.stats["maker_line_kills"] += 1
                self._spawn(self.maker.cancel_all(
                    f"sharp move on {key}: {points_move:.2f} pts / {fair_move * 100:.2f}c fair"))

    @staticmethod
    def _fair(line: SharpLine) -> Optional[float]:
        try:
            (p, _), _ = devig_multiplicative([american_to_decimal(line.odds_for),
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
        return (league, home, away, update.market_type), side

    async def on_market_update(self, update: MarketUpdate, previous: Optional[MarketUpdate]) -> None:
        self.stats["updates"] += 1
        self.stats[f"updates_{update.venue}"] += 1
        log.info("FEED_UPDATE %s %s %s %s %s %s line=%s ask=%s vol=%g bid=%s", update.venue, update.league,
                 update.outcome_id, update.event_id, update.market_type, update.outcome, update.line,
                 "none" if update.price is None else f"{update.price:.4f}", update.available_volume,
                 "none" if update.best_bid is None else f"{update.best_bid:.4f}")

        canon = self._canonical(update)
        if canon is None:
            self.stats["unmapped"] += 1
            bridge_log.warning("BRIDGE unmapped %s outcome %s: home=%r away=%r outcome=%r -> skipped", update.venue,
                               update.outcome_id, update.home_team, update.away_team, update.outcome)
            return
        key, side = canon

        if update.venue == "novig" and self.maker is not None and self.maker.quotes:
            await self._simulate_maker_fills(update, key)

        if update.price is None:
            return
        held = self.positions.get(key)
        if held is not None:
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

        sharp = self.book.lookup(key[0], key[1], key[2], key[3], side)
        if sharp is None:
            self.stats["no_sharp"] += 1
            log.info("DECISION PASS %s %s %s: no fresh sharp line (<=%.0fs)", update.venue, key, side,
                     self.book.max_age)
            return
        sharp_q = SharpQuote(odds_for=sharp.odds_for, odds_against=sharp.odds_against, line=sharp.line,
                             source=sharp.source)
        label = f"{update.venue}/{update.event_id}/{update.market_type}/{side}"
        if update.venue == "kalshi":
            decision = await evaluate_kalshi_edge(update.price * 100, sharp_q, line=update.line, label=label)
        else:
            decision = await evaluate_market_edge(NovigQuote(price=update.price, line=update.line, label=label),
                                                  sharp_q)
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

        contracts = min(decision.contracts, int(update.available_volume))
        if contracts <= 0:
            self.stats["no_liquidity"] += 1
            log.info("EV_TRIGGER skipped: no liquidity at best ask for %s", update.outcome_id)
            return
        stake, fee = order_cost(update.venue, contracts, update.price)
        if contracts < decision.contracts:
            log.info("EV_TRIGGER size limited by liquidity: %d of %d contracts ($%.2f)", contracts,
                     decision.contracts, stake)
            if update.venue == "kalshi":   # the rounded fee changes with size: re-check the net edge
                net = (contracts * decision.fair_prob - stake) / stake
                if net <= MIN_EDGE + 1e-9:
                    log.info("EV_TRIGGER skipped: net edge %+.4f%% after fee at reduced size", net * 100)
                    return
        if not self._kill_switch_allows(stake, update.outcome_id):
            return
        self._record("DIRECTIONAL", update, side, contracts, stake, fee, decision.edge, decision.capped)
        self.positions[key] = MarketPosition(legs=[self.orders[-1]])
        await self._after_taker(key)

    async def _after_taker(self, key: GameKey) -> None:
        if self.maker is not None:
            self.maker.note_taker_activity()
            await self.maker.cancel_market(key, "position opened in this market")

    # ---------------- arbitrage ----------------
    async def _try_arbitrage(self, update: MarketUpdate, key: GameKey, side: str, held: MarketPosition) -> None:
        first = held.primary
        ok, reason, contracts, cost, fee, scenarios = self._arb_check(first, update, key)
        if not ok:
            self.stats["lock_blocked"] += 1
            log.info("POSITION_LOCK blocked opposite side %s on %s of %s (holding #%d %s %s @ %.4f): %s",
                     side, update.venue, key, first.order_id, first.venue, first.side, first.price, reason)
            return
        log.info("ARB_TRIGGER %s: held %s %s @ %.4f + buy %s %s @ %.4f -> %d contracts, hedge $%.2f "
                 "(fee $%.2f), scenarios %s, guaranteed profit $%.2f", key, first.venue, first.side, first.price,
                 update.venue, side, update.price, contracts, cost, fee, json.dumps(scenarios),
                 min(scenarios.values()))
        if not self._kill_switch_allows(cost, update.outcome_id):
            return
        self._record("ARB_HEDGE", update, side, contracts, cost, fee, None, False)
        held.legs.append(self.orders[-1])
        held.hedged = True
        self.stats["arbs"] += 1
        await self._after_taker(key)

    def _arb_check(self, first: PaperOrder, update: MarketUpdate, key: GameKey):
        league, _, _, mtype = key
        fail = lambda why: (False, why, 0, 0.0, 0.0, {})  # noqa: E731
        if mtype == "spread":
            if first.line is None or update.line is None or not math.isclose(update.line, -first.line):
                return fail(f"lines not complementary ({first.line} vs {update.line})")
        elif mtype == "total":
            if first.line is None or update.line is None or not math.isclose(update.line, first.line):
                return fail(f"totals differ ({first.line} vs {update.line})")
        if mtype in {"spread", "total"} and not _is_half_point(update.line):
            return fail(f"whole-number line {update.line} can push")
        contracts = first.contracts
        if update.available_volume < contracts:
            return fail(f"only {update.available_volume:g} contracts at ask, need {contracts} to fully hedge")
        cost, fee = order_cost(update.venue, contracts, update.price)
        if cost > MAX_STAKE_USD + 1e-9:
            return fail(f"hedge ${cost:,.2f} exceeds the ${MAX_STAKE_USD:,.0f} per-position cap")
        scenarios = arbitrage_scenarios(first, update.venue, contracts, cost, league, mtype)
        need = round(ARB_MIN_PROFIT_PER_CONTRACT * contracts, 2)
        worst = min(scenarios, key=scenarios.get)
        if scenarios[worst] < need - 1e-9:
            return fail(f"no arbitrage: worst case '{worst}' nets ${scenarios[worst]:,.2f} "
                        f"(need >= ${need:,.2f}); scenarios {json.dumps(scenarios)}")
        return True, "ok", contracts, cost, fee, scenarios

    # ---------------- orders / exposure ----------------
    def _kill_switch_allows(self, stake: float, outcome_id: str) -> bool:
        ok, reason = self.exposure.check_taker(stake)
        if not ok:
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
        return order

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
            if key in self.positions:
                continue
            sharp = self.book.lookup(key[0], key[1], key[2], key[3], side)
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
        await self.bootstrap()
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
        for closeable in (self.poller.fetch, self.novig_rest, self.kalshi_rest, self.maker_gateway):
            closer = getattr(closeable, "close", None)
            if closer is not None:
                await closer()
        log.info("SUPERVISOR stopped. stats=%s exposure=$%.2f", json.dumps(dict(self.stats)), self.total_exposure())


# ==========================================================================
# Live configuration
# ==========================================================================
class ConfigError(RuntimeError):
    pass


def build_live_supervisor(env: Optional[dict] = None, url: Optional[str] = None) -> Supervisor:
    env = os.environ if env is None else env
    if env.get("TRADING_MODE", "paper").lower() == "live":
        raise ConfigError("TRADING_MODE=live is refused: fill confirmations from Novig's private order channel "
                          "are not wired in yet, so real positions could not be tracked. Run in paper mode.")
    if not env.get("SHARP_PROVIDER_CONFIG"):
        raise ConfigError("live mode needs SHARP_PROVIDER_CONFIG (use --simulate for a demo without it)")
    if not (env.get("NOVIG_EVENTS_URL") or env.get("NOVIG_MARKETS_FILE")):
        raise ConfigError("live mode needs NOVIG_EVENTS_URL (REST bootstrap) or NOVIG_MARKETS_FILE")
    token = env.get("NOVIG_BEARER_TOKEN")
    registry = MarketRegistry.from_json_file(env["NOVIG_MARKETS_FILE"]) if env.get("NOVIG_MARKETS_FILE") else None
    novig_rest = NovigRestClient(env["NOVIG_EVENTS_URL"], token) if env.get("NOVIG_EVENTS_URL") else None
    kw: dict = {}
    if env.get("KALSHI_ENABLED", "0") == "1":
        prod = env.get("KALSHI_ENV", "demo").lower() == "prod"
        key_id, key_path = env.get("KALSHI_KEY_ID"), env.get("KALSHI_PRIVATE_KEY_PATH")
        pk = load_private_key(key_path) if key_path else None
        kw = dict(kalshi_url=KALSHI_PROD_WS_URL if prod else DEFAULT_KALSHI_WS_URL, kalshi_auth=(key_id, pk),
                  kalshi_rest=KalshiRestClient(KALSHI_PROD_REST_BASE if prod else DEFAULT_KALSHI_REST_BASE, key_id,
                                               pk, series_from_env(env.get("KALSHI_SERIES"))))
    return Supervisor(feed_url=url or env.get("NOVIG_WS_URL") or DEFAULT_NOVIG_WS_URL, registry=registry,
                      token=token, novig_rest=novig_rest,
                      sharp_fetch=ProviderSharpSource(ProviderConfig.from_file(env["SHARP_PROVIDER_CONFIG"])),
                      maker_enabled=env.get("MAKER_ENABLED", "1") == "1", **kw)


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


def _pair(market_id, event_id, league, mtype, home, away, a, b, line_a=None, line_b=None):
    base = dict(venue="novig", market_id=market_id, event_id=event_id, league=league, market_type=mtype,
                home_team=home, away_team=away)
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
         market_type="moneyline", home_team="New York Knicks", away_team="Boston Celtics", outcome="New York Knicks"),
    dict(venue="kalshi", outcome_id="KXNBAGAME-BOSNYK-BOS", market_id="KXNBAGAME-BOSNYK",
         sibling_outcome_id="KXNBAGAME-BOSNYK-NYK", event_id="KXNBAGAME-BOSNYK", league="NBA",
         market_type="moneyline", home_team="New York Knicks", away_team="Boston Celtics", outcome="Boston Celtics"),
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


async def run_simulation(duration: float = 20.0) -> Supervisor:
    from mock_novig_server import MockNovigServer  # the mock is a generic WebSocket server

    novig, kalshi = await MockNovigServer().start(), await MockNovigServer().start()
    sup = Supervisor(feed_url=novig.url, token="simulation", registry=MarketRegistry(DEMO_MARKETS),
                     kalshi_url=kalshi.url, kalshi_registry=MarketRegistry(DEMO_KALSHI_MARKETS),
                     sharp_fetch=MockSharpSource(DEMO_SHARP_LINES, jitter_cents=3, latency=0.02),
                     sharp_poll_interval=1.0, heartbeat_interval=2.0,
                     maker_kwargs=dict(refresh_interval=0.5, quiet_seconds=1.0))
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
    args = parser.parse_args()

    path = setup_logging(args.log_dir)
    log.info("SUPERVISOR logging to %s", path.resolve())
    try:
        if args.simulate:
            sup = asyncio.run(run_simulation(args.simulate))
            print(f"\nSimulation finished. stats={dict(sup.stats)}  open exposure=${sup.total_exposure():,.2f}")
            return
        try:
            sup = build_live_supervisor(url=args.url)
        except (ConfigError, ValueError, OSError) as exc:
            log.error("SUPERVISOR cannot start: %s", exc)
            sys.exit(2)
        asyncio.run(sup.run())
    except KeyboardInterrupt:
        log.info("SUPERVISOR interrupted by user")


if __name__ == "__main__":
    main()
