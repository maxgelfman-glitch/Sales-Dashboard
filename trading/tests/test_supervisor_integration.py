"""
Milestone 4 self-test: main_supervisor.py

Part A — full runtime integration against the mock exchange (network, reconnect, log file).
Part B — decision rules driven directly (position lock, arbitrage, liquidity, kill-switch, settlement).

Reference maths (formulas proven in test_execution.py):
    NYK/BOS moneyline sharp -120/+100 -> fair NYK 0.52174, BOS 0.47826
    NYK ask 0.49  -> edge +6.48% -> 1/4 Kelly $1,558 -> capped $1,000 -> floor(1000/0.49) = 2040 contracts ($999.60)
    BOS ask 0.52  -> 0.49 + 0.52 = 1.01 -> NOT an arbitrage -> blocked by position lock
    BOS ask 0.478 -> 0.49 + 0.478 = 0.968 -> arb, 2040 x 0.478 = $975.12 hedge, locks 2040 x 0.032 = $65.28
    Over 47.5 sharp -105/-115 -> fair 0.48915; ask 0.44 -> +11.2% -> 2272 contracts ($999.68)
"""

import asyncio
import logging
import re
from logging.handlers import TimedRotatingFileHandler

import pytest

from execution import ExposureMonitor
from main_supervisor import (
    DEMO_MARKETS,
    DEMO_SHARP_LINES,
    ConfigError,
    Supervisor,
    load_live_inputs,
    setup_logging,
)
from mock_novig_server import MockNovigServer, make_market, make_tick
from novig_feed import MarketRegistry, NovigMarketUpdate
from sharp_feed import MockSharpSource

TS_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \| ")


async def wait_until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


@pytest.fixture
def log_path(tmp_path):
    path = setup_logging(tmp_path, console=False, level=logging.DEBUG)
    yield path
    for h in list(logging.getLogger("trading").handlers):
        h.close()
        logging.getLogger("trading").removeHandler(h)


def registry_with_extras():
    return MarketRegistry(DEMO_MARKETS + [
        make_market(market_id="M-GSW-35", event_id="NBA-LAL-GSW-ALT", market_type="spread", line=-3.5,
                    home_team="Golden State Warriors", away_team="Los Angeles Lakers", outcome="Golden State Warriors"),
        make_market(market_id="M-GOTHAM", event_id="X-1", home_team="Gotham Rogues", outcome="Gotham Rogues"),
    ])


# ======================================================================
# Part A: full runtime
# ======================================================================
async def test_full_runtime_simulation_is_fully_logged(log_path):
    loop = asyncio.get_running_loop()
    async_errors = []
    loop.set_exception_handler(lambda _loop, ctx: async_errors.append(ctx))

    server = await MockNovigServer().start()
    sharp_lines = DEMO_SHARP_LINES + [dict(league="NBA", home_team="Seattle SuperSonics", away_team="Boston",
                                            market_type="moneyline", side="Seattle SuperSonics",
                                            odds_for=-110, odds_against=-110)]
    sup = Supervisor(feed_url=server.url, token="t", registry=registry_with_extras(),
                     sharp_fetch=MockSharpSource(sharp_lines), sharp_poll_interval=0.1,
                     heartbeat_interval=0.2, feed_kwargs={"reconnect_delay": 0.3})
    run = asyncio.create_task(sup.run())
    try:
        await asyncio.wait_for(sup.feed.connected.wait(), 5)
        await wait_until(lambda: len(sup.book) > 0)

        await server.broadcast(make_tick("M-NYK", 49))          # BET: 2040 contracts, capped
        await server.broadcast(make_tick("M-NYK", 48))          # same side: position lock
        await server.broadcast(make_tick("M-BOS", 52))          # opposite, no arb: position lock
        await server.broadcast(make_tick("M-BOS", 47.8))        # opposite, arb: hedge
        await server.broadcast(make_tick("M-GSW-35", 45))       # -3.5 vs sharp -4.5: line mismatch PASS
        await server.broadcast(make_tick("M-GOTHAM", 30))       # registered but unmappable team
        await server.broadcast(make_tick("NOT-REGISTERED", 30)) # ignored by the feed
        await wait_until(lambda: sup.stats["updates"] >= 6)

        server.drop_all_clients()
        await wait_until(lambda: sup.feed.connect_count == 2, timeout=5)
        await server.wait_for_clients(1)
        await server.broadcast(make_tick("M-OVER", 44))         # BET after reconnect
        await wait_until(lambda: sup.stats["paper_orders"] == 3)
        await asyncio.sleep(0.5)
    finally:
        await sup.feed.stop()
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await server.stop()
        loop.set_exception_handler(None)

    # ---- behaviour
    kinds = [(o.kind, o.side, o.contracts, o.stake_usd) for o in sup.orders]
    assert kinds == [("DIRECTIONAL", "New York Knicks", 2040, 999.60),
                     ("ARB_HEDGE", "Boston Celtics", 2040, 975.12),
                     ("DIRECTIONAL", "over", 2272, 999.68)]
    assert sup.stats["lock_blocked"] == 2 and sup.stats["arbs"] == 1 and sup.stats["unmapped"] == 1
    assert sup.total_exposure() == 2974.40
    assert not async_errors, async_errors

    # ---- the log file
    for h in logging.getLogger("trading").handlers:
        h.flush()
    text = log_path.read_text()
    lines = text.splitlines()
    print(f"\n    log lines written: {len(lines)}")
    assert lines and all(TS_LINE.match(l) for l in lines), "every line needs a millisecond timestamp"
    required = {
        "connection state": "CONN_STATE CONNECTED",
        "connection drop": "CONN dropped",
        # priced: NYK, BOS, GSW-35, GOTHAM (4); books: those + NOT-REGISTERED (5)
        "stale state cleared": "cleared 4 stale price frames and 5 order books",
        "recovery measured": "recovery_seconds",
        "contract update": "FEED_UPDATE NBA M-NYK NBA-BOS-NYK moneyline New York Knicks",
        "line shift": "LINE_SHIFT",
        "unregistered market": "FEED_SKIP market NOT-REGISTERED not in registry",
        "sharp poll": "SHARP_POLL ok",
        "token translation": "BRIDGE 'NY Knicks' -> 'New York Knicks' (NBA)",
        "unmapped sharp name": "BRIDGE unmapped sharp name 'Seattle SuperSonics'",
        "unmapped novig market": "BRIDGE unmapped novig market M-GOTHAM",
        "de-vig calc": "CALC NBA-BOS-NYK/moneyline/New York Knicks fair=0.52174",
        "line mismatch": "line mismatch novig=-3.5 sharp=-4.5",
        "+EV trigger": "EV_TRIGGER NBA-BOS-NYK moneyline New York Knicks",
        "same-side lock": "POSITION_LOCK already hold #1",
        "opposite-side lock": "POSITION_LOCK blocked opposite side Boston Celtics",
        "arb": "guaranteed profit $65.28",
        "order": '"kind":"ARB_HEDGE"',
        "post-reconnect order": '"side":"over"',
        "heartbeat": "HEARTBEAT uptime=",
        "shutdown": "SUPERVISOR stopped",
    }
    missing = [name for name, needle in required.items() if needle not in text]
    assert not missing, f"missing from log: {missing}"
    warn_lines = [l for l in lines if "WARNING" in l and "BRIDGE unmapped sharp name" in l]
    assert len(warn_lines) == 1, "repeat unmapped warnings must be de-duplicated (later ones go to DEBUG)"
    assert "Traceback" not in text and "CALLBACK_ERROR" not in text and "SUPERVISOR task" not in text

    handlers = [h for h in logging.getLogger("trading").handlers if isinstance(h, TimedRotatingFileHandler)]
    assert handlers and handlers[0].when == "MIDNIGHT"


async def test_supervisor_restarts_a_crashed_task(log_path):
    server = await MockNovigServer().start()
    sup = Supervisor(feed_url=server.url, token="t", sharp_fetch=MockSharpSource(DEMO_SHARP_LINES),
                     sharp_poll_interval=0.1, heartbeat_interval=0.1)
    crashes = {"n": 0}
    original = sup._task_factories["heartbeat"]

    async def flaky_heartbeat():
        if crashes["n"] == 0:
            crashes["n"] += 1
            raise RuntimeError("simulated bug")
        await original()

    sup._task_factories["heartbeat"] = flaky_heartbeat
    await sup.run(duration=0.6)
    await server.stop()
    text = log_path.read_text()
    assert sup.stats["task_restarts"] == 1
    assert "SUPERVISOR task heartbeat exited unexpectedly" in text and "HEARTBEAT uptime=" in text


# ======================================================================
# Part B: decision rules (no network: updates are fed straight in)
# ======================================================================
MARKETS = {m["market_id"]: m for m in DEMO_MARKETS}


def upd(market_id, price, volume=5000, **overrides):
    return NovigMarketUpdate(**{**MARKETS[market_id], **overrides}, price=price, available_volume=volume)


def make_sup(limit=None, sharp=DEMO_SHARP_LINES):
    sup = Supervisor(feed_url="ws://127.0.0.1:1/unused", token="t", sharp_fetch=MockSharpSource(sharp),
                     exposure=ExposureMonitor(limit) if limit else None)
    sup.book.ingest(sharp)
    return sup


async def test_one_directional_position_per_market():
    sup = make_sup()
    await sup.on_novig_update(upd("M-NYK", 0.49), None)
    await sup.on_novig_update(upd("M-NYK", 0.45), None)    # even better price: still locked
    assert len(sup.orders) == 1 and sup.stats["lock_blocked"] == 1


async def test_opposite_side_blocked_even_when_plus_ev_if_not_arbitrage():
    sup = make_sup()
    await sup.on_novig_update(upd("M-NYK", 0.49), None)
    # Sharp flips: BOS now a -200 favourite (fair ~0.648). BOS ask 0.52 is +24% EV on its own...
    sup.book.ingest([{**DEMO_SHARP_LINES[0], "odds_for": 170, "odds_against": -200}])
    await sup.on_novig_update(upd("M-BOS", 0.52), None)
    # ...but 0.49 + 0.52 = 1.01 locks in a LOSS, so the lock holds.
    assert len(sup.orders) == 1 and sup.stats["arbs"] == 0


async def test_arbitrage_hedge_locks_profit_and_then_fully_locks_market():
    sup = make_sup()
    await sup.on_novig_update(upd("M-NYK", 0.49), None)
    await sup.on_novig_update(upd("M-BOS", 0.478), None)
    first, hedge = sup.orders
    assert hedge.kind == "ARB_HEDGE" and hedge.contracts == first.contracts == 2040
    # Whichever team wins, the payout is 2040 x $1; total cost is $999.60 + $975.12.
    assert round(2040 - (first.stake_usd + hedge.stake_usd), 2) == 65.28
    await sup.on_novig_update(upd("M-BOS", 0.40), None)    # even juicier: market is fully locked now
    await sup.on_novig_update(upd("M-NYK", 0.40), None)
    assert len(sup.orders) == 2


@pytest.mark.parametrize("price,expected_arb", [(0.50, True), (0.5001, False), (0.51, False)])
async def test_arbitrage_minimum_profit_boundary(price, expected_arb):
    # held 1000 NYK @ 0.49 ($490, liquidity-limited); need BOS <= 0.50 to lock >= 1c per contract.
    # (With a full 2040-contract position the $1,020 hedge would hit the $1,000 cap instead.)
    sup = make_sup()
    await sup.on_novig_update(upd("M-NYK", 0.49, volume=1000), None)
    await sup.on_novig_update(upd("M-BOS", price), None)
    assert (sup.stats["arbs"] == 1) is expected_arb


async def test_arbitrage_requires_liquidity_for_full_hedge():
    sup = make_sup()
    await sup.on_novig_update(upd("M-NYK", 0.49), None)
    await sup.on_novig_update(upd("M-BOS", 0.40, volume=2039), None)   # one contract short
    assert sup.stats["arbs"] == 0


async def test_arbitrage_rejected_when_hedge_breaches_1000_cap():
    sup = make_sup()
    await sup.on_novig_update(upd("M-NYK", 0.30), None)                 # 3333 contracts for $999.90
    await sup.on_novig_update(upd("M-BOS", 0.35), None)                 # 3333 x 0.35 = $1,166.55 > cap
    assert sup.stats["arbs"] == 0 and len(sup.orders) == 1


async def test_spread_arbitrage_needs_complementary_half_point_lines():
    sup = make_sup()
    await sup.on_novig_update(upd("M-GSW", 0.45), None)                 # GSW -4.5 +EV vs sharp 0.50
    await sup.on_novig_update(upd("M-LAL", 0.40, line=3.5), None)       # +3.5 is not the mirror of -4.5
    assert sup.stats["arbs"] == 0
    await sup.on_novig_update(upd("M-LAL", 0.40), None)                 # +4.5: valid mirror
    assert sup.stats["arbs"] == 1


async def test_whole_number_lines_never_arbitraged():
    sharp = [dict(league="NBA", home_team="GS Warriors", away_team="LA Lakers", market_type="spread",
                  side="GS Warriors", line=-4.0, odds_for=-110, odds_against=-110)]
    sup = make_sup(sharp=sharp)
    await sup.on_novig_update(upd("M-GSW", 0.45, line=-4.0), None)
    await sup.on_novig_update(upd("M-LAL", 0.40, line=4.0), None)       # a push on 4 would break the lock
    assert len(sup.orders) == 1 and sup.stats["arbs"] == 0


async def test_directional_size_limited_by_liquidity():
    sup = make_sup()
    await sup.on_novig_update(upd("M-NYK", 0.49, volume=100), None)
    [o] = sup.orders
    assert (o.contracts, o.stake_usd) == (100, 49.00)


async def test_kill_switch_halts_new_takers_until_settlement():
    sup = make_sup(limit=1500)                                          # small limit to test cheaply
    await sup.on_novig_update(upd("M-NYK", 0.49), None)                 # $999.60 open
    await sup.on_novig_update(upd("M-OVER", 0.44), None)                # +$999.68 would breach $1,500
    assert len(sup.orders) == 1 and sup.stats["kill_switch_blocked"] == 1
    assert sup.settle_event("NBA-BOS-NYK") == 999.60                    # game settles
    assert sup.total_exposure() == 0.0 and sup.markets == {}
    await sup.on_novig_update(upd("M-NYK", 0.47), None)                 # lock released + takers resume
    assert len(sup.orders) == 2 and sup.orders[-1].market_id == "M-NYK"


async def test_kill_switch_default_is_15000_and_blocks_16th_max_bet():
    sup = make_sup()
    assert sup.exposure.limit == 15_000
    for i in range(16):
        ev = f"NBA-G{i}"
        sup.registry.register(make_market(market_id=f"M{i}", event_id=ev))
        await sup.on_novig_update(NovigMarketUpdate(**make_market(market_id=f"M{i}", event_id=ev),
                                                    price=0.40, available_volume=10_000), None)
    # Same teams as the NYK sharp line; 0.40 vs fair 0.5217 -> 2500 contracts = $1,000.00 each
    assert len(sup.orders) == 15 and sup.total_exposure() == 15_000.0
    assert sup.exposure.taker_halted and sup.stats["kill_switch_blocked"] == 1
    assert sup.exposure.allows_cancellation()


async def test_arbitrage_hedge_also_respects_kill_switch():
    sup = make_sup(limit=1500)
    await sup.on_novig_update(upd("M-NYK", 0.49), None)
    await sup.on_novig_update(upd("M-BOS", 0.478), None)                # $975.12 would breach
    assert sup.stats["arbs"] == 0 and sup.stats["kill_switch_blocked"] == 1


def test_live_mode_refuses_to_run_without_real_inputs(tmp_path):
    with pytest.raises(ConfigError, match="NOVIG_MARKETS_FILE, SHARP_PROVIDER_CONFIG"):
        load_live_inputs({})
    empty = tmp_path / "m.json"
    empty.write_text("[]")
    with pytest.raises(ConfigError, match="no tracked"):
        load_live_inputs({"NOVIG_MARKETS_FILE": str(empty), "SHARP_PROVIDER_CONFIG": "x"})


def test_supervisor_requires_a_sharp_source():
    with pytest.raises(ValueError):
        Supervisor()
