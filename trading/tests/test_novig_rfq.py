"""Novig parlay pricer: shadow learning, quoting within caps, the 1-second last look, settlement, data summary."""

import json

import pytest

from combo_quoter import ComboConfig, LegFair
from main_supervisor import ConfigError, build_live_supervisor
from novig_data import summarise
from novig_rfq import NovigRfqAuth, NovigRfqQuoter, settlement_pnl
from research import ResearchRecorder

# outcome id -> (event id, league/game, fair)
LEGS = {"kc": ("ev-kcbuf", ("NFL", "KC", "BUF"), 0.60), "dal": ("ev-dalphi", ("NFL", "PHI", "DAL"), 0.55),
        "kc-over": ("ev-kcbuf", ("NFL", "KC", "BUF"), 0.50)}
FAIRS = {k: v[2] for k, v in LEGS.items()}


def lookup(oid):
    if oid not in LEGS:
        return None
    ev, game, _ = LEGS[oid]
    return ev, LegFair(prob_yes=FAIRS[oid], source="sharp", age_s=0.0, game=game, spread=0.0, books=2)


class WS:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(json.loads(msg))


def quoter(tmp_path, mode="qa", **kw):
    cfg = ComboConfig(mode=mode, max_loss_per_combo=100, max_leg_exposure=150, max_total_liability=400, **kw)
    q = NovigRfqQuoter(cfg, NovigRfqAuth("tok"), leg_lookup=lookup, research=ResearchRecorder(tmp_path))
    q._ws_conn = WS()
    return q


def rows(tmp_path, kind):
    out = [json.loads(x) for p in sorted(tmp_path.glob("research-*.jsonl")) for x in p.read_text().splitlines()]
    return [r for r in out if r["kind"] == kind]


def created(rid="r1", legs=("kc", "dal"), min_wager="10"):
    return json.dumps({"event": "rfq_created", "data": {"rfq_id": rid, "outcome_ids": list(legs),
                                                         "min_wager": min_wager, "expires_at": "x"}})


async def test_shadow_prices_every_round_and_learns_from_executions(tmp_path):
    q = quoter(tmp_path, mode="shadow")
    await q._handle_raw(created())
    assert q._ws_conn.sent == []                                         # shadow never quotes
    [r] = rows(tmp_path, "COMBO_RFQ")
    assert r["action"] == "QUOTE" and r["venue"] == "novig" and r["slice"] == "NFL:2L:x"
    assert r["fair"] == pytest.approx(0.33) and r["yes_price"] * 1000 == pytest.approx(round(r["yes_price"] * 1000))
    await q._handle_raw(json.dumps({"event": "rfq_executed", "data": {"rfq_id": "r1", "price": "0.40", "wager": "50"}}))
    [t] = rows(tmp_path, "COMBO_TRADE")
    assert t["would_win"] is True and t["margin_vs_winner"] == pytest.approx(0.40 / 0.33 - 1, abs=1e-4)


async def test_quote_within_caps_then_confirm_and_settle(tmp_path):
    q = quoter(tmp_path)
    await q._handle_raw(created())
    [sub] = q._ws_conn.sent
    assert sub["event"] == "create_quote"
    price, wager = float(sub["data"]["price"]), float(sub["data"]["max_wager"])
    assert price > 0.33 and len(sub["data"]["price"]) == 5                  # 0.xxx, rounded up
    assert wager * (1 - price) / price <= 100 + 0.01                      # collateral within the per-combo cap
    assert q.book.total() == 0                                            # reserved only when a quote wins
    await q._handle_raw(json.dumps({"event": "quote_accepted", "data": {
        "rfq_id": "r1", "quote_id": "nv-q1", "wager": "20", "price": sub["data"]["price"]}}))
    confirm = q._ws_conn.sent[-1]
    assert confirm == {"event": "confirm", "data": {"rfq_id": "r1", "quote_id": "nv-q1", "confirmed": True}}
    assert q.book.total() == pytest.approx(20 * (1 - float(sub["data"]["price"])) / float(sub["data"]["price"]),
                                           abs=0.02)
    await q._handle_raw(json.dumps({"event": "quote_executed", "data": {"rfq_id": "r1", "wager": "20",
                                                                         "price": sub["data"]["price"]}}))
    assert "r1" in q.positions

    class Rest:
        async def executions(self, status=None, limit=500):
            return [{"rfq_id": "r1", "status": "settled", "result": "loss", "wager": "20",
                     "price": sub["data"]["price"]}]
    q.rest = Rest()
    assert await q.check_results() == 1
    [res] = rows(tmp_path, "COMBO_RESULT")
    assert res["pnl"] == pytest.approx(20.0, abs=0.02) and q.book.total() == 0   # the parlay lost: we keep the stake


async def test_last_look_declines_when_a_leg_moved(tmp_path):
    q = quoter(tmp_path)
    await q._handle_raw(created())
    price = q._ws_conn.sent[0]["data"]["price"]
    FAIRS["kc"] = 0.75                                                    # news: the parlay is now far likelier
    try:
        await q._handle_raw(json.dumps({"event": "quote_accepted", "data": {
            "rfq_id": "r1", "quote_id": "nv-q1", "wager": "20", "price": price}}))
    finally:
        FAIRS["kc"] = 0.60
    assert q._ws_conn.sent[-1]["data"]["confirmed"] is False and q.book.total() == 0
    assert "moved" in rows(tmp_path, "COMBO_DECLINE")[0]["reason"]


async def test_same_game_parlays_and_tiny_caps_are_declined_live(tmp_path):
    q = quoter(tmp_path)
    await q._handle_raw(created(legs=("kc", "kc-over")))                  # same game: shadow-only
    assert q._ws_conn.sent[-1]["event"] == "decline"
    assert "shadow-only" in rows(tmp_path, "COMBO_RFQ")[-1]["reason"]
    small = quoter(tmp_path / "s")
    small.cfg.max_loss_per_combo = 2                                     # caps allow < the $10 minimum stake
    await small._handle_raw(created())
    assert small._ws_conn.sent[-1]["event"] == "decline"


@pytest.mark.parametrize("result,extra,pnl", [("loss", {}, 42.0), ("win", {}, -58.0),
                                              ("fmv", {"fmv_value": "0.5"}, -8.0), ("push", {}, 0.0)])
def test_settlement_from_the_sellers_side(result, extra, pnl):
    row = {"status": "settled", "result": result, "wager": "42", "price": "0.42", "liability": "58", **extra}
    assert settlement_pnl(row) == pytest.approx(pnl)                    # $100 pot; fmv 0.5: user gets $50
    assert settlement_pnl({**row, "status": "open"}) is None


def test_public_data_summary():
    rows_ = [
        {"timestamp": "2026-10-01T18:00:00Z", "marketId": "c1", "tradeType": "COMBO", "legs": "3", "cost": "50",
         "qty": "200", "side": "TAKER"},
        {"timestamp": "2026-10-01T18:00:00Z", "marketId": "c1", "tradeType": "COMBO", "legs": "3", "cost": "150",
         "qty": "200", "side": "MAKER"},
        {"timestamp": "2026-10-01T19:00:00Z", "marketId": "m1", "tradeType": "STRAIGHT", "league": "NFL",
         "marketType": "MONEY", "legs": "1", "cost": "45.5", "qty": "100", "side": "TAKER"},
        {"timestamp": "2026-10-01T19:00:00Z", "marketId": "m1", "tradeType": "STRAIGHT", "league": "NFL",
         "marketType": "MONEY", "legs": "1", "cost": "54.5", "qty": "100", "side": "MAKER"},
    ]
    s = summarise(["2026-10-01"], lambda d: iter(rows_))
    p = s["parlays"]
    assert p["trades_per_day"] == 1 and p["retail_stake_per_day_usd"] == 50 and p["median_price"] == 0.25
    assert p["quoter_collateral_per_day_usd"] == 150 and p["legs"] == {3: 1} and p["quoters_per_trade"] == {1: 1}
    assert s["straights"]["contracts_per_day"] == 100 and s["straights"]["top_leagues_share"] == {"NFL": 1.0}


def test_configuration(tmp_path):
    with pytest.raises(ConfigError):
        build_live_supervisor({"NOVIG_RFQ": "shadow", "TRADING_LOG_DIR": str(tmp_path)})       # no token
    with pytest.raises(ConfigError):
        build_live_supervisor({"NOVIG_RFQ": "live", "NOVIG_RFQ_ACCESS_TOKEN": "t", "TRADING_LOG_DIR": str(tmp_path)})
    sup = build_live_supervisor({"NOVIG_RFQ": "qa", "NOVIG_RFQ_ACCESS_TOKEN": "t", "TRADING_LOG_DIR": str(tmp_path),
                                 "RESEARCH_ENABLED": "0"})
    assert sup.novig_rfq is not None and sup.novig_rfq.url == "wss://api-qa.novig.us/rfq/ws"
    assert sup.novig_rfq.cfg.maker_fee_rate == 0 and "novig_rfq" in sup._task_factories
    assert sup.novig_rfq.leg_lookup == sup.novig_leg_fair


async def test_restart_restores_open_parlays_with_their_legs(tmp_path):
    q = quoter(tmp_path)

    class Rest:
        async def executions(self, status=None, limit=500):
            assert status == "open"
            return [{"rfq_id": "old", "wager": "42", "price": "0.42", "liability": "58", "status": "open",
                     "outcome_ids": ["kc", "dal"]}]
    q.rest = Rest()
    assert await q.restore() == pytest.approx(58)
    assert q.book.game_exposure("ev-kcbuf") == pytest.approx(58)      # per-game cap sees the restored parlay
    assert "old" in q.positions


async def test_token_is_refreshed_before_every_reconnect(tmp_path):
    q = quoter(tmp_path)
    calls = []

    async def ensure(session=None):
        calls.append(1)
        raise ConnectionError("stop here")
    q.auth.ensure = ensure
    with pytest.raises(ConnectionError):
        await q._session()
    assert calls == [1]


async def test_legs_in_started_or_live_games_are_never_priced(tmp_path):
    import time as _t
    from novig_feed import MarketInfo
    sup = build_live_supervisor({"NOVIG_RFQ": "qa", "NOVIG_RFQ_ACCESS_TOKEN": "t", "TRADING_LOG_DIR": str(tmp_path),
                                 "RESEARCH_ENABLED": "0"})
    info = MarketInfo(outcome_id="o1", market_id="m1", sibling_outcome_id="o2", league="NFL", market_type="moneyline",
                      event_id="e1", home_team="Kansas City Chiefs", away_team="Buffalo Bills",
                      outcome="Kansas City Chiefs", start_time=_t.time() + 3600)
    sup.registry.replace_all([info, info.model_copy(update={"outcome_id": "o2", "sibling_outcome_id": "o1",
                                                            "outcome": "Buffalo Bills"})])
    sup._index_start_times()
    sup.book.ingest([dict(league="NFL", home_team="Kansas City Chiefs", away_team="Buffalo Bills",
                          market_type="moneyline", side="Kansas City Chiefs", odds_for=-150, odds_against=130)])
    hit = sup.novig_leg_fair("o1")
    assert hit is not None and hit[1].start is not None
    sup.mark_game_live(sup._canonical(__import__("novig_feed").MarketUpdate.from_info(info))[0][:3], "test")
    assert sup.novig_leg_fair("o1") is None


async def test_parlay_losses_count_toward_the_daily_loss_stop(tmp_path):
    sup = build_live_supervisor({"NOVIG_RFQ": "qa", "NOVIG_RFQ_ACCESS_TOKEN": "t", "TRADING_LOG_DIR": str(tmp_path),
                                 "RESEARCH_ENABLED": "0", "DAILY_LOSS_LIMIT_USD": "100"})
    sup.novig_rfq.on_settled(-60.0, "a")
    assert sup._combo_block_reason() is None
    sup.novig_rfq.on_settled(-50.0, "b")
    assert sup._combo_block_reason() == "daily loss stop"             # no new parlay quotes today
