"""
Self-test: main_supervisor.py — the dual-venue pipeline.

Part A  full runtime: Novig + Kalshi mock exchanges, REST bootstrap, maker quoting,
        harsh Novig drop -> maker bulk cancel < 200ms -> reconnect, log file audit.
Part B  decision rules fed directly: cross-venue lock, arbitrage scenarios incl. NFL ties,
        Kalshi net-of-fee edges, liquidity, exposure kill-switch, maker fills and kill triggers.

Reference maths
    NYK/BOS sharp -120/+100 -> fair NYK 0.52174, BOS 0.47826
    Novig NYK @ 0.49 -> +6.48% -> capped: 2040 contracts, $999.60
    Kalshi BOS @ 0.45 hedge: 2040 x 0.45 = $918.00 + fee ceil(0.07*2040*0.45*0.55) = $35.35 -> $953.35
        both legs cost $1,952.95, pay $2,040 whoever wins -> +$87.05 locked
    NYG/NYJ sharp +150/-170 -> fair NYG 0.38847
"""

import asyncio
import logging
import re
from logging.handlers import TimedRotatingFileHandler

import pytest
from aiohttp import web

from execution import MAKER_CANCEL_BUDGET_MS, ExposureMonitor, evaluate_kalshi_edge
from main_supervisor import (
    DEMO_KALSHI_MARKETS,
    DEMO_MARKETS,
    DEMO_SHARP_LINES,
    Supervisor,
    arbitrage_scenarios,
    setup_logging,
)
from mock_novig_server import MockNovigServer, kalshi_snapshot, make_outcome, make_tick
from novig_feed import MarketRegistry, MarketUpdate
from novig_rest import NovigRestClient
from sharp_feed import MockSharpSource

TS_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \| ")
K_NYK, K_BOS = "KXNBAGAME-BOSNYK-NYK", "KXNBAGAME-BOSNYK-BOS"


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


async def serve_http(routes):
    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


NOVIG_EVENTS = {"events": [
    {"eventId": "NBA-BOS-NYK", "league": "NBA", "homeTeam": "New York Knicks", "awayTeam": "Boston Celtics",
     "markets": [{"marketId": "MK-NYK-ML", "type": "moneyline",
                  "outcomes": [{"outcomeId": "O-NYK", "name": "New York Knicks"},
                               {"outcomeId": "O-BOS", "name": "Boston Celtics"}]}]},
    {"eventId": "NFL-BUF-KC", "league": "NFL", "homeTeam": "Kansas City Chiefs", "awayTeam": "Buffalo Bills",
     "markets": [{"marketId": "MK-KC-TOT", "type": "total", "line": 47.5,
                  "outcomes": [{"outcomeId": "O-OVER", "name": "Over"}, {"outcomeId": "O-UNDER", "name": "Under"}]}]},
    {"eventId": "X-1", "league": "NBA", "homeTeam": "Gotham Rogues", "awayTeam": "Boston Celtics",
     "markets": [{"marketId": "MK-X", "type": "moneyline",
                  "outcomes": [{"outcomeId": "O-GOTHAM", "name": "Gotham Rogues"},
                               {"outcomeId": "O-BOS2", "name": "Boston Celtics"}]}]},
]}


# ======================================================================
# Part A: full runtime
# ======================================================================
async def test_full_dual_venue_runtime_is_fully_logged(log_path):
    loop = asyncio.get_running_loop()
    async_errors = []
    loop.set_exception_handler(lambda _loop, ctx: async_errors.append(ctx))

    async def events(_request):
        return web.json_response(NOVIG_EVENTS)

    http, base = await serve_http([("GET", "/v1/events", events)])
    novig, kalshi = await MockNovigServer().start(), await MockNovigServer().start()
    sharp_lines = DEMO_SHARP_LINES + [dict(league="NBA", home_team="Seattle SuperSonics", away_team="Boston",
                                            market_type="moneyline", side="Seattle SuperSonics",
                                            odds_for=-110, odds_against=-110)]
    sup = Supervisor(feed_url=novig.url, token="t", novig_rest=NovigRestClient(f"{base}/v1/events"),
                     kalshi_url=kalshi.url, kalshi_registry=MarketRegistry(DEMO_KALSHI_MARKETS),
                     sharp_fetch=MockSharpSource(sharp_lines), sharp_poll_interval=0.1, heartbeat_interval=0.2,
                     feed_kwargs={"reconnect_delay": 0.3}, kalshi_kwargs={"reconnect_delay": 0.3},
                     maker_kwargs=dict(refresh_interval=0.05, quiet_seconds=0.3))
    run = asyncio.create_task(sup.run())
    try:
        await asyncio.wait_for(sup.feed.connected.wait(), 5)
        await asyncio.wait_for(sup.kalshi.connected.wait(), 5)
        assert len(sup.registry) == 6                                     # bootstrapped over REST
        await wait_until(lambda: len(sup.book) > 0)
        await wait_until(lambda: len(sup.maker.quotes) >= 2)              # maker quoting while quiet

        # Two independent sockets have no mutual ordering, so each step waits for the last.
        await novig.broadcast(make_tick("O-NYK", 49))                     # taker BET 2040 NYK
        await wait_until(lambda: len(sup.orders) == 1)
        await novig.broadcast(make_tick("O-NYK", 48))                     # same side: lock
        await kalshi.broadcast(kalshi_snapshot(K_NYK, yes=[(0.40, 500)], no=[(0.52, 5000)], seq=1))  # NYK 48c on Kalshi: lock
        await novig.broadcast(make_tick("O-BOS", 52))                     # opposite, no arb: lock
        await wait_until(lambda: sup.stats["lock_blocked"] == 3)
        await kalshi.broadcast(kalshi_snapshot(K_BOS, yes=[(0.40, 500)], no=[(0.55, 5000)], seq=2))  # BOS 45c: ARB
        await novig.broadcast(make_tick("O-GOTHAM", 30))                  # unmappable team
        await novig.broadcast(make_tick("NOT-REGISTERED", 30))
        await wait_until(lambda: sup.stats["arbs"] == 1 and sup.stats["unmapped"] >= 1)

        await wait_until(lambda: sup.maker.quotes)                        # quotes on the KC total
        novig.drop_all_clients()                                          # harsh drop -> maker kill
        await wait_until(lambda: sup.maker.kill_count >= 1)
        await wait_until(lambda: sup.feed.connect_count == 2, timeout=5)
        await novig.wait_for_clients(1)
        await novig.broadcast(make_tick("O-OVER", 44))                    # BET after reconnect
        await wait_until(lambda: any(o.side == "over" for o in sup.orders))
        await asyncio.sleep(0.5)
    finally:
        await sup.feed.stop()
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        for s in (novig, kalshi):
            await s.stop()
        await http.cleanup()
        loop.set_exception_handler(None)

    kinds = [(o.kind, o.venue, o.side, o.contracts, o.stake_usd, o.fee_usd) for o in sup.orders]
    assert kinds[:2] == [("DIRECTIONAL", "novig", "New York Knicks", 2040, 999.60, 0.0),
                         ("ARB_HEDGE", "kalshi", "Boston Celtics", 2040, 953.35, 35.35)]
    assert kinds[2][:3] == ("DIRECTIONAL", "novig", "over")
    assert sup.maker.last_cancel_ms is not None and sup.maker.last_cancel_ms < MAKER_CANCEL_BUDGET_MS
    assert sup.maker.quotes == {}                                         # pulled at shutdown
    assert not async_errors, async_errors

    for h in logging.getLogger("trading").handlers:
        h.flush()
    text = log_path.read_text()
    lines = text.splitlines()
    print(f"\n    log lines: {len(lines)}; maker bulk cancel took {sup.maker.last_cancel_ms:.2f}ms")
    assert all(TS_LINE.match(l) for l in lines)
    required = {
        "bootstrap": "BOOTSTRAP novig registry now holds 6 outcomes",
        "novig subscription": 'CONN novig sent subscription {"event": "subscribe", "channel": "tape"}',
        "kalshi subscription": "CONN kalshi subscribed orderbook_delta for 2 tickers",
        "maker post": "MAKER_POST LIMIT",
        "maker cleared on position": "MAKER_CANCEL",
        "maker kill on drop": "novig websocket drop",
        "cross-venue lock": "POSITION_LOCK already hold #1",
        "opposite lock": "POSITION_LOCK blocked opposite side Boston Celtics on novig",
        "cross-venue arb": "guaranteed profit $87.05",
        "kalshi fee in order": '"fee_usd":35.35',
        "unmapped": "BRIDGE unmapped novig outcome O-GOTHAM",
        "unregistered": "FEED_SKIP outcome NOT-REGISTERED",
        "drop": "CONN novig dropped",
        "recovery": "recovery_seconds",
        "heartbeat": "HEARTBEAT uptime=",
        "shutdown": "SUPERVISOR stopped",
    }
    missing = [k for k, v in required.items() if v not in text]
    assert not missing, f"missing from log: {missing}"
    assert "Traceback" not in text and "CALLBACK_ERROR" not in text and "SUPERVISOR task" not in text
    handlers = [h for h in logging.getLogger("trading").handlers if isinstance(h, TimedRotatingFileHandler)]
    assert handlers and handlers[0].when == "MIDNIGHT"


async def test_supervisor_restarts_a_crashed_task(log_path):
    server = await MockNovigServer().start()
    sup = Supervisor(feed_url=server.url, token="t", sharp_fetch=MockSharpSource(DEMO_SHARP_LINES),
                     sharp_poll_interval=0.1, heartbeat_interval=0.1, maker_enabled=False)
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
    assert sup.stats["task_restarts"] == 1
    assert "SUPERVISOR task heartbeat exited unexpectedly" in log_path.read_text()


# ======================================================================
# Part B: decision rules
# ======================================================================
NFL_ML_SHARP = DEMO_SHARP_LINES[3]           # NYG +150 / NYJ -170
INFO = {m["outcome_id"]: m for m in DEMO_MARKETS + DEMO_KALSHI_MARKETS}
K_NYG, K_NYJ = "KXNFLGAME-NYJNYG-NYG", "KXNFLGAME-NYJNYG-NYJ"
INFO[K_NYG] = make_outcome(venue="kalshi", outcome_id=K_NYG, sibling=K_NYJ, market_id="KXNFLGAME-NYJNYG",
                           event_id="KXNFLGAME-NYJNYG", league="NFL", home_team="New York Giants",
                           away_team="New York Jets", outcome="New York Giants")
INFO[K_NYJ] = {**INFO[K_NYG], "outcome_id": K_NYJ, "sibling_outcome_id": K_NYG, "outcome": "New York Jets"}


def upd(outcome_id, price, volume=5000, bid=None, **overrides):
    return MarketUpdate(**{**INFO[outcome_id], **overrides}, price=price, available_volume=volume,
                        best_bid=bid, bid_volume=0 if bid is None else 5000)


def make_sup(limit=None, sharp=DEMO_SHARP_LINES, maker=False):
    sup = Supervisor(feed_url="ws://127.0.0.1:1/unused", token="t", sharp_fetch=MockSharpSource(sharp),
                     registry=MarketRegistry(DEMO_MARKETS), kalshi_registry=MarketRegistry(DEMO_KALSHI_MARKETS),
                     exposure=ExposureMonitor(limit) if limit else None, maker_enabled=maker,
                     maker_kwargs=dict(quiet_seconds=0))
    sup.book.ingest(sharp)
    return sup


# ---------------- position lock across venues
async def test_one_position_per_game_across_venues():
    sup = make_sup()
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    await sup.on_market_update(upd(K_NYK, 0.40), None)          # same game + side on Kalshi: locked
    await sup.on_market_update(upd("O-NYK", 0.45), None)
    assert len(sup.orders) == 1 and sup.stats["lock_blocked"] == 2


async def test_opposite_side_blocked_when_not_arbitrage():
    sup = make_sup()
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    sup.book.ingest([{**DEMO_SHARP_LINES[0], "odds_for": 170, "odds_against": -200}])   # BOS now favourite
    await sup.on_market_update(upd("O-BOS", 0.52), None)        # +EV alone, but 0.49 + 0.52 = 1.01
    assert len(sup.orders) == 1 and sup.stats["arbs"] == 0


@pytest.mark.parametrize("price,expected_arb", [(0.50, True), (0.5001, False), (0.51, False)])
async def test_arbitrage_99_cent_boundary(price, expected_arb):
    sup = make_sup()
    await sup.on_market_update(upd("O-NYK", 0.49, volume=1000), None)   # 1000 contracts ($490)
    await sup.on_market_update(upd("O-BOS", price), None)
    assert (sup.stats["arbs"] == 1) is expected_arb


# ---------------- NFL ties (dead heat at 50c on Novig)
async def test_nfl_moneyline_arb_allowed_on_novig_because_tie_pays_50c_per_leg():
    sup = make_sup()
    await sup.on_market_update(upd("O-NYG", 0.36, volume=1000), None)   # NYG +7.9% -> 1000 contracts $360
    await sup.on_market_update(upd("O-NYJ", 0.63), None)                # 0.36 + 0.63 = 0.99
    assert sup.stats["arbs"] == 1
    first, hedge = sup.orders
    s = arbitrage_scenarios(first, "novig", 1000, hedge.stake_usd, "NFL", "moneyline")
    assert s == {"first_side_wins": 10.0, "other_side_wins": 10.0, "tie": 10.0}   # 1000 x (0.5 + 0.5) - $990


async def test_nfl_moneyline_cross_venue_arb_allowed_now_both_venues_dead_heat():
    """
    Kalshi NFL ties settle each team at 50c (brief; matches public Kalshi FAQs), like Novig.
    Novig NYG 1000 @ 0.36 ($360) + Kalshi NYJ 1000 @ 0.55 ($550 + fee ceil(0.07*1000*0.55*0.45)=$17.33)
    total $927.33. Payout $1,000 if either team wins, and 1000 x (0.50 + 0.50) = $1,000 on a tie.
    """
    sup = make_sup()
    sup.kalshi_registry.register(INFO[K_NYG])
    sup.kalshi_registry.register(INFO[K_NYJ])
    await sup.on_market_update(upd("O-NYG", 0.36, volume=1000), None)
    await sup.on_market_update(upd(K_NYJ, 0.55), None)
    assert sup.stats["arbs"] == 1
    first, hedge = sup.orders
    assert (hedge.venue, hedge.fee_usd, hedge.stake_usd) == ("kalshi", 17.33, 567.33)
    s = arbitrage_scenarios(first, "kalshi", 1000, hedge.stake_usd, "NFL", "moneyline")
    assert s == {"first_side_wins": 72.67, "other_side_wins": 72.67, "tie": 72.67}


def test_tie_scenario_still_refuses_venues_without_dead_heat(monkeypatch):
    import main_supervisor
    monkeypatch.setattr(main_supervisor, "DEAD_HEAT_VENUES", frozenset({"novig"}))

    class Leg:
        venue, stake_usd = "novig", 360.0
    s = arbitrage_scenarios(Leg, "other_exchange", 1000, 567.33, "NFL", "moneyline")
    assert s["tie"] == round(500 - 927.33, 2) < 0          # only the Novig leg pays on a tie


def test_scenarios_for_nba_have_no_tie():
    class Leg:
        venue, stake_usd = "novig", 490.0
    assert set(arbitrage_scenarios(Leg, "kalshi", 1000, 400.0, "NBA", "moneyline")) == {
        "first_side_wins", "other_side_wins"}


async def test_cross_venue_nba_arb_nets_kalshi_fee():
    sup = make_sup()
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    await sup.on_market_update(upd(K_BOS, 0.45), None)
    first, hedge = sup.orders
    assert (hedge.venue, hedge.contracts, hedge.fee_usd, hedge.stake_usd) == ("kalshi", 2040, 35.35, 953.35)
    assert round(2040 - first.stake_usd - hedge.stake_usd, 2) == 87.05


# ---------------- Kalshi taker fee
async def test_same_price_bets_on_novig_but_fee_blocks_kalshi():
    sup = make_sup()
    await sup.on_market_update(upd(K_NYK, 0.495), None)         # fee drags +5.4% down to +1.8%
    assert sup.orders == [] and sup.stats["decision_pass"] == 1
    await sup.on_market_update(upd("O-NYK", 0.495), None)       # Novig, no fee: BET
    assert sup.orders[0].venue == "novig"


async def test_kalshi_bet_sized_and_costed_net_of_fee():
    sup = make_sup()
    await sup.on_market_update(upd(K_NYK, 0.45), None)
    [o] = sup.orders
    expected = await evaluate_kalshi_edge(45, {"odds_for": -120, "odds_against": 100})
    assert (o.contracts, o.fee_usd, o.stake_usd) == (expected.contracts, expected.fee_usd, expected.stake_usd)
    assert o.stake_usd <= 1000


async def test_kalshi_liquidity_reduction_rechecks_net_edge():
    # At 49c full size nets +2.8% (fee 1.749c/contract) -> BET ...
    full = await evaluate_kalshi_edge(49, {"odds_for": -120, "odds_against": 100})
    assert full.action == "BET"
    # ... but only 1 contract is offered: the fee rounds UP to 2c -> 0.52174 / 0.51 - 1 = +2.3% -> skip
    sup = make_sup()
    await sup.on_market_update(upd(K_NYK, 0.49, volume=1), None)
    assert sup.orders == []


# ---------------- liquidity / exposure / settlement
async def test_directional_size_limited_by_liquidity():
    sup = make_sup()
    await sup.on_market_update(upd("O-NYK", 0.49, volume=100), None)
    assert (sup.orders[0].contracts, sup.orders[0].stake_usd) == (100, 49.00)


async def test_kill_switch_halts_new_takers_until_settlement():
    sup = make_sup(limit=1500)
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    await sup.on_market_update(upd("O-OVER", 0.44), None)
    assert len(sup.orders) == 1 and sup.stats["kill_switch_blocked"] == 1
    assert sup.settle_event("NBA-BOS-NYK") == 999.60 and sup.positions == {}
    await sup.on_market_update(upd("O-NYK", 0.47), None)
    assert len(sup.orders) == 2


async def test_arbitrage_hedge_also_respects_kill_switch():
    sup = make_sup(limit=1500)
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    await sup.on_market_update(upd("O-BOS", 0.478), None)
    assert sup.stats["arbs"] == 0 and sup.stats["kill_switch_blocked"] == 1


# ---------------- maker integration
async def maker_sup(**kw):
    sup = make_sup(maker=True, **kw)
    sup.feed.connected.set()
    await sup.maker.refresh()
    return sup


async def test_maker_quotes_one_outcome_per_market_with_fresh_sharp():
    sup = await maker_sup()
    quoted = {q.outcome_id for q in sup.maker.quotes.values()}
    # NYK ML, GSW spread, OVER total, NYG ML -> the alphabetically-first outcome of each market
    assert quoted == {"O-BOS", "O-GSW", "O-OVER", "O-NYG"}
    assert all(q.side in {"buy", "sell"} for q in sup.maker.quotes.values())


async def test_maker_not_quoting_when_feed_down_or_position_held():
    sup = make_sup(maker=True)
    await sup.maker.refresh()
    assert sup.maker.quotes == {}                                   # feed not connected
    sup.feed.connected.set()
    await sup.on_market_update(upd("O-NYK", 0.49), None)            # take a NYK/BOS position
    await sup.maker.refresh()
    assert "O-BOS" not in {q.outcome_id for q in sup.maker.quotes.values()}


async def test_maker_buy_fill_becomes_position_and_locks_market():
    sup = await maker_sup()
    bid = next(q for q in sup.maker.quotes.values() if q.outcome_id == "O-BOS" and q.side == "buy")
    await sup.on_market_update(upd("O-BOS", bid.price_cents / 100), None)   # ask trades down to our bid
    fill = sup.orders[0]
    assert (fill.kind, fill.side, fill.price, fill.contracts) == ("MAKER_FILL", "Boston Celtics",
                                                                  bid.price_cents / 100, bid.contracts)
    assert not any(q.outcome_id == "O-BOS" for q in sup.maker.quotes.values())   # market quotes pulled
    assert sup.total_exposure() == fill.stake_usd


async def test_maker_sell_fill_is_long_the_sibling():
    sup = await maker_sup()
    ask = next(q for q in sup.maker.quotes.values() if q.outcome_id == "O-BOS" and q.side == "sell")
    await sup.on_market_update(upd("O-BOS", None, bid=ask.price_cents / 100), None)   # bid lifts our ask
    fill = sup.orders[0]
    assert (fill.side, fill.price) == ("New York Knicks", pytest.approx(1 - ask.price_cents / 100))


async def test_sharp_spread_move_over_half_point_bulk_cancels():
    sup = await maker_sup()
    assert sup.maker.quotes
    sup.book.ingest([{**DEMO_SHARP_LINES[1], "line": -5.0}])        # exactly 0.5: NOT greater -> keep
    await asyncio.sleep(0.01)
    assert sup.maker.kill_count == 0
    sup.book.ingest([{**DEMO_SHARP_LINES[1], "line": -5.75}])       # 0.75 > 0.5 -> kill all
    await wait_until(lambda: sup.maker.kill_count == 1)
    assert sup.maker.quotes == {} and sup.maker.last_cancel_ms < MAKER_CANCEL_BUDGET_MS


async def test_sharp_moneyline_fair_move_bulk_cancels():
    sup = await maker_sup()
    sup.book.ingest([{**DEMO_SHARP_LINES[0], "odds_for": -135, "odds_against": 115}])   # fair +4c
    await wait_until(lambda: sup.maker.kill_count == 1)
    assert sup.maker.quotes == {}


async def test_websocket_drop_bulk_cancels_under_200ms():
    sup = await maker_sup()
    n = len(sup.maker.quotes)
    await sup.on_feed_state("DISCONNECTED", {"venue": "novig", "error": "test"})
    assert sup.maker.quotes == {} and len(sup.maker_gateway.cancel_calls[-1]) == n
    assert sup.maker.last_cancel_ms < MAKER_CANCEL_BUDGET_MS


async def test_kalshi_drop_does_not_touch_novig_quotes():
    sup = await maker_sup()
    await sup.on_feed_state("DISCONNECTED", {"venue": "kalshi", "error": "test"})
    assert sup.maker.quotes and sup.maker.kill_count == 0


# ---------------- configuration safety (live config: see test_live_execution.py)
def test_no_sharp_source_means_measurement_mode():
    sup = Supervisor()
    assert not sup.sharp_enabled and "sharp_poller" not in sup._task_factories
