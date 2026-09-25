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
    return None if p is None else LegFair(prob_yes=p, source="sharp", age_s=1.0)


def rfq(rid="r1", legs=((T1, "yes"), (T2, "yes")), contracts=20, ticker="KXMVE-COMBO-1"):
    return {"id": rid, "market_ticker": ticker, "contracts_fp": str(contracts), "status": "open",
            "mve_selected_legs": [{"market_ticker": t, "event_ticker": t.rsplit("-", 1)[0], "side": s} for t, s in legs]}


def pricer(**kw):
    return ComboPricer(ComboConfig(**kw), leg_fair)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
def test_prices_independent_legs_with_margin_growing_per_leg():
    cp = pricer().price(rfq())
    assert cp.action == "QUOTE"
    assert cp.fair == pytest.approx(0.55 * 0.60)
    assert cp.margin == pytest.approx(0.04 + 0.02 * 2)
    assert cp.yes_price == pytest.approx(round(0.33 * 1.08, 4)) and cp.no_bid == pytest.approx(1 - cp.yes_price)
    assert cp.fee == maker_fee(20, cp.no_bid)
    assert cp.expected_profit == pytest.approx(20 * (cp.yes_price - 0.33) - cp.fee, abs=1e-4)
    assert cp.roc == pytest.approx(cp.expected_profit / (20 * cp.no_bid), abs=1e-4)


def test_no_side_leg_uses_the_complement():
    cp = pricer().price(rfq(legs=((T1, "no"), (T2, "yes"))))
    assert cp.fair == pytest.approx(0.45 * 0.60)


@pytest.mark.parametrize("legs,reason", [
    (((T1, "yes"), ("KXNBAPTS-26OCT01BOSNYK-TATUM25", "yes")), "same game"),   # prop + moneyline of one game
    (((T1, "yes"), ("KXNBAGAME-26OCT01BOSNYK-BOS", "no")), "same game"),
    (((T1, "yes"), ("KXUNKNOWN-26OCT09AAABBB-X", "yes")), "no fair value"),
    (((T1, "yes"), (T2, "yes"), (T3, "yes"), ("KXNHLGAME-26OCT03NYRBOS-NYR", "yes"),
      ("KXWNBAGAME-26OCT03NYLLVA-NYL", "yes")), "legs > max"),
])
def test_rejections(legs, reason):
    cp = pricer().price(rfq(legs=legs))
    assert cp.action == "SKIP" and reason in cp.reason


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
    assert c["yes_bid"] == 0.0 and c["no_bid"] == pytest.approx(1 - round(0.33 * 1.08, 4))   # we sell the combo
    assert q.book.total() > 0                                                               # liability reserved
    q.comms.quote_status["q1"] = {"status": "accepted", "accepted_side": "yes"}
    await q.step()
    assert q.comms.confirmed == ["q1"] and rows(tmp_path, "COMBO_CONFIRM")


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
    assert calls == ["KXMVE-COMBO-1"]                     # only the combo's own expiry lookup, no leg fetches
    q.comms.quote_status["q1"] = {"status": "executed"}
    await q.step()
    q.comms.markets["KXMVE-COMBO-1"] = {"status": "settled", "result": ""}
    assert await q.check_results() == 1
    assert rows(tmp_path, "COMBO_RESULT")[0]["result"] == "void" and q.book.total() == 0
