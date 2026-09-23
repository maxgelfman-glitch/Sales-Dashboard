"""
main_supervisor.py — The orchestrator. Runs everything and writes the audit log.

WHAT RUNS CONCURRENTLY
    1. NovigFeed     — WebSocket order-book stream (novig_feed.py)
    2. SharpPoller   — polls the sharp-odds provider (sharp_feed.py)
    3. Heartbeat     — logs system health every few seconds
    The supervisor watches all three; if one dies unexpectedly it is logged
    and restarted, so one bug cannot silently stop the engine.

DATA FLOW FOR EVERY NOVIG BEST-ASK CHANGE
    Novig update -> team names normalised (team_normalizer.py)
                 -> POSITION LOCK: already in this game+market?
                       same side          -> ignored
                       opposite side      -> only a guaranteed-profit ARBITRAGE hedge may pass
                 -> fresh sharp line looked up (<= 30s old, sharp_feed.py)
                 -> evaluate_market_edge (execution.py)
                 -> size limited by visible liquidity at the best ask
                 -> GLOBAL EXPOSURE KILL-SWITCH (execution.ExposureMonitor, $15,000)
                 -> PAPER order

POSITION LOCK RULES
    * One directional position per (event, market type) — e.g. one NYK/BOS
      moneyline position, whichever side, whichever line.
    * The opposite side may be bought ONLY as a full hedge that locks in a
      guaranteed profit:  held_price + new_price <= 1 - ARB_MIN_PROFIT_PER_CONTRACT,
      lines exactly complementary (spread -x vs +x, same total, or moneyline),
      half-point lines only (a push cannot break the lock), enough resting
      volume to hedge every contract, and within the $1,000 per-position cap
      and the global kill-switch. NFL moneyline ties depend on Novig's rules — confirm.
    * After a hedge the market is fully locked until it settles.

!! PAPER TRADING ONLY !!
    This file contains NO code that sends real orders, and NO maker (resting)
    orders exist yet. The kill-switch only gates new taker orders; it never
    blocks cancellations (see ExposureMonitor.allows_cancellation).

LOGGING
    logs/trading_engine.log, rotated at midnight (30 days kept), millisecond timestamps.
    Tags: CONN / CONN_STATE, FEED_UPDATE, LINE_SHIFT, SHARP_POLL, SHARP_STALE, BRIDGE,
          CALC, DECISION, EV_TRIGGER, POSITION_LOCK, ARB_TRIGGER, KILL_SWITCH, ORDER,
          SETTLE, HEARTBEAT, SUPERVISOR

USAGE
    python main_supervisor.py --simulate 30      # self-contained local simulation
    # live (paper) mode needs real inputs — it refuses to run on mock data:
    export NOVIG_BEARER_TOKEN=...  SHARP_API_KEY=...
    export NOVIG_MARKETS_FILE=config/markets.json
    export SHARP_PROVIDER_CONFIG=config/sharp_provider.json
    python main_supervisor.py
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
    MAX_STAKE_USD,
    ExposureMonitor,
    NovigQuote,
    SharpQuote,
    evaluate_market_edge,
)
from novig_feed import DEFAULT_NOVIG_WS_URL, MarketRegistry, NovigFeed, NovigMarketUpdate
from sharp_feed import (
    MockSharpSource,
    ProviderConfig,
    ProviderSharpSource,
    SharpBook,
    SharpFetch,
    SharpPoller,
)
from team_normalizer import normalize_outcome, normalize_team_name

LOG_FILE_NAME = "trading_engine.log"
LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)-20s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

ARB_MIN_PROFIT_PER_CONTRACT = 0.01   # $0.01 per $1 contract (1c) minimum locked-in profit

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
    kind: Literal["DIRECTIONAL", "ARB_HEDGE"]
    market_id: str
    event_id: str
    league: str
    market_type: str
    side: str
    line: Optional[float]
    price: float
    stake_usd: float
    contracts: int
    edge: Optional[float]
    capped: bool
    placed_at: float

    @property
    def position_id(self) -> str:
        return f"order-{self.order_id}"


class MarketPosition(BaseModel):
    """Everything we hold in one (event, market type)."""
    legs: list[PaperOrder]
    hedged: bool = False

    @property
    def primary(self) -> PaperOrder:
        return self.legs[0]


MarketKey = tuple[str, str]  # (event_id, market_type)


def _floor_cents(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100.0


def _is_half_point(line: Optional[float]) -> bool:
    return line is not None and abs((abs(line) % 1) - 0.5) < 1e-9


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
    ) -> None:
        if sharp_fetch is None:
            raise ValueError("a sharp_fetch source is required (ProviderSharpSource live, MockSharpSource in tests)")
        self.book = SharpBook(max_age_seconds=sharp_max_age)
        self.registry = registry if registry is not None else MarketRegistry()
        self.feed = NovigFeed(url=feed_url, token=token, registry=self.registry, on_update=self.on_novig_update,
                              on_state_change=self.on_feed_state, **(feed_kwargs or {}))
        self.poller = SharpPoller(sharp_fetch, self.book, sharp_poll_interval)
        self.exposure = exposure or ExposureMonitor()
        self.heartbeat_interval = heartbeat_interval
        self.markets: dict[MarketKey, MarketPosition] = {}
        self.orders: list[PaperOrder] = []
        self.stats: Counter[str] = Counter()
        self.started_at = time.monotonic()
        self._task_factories: dict[str, Callable[[], Awaitable[None]]] = {
            "novig_feed": self.feed.run,
            "sharp_poller": self.poller.run,
            "heartbeat": self._heartbeat_loop,
        }

    # ---------------- event handlers ----------------
    async def on_feed_state(self, state: str, details: dict) -> None:
        self.stats[f"conn_{state.lower()}"] += 1
        log.info("CONN_STATE %s %s", state, json.dumps(details, default=str))

    async def on_novig_update(self, update: NovigMarketUpdate, previous: Optional[NovigMarketUpdate]) -> None:
        self.stats["updates"] += 1
        log.info("FEED_UPDATE %s %s %s %s %s line=%s ask=%.4f vol=%g%s", update.league, update.market_id,
                 update.event_id, update.market_type, update.outcome, update.line, update.price,
                 update.available_volume, "" if previous is None else f" (was {previous.price:.4f})")

        league = update.league
        home = normalize_team_name(update.home_team, league)
        away = normalize_team_name(update.away_team, league)
        side = normalize_outcome(update.outcome, league)
        if None in (home, away, side):
            self.stats["unmapped"] += 1
            bridge_log.warning("BRIDGE unmapped novig market %s: home=%r away=%r outcome=%r -> skipped",
                               update.market_id, update.home_team, update.away_team, update.outcome)
            return

        # ---- position lock
        key = (update.event_id, update.market_type)
        held = self.markets.get(key)
        if held is not None:
            if held.hedged:
                self.stats["lock_blocked"] += 1
                log.debug("POSITION_LOCK %s fully hedged; ignoring", key)
            elif side == held.primary.side:
                self.stats["lock_blocked"] += 1
                log.info("POSITION_LOCK already hold #%d on %s %s; ignoring same-side update",
                         held.primary.order_id, key, side)
            else:
                await self._try_arbitrage(update, side, held)
            return

        # ---- directional edge
        sharp = self.book.lookup(league, home, away, update.market_type, side)
        if sharp is None:
            self.stats["no_sharp"] += 1
            log.info("DECISION PASS %s %s %s: no fresh sharp line (<=%.0fs)", update.event_id,
                     update.market_type, side, self.book.max_age)
            return
        decision = await evaluate_market_edge(
            NovigQuote(price=update.price, line=update.line, label=f"{update.event_id}/{update.market_type}/{side}"),
            SharpQuote(odds_for=sharp.odds_for, odds_against=sharp.odds_against, line=sharp.line, source=sharp.source),
        )
        self.stats[f"decision_{decision.action.lower()}"] += 1
        log.info("DECISION %s %s %s %s edge=%s stake=$%.2f reason=%s", decision.action, update.event_id,
                 update.market_type, side, "n/a" if decision.edge is None else f"{decision.edge:+.4%}",
                 decision.stake_usd, decision.reason)
        if decision.action != "BET":
            return
        log.info("EV_TRIGGER %s %s %s ask=%.4f fair=%.4f edge=%+.4f%% kelly=$%.2f stake=$%.2f capped=%s%s",
                 update.event_id, update.market_type, side, update.price, decision.fair_prob, decision.edge * 100,
                 decision.kelly_stake_usd, decision.stake_usd, decision.capped,
                 " SUSPICIOUS-EDGE" if decision.suspicious else "")

        # ---- liquidity: never size beyond what is resting at the best ask
        contracts = min(decision.contracts, int(update.available_volume))
        stake = _floor_cents(contracts * update.price)
        if contracts <= 0 or stake <= 0:
            self.stats["no_liquidity"] += 1
            log.info("EV_TRIGGER skipped: no liquidity at best ask for %s", update.market_id)
            return
        if contracts < decision.contracts:
            log.info("EV_TRIGGER size limited by liquidity: %d of %d contracts ($%.2f)",
                     contracts, decision.contracts, stake)

        if not self._kill_switch_allows(stake, update.market_id):
            return
        order = self._place_paper_order("DIRECTIONAL", update, side, stake, contracts, decision.edge, decision.capped)
        self.markets[key] = MarketPosition(legs=[order])

    async def _try_arbitrage(self, update: NovigMarketUpdate, side: str, held: MarketPosition) -> None:
        """Opposite side of a held market: allowed only as a guaranteed-profit full hedge."""
        first = held.primary
        ok, reason = self._arb_conditions(first, update)
        if not ok:
            self.stats["lock_blocked"] += 1
            log.info("POSITION_LOCK blocked opposite side %s of %s (holding #%d %s @ %.4f): %s",
                     side, (update.event_id, update.market_type), first.order_id, first.side, first.price, reason)
            return
        contracts = first.contracts
        stake = round(contracts * update.price, 2)
        profit = round(contracts * (1.0 - first.price - update.price), 2)
        log.info("ARB_TRIGGER %s %s: held %s @ %.4f + buy %s @ %.4f = %.4f per $1 -> %d contracts, "
                 "hedge $%.2f, guaranteed profit $%.2f", update.event_id, update.market_type, first.side,
                 first.price, side, update.price, first.price + update.price, contracts, stake, profit)
        if not self._kill_switch_allows(stake, update.market_id):
            return
        self._place_paper_order("ARB_HEDGE", update, side, stake, contracts, None, False)
        held.legs.append(self.orders[-1])
        held.hedged = True
        self.stats["arbs"] += 1

    @staticmethod
    def _arb_conditions(first: PaperOrder, update: NovigMarketUpdate) -> tuple[bool, str]:
        if update.market_type == "spread":
            if first.line is None or update.line is None or not math.isclose(update.line, -first.line):
                return False, f"lines not complementary ({first.line} vs {update.line})"
        elif update.market_type == "total":
            if first.line is None or update.line is None or not math.isclose(update.line, first.line):
                return False, f"totals differ ({first.line} vs {update.line})"
        if update.market_type in {"spread", "total"} and not _is_half_point(update.line):
            return False, f"whole-number line {update.line} can push"
        margin = 1.0 - first.price - update.price
        if margin < ARB_MIN_PROFIT_PER_CONTRACT - 1e-9:
            return False, (f"no arbitrage: {first.price:.4f} + {update.price:.4f} = "
                           f"{first.price + update.price:.4f} (need <= {1 - ARB_MIN_PROFIT_PER_CONTRACT:.2f})")
        if update.available_volume < first.contracts:
            return False, f"only {update.available_volume:g} contracts at ask, need {first.contracts} to fully hedge"
        stake = first.contracts * update.price
        if stake > MAX_STAKE_USD + 1e-9:
            return False, f"hedge ${stake:,.2f} exceeds the ${MAX_STAKE_USD:,.0f} per-position cap"
        return True, "ok"

    def _kill_switch_allows(self, stake: float, market_id: str) -> bool:
        ok, reason = self.exposure.check_taker(stake)
        if not ok:
            self.stats["kill_switch_blocked"] += 1
            log.warning("KILL_SWITCH blocked taker order on %s ($%.2f): %s", market_id, stake, reason)
        return ok

    def _place_paper_order(self, kind: str, update: NovigMarketUpdate, side: str, stake: float,
                           contracts: int, edge: Optional[float], capped: bool) -> PaperOrder:
        """Record a SIMULATED order. Sends nothing to any exchange."""
        order = PaperOrder(
            order_id=len(self.orders) + 1, kind=kind, market_id=update.market_id, event_id=update.event_id,
            league=update.league, market_type=update.market_type, side=side, line=update.line,
            price=update.price, stake_usd=stake, contracts=contracts, edge=edge, capped=capped,
            placed_at=time.time(),
        )
        self.exposure.record_open(order.position_id, stake)
        self.orders.append(order)
        self.stats["paper_orders"] += 1
        order_log.info("ORDER PAPER %s", order.model_dump_json())
        return order

    def settle_event(self, event_id: str) -> float:
        """Mark every position on an event as settled: releases exposure and the position lock."""
        released = 0.0
        for key in [k for k in self.markets if k[0] == event_id]:
            for leg in self.markets.pop(key).legs:
                released += self.exposure.settle(leg.position_id)
        log.info("SETTLE %s released $%.2f; open exposure now $%.2f", event_id, released, self.exposure.open_exposure)
        return round(released, 2)

    # ---------------- lifecycle ----------------
    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            log.info("HEARTBEAT uptime=%.1fs feed_connected=%s reconnects=%d books=%d sharp_lines=%d "
                     "sharp_polls=%d updates=%d open_markets=%d exposure=$%.2f/$%.0f taker_halted=%s",
                     time.monotonic() - self.started_at, self.feed.connected.is_set(),
                     max(self.feed.connect_count - 1, 0), len(self.feed.books), len(self.book), self.poller.polls,
                     self.stats["updates"], len(self.markets), self.exposure.open_exposure, self.exposure.limit,
                     self.exposure.taker_halted)

    def total_exposure(self) -> float:
        return self.exposure.open_exposure

    async def run(self, duration: Optional[float] = None) -> None:
        """Run all components; restart any that crash. Stops after `duration` seconds (None = forever)."""
        log.info("SUPERVISOR starting (feed=%s, markets=%d, paper trading only)", self.feed.url, len(self.registry))
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
        await self.feed.stop()
        for name, task in tasks.items():
            if name != "novig_feed":
                task.cancel()
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks, results):
            if isinstance(result, Exception):
                log.error("SUPERVISOR task %s raised during shutdown: %r", name, result)
        closer = getattr(self.poller.fetch, "close", None)
        if closer is not None:
            await closer()
        log.info("SUPERVISOR stopped. stats=%s exposure=$%.2f", json.dumps(dict(self.stats)), self.total_exposure())


# ==========================================================================
# Live configuration (refuses to run on mock data)
# ==========================================================================
class ConfigError(RuntimeError):
    pass


def load_live_inputs(env: Optional[dict] = None) -> tuple[MarketRegistry, ProviderSharpSource]:
    env = os.environ if env is None else env
    missing = [k for k in ("NOVIG_MARKETS_FILE", "SHARP_PROVIDER_CONFIG") if not env.get(k)]
    if missing:
        raise ConfigError(f"live mode needs {', '.join(missing)} (use --simulate for a demo without them)")
    registry = MarketRegistry.from_json_file(env["NOVIG_MARKETS_FILE"])
    if len(registry) == 0:
        raise ConfigError(f"{env['NOVIG_MARKETS_FILE']} contains no tracked NFL/NBA markets")
    return registry, ProviderSharpSource(ProviderConfig.from_file(env["SHARP_PROVIDER_CONFIG"]))


# ==========================================================================
# Demo / simulation data
#   Sharp lines use aggregator-style names ("NY Knicks"); Novig market metadata
#   uses full names ("New York Knicks") — exercising the data bridge.
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

DEMO_MARKETS = [
    dict(market_id="M-NYK", event_id="NBA-BOS-NYK", league="NBA", market_type="moneyline",
         home_team="New York Knicks", away_team="Boston Celtics", outcome="New York Knicks"),
    dict(market_id="M-BOS", event_id="NBA-BOS-NYK", league="NBA", market_type="moneyline",
         home_team="New York Knicks", away_team="Boston Celtics", outcome="Boston Celtics"),
    dict(market_id="M-GSW", event_id="NBA-LAL-GSW", league="NBA", market_type="spread", line=-4.5,
         home_team="Golden State Warriors", away_team="Los Angeles Lakers", outcome="Golden State Warriors"),
    dict(market_id="M-LAL", event_id="NBA-LAL-GSW", league="NBA", market_type="spread", line=4.5,
         home_team="Golden State Warriors", away_team="Los Angeles Lakers", outcome="Los Angeles Lakers"),
    dict(market_id="M-OVER", event_id="NFL-BUF-KC", league="NFL", market_type="total", line=47.5,
         home_team="Kansas City Chiefs", away_team="Buffalo Bills", outcome="Over"),
    dict(market_id="M-UNDER", event_id="NFL-BUF-KC", league="NFL", market_type="total", line=47.5,
         home_team="Kansas City Chiefs", away_team="Buffalo Bills", outcome="Under"),
    dict(market_id="M-NYG", event_id="NFL-NYJ-NYG", league="NFL", market_type="moneyline",
         home_team="New York Giants", away_team="New York Jets", outcome="New York Giants"),
]
DEMO_FAIR = {"M-NYK": 0.522, "M-BOS": 0.478, "M-GSW": 0.50, "M-LAL": 0.50,
             "M-OVER": 0.489, "M-UNDER": 0.511, "M-NYG": 0.387}


async def _simulated_exchange(server, duration: float, tick: float = 0.2) -> None:
    """Random-walk best asks around fair value and force one harsh drop mid-run."""
    end = time.monotonic() + duration
    dropped = False
    while time.monotonic() < end:
        market_id = random.choice(list(DEMO_FAIR))
        cents = round(min(97, max(3, DEMO_FAIR[market_id] * 100 * random.uniform(0.92, 1.04))))
        await server.broadcast({"market_id": market_id, "price_cents": cents, "side": "sell",
                                "volume": random.choice([500, 2000, 5000])})
        if not dropped and time.monotonic() > end - duration / 2:
            dropped = True
            logging.getLogger("trading.sim").warning("SIM forcing a harsh connection drop")
            server.drop_all_clients()
        await asyncio.sleep(tick)


async def run_simulation(duration: float = 20.0) -> Supervisor:
    from mock_novig_server import MockNovigServer  # only needed for simulation

    server = await MockNovigServer().start()
    sup = Supervisor(feed_url=server.url, token="simulation", registry=MarketRegistry(DEMO_MARKETS),
                     sharp_fetch=MockSharpSource(DEMO_SHARP_LINES, jitter_cents=3, latency=0.02),
                     sharp_poll_interval=1.0, heartbeat_interval=2.0)
    sim = asyncio.create_task(_simulated_exchange(server, duration))
    try:
        await sup.run(duration=duration + 1.0)
    finally:
        sim.cancel()
        await asyncio.gather(sim, return_exceptions=True)
        await server.stop()
    return sup


def main() -> None:
    parser = argparse.ArgumentParser(description="Novig trading engine supervisor (PAPER TRADING ONLY)")
    parser.add_argument("--simulate", type=float, metavar="SECONDS",
                        help="run a self-contained local simulation for N seconds")
    parser.add_argument("--log-dir", default=os.environ.get("TRADING_LOG_DIR", "logs"))
    parser.add_argument("--url", default=os.environ.get("NOVIG_WS_URL", DEFAULT_NOVIG_WS_URL))
    args = parser.parse_args()

    path = setup_logging(args.log_dir)
    log.info("SUPERVISOR logging to %s", path.resolve())
    try:
        if args.simulate:
            sup = asyncio.run(run_simulation(args.simulate))
            print(f"\nSimulation finished. stats={dict(sup.stats)}  open exposure=${sup.total_exposure():,.2f}")
            return
        try:
            registry, fetch = load_live_inputs()
        except (ConfigError, ValueError, OSError) as exc:
            log.error("SUPERVISOR cannot start live mode: %s", exc)
            sys.exit(2)
        asyncio.run(Supervisor(feed_url=args.url, registry=registry, sharp_fetch=fetch).run())
    except KeyboardInterrupt:
        log.info("SUPERVISOR interrupted by user")


if __name__ == "__main__":
    main()
