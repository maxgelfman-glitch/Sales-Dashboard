"""
Self-test: combo (parlay) quoting via Kalshi RFQs — pricing, every rejection rule, the risk book, the quote /
last-look / confirm flow, shadow follow-up (would we have won?) and settlement, and supervisor wiring.
All against a fake Kalshi; nothing touches the exchange.
"""

import json
import time

import pytest

from combo_quoter import (
    ComboConfig,
    ComboPricer,
    ComboQuoter,
    Leg,
    LegFair,
    RiskBook,
    game_code,
    maker_fee,
)
from main_supervisor import ConfigError, build_live_supervisor, format_state_report
from research import ResearchRecorder
from research_report import build_report, combo_summary
from tests.test_kalshi_trading import kalshi_sup

T1, T2, T3 = "KXNBAGAME-26OCT01BOSNYK-NYK", "KXNFLGAME-26OCT04KCBUF-KC", "KXMLBGAME-26OCT02NYYBOS-NYY"
FAIRS = {T1: 0.55, T2: 0.60, T3: 0.50}


def leg_fair(leg):
    p = FAIRS.get(leg.market_ticker)
    return None if p is None else LegFair(prob_yes=p, source="sharp", age_s=1.0, start=time.time() + 3600)


def rfq(rid="r1", legs=((T1, "yes"), (T2, "yes")), contracts=20, ticker="KXMVE-COMBO-1"):
    return {"id": rid, "market_ticker": ticker, "contracts_fp": str(contracts), "status": "open",
            "mve_selected_legs": [{"market_ticker": t, "event_ticker": t.rsplit("-", 1)[0], "side": s} for t, s in legs]}


def pricer(**kw):
    return ComboPricer(ComboConfig(**kw), leg_fair)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
def _yes(fair, margin, rate=0.035):
    import math
    base = fair * (1 + margin)
    return math.ceil((base + rate * base * (1 - base)) * 10_000 - 1e-6) / 10_000     # rounded UP


def test_prices_independent_legs_with_margin_growing_per_leg():
    cp = pricer().price(rfq())                               # NBA leg + NFL leg, different games
    assert cp.action == "QUOTE"
    assert cp.fair == pytest.approx(0.55 * 0.60)
    age = 2 * 0.01 * 1.0 / 30                                            # two legs, 1 s old
    assert cp.margin == pytest.approx(0.04 + 0.02 * 2 + 0.005 + age)    # + NBA's league margin; NFL adds none
    assert cp.yes_price == pytest.approx(_yes(0.33, cp.margin)) and cp.no_bid == pytest.approx(1 - cp.yes_price)
    assert cp.slice == "NBA+NFL:2L:x" and not cp.fee_exempt
    assert cp.fee == maker_fee(20, cp.no_bid, 0.035)
    assert cp.expected_profit == pytest.approx(20 * (cp.yes_price - 0.33) - cp.fee, abs=1e-4)
    assert cp.roc == pytest.approx(cp.expected_profit / (20 * cp.no_bid), abs=1e-4)


def test_no_side_leg_uses_the_complement():
    cp = pricer().price(rfq(legs=((T1, "no"), (T2, "yes"))))
    assert cp.fair == pytest.approx(0.45 * 0.60)


@pytest.mark.parametrize("legs,reason", [
    (((T1, "yes"), ("KXUNKNOWN-26OCT09AAABBB-X", "yes")), "no fair value"),
    (((T1, "yes"), (T2, "yes"), (T3, "yes"), ("KXNHLGAME-26OCT03NYRBOS-NYR", "yes"),
      ("KXWNBAGAME-26OCT03NYLLVA-NYL", "yes")), "legs > max"),
])
def test_rejections(legs, reason):
    cp = pricer(max_legs=4).price(rfq(legs=legs))
    assert cp.action == "SKIP" and reason in cp.reason


def test_same_game_legs_are_never_priced_as_independent():
    prop = "KXNBAPTS-26OCT01BOSNYK-TATUM25"
    pr = ComboPricer(ComboConfig(), lambda leg: LegFair(prob_yes=0.5, source="sharp", age_s=1.0))
    cp = pr.price(rfq(legs=((T1, "yes"), (prop, "yes"))))     # prop + moneyline of one game
    assert cp.action == "SKIP" and "joint probability" in cp.reason
    assert pr.live_allowed(pr.price(rfq())) is None                      # different games: may go live


@pytest.mark.parametrize("legs", [
    (("KXNFLGAME-26OCT04KCBUF-KC", "yes"), ("KXNFLGAME-26OCT04KCBUF-BUF", "no")),   # the same outcome twice
    (("KXNFLGAME-26OCT04KCBUF-KC", "yes"), ("KXNFLGAME-26OCT04KCBUF-KC", "yes")),   # a duplicate leg
    (("KXNFLGAME-26OCT04KCBUF-KC", "yes"), ("KXNFLSPREAD-26OCT04KCBUF-KC3", "yes")),  # ML + covering spread
])
def test_nested_duplicate_and_mirrored_legs_are_declined(legs):
    # Product of fairs: 0.6*0.6 = 0.36 vs the true 0.60 for the same outcome twice: a -32% return if quoted
    pr = ComboPricer(ComboConfig(), lambda leg: LegFair(prob_yes=0.6, source="sharp", age_s=1.0))
    assert pr.price(rfq(legs=legs)).action == "SKIP"


def test_same_game_parlays_cannot_be_enabled():
    from main_supervisor import ConfigError, parlay_config
    with pytest.raises(ConfigError, match="joint probability"):
        parlay_config({"COMBO_LIVE_KINDS": "xgame,sgp"}, "shadow", None, "kalshi")


def test_games_of_different_leagues_with_the_same_code_are_different_games():
    from combo_quoter import Leg, leg_game
    nfl, nhl = Leg("KXNFLGAME-26NOV01DETMIN-DET", "", "yes"), Leg("KXNHLGAME-26NOV01DETMIN-DET", "", "yes")
    assert leg_game(nfl) != leg_game(nhl)
    pr = ComboPricer(ComboConfig(), lambda leg: LegFair(prob_yes=0.5, source="sharp", age_s=1.0))
    assert pr.price(rfq(legs=((nfl.market_ticker, "yes"), (nhl.market_ticker, "yes")))).action == "QUOTE"


def test_a_fractional_rfq_size_is_never_booked_as_a_smaller_one():
    from combo_quoter import rfq_contracts
    assert rfq_contracts({"contracts_fp": "10.5"}, 0.4) == 0             # skipped, not booked as 10
    assert rfq_contracts({"contracts_fp": "10.00"}, 0.4) == 10


def test_nfl_cross_game_parlays_pay_no_maker_fee():
    t4 = "KXNFLGAME-26OCT04DALPHI-DAL"
    pr = ComboPricer(ComboConfig(), lambda leg: LegFair(prob_yes=0.6, source="sharp", age_s=0.0))
    cp = pr.price(rfq(legs=((T2, "yes"), (t4, "yes"))))
    assert cp.fee_exempt and cp.fee == 0 and cp.slice == "NFL:2L:x"
    assert cp.yes_price == pytest.approx(round(0.36 * (1 + 0.08), 4))


def test_uncertainty_widens_margin_and_cuts_size():
    sure = ComboPricer(ComboConfig(), lambda leg: LegFair(0.5, "sharp", 0.0, spread=0.0, books=3)).price(rfq())
    unsure = ComboPricer(ComboConfig(), lambda leg: LegFair(0.5, "sharp", 25.0, spread=0.02, books=2)).price(rfq())
    single = ComboPricer(ComboConfig(), lambda leg: LegFair(0.5, "sharp", 0.0, books=1)).price(rfq())
    assert unsure.margin > single.margin > sure.margin
    assert unsure.size_factor < sure.size_factor
    split = ComboPricer(ComboConfig(), lambda leg: LegFair(0.5, "sharp", 0.0, spread=0.05, books=2)).price(rfq())
    assert split.action == "SKIP" and "disagree" in split.reason


def test_sportsbook_price_is_a_floor_when_unsure_and_a_tripwire():
    unsure_leg = lambda leg: LegFair(0.5, "sharp", 0.0, books=1)   # noqa: E731
    pr = ComboPricer(ComboConfig(sportsbook_when_uncertain=0.0), unsure_leg, sportsbook_price=lambda legs: 0.33)
    cp = pr.price(rfq())
    assert cp.yes_price == pytest.approx(0.33 * 0.97, abs=1e-4)          # no tighter than the book, minus 3%
    wrong = ComboPricer(ComboConfig(), unsure_leg, sportsbook_price=lambda legs: 0.20).price(rfq())
    assert wrong.action == "SKIP" and "sportsbook" in wrong.reason       # our fair 0.25 > book's 0.20


def test_winning_requesters_get_wider_quotes_then_none():
    cfg = ComboConfig(requester_min_trades=3)
    pr = ComboPricer(cfg, leg_fair)
    base = pr.price({**rfq(), "creator_id": "sharp1"}).margin
    for _ in range(3):
        pr.requesters.add("sharp1", 1.0, 10.0)                          # +10% ROI as a taker
    assert pr.price({**rfq(), "creator_id": "sharp1"}).margin == pytest.approx(base + cfg.requester_widen_margin)
    pr.requesters.add("sharp1", 30.0, 10.0)
    cp = pr.price({**rfq(), "creator_id": "sharp1"})
    assert cp.action == "SKIP" and "requester" in cp.reason
    assert pr.price({**rfq(), "creator_id": "retail9"}).margin == pytest.approx(base)


def test_stale_legs_horizon_price_band_and_longshot_trap():
    stale = ComboPricer(ComboConfig(), lambda leg: LegFair(prob_yes=0.5, source="sharp", age_s=99))
    assert "stale" in stale.price(rfq()).reason
    assert "horizon" in pricer().price(rfq(), expires_at=time.time() + 5 * 86400).reason
    longshot = ComboPricer(ComboConfig(), lambda leg: LegFair(prob_yes=0.1, source="sharp", age_s=1))
    cp = longshot.price(rfq(legs=((T1, "yes"), (T2, "yes"), (T3, "yes"))))   # fair 0.001: the 0.1c trap
    assert cp.action == "SKIP" and "outside band" in cp.reason
    thin = pricer(min_roc=0.5).price(rfq())
    assert thin.action == "SKIP" and "return on collateral" in thin.reason


def test_game_code_extraction():
    assert game_code("KXNBAPTS-26OCT01BOSNYK-TATUM25") == "26OCT01BOSNYK"
    assert game_code("KXMVE-COMBO") is None


# ---------------------------------------------------------------------------
# Risk book
# ---------------------------------------------------------------------------
def test_risk_book_caps_per_combo_per_leg_and_total():
    cfg = ComboConfig(max_loss_per_combo=25, max_leg_exposure=30, max_total_liability=40)
    book, pr = RiskBook(cfg), ComboPricer(cfg, leg_fair)
    a = pr.price(rfq(contracts=20))                                        # ~ $13.6 max loss
    assert book.check(a) is None
    book.add("q1", a)
    b = pr.price(rfq("r2", contracts=20))
    assert book.check(b) is None
    book.add("q2", b)
    c = pr.price(rfq("r3", contracts=20))                                  # same legs again: leg cap hit
    assert "leg" in book.check(c)
    big = pr.price(rfq("r4", legs=((T3, "yes"),), contracts=200))
    assert "per combo" in book.check(big)
    other = pr.price(rfq("r5", legs=((T3, "yes"), ("KXNHLGAME-26OCT03NYRBOS-NYR", "yes")), contracts=40))
    FAIRS["KXNHLGAME-26OCT03NYRBOS-NYR"] = 0.5
    other = pr.price(rfq("r5", legs=((T3, "yes"), ("KXNHLGAME-26OCT03NYRBOS-NYR", "yes")), contracts=30))
    assert "total liability" in book.check(other)
    FAIRS.pop("KXNHLGAME-26OCT03NYRBOS-NYR")


# ---------------------------------------------------------------------------
# The quote / accept / last look / confirm flow against a fake Kalshi
# ---------------------------------------------------------------------------
class FakeComms:
    def __init__(self):
        self.rfqs, self.quote_status, self.created, self.deleted, self.confirmed = [], {}, [], [], []
        self.trade_rows, self.results, self.markets = {}, {}, {}
        self.n = 0

    async def open_rfqs(self):
        return list(self.rfqs)

    async def create_quote(self, rfq_id, yes_bid, no_bid):
        self.n += 1
        qid = f"q{self.n}"
        self.created.append(dict(id=qid, rfq_id=rfq_id, yes_bid=yes_bid, no_bid=no_bid))
        self.quote_status[qid] = {"status": "open"}
        return qid

    async def get_quote(self, qid):
        return self.quote_status[qid]

    async def delete_quote(self, qid):
        self.deleted.append(qid)

    async def confirm_quote(self, qid):
        self.confirmed.append(qid)

    async def market(self, ticker):
        return self.markets.get(ticker, {"result": self.results.get(ticker, "")})

    async def trades(self, ticker, min_ts=None):
        return self.trade_rows.get(ticker, [])

    async def get_rfq(self, rid):
        self.rfq_lookups = getattr(self, "rfq_lookups", 0) + 1
        return next((r for r in self.rfqs if r["id"] == rid), {})


def quoter(tmp_path, mode="demo", **kw):
    clock = {"t": 1_000_000.0}
    q = ComboQuoter(ComboConfig(mode=mode, followup_after_s=10, **kw), FakeComms(), leg_fair,
                    research=ResearchRecorder(tmp_path, clock=lambda: clock["t"]), clock=lambda: clock["t"])
    return q, clock


def rows(tmp_path, kind=None):
    out = [json.loads(x) for p in sorted(tmp_path.glob("research-*.jsonl")) for x in p.read_text().splitlines()]
    return [r for r in out if kind is None or r["kind"] == kind]


async def test_demo_quote_then_confirm_on_unchanged_fairs(tmp_path):
    q, clock = quoter(tmp_path)
    q.comms.rfqs = [rfq()]
    await q.step()
    [c] = q.comms.created
    assert c["yes_bid"] == 0.0 and c["no_bid"] == pytest.approx(1 - _yes(0.33, 0.085 + 2 * 0.01 / 30))   # we sell it
    assert q.book.total() == 0                                      # nothing reserved until a quote wins
    q.comms.quote_status["q1"] = {"status": "accepted", "accepted_side": "yes"}
    await q.step()
    assert q.comms.confirmed == ["q1"] and rows(tmp_path, "COMBO_CONFIRM")
    assert q.book.total() > 0                                       # reserved at the last look


async def test_last_look_declines_when_a_leg_moved(tmp_path):
    q, clock = quoter(tmp_path)
    q.comms.rfqs = [rfq()]
    await q.step()
    FAIRS[T1] = 0.70                                                         # injury news: leg now much likelier
    try:
        q.comms.quote_status["q1"] = {"status": "accepted", "accepted_side": "yes"}
        await q.step()
    finally:
        FAIRS[T1] = 0.55
    assert q.comms.confirmed == [] and q.comms.deleted == ["q1"] and q.book.total() == 0
    [d] = rows(tmp_path, "COMBO_DECLINE")
    assert "moved" in d["reason"]


async def test_quotes_expire_and_release_liability(tmp_path):
    q, clock = quoter(tmp_path, quote_ttl_s=5)
    q.comms.rfqs = [rfq()]
    await q.step()
    clock["t"] += 6
    await q.step()
    assert q.comms.deleted == ["q1"] and q.book.total() == 0


async def test_executed_position_settles(tmp_path):
    q, clock = quoter(tmp_path)
    q.comms.rfqs = [rfq()]
    await q.step()
    q.comms.quote_status["q1"] = {"status": "executed"}
    await q.step()
    assert "q1" in q.positions
    q.comms.results["KXMVE-COMBO-1"] = "no"                                  # combo missed: we keep the premium
    assert await q.check_results() == 1
    [r] = rows(tmp_path, "COMBO_RESULT")
    cp = q.pricer.price(rfq())
    assert r["pnl"] == pytest.approx(round(20 * (1 - cp.no_bid) - cp.fee, 2)) and q.book.total() == 0


async def test_kill_switch_blocks_new_quotes(tmp_path):
    q, _ = quoter(tmp_path)
    q.allowed = lambda: "daily loss stop"
    q.comms.rfqs = [rfq()]
    await q.step()
    assert q.comms.created == [] and rows(tmp_path, "COMBO_RFQ")[0]["reason"] == "daily loss stop"


# ---------------------------------------------------------------------------
# Shadow mode: would we have won, and did it pay?
# ---------------------------------------------------------------------------
async def test_shadow_follow_up_and_settlement(tmp_path):
    q, clock = quoter(tmp_path, mode="shadow")
    q.comms.rfqs = [rfq("r1", ticker="C1"), rfq("r2", ticker="C2", legs=((T1, "yes"), (T3, "yes")))]
    await q.step()
    assert q.comms.created == []                                              # shadow never quotes
    q.comms.trade_rows = {"C1": [{"yes_price_dollars": "0.3900"}],            # we'd have offered ~0.356: we win
                          "C2": [{"yes_price_dollars": "0.2800"}]}            # we'd have offered ~0.297: we lose
    clock["t"] += 11
    await q.step()
    trades = {r["market_ticker"]: r for r in rows(tmp_path, "COMBO_TRADE")}
    assert trades["C1"]["would_win"] is True and trades["C2"]["would_win"] is False
    q.comms.results["C1"] = "yes"                                             # the combo hit: we'd have paid out
    await q.check_results()
    [res] = rows(tmp_path, "COMBO_RESULT")
    assert res["pnl"] < 0 and res["position_type"] == "shadow"
    summary = combo_summary(rows(tmp_path))
    assert summary["rfqs"] == 2 and summary["would_win"] == 1 and summary["win_rate"] == 0.5
    assert "[COMBO QUOTING]" in build_report(rows(tmp_path))


# ---------------------------------------------------------------------------
# Supervisor wiring
# ---------------------------------------------------------------------------
def test_supervisor_prices_kalshi_game_legs_from_the_sharp_line(tmp_path):
    q, _ = quoter(tmp_path, mode="shadow")
    sup = kalshi_sup(combo_quoter=q)
    lf = sup.combo_leg_fair(Leg("KXNBAGAME-BOSNYK-NYK", "KXNBAGAME-BOSNYK", "yes"))
    assert lf.source == "sharp" and lf.prob_yes == pytest.approx(0.5217, abs=1e-3)
    assert lf.game == ("NBA", "New York Knicks", "Boston Celtics")
    assert "combo_quoter" in sup._task_factories
    sup.loss_halted_day = sup._utc_day()
    assert q.allowed() == "daily loss stop"


def test_config(tmp_path):
    with pytest.raises(ConfigError):
        build_live_supervisor({"COMBO_QUOTER": "shadow", "RESEARCH_ENABLED": "0"})     # needs Kalshi keys
    with pytest.raises(ConfigError):
        build_live_supervisor({"COMBO_QUOTER": "sideways", "RESEARCH_ENABLED": "0"})
    from cryptography.hazmat.primitives import serialization
    from tests.test_kalshi_trading import KEY
    pem = tmp_path / "k.pem"
    pem.write_bytes(KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    base = {"KALSHI_KEY_ID": "k", "KALSHI_PRIVATE_KEY_PATH": str(pem), "RESEARCH_DIR": str(tmp_path)}
    with pytest.raises(ConfigError):
        build_live_supervisor({**base, "COMBO_QUOTER": "live"})                          # paper mode
    sup = build_live_supervisor({**base, "COMBO_QUOTER": "shadow", "COMBO_MAX_LEGS": "3"})
    assert sup.combo.cfg.mode == "shadow" and sup.combo.cfg.max_legs == 3 and sup.combo.research is not None
    assert "SHADOW: <= 3 legs" in format_state_report(sup)


async def test_void_settlement_clears_the_book_and_sharp_legs_need_no_api_calls(tmp_path):
    q, clock = quoter(tmp_path)
    calls = []
    orig = q.comms.market

    async def counting(ticker):
        calls.append(ticker)
        return await orig(ticker)
    q.comms.market = counting
    q.comms.rfqs = [rfq()]
    await q.step()
    assert calls == []                                    # legs carry start times: no lookups at all
    q.comms.quote_status["q1"] = {"status": "executed"}
    await q.step()
    q.comms.markets["KXMVE-COMBO-1"] = {"status": "settled", "result": ""}
    assert await q.check_results() == 1
    assert rows(tmp_path, "COMBO_RESULT")[0]["result"] == "void" and q.book.total() == 0


def test_report_breaks_results_down_by_slice_and_flags_suspiciously_cheap_quotes():
    rows_ = [
        {"kind": "COMBO_RFQ", "action": "QUOTE", "slice": "NFL:2L:x", "rfq_id": "a", "expected_profit": 1.0},
        {"kind": "COMBO_RFQ", "action": "QUOTE", "slice": "NBA:2L:sgp", "rfq_id": "b", "expected_profit": 1.0},
        {"kind": "COMBO_TRADE", "slice": "NFL:2L:x", "rfq_id": "a", "traded_yes_price": 0.40, "our_yes_price": 0.38,
         "would_win": True, "margin_vs_winner": 0.12},
        {"kind": "COMBO_TRADE", "slice": "NBA:2L:sgp", "rfq_id": "b", "traded_yes_price": 0.40,
         "our_yes_price": 0.30, "would_win": True, "margin_vs_winner": 0.30},
        {"kind": "COMBO_RESULT", "slice": "NFL:2L:x", "pnl": 7.5, "expected_profit": 1.0},
    ]
    c = combo_summary(rows_)
    assert c["far_below_market"] == 1                                    # 0.30 vs a 0.40 trade
    assert c["by_slice"]["kalshi NFL:2L:x"]["win_rate"] == 1 and c["by_slice"]["kalshi NFL:2L:x"]["pnl"] == 7.5
    assert c["by_slice"]["kalshi NBA:2L:sgp"]["below"] == 1


def test_one_game_cannot_carry_more_than_its_cap_across_parlays():
    cfg = ComboConfig(max_loss_per_combo=100, max_leg_exposure=1000, max_total_liability=1000, max_game_exposure=20)
    book, pr = RiskBook(cfg), ComboPricer(cfg, leg_fair)
    a = pr.price(rfq(contracts=20))                                   # T1 (BOS-NYK) + T2 (KC-BUF), ~$13.6
    book.add("a", a)
    other_nyk = "KXNBAGAME-26OCT01BOSNYK-BOS"                         # a different leg, same game
    FAIRS[other_nyk] = 0.45
    try:
        b = pr.price(rfq(legs=((other_nyk, "yes"), (T3, "yes")), contracts=20))
        assert "game NBA:26OCT01BOSNYK exposure" in book.check(b)
    finally:
        FAIRS.pop(other_nyk)


async def test_two_wins_at_once_cannot_both_slip_under_the_cap(tmp_path):
    q, clock = quoter(tmp_path, max_total_liability=20)               # room for one ~$13.6 parlay, not two
    q.comms.rfqs = [rfq("r1"), rfq("r2", ticker="KXMVE-COMBO-2")]
    await q.step()
    assert len(q.comms.created) == 2                                  # both quoted (nothing reserved yet)
    q.comms.quote_status["q1"] = {"status": "accepted", "accepted_side": "yes"}
    q.comms.quote_status["q2"] = {"status": "accepted", "accepted_side": "yes"}
    await q.step()
    assert len(q.comms.confirmed) == 1 and len(q.comms.deleted) == 1  # the second fails the cap at last look


async def test_restart_restores_open_short_parlays_into_the_total_cap(tmp_path):
    q, clock = quoter(tmp_path)

    async def positions():
        return [{"ticker": "KXMVE-COMBO-9", "position_fp": "-40", "market_exposure_dollars": "26.40"},
                {"ticker": "KXNBAGAME-26OCT01BOSNYK-NYK", "position_fp": "10", "market_exposure_dollars": "5"}]
    q.comms.positions = positions
    q.comms.markets["KXMVE-COMBO-9"] = {"mve_selected_legs": [{"market_ticker": T1, "event_ticker": "E", "side": "yes"}]}
    assert await q.restore() == pytest.approx(26.40) and q.book.total() == pytest.approx(26.40)
    assert q.book.leg_exposure(Leg(T1, "E", "yes")) == pytest.approx(26.40)      # the leg cap sees it


async def test_unpriceable_legs_are_looked_up_within_a_budget(tmp_path):
    q, clock = quoter(tmp_path, book_fetches_per_step=3)
    calls = []

    async def market(ticker):
        calls.append(ticker)
        return {}
    q.comms.market = market
    q.comms.rfqs = [rfq(f"r{i}", legs=((T1, "yes"), (f"KXNBAPTS-26OCT0{i}XXXYYY-P", "yes"))) for i in range(1, 8)]
    await q.step()
    assert len(calls) <= 3                                            # not one lookup per prop per RFQ


async def test_open_quotes_are_capped(tmp_path):
    q, clock = quoter(tmp_path, max_open_quotes=2, max_total_liability=10_000)
    q.comms.rfqs = [rfq(f"r{i}", ticker=f"KXMVE-C{i}") for i in range(5)]
    await q.step()
    assert len(q.comms.created) == 2


# ---------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------
def test_a_blocked_leg_never_falls_back_to_kalshis_book(tmp_path):
    from combo_quoter import LEG_BLOCKED
    q, clock = quoter(tmp_path)
    q.pricer.leg_fair = q._leg_fair_with_fallback(lambda leg: LEG_BLOCKED)
    q._book_cache[T1] = (clock["t"], LegFair(0.51, "kalshi_book", 0.0))
    assert q.pricer.leg_fair(Leg(T1, "", "yes")) is None


def test_book_fairs_report_their_real_age(tmp_path):
    q, clock = quoter(tmp_path)
    q.pricer.leg_fair = q._leg_fair_with_fallback(lambda leg: None)
    q._book_cache["KXPROP-X"] = (clock["t"], LegFair(0.5, "kalshi_book", 0.0, spread=0.01))
    clock["t"] += 600
    assert q.pricer.leg_fair(Leg("KXPROP-X", "", "yes")).age_s == pytest.approx(600)


def test_book_priced_legs_are_shadow_only_live():
    pr = ComboPricer(ComboConfig(), lambda leg: LegFair(0.5, "kalshi_book", 0.0, start=time.time() + 3600))
    cp = pr.price(rfq())
    assert cp.action == "QUOTE" and "Kalshi's own book" in pr.live_allowed(cp)


def test_legs_need_an_explicit_side():
    bad = {**rfq(), "mve_selected_legs": [{"market_ticker": T1, "event_ticker": "E1"},
                                          {"market_ticker": T2, "event_ticker": "E2", "side": "yes"}]}
    assert "side" in pricer().price(bad).reason


def test_same_game_groups_merge_transitively():
    a, b, c = "KXNBAGAME-26OCT01BOSNYK-NYK", "KXNBASPREAD-X-1", "KXNBATOTAL-26OCT01BOSNYK-O"
    game = ("NBA", "NYK", "BOS")
    fairs = {a: LegFair(0.5, "sharp", 0, start=time.time() + 3600),
             b: LegFair(0.5, "sharp", 0, game=game, start=time.time() + 3600),
             c: LegFair(0.5, "sharp", 0, game=game, start=time.time() + 3600)}
    pr = ComboPricer(ComboConfig(), lambda leg: fairs[leg.market_ticker])
    # b shares the canonical game with c, c shares the ticker game code with a: all one game, so declined
    assert pr.price(rfq(legs=((a, "yes"), (b, "yes"), (c, "yes")))).action == "SKIP"


async def test_last_look_counts_the_maker_fee(tmp_path):
    q, clock = quoter(tmp_path, last_look_min_margin=0.09)              # NBA+NFL: fee applies
    q.comms.rfqs = [rfq()]
    await q.step()
    q.comms.quote_status["q1"] = {"status": "accepted", "accepted_side": "yes"}
    await q.step()                                    # 8.6% margin net of the fee (10.9% if the fee counted)
    assert q.comms.confirmed == [] and q.comms.deleted == ["q1"]


async def test_a_failed_confirm_keeps_the_reservation_and_other_quotes_proceed(tmp_path):
    q, clock = quoter(tmp_path)
    q.comms.rfqs = [rfq("r1"), rfq("r2", ticker="KXMVE-COMBO-2")]
    await q.step()

    async def boom(qid):
        if qid == "q1":
            raise RuntimeError("timeout")
        q.comms.confirmed.append(qid)
    q.comms.confirm_quote = boom
    q.comms.quote_status["q1"] = {"status": "accepted", "accepted_side": "yes"}
    q.comms.quote_status["q2"] = {"status": "accepted", "accepted_side": "yes"}
    await q.step()
    assert q.comms.confirmed == ["q2"] and "q1" in q.book.open          # q1 may have gone through: kept
    await q.step()
    assert q.comms.confirmed == ["q2"]                                  # never confirmed twice


def test_parlays_are_held_to_the_canary_and_hard_ceilings(tmp_path):
    from main_supervisor import parlay_config

    class Plan:
        scaled_up = False
    cfg = parlay_config({"COMBO_MAX_TOTAL_LIABILITY": "5000"}, "live", Plan(), "kalshi")
    assert cfg.max_total_liability == 100 and cfg.max_loss_per_combo == 10
    Plan.scaled_up = True
    assert parlay_config({"COMBO_MAX_TOTAL_LIABILITY": "5000"}, "live", Plan(), "kalshi").max_total_liability == 5000
    for bad in ("nan", "inf", "20000", "-1"):
        with pytest.raises(ConfigError):
            parlay_config({"COMBO_MAX_TOTAL_LIABILITY": bad}, "shadow", None, "kalshi")



async def test_websocket_pushes_rfqs_wins_and_executions_without_polling(tmp_path):
    import json as _json
    from combo_quoter import KalshiCommsFeed
    from cryptography.hazmat.primitives.asymmetric import rsa
    ws = KalshiCommsFeed("wss://demo/trade-api/ws/v2", "kid", rsa.generate_private_key(public_exponent=65537,
                                                                                       key_size=2048))
    clock = {"t": 1_000_000.0}
    q = ComboQuoter(ComboConfig(mode="demo", followup_after_s=10), FakeComms(), leg_fair,
                    research=ResearchRecorder(tmp_path, clock=lambda: clock["t"]), clock=lambda: clock["t"], ws=ws)
    ws.feed.connected.set()                                          # push mode: no polling of open RFQs
    q.comms.rfqs = [rfq("r1")]
    await ws.dispatch(_json.dumps({"type": "rfq_created", "msg": {"rfq_id": "r1", "market_ticker": "KXMVE-COMBO-1"}}))
    assert q.comms.rfq_lookups == 1 and len(q.comms.created) == 1     # legs read once, then quoted
    await ws.dispatch(_json.dumps({"type": "quote_accepted", "msg": {"quote_id": "q1", "accepted_side": "yes"}}))
    assert q.comms.confirmed == ["q1"]                                # last look on the event itself
    await ws.dispatch(_json.dumps({"type": "quote_executed", "msg": {"quote_id": "q1"}}))
    assert "q1" in q.positions and "q1" not in q.quotes
    await q.step()                                                    # a poll in push mode reads no RFQ list
    assert len(q.comms.created) == 1


def test_slice_verdicts_need_statistical_evidence():
    from research_report import slice_verdict, wilson
    lo, hi = wilson(12, 200)
    assert lo < 0.06 < hi                                             # 6% of 200 is not "confidently above 5%"
    base = dict(traded=1000, wins=120, win_ci=wilson(120, 1000), median_margin=0.10, settled=150, pnl=500.0,
                pnl_t=1.8, below=5)
    assert slice_verdict(base) == "PASS"
    assert slice_verdict({**base, "traded": 150}) == "WAIT"
    assert slice_verdict({**base, "below": 60}) == "FAIL"              # winning mostly by underpricing
    assert slice_verdict({**base, "pnl": -900.0, "pnl_t": -2.5}) == "FAIL"
    assert slice_verdict({**base, "settled": 40}) == "WATCH"


def test_unknown_requesters_can_carry_an_extra_cushion():
    from combo_quoter import ComboConfig, RequesterBook
    book = RequesterBook(ComboConfig(requester_unknown_margin=0.02, requester_min_trades=2))
    assert book.adjust("new") == (0.02, None) and book.adjust(None) == (0.02, None)
    book.add("reg", -10.0, 100.0)
    book.add("reg", -10.0, 100.0)                                       # a losing regular: no cushion needed
    assert book.adjust("reg") == (0.0, None)
    assert RequesterBook(ComboConfig()).adjust("new") == (0.0, None)   # off by default


def test_unknown_requester_margin_is_bounded():
    from main_supervisor import ConfigError, parlay_config
    assert parlay_config({"COMBO_UNKNOWN_REQUESTER_MARGIN": "0.02"}, "shadow", None, "kalshi") \
        .requester_unknown_margin == 0.02
    with pytest.raises(ConfigError):
        parlay_config({"COMBO_UNKNOWN_REQUESTER_MARGIN": "0.5"}, "shadow", None, "kalshi")
