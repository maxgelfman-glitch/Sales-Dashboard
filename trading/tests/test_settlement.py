"""
Self-test: settlement & exposure release (settlement.py + Supervisor sync / sweep) and the dashboard's view of it.

Reference: canary $10 stake -> NYK ask 0.49 -> 20 contracts, $9.80.
    WIN  -> payout 20 x $1 = $20.00, net +$10.20
    LOSS -> payout $0,               net -$9.80
    TIE  -> payout 20 x $0.50 = $10, net +$0.20
"""

import asyncio
import json
import time

import pandas as pd
import pytest
from aiohttp import web

import dashboard as db
from execution import ExposureMonitor
from main_supervisor import DEMO_MARKETS, DEMO_SHARP_LINES, LivePlan, Supervisor
from novig_feed import MarketRegistry, MarketUpdate
from novig_private import FillSlip
from settlement import (
    BASELINE_CAPITAL_USD,
    ExchangePosition,
    PositionsClient,
    load_ledger_settlements,
    parse_position,
    parse_positions,
    settlement_pnl,
)
from sharp_feed import MockSharpSource

INFO = {m["outcome_id"]: m for m in DEMO_MARKETS}


# ================================================================ parsing & maths
def test_parse_position_field_variants():
    a = parse_position({"positionId": 7, "outcomeId": "O-NYK", "quantity": "20", "avgPrice": 49, "status": "open"})
    assert (a.settlement_id, a.outcome_id, a.contracts, a.cost_usd, a.status) == ("7", "O-NYK", 20, 9.8, "OPEN")
    b = parse_position({"outcome": {"id": "O-X"}, "size": 10, "cost": 4.4, "result": "won",
                        "settledAt": "2026-09-23T12:00:00Z"})
    assert (b.outcome_id, b.cost_usd, b.result, b.is_settled, b.settled_at) == ("O-X", 4.4, "WON", True, 1790164800)
    c = parse_position({"outcome_id": "O-Y", "contracts": 5, "price": 0.4})
    assert c.cost_usd == 2.0 and not c.is_settled
    assert parse_position({"no": "outcome"}) is None


def test_settlement_id_is_stable_without_exchange_id():
    row = {"outcomeId": "O-1", "contracts": 3, "status": "SETTLED", "settled_at": 1790000000}
    assert parse_position(dict(row)).settlement_id == parse_position(dict(row)).settlement_id


@pytest.mark.parametrize("payload", [None, "x", {}, {"positions": "nope"}, [1, 2], {"data": [{"bad": 1}]}])
def test_parse_positions_never_raises(payload):
    assert parse_positions(payload) == []


def pos(**kw):
    base = dict(settlement_id="s1", outcome_id="O-NYK", contracts=20, status="SETTLED")
    return ExchangePosition(**{**base, **kw})


@pytest.mark.parametrize("kw,expected", [
    (dict(result="WIN"), (10.20, 20.0, "result_win")),
    (dict(result="LOSS"), (-9.80, 0.0, "result_loss")),
    (dict(result="TIE"), (0.20, 10.0, "result_tie")),               # dead heat: 50c per contract
    (dict(result="VOID"), (0.0, 9.80, "result_void")),              # refund
    (dict(payout_usd=20.0), (10.20, 20.0, "exchange_payout")),
    (dict(pnl_usd=10.25, result="WIN"), (10.25, 20.05, "exchange_pnl")),   # exchange P&L wins over our maths
    (dict(), (None, None, "unknown")),
])
def test_settlement_pnl_rules(kw, expected):
    assert settlement_pnl(pos(**kw), 9.80) == expected


def test_load_ledger_settlements(tmp_path):
    f = tmp_path / "l.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in [
        {"event": "SETTLE", "settlement_id": "a", "net_profit_usd": 10.2},
        {"event": "SETTLE", "settlement_id": "b", "net_profit_usd": None},
        {"event": "ORDER"}]) + "\nnot json\n")
    assert load_ledger_settlements(f) == ({"a", "b"}, 10.2)
    assert load_ledger_settlements(tmp_path / "missing") == (set(), 0.0)


# ================================================================ supervisor harness
class FakeGateway:
    live, api_base = True, "fake://novig"

    def __init__(self):
        self.n = 0

    async def place_limit(self, *a):
        self.n += 1
        return f"ex-{self.n}"

    async def cancel_orders(self, ids):
        pass

    async def close(self):
        pass


class FakePositions:
    url, status_param, open_status, settled_status = "fake://novig/v1/positions", "status", "OPEN", "SETTLED"

    def __init__(self, open_=(), settled=(), fail_open=0):
        self.open, self.settled, self.fail_open, self.calls = list(open_), list(settled), fail_open, []

    async def open_positions(self):
        self.calls.append("OPEN")
        if self.fail_open:
            self.fail_open -= 1
            raise ConnectionError("novig down")
        return self.open

    async def settled_positions(self):
        self.calls.append("SETTLED")
        return self.settled

    async def close(self):
        pass


def engine(tmp_path, positions=None, limit=100.0, synced=True):
    ledger = tmp_path / "live_ledger.jsonl"
    sup = Supervisor(feed_url="ws://127.0.0.1:1/x", token="t", sharp_fetch=MockSharpSource(DEMO_SHARP_LINES),
                     registry=MarketRegistry(DEMO_MARKETS), live=True, order_gateway=FakeGateway(),
                     maker_enabled=False, max_stake=10.0, exposure=ExposureMonitor(limit), ledger_path=ledger,
                     positions_client=positions or FakePositions(), sync_retries=2, sync_retry_delay=0.01,
                     live_plan=LivePlan(max_stake=10, exposure_limit=limit, maker_enabled=False, scaled_up=False))
    sup.book.ingest(DEMO_SHARP_LINES)
    sup.feed.connected.set()
    sup.orders_channel_confirmed = True
    sup.record_live_plan()
    sup._ledger("SESSION_START", max_stake_usd=10.0, exposure_limit_usd=limit, maker=False)
    sup.synced = synced
    return sup, ledger


async def buy(sup, outcome, price, oid):
    await sup.on_market_update(MarketUpdate(**INFO[outcome], price=price, available_volume=5000), None)
    n = sup.order_gateway.n
    await sup.on_fill_slip(FillSlip(order_id=f"ex-{n}", status="FILLED",
                                    filled_volume=sup.live_orders[f"ex-{n}"].requested, price_cents=price * 100))
    return f"ex-{n}"


def ledger_rows(path, event=None):
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    return [r for r in rows if event is None or r["event"] == event]


def dashboard_view(path):
    return db.snapshot(db.LedgerStore(path).refresh(), pd.DataFrame(columns=["ts", "level", "logger", "msg"]))


# ================================================================ settlement releases exposure
async def test_win_settlement_releases_exposure_and_writes_settle_row(tmp_path):
    sup, ledger = engine(tmp_path)
    oid = await buy(sup, "O-NYK", 0.49, "ex-1")
    assert sup.total_exposure() == 9.80
    sup.positions_client.settled = [pos(settlement_id="novig-pos-1", result="WIN")]
    await sup.settlement_sweep()
    assert sup.total_exposure() == 0.0 and sup.positions == {}          # released + unlocked
    [row] = ledger_rows(ledger, "SETTLE")
    assert {k: row[k] for k in ("game_id", "market_type", "outcome_id", "net_profit_usd", "resulting_capital_pool",
                                "stake_usd", "payout_usd", "released_exposure_usd", "settlement_id")} == {
        "game_id": "NBA-BOS-NYK", "market_type": "moneyline", "outcome_id": "O-NYK", "net_profit_usd": 10.2,
        "resulting_capital_pool": 100_010.2, "stake_usd": 9.8, "payout_usd": 20.0, "released_exposure_usd": 9.8,
        "settlement_id": "novig-pos-1"}
    assert row["timestamp"].endswith("+00:00") and row["released_order_ids"] == [oid]
    assert sup.capital_pool == 100_010.20


async def test_the_exposure_trap_is_gone_canary_resumes_after_settlement(tmp_path):
    """Before: exposure only grew, so the $100 canary halted forever after ~10 fills."""
    sup, _ = engine(tmp_path, limit=20.0)                                # 2 x $9.80 fits, a 3rd does not
    await buy(sup, "O-NYK", 0.49, "ex-1")
    await buy(sup, "O-OVER", 0.44, "ex-2")
    await sup.on_market_update(MarketUpdate(**INFO["O-GSW"], price=0.45, available_volume=5000), None)
    assert sup.order_gateway.n == 2 and sup.stats["kill_switch_blocked"] == 1      # halted
    sup.positions_client.settled = [pos(settlement_id="s-nyk", result="LOSS")]
    await sup.settlement_sweep()
    assert sup.total_exposure() == 9.68                                   # only the OVER leg remains
    await sup.on_market_update(MarketUpdate(**INFO["O-GSW"], price=0.45, available_volume=5000), None)
    assert sup.order_gateway.n == 3                                       # trading resumed
    assert sup.capital_pool == BASELINE_CAPITAL_USD - 9.80


async def test_settlements_are_idempotent_across_sweeps_and_restarts(tmp_path):
    sup, ledger = engine(tmp_path)
    await buy(sup, "O-NYK", 0.49, "ex-1")
    sup.positions_client.settled = [pos(settlement_id="once", result="WIN")]
    await sup.settlement_sweep()
    await sup.settlement_sweep()                                          # same settlement again
    assert len(ledger_rows(ledger, "SETTLE")) == 1
    restarted, _ = engine(tmp_path, positions=FakePositions(settled=[pos(settlement_id="once", result="WIN")]))
    assert "once" in restarted.processed_settlements and restarted.capital_pool == 100_010.20
    await restarted.settlement_sweep()
    assert len(ledger_rows(ledger, "SETTLE")) == 1                        # never double-counted


async def test_hedged_pair_both_legs_settle_and_game_unlocks(tmp_path):
    sup, ledger = engine(tmp_path)
    await buy(sup, "O-NYK", 0.49, "ex-1")
    await sup.on_market_update(MarketUpdate(**INFO["O-BOS"], price=0.478, available_volume=5000), None)
    await sup.on_fill_slip(FillSlip(order_id="ex-2", status="FILLED", filled_volume=20, price_cents=47.8))
    assert len(sup.positions[("NBA", "New York Knicks", "Boston Celtics", "moneyline")].legs) == 2
    sup.positions_client.settled = [pos(settlement_id="w", outcome_id="O-NYK", result="WIN"),
                                    pos(settlement_id="l", outcome_id="O-BOS", result="LOSS")]
    await sup.settlement_sweep()
    rows = ledger_rows(ledger, "SETTLE")
    assert sorted(r["net_profit_usd"] for r in rows) == [-9.56, 10.2]    # locked +$0.64 on the pair
    assert sup.positions == {} and sup.total_exposure() == 0.0 and sup.capital_pool == 100_000.64


async def test_nfl_tie_settles_at_50_cents(tmp_path):
    sup, ledger = engine(tmp_path)
    await buy(sup, "O-NYG", 0.36, "ex-1")                                 # 27 contracts, $9.72
    sup.positions_client.settled = [pos(settlement_id="t", outcome_id="O-NYG", contracts=27, result="TIE")]
    await sup.settlement_sweep()
    [row] = ledger_rows(ledger, "SETTLE")
    assert (row["payout_usd"], row["net_profit_usd"]) == (13.5, 3.78)


async def test_unknown_pnl_still_releases_exposure(tmp_path):
    sup, ledger = engine(tmp_path)
    await buy(sup, "O-NYK", 0.49, "ex-1")
    sup.positions_client.settled = [pos(settlement_id="u", status="SETTLED")]
    await sup.settlement_sweep()
    [row] = ledger_rows(ledger, "SETTLE")
    assert row["net_profit_usd"] is None and row["pnl_method"] == "unknown" and sup.total_exposure() == 0.0
    assert dashboard_view(ledger)["unknown_pnl_settlements"] == 1


async def test_stale_open_list_never_resurrects_a_settled_position(tmp_path):
    sup, ledger = engine(tmp_path)
    await buy(sup, "O-NYK", 0.49, "ex-1")
    sup.positions_client.settled = [pos(settlement_id="s", result="WIN")]
    sup.positions_client.open = [pos(settlement_id="s", status="OPEN", cost_usd=9.8)]   # exchange lagging
    await sup.settlement_sweep()
    assert sup.total_exposure() == 0.0 and ledger_rows(ledger, "RESTORE") == []


async def test_untracked_settlements_are_ignored(tmp_path):
    sup, ledger = engine(tmp_path)
    sup.positions_client.settled = [pos(settlement_id="ancient", outcome_id="O-OLD", result="WIN")]
    await sup.settlement_sweep()
    assert ledger_rows(ledger, "SETTLE") == [] and sup.capital_pool == BASELINE_CAPITAL_USD


# ================================================================ startup sync (restart risk)
async def test_startup_sync_restores_open_positions_before_trading(tmp_path):
    client = FakePositions(open_=[pos(settlement_id="p1", outcome_id="O-NYK", contracts=20, cost_usd=9.8,
                                      status="OPEN"),
                                  pos(settlement_id="p2", outcome_id="O-UNKNOWN", contracts=10, status="OPEN")])
    sup, ledger = engine(tmp_path, positions=client, synced=False)
    assert not sup._live_ready()                                          # no trading before the sync
    assert await sup.startup_sync()
    assert sup.total_exposure() == 9.80 + 10.0                            # unknown cost -> contracts x $1
    await sup.on_market_update(MarketUpdate(**INFO["O-NYK"], price=0.45, available_volume=5000), None)
    assert sup.order_gateway.n == 0                                       # restored NYK position locks the game
    restores = ledger_rows(ledger, "RESTORE")
    assert [(r["outcome_id"], r["mapped"]) for r in restores] == [("O-NYK", True), ("O-UNKNOWN", False)]
    client.settled = [pos(settlement_id="p1", outcome_id="O-NYK", cost_usd=9.8, result="WIN")]
    await sup.settlement_sweep()
    assert sup.total_exposure() == 10.0 and sup.capital_pool == 100_010.20


async def test_restored_exposure_over_the_canary_halts_takers(tmp_path):
    client = FakePositions(open_=[pos(settlement_id="big", outcome_id="O-GSW", contracts=300, cost_usd=150.0,
                                      status="OPEN")])
    sup, _ = engine(tmp_path, positions=client, synced=False)
    await sup.startup_sync()
    assert sup.exposure.taker_halted
    await sup.on_market_update(MarketUpdate(**INFO["O-NYK"], price=0.45, available_volume=5000), None)
    assert sup.order_gateway.n == 0 and sup.stats["kill_switch_blocked"] == 1


async def test_failed_startup_sync_refuses_to_trade(tmp_path):
    sup, ledger = engine(tmp_path, positions=FakePositions(fail_open=5), synced=False)
    await sup.run(duration=0.2)
    assert not sup.synced and sup.order_gateway.n == 0
    assert ledger_rows(ledger, "SYNC_FAILED")[0]["attempts"] == 2


async def test_startup_sync_retries_then_succeeds(tmp_path):
    sup, _ = engine(tmp_path, positions=FakePositions(fail_open=1), synced=False)
    assert await sup.startup_sync() and sup.synced


# ================================================================ reconciliation of UNCONFIRMED / untracked
async def test_unconfirmed_order_resolved_or_released_by_exchange_positions(tmp_path):
    sup, ledger = engine(tmp_path)
    sup.orders_channel_confirmed = False
    sup.taker_fill_timeout = 0.02
    await sup.on_market_update(MarketUpdate(**INFO["O-NYK"], price=0.49, available_volume=5000), None)
    await sup.on_market_update(MarketUpdate(**INFO["O-OVER"], price=0.44, available_volume=5000), None)
    await asyncio.sleep(0.1)
    assert len(sup.unconfirmed_legs) == 2 and sup.total_exposure() == 9.80 + 9.68
    for leg_id in sup.unconfirmed_legs:
        sup.unconfirmed_legs[leg_id] = time.time() - 120                  # older than the 60s grace period
    sup.positions_client.open = [pos(settlement_id="x", outcome_id="O-NYK", contracts=20, cost_usd=9.8,
                                     status="OPEN")]
    await sup.settlement_sweep()
    assert sup.unconfirmed_legs == {}
    assert sup.total_exposure() == 9.80                                   # NYK really filled, OVER never did
    assert [r["event"] for r in ledger_rows(ledger) if r["event"].startswith("UNCONFIRMED_")] == [
        "UNCONFIRMED_RESOLVED", "UNCONFIRMED_RELEASED"]


async def test_sweep_restores_untracked_positions_and_flags_mismatches(tmp_path):
    sup, ledger = engine(tmp_path)
    await buy(sup, "O-NYK", 0.49, "ex-1")
    sup.positions_client.open = [pos(settlement_id="a", outcome_id="O-NYK", contracts=25, cost_usd=12.25,
                                     status="OPEN"),
                                 pos(settlement_id="m", outcome_id="O-GSW", contracts=4, cost_usd=2.0, status="OPEN")]
    await sup.settlement_sweep()
    assert ledger_rows(ledger, "POSITION_MISMATCH")[0]["exchange_contracts"] == 25
    assert ledger_rows(ledger, "RESTORE")[0]["outcome_id"] == "O-GSW" and sup.total_exposure() == 11.80


# ================================================================ HTTP client
async def test_positions_client_http(monkeypatch):
    seen = []

    async def handler(request):
        seen.append((dict(request.query), request.headers.get("Authorization")))
        status = request.query["status"]
        return web.json_response({"positions": [{"positionId": 1, "outcomeId": "O-NYK", "quantity": 20,
                                                 "avgPrice": 49, "status": status}]})

    app = web.Application()
    app.router.add_get("/v1/positions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    client = PositionsClient(base, "tok")
    try:
        [o] = await client.open_positions()
        [s] = await client.settled_positions()
    finally:
        await client.close()
        await runner.cleanup()
    assert seen == [({"status": "OPEN"}, "Bearer tok"), ({"status": "SETTLED"}, "Bearer tok")]
    assert (o.cost_usd, o.is_settled, s.is_settled) == (9.8, False, True)


# ================================================================ dashboard sees exactly what the engine did
async def test_dashboard_capital_curve_and_exposure_match_engine(tmp_path):
    client = FakePositions(open_=[pos(settlement_id="r1", outcome_id="O-GSW", contracts=4, cost_usd=2.0,
                                      status="OPEN")])
    sup, ledger = engine(tmp_path, positions=client, synced=False)
    await sup.startup_sync()
    await buy(sup, "O-NYK", 0.49, "ex-1")
    await buy(sup, "O-OVER", 0.44, "ex-2")
    view = dashboard_view(ledger)
    assert view["exposure"] == pytest.approx(sup.total_exposure()) == pytest.approx(21.48)
    assert "Novig (restored)" in set(view["positions"]["Exchange Pool"])
    client.settled = [pos(settlement_id="s1", outcome_id="O-NYK", result="WIN"),
                      pos(settlement_id="s2", outcome_id="O-GSW", contracts=4, cost_usd=2.0, result="LOSS")]
    await sup.settlement_sweep()
    view = dashboard_view(ledger)
    assert view["exposure"] == pytest.approx(sup.total_exposure()) == pytest.approx(9.68)
    assert view["capital"] == sup.capital_pool == 100_008.20                 # +10.20 - 2.00
    assert list(view["curve"]["cum_pnl"]) == [10.2, 8.2]
    assert list(view["positions"]["Game / Outcome ID"]) == ["O-OVER"]         # settled rows gone from the table
    assert any("SETTLE" in t for t in view["audit"]["text"])
