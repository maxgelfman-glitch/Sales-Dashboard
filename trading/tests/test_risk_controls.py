"""
Self-test: measurement mode (no sharp feed), per-game correlation cap, daily loss stop, alerts.
"""

import json
import logging
import time

import pytest

from alerts import AlertHandler
from execution import ExposureMonitor
from main_supervisor import (
    DEMO_MARKETS,
    DEMO_SHARP_LINES,
    ConfigError,
    Supervisor,
    build_live_supervisor,
    format_state_report,
    risk_controls_from_env,
)
from novig_feed import MarketRegistry, MarketUpdate
from settlement import ExchangePosition
from sharp_feed import MockSharpSource
from tests.test_live_execution import live_sup, slip, upd

NYK_GAME = ("NBA", "New York Knicks", "Boston Celtics")
NYK_TOTAL = [
    dict(venue="novig", market_id="MK-NYK-TOT", event_id="NBA-BOS-NYK", league="NBA", market_type="total",
         home_team="New York Knicks", away_team="Boston Celtics", outcome_id="O-NYK-OV", sibling_outcome_id="O-NYK-UN",
         outcome="Over", line=221.5, start_time=time.time() + 86400),
    dict(venue="novig", market_id="MK-NYK-TOT", event_id="NBA-BOS-NYK", league="NBA", market_type="total",
         home_team="New York Knicks", away_team="Boston Celtics", outcome_id="O-NYK-UN", sibling_outcome_id="O-NYK-OV",
         outcome="Under", line=221.5, start_time=time.time() + 86400),
]
TOTAL_SHARP = dict(league="NBA", home_team="NY Knicks", away_team="Boston", market_type="total", side="Over",
                   line=221.5, odds_for=-110, odds_against=-110, source="mock")
INFO = {m["outcome_id"]: m for m in list(DEMO_MARKETS) + NYK_TOTAL}


def paper(tmp_path=None, **kw):
    lines = DEMO_SHARP_LINES + [TOTAL_SHARP]
    sup = Supervisor(feed_url="ws://127.0.0.1:1/x", token="t", sharp_fetch=MockSharpSource(lines),
                     registry=MarketRegistry(list(DEMO_MARKETS) + NYK_TOTAL), maker_enabled=False, **kw)
    sup.book.ingest(lines)
    sup.feed.connected.set()
    return sup


def u(oid, price, volume=5000):
    return MarketUpdate(**INFO[oid], price=price, available_volume=volume)


# ---------------------------------------------------------------------------
# Measurement mode
# ---------------------------------------------------------------------------
def test_build_without_sharp_config_is_measurement_mode(tmp_path):
    sup = build_live_supervisor({"NOVIG_BEARER_TOKEN": "t", "RESEARCH_DIR": str(tmp_path)})
    assert not sup.sharp_enabled and "sharp_poller" not in sup._task_factories
    assert "NONE: measurement mode" in format_state_report(sup)


async def test_measurement_mode_records_gaps_but_never_trades(tmp_path):
    from research import ResearchRecorder
    sup = Supervisor(feed_url="ws://127.0.0.1:1/x", token="t", registry=MarketRegistry(DEMO_MARKETS),
                     research=ResearchRecorder(tmp_path), maker_enabled=False)
    await sup.on_market_update(upd("O-NYK", 0.40), None)
    await sup.on_market_update(upd("O-BOS", 0.40), None)                       # 20c locked gap on Novig
    assert sup.orders == [] and sup.stats["no_sharp"] == 2
    rows = [json.loads(line) for p in tmp_path.glob("research-*.jsonl") for line in p.read_text().splitlines()]
    assert any(r["kind"] == "GAP_OPEN" for r in rows)


# ---------------------------------------------------------------------------
# Per-game cap (moneyline + spread + total of one game are correlated)
# ---------------------------------------------------------------------------
async def test_second_market_on_same_game_only_gets_the_remaining_room():
    sup = paper(game_exposure_limit=1000.0)
    await sup.on_market_update(u("O-NYK", 0.49, volume=1000), None)          # $490 on the moneyline
    assert sup.game_unhedged(NYK_GAME) == pytest.approx(490.0)
    await sup.on_market_update(u("O-NYK-OV", 0.45), None)                      # total: big edge, capped by room
    total = sup.orders[-1]
    assert total.market_type == "total" and total.stake_usd <= 510.0 + 1e-6
    assert sup.game_unhedged(NYK_GAME) <= 1000.0 + 1e-6


async def test_game_at_cap_blocks_new_markets_but_hedges_free_room():
    sup = paper(game_exposure_limit=490.0)
    await sup.on_market_update(u("O-NYK", 0.49, volume=1000), None)
    await sup.on_market_update(u("O-NYK-OV", 0.45), None)
    assert len(sup.orders) == 1 and sup.stats["game_cap_blocked"] == 1
    await sup.on_market_update(u("O-BOS", 0.45, volume=1000), None)            # hedge: always allowed
    assert sup.orders[-1].kind == "ARB_HEDGE" and sup.game_unhedged(NYK_GAME) == 0
    await sup.on_market_update(u("O-NYK-OV", 0.44), None)                      # room again
    assert sup.orders[-1].market_type == "total"


async def test_live_pending_order_counts_toward_the_game_cap():
    sup = live_sup(game_exposure_limit=600.0)
    await sup.on_market_update(upd("O-NYK", 0.49, volume=1000), None)          # $490 reserved, still filling
    assert sup.game_unhedged(NYK_GAME) == pytest.approx(490.0)
    await sup.on_fill_slip(slip("ex-1", "FILLED", 1000, 49))
    assert sup.game_unhedged(NYK_GAME) == pytest.approx(490.0)


def test_risk_env_parsing():
    assert risk_controls_from_env({}) == dict(game_exposure_limit=1000.0, daily_loss_limit=2000.0)
    assert risk_controls_from_env({"GAME_EXPOSURE_LIMIT_USD": "1500", "DAILY_LOSS_LIMIT_USD": "off"}) == dict(
        game_exposure_limit=1500.0, daily_loss_limit=None)
    for bad in ({"GAME_EXPOSURE_LIMIT_USD": "0"}, {"GAME_EXPOSURE_LIMIT_USD": "99999"},
                {"DAILY_LOSS_LIMIT_USD": "x"}):
        with pytest.raises(ConfigError):
            risk_controls_from_env(bad)


# ---------------------------------------------------------------------------
# Daily loss stop
# ---------------------------------------------------------------------------
def settle_loss(sup, oid, contracts, sid):
    pos = ExchangePosition(settlement_id=sid, outcome_id=oid, contracts=contracts, status="SETTLED", result="LOSS")
    legs = [(k, leg) for k, p in sup.positions.items() for leg in p.legs if leg.outcome_id == oid]
    return sup._apply_settlement(pos, legs)


async def test_daily_loss_stop_halts_new_positions_and_survives_a_restart(tmp_path):
    ledger = tmp_path / "live_ledger.jsonl"
    sup = paper(daily_loss_limit=400.0, ledger_path=ledger)
    await sup.on_market_update(u("O-NYK", 0.49, volume=1000), None)
    row = settle_loss(sup, "O-NYK", 1000, "s1")
    assert row["net_profit_usd"] == -490.0 and sup._loss_halted()
    await sup.on_market_update(u("O-NYK-OV", 0.45), None)
    assert len(sup.orders) == 1 and sup.stats["daily_loss_blocked"] == 1
    assert '"DAILY_LOSS_STOP"' in ledger.read_text()
    again = paper(daily_loss_limit=400.0, ledger_path=ledger)                    # restart the same day
    assert again._loss_halted() and again.daily_pnl[again._utc_day()] == -490.0


async def test_loss_stop_is_per_utc_day(tmp_path):
    sup = paper(daily_loss_limit=100.0)
    sup._record_daily_pnl(-150.0, ts=time.time() - 86400)                       # yesterday
    assert not sup._loss_halted()


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
def test_alert_handler_sends_critical_once_and_never_raises():
    sent = []
    handler = AlertHandler("https://example.test/hook", sender=sent.append)
    logger = logging.getLogger("trading.test_alerts")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.warning("just a warning")
        logger.critical("KILL_SWITCH engaged")
        logger.critical("KILL_SWITCH engaged")                                   # duplicate within 5 min
        handler.flush_for_tests()
        assert len(sent) == 1 and "KILL_SWITCH engaged" in sent[0]

        def boom(_):
            raise OSError("network down")
        handler._send = boom
        logger.critical("another critical")                                      # failure is swallowed
        handler.flush_for_tests()
    finally:
        logger.removeHandler(handler)
        handler.close()


async def test_kill_switch_engaging_logs_critical_once():
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record)
    grab = Grab(logging.CRITICAL)
    logging.getLogger("trading").addHandler(grab)
    try:
        sup = paper(exposure=ExposureMonitor(100.0))
        sup.exposure.record_open("existing", 100.0)                              # limit already used up
        assert sup.exposure.taker_halted
        await sup.on_market_update(u("O-NYK", 0.49, volume=1000), None)
        await sup.on_market_update(u("O-NYK-OV", 0.45), None)
    finally:
        logging.getLogger("trading").removeHandler(grab)
    assert sup.orders == []
    assert sum("KILL_SWITCH engaged" in r.getMessage() for r in records) == 1
