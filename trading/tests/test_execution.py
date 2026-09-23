"""
Milestone 3 self-test: execution.py

Every expected number below is derived by hand in the comments, so the test
is an independent check of the formulas — not the code checking itself.
"""

import math

import pytest

import execution
from execution import (
    MAX_STAKE_USD,
    american_to_decimal,
    apply_safety_ceiling,
    devig_multiplicative,
    evaluate_market_edge,
)


# ---------------------------------------------------------------- conversions / de-vig
@pytest.mark.parametrize("american,decimal", [(100, 2.0), (150, 2.5), (-150, 1 + 100 / 150), (-110, 1 + 100 / 110), (300, 4.0)])
def test_american_to_decimal(american, decimal):
    assert american_to_decimal(american) == pytest.approx(decimal)


@pytest.mark.parametrize("bad", [0, 50, -99.9])
def test_american_to_decimal_rejects_invalid(bad):
    with pytest.raises(ValueError):
        american_to_decimal(bad)


def test_devig_symmetric_minus_110():
    # -110/-110: raw = 0.52381 each, overround = 1.047619, fair = 0.5 each
    fair, over = devig_multiplicative([american_to_decimal(-110)] * 2)
    assert fair == pytest.approx([0.5, 0.5])
    assert over == pytest.approx(1.0476190476)


def test_devig_asymmetric_minus_150_plus_130():
    # raw: 1/1.6667 = 0.600000, 1/2.30 = 0.434783; overround = 1.034783
    # fair: 0.600000/1.034783 = 0.579832 ; 0.434783/1.034783 = 0.420168
    fair, over = devig_multiplicative([american_to_decimal(-150), american_to_decimal(130)])
    assert over == pytest.approx(1.034783, abs=1e-6)
    assert fair == pytest.approx([0.579832, 0.420168], abs=1e-6)
    assert sum(fair) == pytest.approx(1.0)


# ---------------------------------------------------------------- required scenarios
async def test_five_percent_edge_uses_quarter_kelly():
    """
    Sharp +300 / -400 : raw 0.25 + 0.80 = 1.05 overround
                        fair(underdog) = 0.25 / 1.05 = 0.2380952
    Novig price set so edge is exactly 5%: c = 0.2380952 / 1.05 = 0.2267574
    f*     = (0.2380952 - 0.2267574) / (1 - 0.2267574) = 0.0146628
    1/4 K  = 0.25 * 0.01466276 * 100,000 = $366.5689 -> floored to the cent = $366.56 (below the $1,000 cap)
    """
    fair = 0.25 / 1.05
    price = fair / 1.05
    d = await evaluate_market_edge({"price": price, "label": "5% dog"}, {"odds_for": 300, "odds_against": -400})
    assert d.action == "BET"
    assert d.edge == pytest.approx(0.05)
    assert d.full_kelly_fraction == pytest.approx(0.0146628, abs=1e-7)
    assert d.kelly_stake_usd == 366.57   # unrounded Kelly, reported to 2dp
    assert d.stake_usd == 366.56        # stakes are always rounded DOWN (never over-bet)
    assert d.capped is False
    assert d.contracts == int(366.56 // price) == 1616


async def test_five_percent_edge_at_even_money_is_capped():
    """
    Sharp -110/-110 -> fair 0.5. Price = 0.5/1.05 = 0.4761905 (5% edge).
    f* = (0.5 - 0.4761905)/(0.5238095) = 0.0454545 ; 1/4 K = $1,136.36 -> capped to $1,000.00
    """
    d = await evaluate_market_edge({"price": 0.5 / 1.05}, {"odds_for": -110, "odds_against": -110})
    assert d.action == "BET"
    assert d.kelly_stake_usd == pytest.approx(1136.36, abs=0.01)
    assert d.stake_usd == 1000.00 and d.capped is True


async def test_massive_edge_is_limited_to_exactly_1000():
    """Price 0.30 vs fair 0.50 -> +66.7% edge; full Kelly 28.6% of bankroll ($7,142 at 1/4). Must be $1,000."""
    d = await evaluate_market_edge({"price": 0.30}, {"odds_for": -110, "odds_against": -110})
    assert d.action == "BET"
    assert d.edge == pytest.approx(0.6666667)
    assert d.kelly_stake_usd == pytest.approx(7142.86, abs=0.01)
    assert d.stake_usd == 1000.00
    assert d.capped is True
    assert d.suspicious is True  # 66% edge is flagged as probable bad data


async def test_zero_edge_passes_completely():
    d = await evaluate_market_edge({"price": 0.50}, {"odds_for": -110, "odds_against": -110})
    assert d.action == "PASS"
    assert d.edge == pytest.approx(0.0)
    assert d.stake_usd == 0.0 and d.contracts == 0


async def test_negative_edge_passes():
    d = await evaluate_market_edge({"price": 0.55}, {"odds_for": -110, "odds_against": -110})
    assert d.action == "PASS" and d.edge < 0 and d.stake_usd == 0


async def test_exactly_2_5_percent_is_not_enough():
    # 0.5 / 1.025 produces an edge of 0.025000000000000022 in floating point: must still PASS.
    d = await evaluate_market_edge({"price": 0.5 / 1.025}, {"odds_for": -110, "odds_against": -110})
    assert d.action == "PASS"


async def test_just_above_2_5_percent_bets():
    # edge 2.6%: c = 0.48732943, f* = 0.01267057/0.51267057 = 0.02471483 -> 1/4 K = $617.8708 -> $617.87
    price = 0.5 / 1.026
    d = await evaluate_market_edge({"price": price}, {"odds_for": -110, "odds_against": -110})
    assert d.action == "BET"
    expected = math.floor(0.25 * (0.5 - price) / (1 - price) * 100_000 * 100) / 100
    assert d.stake_usd == expected == 617.87


async def test_fees_reduce_edge():
    # 3% edge before a 1c fee; after the fee the price is worse and the edge disappears.
    price = 0.5 / 1.03
    no_fee = await evaluate_market_edge({"price": price}, {"odds_for": -110, "odds_against": -110})
    with_fee = await evaluate_market_edge({"price": price, "fee_per_contract": 0.01}, {"odds_for": -110, "odds_against": -110})
    assert no_fee.action == "BET" and with_fee.action == "PASS"


async def test_line_mismatch_passes():
    d = await evaluate_market_edge({"price": 0.30, "line": -3.5}, {"odds_for": -110, "odds_against": -110, "line": -3.0})
    assert d.action == "PASS" and "line mismatch" in d.reason


@pytest.mark.parametrize("novig,sharp", [
    ({"price": 0}, {"odds_for": -110, "odds_against": -110}),
    ({"price": 1.2}, {"odds_for": -110, "odds_against": -110}),
    ({"price": 0.4}, {"odds_for": 50, "odds_against": -110}),
    ({}, {"odds_for": -110, "odds_against": -110}),
    ({"price": 0.4}, {}),
])
async def test_bad_inputs_never_raise(novig, sharp):
    d = await evaluate_market_edge(novig, sharp)
    assert d.action == "PASS" and d.stake_usd == 0


# ---------------------------------------------------------------- the ceiling itself
@pytest.mark.parametrize("raw,expected,capped", [
    (500.0, 500.0, False), (1000.0, 1000.0, False), (1000.01, 1000.0, True),
    (1e12, 1000.0, True), (float("inf"), 0.0, False), (float("nan"), 0.0, False), (-50.0, 0.0, False),
])
def test_safety_ceiling(raw, expected, capped):
    assert apply_safety_ceiling(raw) == (expected, capped)


async def test_cap_holds_across_a_sweep_of_prices():
    """Brute force: no price/odds combination can ever produce a stake above $1,000."""
    worst = 0.0
    for odds in (-1000, -300, -110, 100, 250, 1000):
        for cents in range(1, 100):
            d = await evaluate_market_edge({"price": cents / 100}, {"odds_for": odds, "odds_against": -110})
            worst = max(worst, d.stake_usd)
    assert worst == MAX_STAKE_USD
    assert execution.MAX_STAKE_USD == 0.01 * execution.BANKROLL_USD
