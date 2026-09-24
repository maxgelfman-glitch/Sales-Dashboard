"""
Self-test: locked pairs — buying BOTH sides across venues when they lock a profit after fees, with no
sharp line needed (works in measurement mode), sized for the worst case (one leg fills, the other not).
"""

import asyncio

import pytest

from execution import kalshi_taker_fee
from main_supervisor import DEMO_KALSHI_MARKETS, DEMO_MARKETS, Supervisor
from novig_feed import MarketRegistry
from research import ResearchRecorder
from tests.test_kalshi_trading import K_BOS, FakeKalshiGateway, kalshi_sup
from tests.test_live_execution import live_sup, slip, upd

KEY = ("NBA", "New York Knicks", "Boston Celtics", "moneyline")


def measurement_sup(tmp_path, **kw):
    sup = Supervisor(feed_url="ws://127.0.0.1:1/x", token="t", registry=MarketRegistry(DEMO_MARKETS),
                     kalshi_registry=MarketRegistry(DEMO_KALSHI_MARKETS), research=ResearchRecorder(tmp_path),
                     maker_enabled=False, **kw)
    sup.feed.connected.set()
    return sup


async def show(sup, u):
    (sup.kalshi.latest if u.venue == "kalshi" else sup.feed.latest)[u.outcome_id] = u
    await sup.on_market_update(u, None)


async def test_cross_venue_pair_trades_without_any_sharp_line(tmp_path):
    sup = measurement_sup(tmp_path)
    await show(sup, upd("O-NYK", 0.47, volume=800))
    assert sup.orders == []                                           # one side alone: nothing to do
    await show(sup, upd(K_BOS, 0.45, volume=600))                     # 47 + 45 + 1.73 fee < 100
    a, b = sup.orders
    assert (a.kind, b.kind) == ("ARB_PAIR", "ARB_PAIR") and {a.venue, b.venue} == {"novig", "kalshi"}
    assert a.contracts == b.contracts == 600                          # the thinner side
    pos = sup.positions[KEY]
    assert pos.hedged and pos.unhedged() == 0 and sup.game_unhedged(KEY[:3]) == 0
    locked = 600 - a.stake_usd - b.stake_usd
    assert locked == pytest.approx(600 * (1 - 0.47 - 0.45) - kalshi_taker_fee(600, 45), abs=0.01)
    assert sup.stats["arb_pairs"] == 1


async def test_no_pair_when_fees_eat_the_gap(tmp_path):
    sup = measurement_sup(tmp_path)
    await show(sup, upd("O-NYK", 0.50))
    await show(sup, upd(K_BOS, 0.49))                                 # 99c + 1.75c fee: a loss
    assert sup.orders == []


async def test_pair_is_sized_so_one_leg_alone_fits_the_game_cap(tmp_path):
    sup = measurement_sup(tmp_path, game_exposure_limit=100.0)
    await show(sup, upd("O-NYK", 0.47, volume=5000))
    await show(sup, upd(K_BOS, 0.45, volume=5000))
    a, b = sup.orders
    assert max(a.stake_usd, b.stake_usd) <= 100.0 + 1e-6


async def test_pairs_can_be_switched_off(tmp_path):
    sup = measurement_sup(tmp_path, arb_pairs_enabled=False)
    await show(sup, upd("O-NYK", 0.47))
    await show(sup, upd(K_BOS, 0.45))
    assert sup.orders == []


# Live tests use prices where NEITHER side beats the sharp line by 2.5% on its own (fair NYK 52.17c /
# BOS 47.83c), but together they lock > 1c: NYK 51c on Novig + BOS 46c + 1.74c fee on Kalshi = 98.74c.
async def test_live_pair_sends_both_legs_on_their_venues():
    sup = kalshi_sup()
    await show(sup, upd("O-NYK", 0.51, volume=500))
    assert sup.order_gateway.placed == []                                     # 2.3% edge alone: no bet
    await show(sup, upd(K_BOS, 0.46, volume=400))
    [novig] = sup.order_gateway.placed
    [kalshi] = sup.kalshi_gateway.placed
    assert (novig["outcome_id"], kalshi["outcome_id"]) == ("O-NYK", K_BOS)
    assert novig["contracts"] == kalshi["contracts"] == 400
    pos = sup.positions[KEY]
    assert pos.hedged and len(pos.legs) == 2 and sup.stats["arb_pairs"] == 1


async def test_live_without_kalshi_execution_never_pairs_across_venues():
    sup = live_sup()
    sup.kalshi.connected.set()
    await show(sup, upd("O-NYK", 0.51))
    await show(sup, upd(K_BOS, 0.46))
    assert sup.order_gateway.placed == [] and sup.stats["arb_pairs"] == 0


async def test_one_leg_missing_leaves_a_plain_position_that_can_still_be_hedged():
    sup = kalshi_sup(FakeKalshiGateway(reported_fill=0), timeout=0.05)
    await show(sup, upd("O-NYK", 0.51, volume=400))
    await show(sup, upd(K_BOS, 0.46, volume=400))
    await sup.on_fill_slip(slip("ex-1", "FILLED", 400, 51))                   # Novig leg fills
    await asyncio.sleep(0.2)                                                   # Kalshi leg: nothing (IOC)
    pos = sup.positions[KEY]
    assert [l.venue for l in pos.legs] == ["novig"] and not pos.hedged and pos.unhedged() == 400
    await show(sup, upd(K_BOS, 0.46, volume=400))                              # the gap is still there
    assert len(sup.kalshi_gateway.placed) == 2                                 # normal hedge path retries
