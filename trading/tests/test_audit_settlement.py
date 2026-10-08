"""Regressions from the independent settlement audit: every lifecycle edge must conserve exposure and P&L."""

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from novig_feed import MarketUpdate
from novig_private import FillSlip
from settlement import load_ledger_holdings
from tests.test_settlement import INFO, engine, ledger_rows, pos

NY = ZoneInfo("America/New_York")


async def _unconfirmed(sup, how):
    if how == "no_slip":                     # acked, no slip, channel unproven: UNCONFIRMED
        sup.orders_channel_confirmed = False
        sup.taker_fill_timeout = 0.02
        await sup.on_market_update(MarketUpdate(**INFO["O-NYK"], price=0.49, available_volume=5000), None)
        await asyncio.sleep(0.1)
    else:                                    # the order call timed out: it may be live
        async def boom(*a):
            raise asyncio.TimeoutError()
        sup.order_gateway.place_limit = boom
        await sup.on_market_update(MarketUpdate(**INFO["O-NYK"], price=0.49, available_volume=5000), None)


@pytest.mark.parametrize("how", ["no_slip", "ambiguous_error"])
@pytest.mark.parametrize("result,net", [("LOSS", -9.80), ("WIN", 10.20)])
async def test_an_unconfirmed_order_that_settles_first_is_booked_at_its_real_cost(tmp_path, how, result, net):
    sup, ledger = engine(tmp_path)
    await _unconfirmed(sup, how)
    sup.positions_client.settled = [pos(settlement_id="s", contracts=20, cost_usd=9.8, result=result)]
    await sup.settlement_sweep()
    [row] = ledger_rows(ledger, "SETTLE")
    assert row["net_profit_usd"] == pytest.approx(net) and sup.total_exposure() == 0
    assert sup.daily_pnl[sup._utc_day()] == pytest.approx(net)


async def test_unconfirmed_without_exchange_cost_is_charged_at_the_limit_price(tmp_path):
    sup, ledger = engine(tmp_path)
    await _unconfirmed(sup, "ambiguous_error")
    sup.positions_client.settled = [pos(settlement_id="s", contracts=20, result="LOSS")]
    await sup.settlement_sweep()
    assert ledger_rows(ledger, "SETTLE")[0]["net_profit_usd"] == pytest.approx(-9.80)   # 20 x 0.49 limit


async def test_an_unknown_tranche_keeps_its_reservation_when_its_sibling_finishes(tmp_path):
    sup, _ = engine(tmp_path, limit=1000.0)
    sup.taker_fill_timeout = 0.03
    calls, real = {"n": 0}, sup.order_gateway.place_limit

    async def gw(*a):
        calls["n"] += 1
        if calls["n"] == 2:
            raise asyncio.TimeoutError()
        return await real(*a)
    sup.order_gateway.place_limit = gw
    await sup.on_market_update(MarketUpdate(**INFO["O-NYK"], price=0.47, available_volume=10,
                                            ask_levels=[(0.47, 10), (0.48, 10)]), None)
    reserved = sup.total_exposure()
    oid = next(iter(sup.live_orders))
    lo = sup.live_orders[oid]
    await sup.on_fill_slip(FillSlip(order_id=oid, status="FILLED", filled_volume=lo.requested,
                                    price_cents=lo.limit_price * 100))
    await asyncio.sleep(0.1)
    assert sup.total_exposure() == pytest.approx(reserved)          # 4.70 filled + 4.80 still unknown


@pytest.mark.parametrize("result,net", [("WIN", 12.75), ("LOSS", -12.25), ("TIE", 0.25)])
async def test_payout_and_cost_always_come_from_one_view(tmp_path, result, net):
    sup, ledger = engine(tmp_path)
    await sup.on_market_update(MarketUpdate(**INFO["O-NYK"], price=0.49, available_volume=5000), None)
    oid = f"ex-{sup.order_gateway.n}"
    await sup.on_fill_slip(FillSlip(order_id=oid, status="FILLED", filled_volume=20, price_cents=49))
    sup.positions_client.settled = [pos(settlement_id="s", contracts=25, cost_usd=12.25, result=result)]
    await sup.settlement_sweep()
    assert ledger_rows(ledger, "SETTLE")[0]["net_profit_usd"] == pytest.approx(net)


async def test_a_fill_we_never_saw_is_still_charged_and_a_late_slip_never_re_reserves(tmp_path):
    sup, ledger = engine(tmp_path)
    sup.taker_fill_timeout = 0.02
    await sup.on_market_update(MarketUpdate(**INFO["O-NYK"], price=0.49, available_volume=5000), None)
    oid = f"ex-{sup.order_gateway.n}"
    await sup.on_fill_slip(FillSlip(order_id=oid, status="PARTIAL", filled_volume=10, price_cents=49))
    await asyncio.sleep(0.1)
    sup.positions_client.settled = [pos(settlement_id="s", contracts=20, result="LOSS")]
    await sup.settlement_sweep()
    assert ledger_rows(ledger, "SETTLE")[0]["net_profit_usd"] == pytest.approx(-9.80)
    await sup.on_fill_slip(FillSlip(order_id=oid, status="FILLED", filled_volume=20, price_cents=49))
    await sup.settlement_sweep()
    assert sup.total_exposure() == 0 and ledger_rows(ledger, "LATE_FILL_AFTER_SETTLE")


async def test_a_late_fill_is_in_the_ledger_holdings_used_after_a_restart(tmp_path):
    sup, ledger = engine(tmp_path)
    sup.taker_fill_timeout = 0.02
    await sup.on_market_update(MarketUpdate(**INFO["O-NYK"], price=0.49, available_volume=5000), None)
    oid = f"ex-{sup.order_gateway.n}"
    await sup.on_fill_slip(FillSlip(order_id=oid, status="PARTIAL", filled_volume=10, price_cents=49))
    await asyncio.sleep(0.1)
    await sup.on_fill_slip(FillSlip(order_id=oid, status="FILLED", filled_volume=20, price_cents=49))
    [held] = load_ledger_holdings(ledger).values()
    assert held["contracts"] == pytest.approx(20) and held["cost_usd"] == pytest.approx(9.80)


async def test_a_settlement_during_downtime_counts_on_the_day_it_settled(tmp_path):
    sup, ledger = engine(tmp_path)
    sup.daily_loss_limit = 500.0
    yesterday = datetime.now(NY).timestamp() - 2 * 86400
    await sup.on_market_update(MarketUpdate(**INFO["O-NYK"], price=0.49, available_volume=5000), None)
    oid = f"ex-{sup.order_gateway.n}"
    await sup.on_fill_slip(FillSlip(order_id=oid, status="FILLED", filled_volume=20, price_cents=49))
    sup.positions_client.settled = [pos(settlement_id="s", contracts=20, result="WIN", settled_at=yesterday)]
    await sup.settlement_sweep()
    assert sup.daily_pnl.get(sup._utc_day(), 0.0) == 0.0             # an old win never loosens today's stop
    assert sup.daily_pnl[sup._utc_day(yesterday)] == pytest.approx(10.20)


async def test_unknown_pnl_charges_the_released_stake_to_the_daily_stop(tmp_path):
    sup, ledger = engine(tmp_path)
    await sup.on_market_update(MarketUpdate(**INFO["O-NYK"], price=0.49, available_volume=5000), None)
    oid = f"ex-{sup.order_gateway.n}"
    await sup.on_fill_slip(FillSlip(order_id=oid, status="FILLED", filled_volume=20, price_cents=49))
    sup.positions_client.settled = [pos(settlement_id="s", contracts=20)]           # no result, no payout
    await sup.settlement_sweep()
    assert sup.daily_pnl[sup._utc_day()] == pytest.approx(-9.80)
    sup2, _ = engine(tmp_path)                                          # survives a restart
    assert sup2.daily_pnl[sup2._utc_day()] == pytest.approx(-9.80)


class _Rest:
    def __init__(self, open_, settled):
        self.o, self.s = open_, settled

    async def executions(self, status=None):
        return self.o if status == "open" else self.s


async def test_a_parlay_that_settles_while_the_engine_is_down_is_booked_once(tmp_path):
    from main_supervisor import build_live_supervisor
    env = {"NOVIG_RFQ": "qa", "NOVIG_RFQ_ACCESS_TOKEN": "t", "TRADING_LOG_DIR": str(tmp_path),
           "RESEARCH_ENABLED": "0", "DAILY_LOSS_LIMIT_USD": "1000"}
    ledger = tmp_path / "live_ledger.jsonl"

    def start():
        sup = build_live_supervisor(env)
        sup.ledger_path = ledger                                       # as in live mode
        sup.cumulative_pnl = __import__("settlement").load_ledger_settlements(ledger)[1]
        sup.novig_rfq.ledger_open = sup._ledger_open_parlays("novig")
        return sup
    sup = start()
    sup.novig_rfq.on_opened("r1", price=0.25, wager=50.0, liability=150.0)        # sold, then the engine stops
    row = dict(rfq_id="r1", wager=50.0, price=0.25, liability=150.0, status="settled", result="win")
    for _ in range(2):                                                 # two restarts: booked exactly once
        sup = start()
        sup.novig_rfq.rest = _Rest(open_=[], settled=[row])
        await sup.novig_rfq.restore()
        await sup.novig_rfq.check_results()
    settles = [json.loads(x) for x in sup.ledger_path.read_text().splitlines() if '"SETTLE"' in x]
    assert [r["net_profit_usd"] for r in settles] == [-150.0]
    assert sup.cumulative_pnl == -150.0
