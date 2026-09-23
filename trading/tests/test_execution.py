"""
Milestone 3 self-test: execution.py

Every expected number below is derived by hand in the comments, so the test
is an independent check of the formulas — not the code checking itself.
"""

import logging
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
def test_whole_contracts_is_float_safe():
    from execution import _whole_contracts
    assert 1000 // 0.40 == 2499.0                       # the floating-point trap this guards against
    assert _whole_contracts(1000.0, 0.40) == 2500
    assert _whole_contracts(999.99, 0.40) == 2499
    assert _whole_contracts(1000.0, 0.49) == 2040


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


# ---------------------------------------------------------------- global exposure kill-switch
from execution import GLOBAL_EXPOSURE_LIMIT_USD, ExposureLimitError, ExposureMonitor  # noqa: E402


def test_exposure_limit_is_15_percent_of_bankroll():
    assert GLOBAL_EXPOSURE_LIMIT_USD == 15_000.00 == 0.15 * execution.BANKROLL_USD


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_kill_switch_engages_at_15000_and_releases_on_settlement():
    # Attach directly: the engine's 'trading' logger does not propagate to pytest's capture.
    cap = _Capture()
    logging.getLogger("trading.execution").addHandler(cap)
    m = ExposureMonitor()
    for i in range(15):                                   # 15 x $1,000 max-size positions
        assert m.check_taker(1000.0) == (True, "ok")
        m.record_open(f"p{i}", 1000.0)
    assert m.open_exposure == 15_000.0 and m.taker_halted
    ok, reason = m.check_taker(0.01)                      # even one cent is refused
    assert not ok and "KILL_SWITCH engaged" in reason
    assert m.allows_cancellation() is True                # maker cancels never blocked
    assert m.settle("p0") == 1000.0
    assert not m.taker_halted and m.check_taker(1000.0)[0]
    logging.getLogger("trading.execution").removeHandler(cap)
    msgs = cap.messages
    assert any("KILL_SWITCH ENGAGED" in x for x in msgs) and any("KILL_SWITCH RELEASED" in x for x in msgs)


def test_order_that_would_breach_is_refused_without_halting():
    m = ExposureMonitor()
    for i in range(14):
        m.record_open(f"p{i}", 1000.0)
    m.record_open("p14", 600.0)                           # $14,600 open, $400 headroom
    ok, reason = m.check_taker(1000.0)
    assert not ok and "would lift exposure to $15,600.00" in reason
    assert m.check_taker(400.0) == (True, "ok")           # exactly to the limit is allowed
    assert not m.taker_halted


def test_record_open_rechecks_limit_as_last_line_of_defence():
    m = ExposureMonitor(limit_usd=1500)
    m.record_open("a", 1000)
    with pytest.raises(ExposureLimitError):
        m.record_open("b", 1000)                          # caller "forgot" check_taker
    assert m.open_exposure == 1000
    with pytest.raises(ValueError):
        m.record_open("a", 100)                           # duplicate id


@pytest.mark.parametrize("bad", [0, -5, float("nan"), float("inf")])
def test_kill_switch_rejects_invalid_stakes(bad):
    assert ExposureMonitor().check_taker(bad)[0] is False


def test_settling_unknown_position_is_harmless():
    assert ExposureMonitor().settle("nope") == 0.0


# ================================================================ Kalshi fee + net edge
from execution import (  # noqa: E402
    MAKER_CANCEL_BUDGET_MS,
    MakerEngine,
    MakerTarget,
    evaluate_kalshi_edge,
    kalshi_taker_fee,
    maker_quote_contracts,
    maker_quote_prices,
)

EVEN = {"odds_for": -110, "odds_against": -110}   # de-vigs to fair 0.50


@pytest.mark.parametrize("contracts,cents,expected", [
    (100, 53, 1.75),     # 0.07*100*0.53*0.47 = 1.7437 -> rounded UP to 1.75
    (1, 50, 0.02),       # 0.0175 -> 0.02
    (2139, 45, 37.06),   # 37.0582 -> 37.06
    (1000, 1, 0.70),     # 0.693 -> 0.70
])
def test_kalshi_taker_fee_formula(contracts, cents, expected):
    assert kalshi_taker_fee(contracts, cents) == expected


async def test_kalshi_edge_is_net_of_fee_and_capped():
    """
    45c vs fair 0.50. Gross edge 11.1%. Fee/contract 0.07*0.45*0.55 = 0.017325 -> effective 0.467325.
    1/4 Kelly caps at $1,000 -> 2139 contracts: 2139*0.45 = $962.55 + fee $37.06 = $999.61 cash out.
    Net edge = (2139*0.50 - 999.61) / 999.61 = +6.99%.
    """
    d = await evaluate_kalshi_edge(45, EVEN)
    assert d.action == "BET" and d.contracts == 2139
    assert d.fee_usd == 37.06 and d.stake_usd == 999.61 and d.stake_usd <= 1000
    assert d.edge == pytest.approx((2139 * 0.5 - 999.61) / 999.61) == pytest.approx(0.0699, abs=1e-4)


async def test_kalshi_fee_can_kill_an_edge():
    # 48c vs fair 0.50: gross +4.17% (would BET on Novig) but fee 0.017472/contract -> net +0.51% -> PASS
    novig = await evaluate_market_edge({"price": 0.48}, EVEN)
    kalshi = await evaluate_kalshi_edge(48, EVEN)
    assert novig.action == "BET" and kalshi.action == "PASS"
    assert kalshi.edge == pytest.approx(0.5 / (0.48 + 0.07 * 0.48 * 0.52) - 1)
    assert "after Kalshi fee" in kalshi.reason


async def test_kalshi_small_edge_sizing_uses_exact_rounded_fee():
    # 46c vs fair 0.50: net edge ~5.0% -> Kelly small enough to stay under the cap
    d = await evaluate_kalshi_edge(46, EVEN)
    assert d.action == "BET"
    assert d.fee_usd == kalshi_taker_fee(d.contracts, 46)
    assert d.stake_usd == round(d.contracts * 0.46 + d.fee_usd, 2)


async def test_kalshi_bad_price_passes():
    assert (await evaluate_kalshi_edge(0, EVEN)).action == "PASS"


# ================================================================ maker quote maths
def test_maker_quotes_around_even_money():
    assert maker_quote_prices(0.50) == (48, 52)


@pytest.mark.parametrize("fair", [i / 200 for i in range(6, 195)])
def test_every_maker_fill_beats_the_2_5_percent_threshold(fair):
    bid, ask = maker_quote_prices(fair)
    if bid is not None:
        assert fair / (bid / 100) - 1 > 0.025
        assert bid + 1 > 100 * fair / 1.025 - 1e-9          # and it is the tightest such bid
    if ask is not None:
        assert (1 - fair) / (1 - ask / 100) - 1 > 0.025
        assert ask - 1 < 100 * (1 - (1 - fair) / 1.025) + 1e-9


@pytest.mark.parametrize("fair,expected_ask", [(0.18, 21), (0.59, 61)])
def test_maker_ask_is_strict_despite_float_noise(fair, expected_ask):
    # 100*(1-(1-fair)/1.025) is exactly 20 / 60 in real maths but lands a hair BELOW in floating point
    assert maker_quote_prices(fair)[1] == expected_ask


def test_maker_bid_is_strict_at_exact_boundary():
    # fair 0.5125 -> 0.5125/1.025 = exactly 50c, which would be EXACTLY 2.5%: must quote 49c
    assert maker_quote_prices(0.5125)[0] == 49


def test_maker_quote_size_is_capped():
    n = maker_quote_contracts(0.50, 48, "buy")               # 1/4 Kelly = $961.54
    assert n * 0.48 <= 1000
    assert maker_quote_contracts(0.90, 40, "buy") * 0.40 <= 1000    # huge edge still capped


# ================================================================ maker engine
from novig_rest import PaperOrderGateway  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_engine(fair=0.50, limit=None, gateway=None):
    targets = [MakerTarget(outcome_id="O-NYK", market_key=("NBA", "H", "A", "moneyline"), fair_prob=fair)]
    clock = Clock()
    eng = MakerEngine(gateway or PaperOrderGateway(), ExposureMonitor(limit) if limit else ExposureMonitor(),
                      lambda: targets, clock=clock, quiet_seconds=5)
    return eng, targets, clock


async def test_maker_posts_two_sided_limit_quotes_once():
    eng, _, _ = make_engine()
    await eng.refresh()
    placed = eng.gateway.placed
    assert [(o["side"], o["price_cents"], o["order_type"]) for o in placed] == [("buy", 48, "LIMIT"), ("sell", 52, "LIMIT")]
    await eng.refresh()                                       # nothing changed: no churn
    assert len(eng.gateway.placed) == 2 and len(eng.quotes) == 2


async def test_maker_requotes_when_fair_moves():
    eng, targets, _ = make_engine()
    await eng.refresh()
    targets[0] = targets[0].model_copy(update=dict(fair_prob=0.56))
    await eng.refresh()
    assert sorted(q.price_cents for q in eng.quotes.values()) == [54, 58]
    assert eng.gateway.cancel_calls and len(eng.gateway.cancel_calls[-1]) == 2


async def test_maker_bulk_cancel_is_one_call_under_200ms():
    eng, _, _ = make_engine()
    await eng.refresh()
    ids = set(eng.quotes)
    ms = await eng.cancel_all("test: websocket drop")
    assert eng.quotes == {} and eng.gateway.cancel_calls[-1] and set(eng.gateway.cancel_calls[-1]) == ids
    assert ms < MAKER_CANCEL_BUDGET_MS and eng.kill_count == 1


async def test_maker_waits_for_taker_quiet_period():
    eng, _, clock = make_engine()
    eng.note_taker_activity()
    await eng.refresh()
    assert eng.quotes == {}
    clock.t += 5.0
    await eng.refresh()
    assert len(eng.quotes) == 2


async def test_maker_pulls_everything_when_exposure_kill_switch_engages():
    eng, _, _ = make_engine()
    await eng.refresh()
    for i in range(15):
        eng.exposure.record_open(f"p{i}", 1000)
    await eng.refresh()
    assert eng.quotes == {} and eng.kill_count == 1
    await eng.refresh()
    assert eng.quotes == {}                                   # and posts nothing new while halted


async def test_maker_respects_exposure_headroom():
    eng, _, _ = make_engine(limit=1500)                       # each full-size quote risks ~$960-$1,000
    await eng.refresh()
    assert len(eng.quotes) == 1 and eng.resting_worst_case() <= 1500


async def test_maker_drops_quotes_for_ineligible_outcomes():
    eng, targets, _ = make_engine()
    await eng.refresh()
    targets.clear()
    await eng.refresh()
    assert eng.quotes == {}


async def test_maker_cancel_retries_and_keeps_quotes_if_exchange_fails():
    class Flaky(PaperOrderGateway):
        fails = 2

        async def cancel_orders(self, ids):
            if self.fails:
                self.fails -= 1
                raise ConnectionError("exchange down")
            await super().cancel_orders(ids)

    eng, _, _ = make_engine(gateway=Flaky())
    await eng.refresh()
    await eng.cancel_all("drop")
    assert len(eng.quotes) == 2                               # both attempts failed: still tracked
    await eng.cancel_all("drop again")
    assert eng.quotes == {}


def test_record_fill_never_refuses():
    m = ExposureMonitor(limit_usd=100)
    m.record_fill("f1", 150)                                  # a fill already happened: record it
    assert m.open_exposure == 150 and m.taker_halted
