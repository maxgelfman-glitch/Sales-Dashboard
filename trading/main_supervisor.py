"""
main_supervisor.py — The orchestrator. Runs everything and writes the audit log.

WHAT RUNS CONCURRENTLY
    1. NovigFeed         — WebSocket price stream (novig_feed.py)
    2. SharpPoller       — polls a sharp-book source (mock or HTTP) on an interval
    3. Heartbeat         — logs system health every few seconds
    The supervisor watches all three; if one ever dies unexpectedly it is
    logged and restarted, so one bug cannot silently stop the engine.

DATA FLOW FOR EVERY NOVIG PRICE UPDATE
    Novig update -> team names normalised (team_normalizer.py)
                 -> matching sharp line looked up (must be fresh)
                 -> evaluate_market_edge (execution.py)
                 -> if BET and we hold no position on that contract: PAPER order

!! PAPER TRADING ONLY !!
    This file contains NO code that sends real orders. `place_paper_order`
    only records and logs the order. Live execution is a separate, future
    milestone that should get its own review and kill-switch.

LOGGING
    logs/trading_engine.log, rotated at midnight (30 days kept), millisecond
    timestamps. Every line starts with a tag you can grep for:
      CONN / CONN_STATE   connection lifecycle       FEED_UPDATE  every contract update
      LINE_SHIFT          price/line moved           SHARP_POLL   sharp source polled
      BRIDGE              name translation           CALC         de-vig + edge maths
      DECISION            BET/PASS outcome           EV_TRIGGER   +EV opportunity
      ORDER               paper order                HEARTBEAT    system health
      SUPERVISOR          task crash / restart

USAGE
    python main_supervisor.py --simulate 30     # full local simulation, no network needed
    python main_supervisor.py                   # Novig staging feed + mock sharp data (paper only)
    SHARP_API_URL=https://... python main_supervisor.py   # sharp data from an HTTP JSON endpoint
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time
from collections import Counter
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

import aiohttp
from pydantic import BaseModel, ValidationError

from execution import NovigQuote, SharpQuote, evaluate_market_edge
from novig_feed import DEFAULT_NOVIG_WS_URL, NovigFeed, NovigMarketUpdate
from team_normalizer import normalize_outcome, normalize_team_name

LOG_FILE_NAME = "trading_engine.log"
LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)-20s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

log = logging.getLogger("trading.supervisor")
bridge_log = logging.getLogger("trading.bridge")
order_log = logging.getLogger("trading.orders")
sharp_log = logging.getLogger("trading.sharp")


# ==========================================================================
# Logging
# ==========================================================================
def setup_logging(log_dir: str | Path = "logs", level: int = logging.INFO, console: bool = True,
                  backup_days: int = 30) -> Path:
    """
    Configure the 'trading' logger tree: daily-rolling file + optional console.
    Safe to call more than once (old handlers are closed and replaced).
    """
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
# Sharp-book data
# ==========================================================================
class SharpLine(BaseModel):
    """One side of a sharp two-way market, as delivered by an external source (raw names)."""
    league: str
    home_team: str
    away_team: str
    market_type: Literal["spread", "moneyline", "total"]
    side: str                      # team name, or "over"/"under"
    odds_for: float                # American odds on `side`
    odds_against: float            # American odds on the other side
    line: Optional[float] = None   # spread from `side`'s perspective, or the total
    source: str = "sharp"

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

    def __init__(self, max_age_seconds: float = 30.0) -> None:
        self.max_age = max_age_seconds
        self._lines: dict[BookKey, tuple[SharpLine, float]] = {}
        self._seen_translations: set[tuple[str, str]] = set()

    def __len__(self) -> int:
        return len(self._lines)

    def _translate(self, raw: str, league: str, outcome: bool = False) -> Optional[str]:
        canonical = (normalize_outcome if outcome else normalize_team_name)(raw, league)
        if canonical is None:
            first_time = (raw, "") not in self._seen_translations
            self._seen_translations.add((raw, ""))
            bridge_log.log(logging.WARNING if first_time else logging.DEBUG,
                           "BRIDGE unmapped sharp name %r (%s) -> line skipped", raw, league)
        elif canonical != raw:
            level = logging.DEBUG if (raw, canonical) in self._seen_translations else logging.INFO
            self._seen_translations.add((raw, canonical))
            bridge_log.log(level, "BRIDGE %r -> %r (%s)", raw, canonical, league)
        return canonical

    def ingest(self, raw_lines: list[Any]) -> tuple[int, int]:
        """Validate, normalise and store lines. Returns (stored, rejected). Never raises."""
        stored = rejected = 0
        now = time.monotonic()
        for raw in raw_lines if isinstance(raw_lines, list) else []:
            try:
                line = raw if isinstance(raw, SharpLine) else SharpLine.model_validate(raw)
            except ValidationError as exc:
                sharp_log.warning("SHARP_POLL invalid line skipped (%d errors)", exc.error_count())
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
            for item in (canon, canon.mirrored()):
                self._lines[(league, home, away, item.market_type, item.side)] = (item, now)
            stored += 1
        return stored, rejected

    def lookup(self, league: str, home: str, away: str, market_type: str, side: str) -> Optional[SharpLine]:
        hit = self._lines.get((league, home, away, market_type, side))
        if hit is None:
            return None
        line, stored_at = hit
        if time.monotonic() - stored_at > self.max_age:
            return None  # stale sharp data is worse than none
        return line


SharpFetch = Callable[[], Awaitable[list[Any]]]


class MockSharpSource:
    """Stands in for a Pinnacle/Circa REST API. Returns fixed lines, optionally jittered."""

    def __init__(self, lines: list[dict], jitter_cents: int = 0, latency: float = 0.0) -> None:
        self.lines = lines
        self.jitter = jitter_cents
        self.latency = latency

    async def __call__(self) -> list[dict]:
        if self.latency:
            await asyncio.sleep(self.latency)  # simulate network round-trip
        if not self.jitter:
            return [dict(x) for x in self.lines]
        out = []
        for x in self.lines:
            x = dict(x)
            for k in ("odds_for", "odds_against"):
                x[k] = _jitter_american(x[k], self.jitter)
            out.append(x)
        return out


def _jitter_american(odds: float, cents: int) -> float:
    """Move American odds by a few cents without ever producing invalid values (-100 < x < +100)."""
    moved = odds + random.randint(-cents, cents)
    if -100 < moved < 100:
        moved = 100 if odds > 0 else -100
    return moved


class HttpSharpSource:
    """
    Polls an HTTP endpoint that returns a JSON list of SharpLine-shaped objects.
    One persistent aiohttp session is reused for low-latency polling.
    Adapting to a specific vendor (Pinnacle, OddsJam, etc.) = transform its JSON here.
    """

    def __init__(self, url: str, headers: Optional[dict] = None, timeout: float = 5.0) -> None:
        self.url = url
        self.headers = headers or {}
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def __call__(self) -> list[Any]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout, headers=self.headers)
        async with self._session.get(self.url) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        return data if isinstance(data, list) else data.get("lines", [])

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


class SharpPoller:
    """Calls `fetch` every `interval` seconds and loads the result into the SharpBook."""

    def __init__(self, fetch: SharpFetch, book: SharpBook, interval: float = 2.0) -> None:
        self.fetch = fetch
        self.book = book
        self.interval = interval
        self.polls = self.failures = 0

    async def run(self) -> None:
        while True:
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
            await asyncio.sleep(self.interval)


# ==========================================================================
# Paper orders
# ==========================================================================
class PaperOrder(BaseModel):
    order_id: int
    event_id: str
    league: str
    market_type: str
    side: str
    line: Optional[float]
    price: float
    stake_usd: float
    contracts: int
    edge: float
    capped: bool
    placed_at: float


# ==========================================================================
# The supervisor
# ==========================================================================
class Supervisor:
    def __init__(
        self,
        feed_url: str = DEFAULT_NOVIG_WS_URL,
        sharp_fetch: Optional[SharpFetch] = None,
        sharp_poll_interval: float = 2.0,
        sharp_max_age: float = 30.0,
        heartbeat_interval: float = 5.0,
        token: Optional[str] = None,
        feed_kwargs: Optional[dict] = None,
    ) -> None:
        self.book = SharpBook(max_age_seconds=sharp_max_age)
        self.feed = NovigFeed(url=feed_url, token=token, on_update=self.on_novig_update,
                              on_state_change=self.on_feed_state, **(feed_kwargs or {}))
        self.poller = SharpPoller(sharp_fetch or MockSharpSource(DEMO_SHARP_LINES), self.book, sharp_poll_interval)
        self.heartbeat_interval = heartbeat_interval
        self.positions: dict[tuple[str, str, str], PaperOrder] = {}
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
        log.info("FEED_UPDATE %s %s %s %s line=%s price=%.4f%s", update.league, update.event_id, update.market_type,
                 update.outcome, update.line, update.price,
                 "" if previous is None else f" (was {previous.price:.4f})")

        league = update.league
        home = normalize_team_name(update.home_team, league)
        away = normalize_team_name(update.away_team, league)
        side = normalize_outcome(update.outcome, league)
        if None in (home, away, side):
            self.stats["unmapped"] += 1
            bridge_log.warning("BRIDGE unmapped novig contract %s: home=%r away=%r outcome=%r -> skipped",
                               update.event_id, update.home_team, update.away_team, update.outcome)
            return
        bridge_log.debug("BRIDGE novig %r/%r/%r -> %r/%r/%r", update.home_team, update.away_team,
                         update.outcome, home, away, side)

        sharp = self.book.lookup(league, home, away, update.market_type, side)
        if sharp is None:
            self.stats["no_sharp"] += 1
            log.info("DECISION PASS %s %s %s: no fresh sharp line", update.event_id, update.market_type, side)
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

        key = (update.event_id, update.market_type, side)
        log.info("EV_TRIGGER %s %s %s price=%.4f fair=%.4f edge=%+.4f%% kelly=$%.2f stake=$%.2f capped=%s%s",
                 update.event_id, update.market_type, side, update.price, decision.fair_prob, decision.edge * 100,
                 decision.kelly_stake_usd, decision.stake_usd, decision.capped,
                 " SUSPICIOUS-EDGE" if decision.suspicious else "")
        if key in self.positions:
            self.stats["duplicate_blocked"] += 1
            log.info("EV_TRIGGER skipped: already hold paper position #%d on %s", self.positions[key].order_id, key)
            return
        self.place_paper_order(update, side, decision)

    def place_paper_order(self, update: NovigMarketUpdate, side: str, decision) -> PaperOrder:
        """Record a SIMULATED order. Sends nothing to any exchange."""
        order = PaperOrder(
            order_id=len(self.positions) + 1, event_id=update.event_id, league=update.league,
            market_type=update.market_type, side=side, line=update.line, price=update.price,
            stake_usd=decision.stake_usd, contracts=decision.contracts, edge=decision.edge,
            capped=decision.capped, placed_at=time.time(),
        )
        self.positions[(update.event_id, update.market_type, side)] = order
        self.stats["paper_orders"] += 1
        order_log.info("ORDER PAPER %s", order.model_dump_json())
        return order

    # ---------------- lifecycle ----------------
    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            log.info("HEARTBEAT uptime=%.1fs feed_connected=%s reconnects=%d contracts=%d sharp_lines=%d "
                     "sharp_polls=%d updates=%d paper_orders=%d exposure=$%.2f",
                     time.monotonic() - self.started_at, self.feed.connected.is_set(),
                     max(self.feed.connect_count - 1, 0), len(self.feed.latest), len(self.book), self.poller.polls,
                     self.stats["updates"], len(self.positions), self.total_exposure())

    def total_exposure(self) -> float:
        return round(sum(o.stake_usd for o in self.positions.values()), 2)

    async def run(self, duration: Optional[float] = None) -> None:
        """Run all components; restart any that crash. Stops after `duration` seconds (None = forever)."""
        log.info("SUPERVISOR starting (feed=%s, paper trading only)", self.feed.url)
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
# Demo / simulation data
#   Sharp lines use aggregator-style names ("NY Knicks"); the simulated Novig
#   tape uses full names ("New York Knicks") — exercising the data bridge.
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

DEMO_NOVIG_MARKETS = [
    # (event_id, league, market_type, home, away, outcome, line, fair_prob_guess)
    ("NBA-BOS-NYK", "NBA", "moneyline", "New York Knicks", "Boston Celtics", "New York Knicks", None, 0.522),
    ("NBA-BOS-NYK", "NBA", "moneyline", "New York Knicks", "Boston Celtics", "Boston Celtics", None, 0.478),
    ("NBA-LAL-GSW", "NBA", "spread", "Golden State Warriors", "Los Angeles Lakers", "Golden State Warriors", -4.5, 0.50),
    ("NFL-BUF-KC", "NFL", "total", "Kansas City Chiefs", "Buffalo Bills", "Over", 47.5, 0.489),
    ("NFL-NYJ-NYG", "NFL", "moneyline", "New York Giants", "New York Jets", "New York Giants", None, 0.387),
]


async def _simulated_exchange(server, duration: float, tick: float = 0.25) -> None:
    """Random-walk Novig prices around fair value and force one harsh drop mid-run."""
    end = time.monotonic() + duration
    dropped = False
    while time.monotonic() < end:
        ev, lg, mkt, home, away, outcome, line, fair = random.choice(DEMO_NOVIG_MARKETS)
        price = round(min(0.97, max(0.03, fair * random.uniform(0.93, 1.04))), 3)
        msg = dict(league=lg, market_type=mkt, event_id=ev, home_team=home, away_team=away, outcome=outcome, price=price)
        if line is not None:
            msg["line"] = line
        await server.broadcast(msg)
        if not dropped and time.monotonic() > end - duration / 2:
            dropped = True
            logging.getLogger("trading.sim").warning("SIM forcing a harsh connection drop")
            server.drop_all_clients()
        await asyncio.sleep(tick)


async def run_simulation(duration: float = 20.0, log_dir: str | Path = "logs") -> Supervisor:
    from mock_novig_server import MockNovigServer  # local import: only needed for simulation

    server = await MockNovigServer().start()
    sup = Supervisor(feed_url=server.url, token="simulation",
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
            sup = asyncio.run(run_simulation(args.simulate, args.log_dir))
            print(f"\nSimulation finished. stats={dict(sup.stats)}  exposure=${sup.total_exposure():,.2f}")
        else:
            sharp_url = os.environ.get("SHARP_API_URL")
            if sharp_url:
                fetch: SharpFetch = HttpSharpSource(sharp_url)
            else:
                log.warning("SUPERVISOR no SHARP_API_URL set: using MOCK sharp lines (demo only)")
                fetch = MockSharpSource(DEMO_SHARP_LINES)
            asyncio.run(Supervisor(feed_url=args.url, sharp_fetch=fetch).run())
    except KeyboardInterrupt:
        log.info("SUPERVISOR interrupted by user")


if __name__ == "__main__":
    main()
