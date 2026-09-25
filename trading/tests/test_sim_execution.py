"""
Self-test: realistic paper execution — simulated exchange (latency, depth haircut, consumed depth, misses),
the live order path in paper mode, edge survival, and the execution / projected-monthly report.
"""

import asyncio
import json
import time

import pytest

from main_supervisor import (
    ConfigError,
    DEMO_KALSHI_MARKETS,
    DEMO_MARKETS,
    DEMO_SHARP_LINES,
    Supervisor,
    build_live_supervisor,
    format_state_report,
)
from novig_feed import MarketRegistry, MarketUpdate
from research import ResearchRecorder
from research_report import build_report, execution_summary, projected_monthly, survival_summary
from sharp_feed import MockSharpSource
from sim_exchange import SimulatedGateway
from tests.test_kalshi_trading import K_BOS
from tests.test_live_execution import upd

KEY = ("NBA", "New York Knicks", "Boston Celtics", "moneyline")


def rows(tmp_path, kind=None):
    out = [json.loads(x) for p in sorted(tmp_path.glob("research-*.jsonl")) for x in p.read_text().splitlines()]
    return [r for r in out if kind is None or r["kind"] == kind]


def sim_sup(tmp_path, latency_ms=20, haircut=0.5, **kw):
    sup = Supervisor(feed_url="ws://127.0.0.1:1/x", token="t", sharp_fetch=MockSharpSource(DEMO_SHARP_LINES),
                     registry=MarketRegistry(DEMO_MARKETS), kalshi_registry=MarketRegistry(DEMO_KALSHI_MARKETS),
                     maker_enabled=False, research=ResearchRecorder(tmp_path), paper_execution="simulated",
                     sim_latency_ms=latency_ms, sim_depth_haircut=haircut, **kw)
    sup.book.ingest(DEMO_SHARP_LINES)
    sup.feed.connected.set()
    return sup


async def show(sup, u, prev=None):
    (sup.kalshi.latest if u.venue == "kalshi" else sup.feed.latest)[u.outcome_id] = u
    await sup.on_market_update(u, prev)


async def settle(sup, s=0.1):
    await asyncio.sleep(s)


# ---------------------------------------------------------------------------
# The simulated exchange on its own
# ---------------------------------------------------------------------------
class Book:
    def __init__(self):
        self.u = {}
        self.fills = []

    def get(self, venue, oid):
        return self.u.get(oid)

    async def on_fill(self, slip):
        self.fills.append(slip)


async def test_haircut_consumption_and_level_pricing():
    b = Book()
    b.u["X"] = MarketUpdate(**{**DEMO_KALSHI_MARKETS[0], "outcome_id": "X"}, price=0.45, available_volume=100,
                            ask_levels=[(0.45, 100), (0.46, 100)])
    gw = SimulatedGateway("kalshi", b.get, b.on_fill, latency_ms=5, depth_haircut=0.5)
    await gw.place_limit("X", "buy", 46.0, 1000, "c1")
    await asyncio.sleep(0.05)
    [s] = b.fills
    assert s.filled_volume == 100 and s.status == "CANCELLED"              # 50 + 50 of 200 displayed
    assert s.price_cents == pytest.approx(45.5)                             # Kalshi: each level's own price
    await gw.place_limit("X", "buy", 46.0, 1000, "c2")                      # same book: already consumed
    await asyncio.sleep(0.05)
    assert b.fills[1].filled_volume == 0
    b.u["X"] = b.u["X"].model_copy(update={"received_at": time.time() + 1})  # book re-quoted
    await gw.place_limit("X", "buy", 46.0, 10, "c3")
    await asyncio.sleep(0.05)
    assert b.fills[2].filled_volume == 10 and b.fills[2].status == "FILLED"


async def test_novig_fills_at_the_limit_and_price_moves_mean_misses():
    b = Book()
    oid = DEMO_MARKETS[0]["outcome_id"]
    book = MarketUpdate(**DEMO_MARKETS[0], price=0.49, available_volume=400, ask_levels=[(0.49, 400)])
    b.u[oid] = book
    gw = SimulatedGateway("novig", b.get, b.on_fill, latency_ms=30, depth_haircut=1.0)
    await gw.place_limit(oid, "buy", 50.0, 100, "c1")
    await asyncio.sleep(0.05)
    assert b.fills[0].price_cents == pytest.approx(50.0)                     # limit, not 49 (conservative)
    b.u[oid] = book.model_copy(update={"received_at": time.time() + 1})
    await gw.place_limit(oid, "buy", 49.0, 100, "c2")
    b.u[oid] = book.model_copy(update={"price": 0.52, "ask_levels": [(0.52, 400)],
                                       "received_at": time.time() + 2})       # moved during latency
    await asyncio.sleep(0.06)
    assert b.fills[-1].filled_volume == 0


# ---------------------------------------------------------------------------
# Paper engine through the simulated exchange
# ---------------------------------------------------------------------------
async def test_paper_directional_fills_only_what_is_realistic(tmp_path):
    sup = sim_sup(tmp_path, haircut=0.5)
    await show(sup, upd("O-NYK", 0.49, volume=400))
    assert sup.orders[0].pending                                            # not filled instantly any more
    await settle(sup)
    leg = sup.orders[0]
    assert not leg.pending and leg.contracts == 200                         # half of the 400 displayed
    assert sup.total_exposure() == pytest.approx(200 * 0.49)
    [ex] = rows(tmp_path, "EXECUTION")
    assert ex["simulated"] and ex["fill_ratio"] == 0.5 and ex["expected_profit_usd"] > 0
    assert ex["slippage_cents"] == pytest.approx(0.0) and ex["fill_ms"] >= 15


async def test_paper_miss_when_the_price_moves_first(tmp_path):
    sup = sim_sup(tmp_path, latency_ms=40)
    await show(sup, upd("O-NYK", 0.49, volume=400))
    sup.feed.latest["O-NYK"] = upd("O-NYK", 0.53, volume=400)             # someone took it
    await settle(sup)
    assert sup.total_exposure() == 0 and KEY not in sup.positions
    [ex] = rows(tmp_path, "EXECUTION")
    assert ex["filled"] == 0 and ex["expected_profit_usd"] == 0


async def test_paper_pair_goes_through_both_simulated_venues(tmp_path):
    sup = sim_sup(tmp_path, haircut=1.0)
    await show(sup, upd("O-NYK", 0.51, volume=300))
    await show(sup, upd(K_BOS, 0.46, volume=300))
    await settle(sup)
    pos = sup.positions[KEY]
    assert {l.venue for l in pos.legs} == {"novig", "kalshi"} and pos.unhedged() == 0
    ex = rows(tmp_path, "EXECUTION")
    assert sum(r["expected_profit_usd"] for r in ex) > 0                     # the hedge leg carries the lock


async def test_edge_survival_is_measured(tmp_path):
    sup = sim_sup(tmp_path, latency_ms=5, haircut=1.0)
    await show(sup, upd("O-NYK", 0.49, volume=400))
    await asyncio.sleep(0.03)
    await show(sup, upd("O-NYK", 0.52, volume=400))                          # edge gone
    [sv] = rows(tmp_path, "EDGE_SURVIVAL")
    assert sv["reason"] == "price moved away" and 20 <= sv["survival_ms"] < 2000


def test_report_sections(tmp_path):
    t = 1_790_000_000.0
    data = [
        dict(ts=t, kind="EXECUTION", simulated=True, venue="novig", requested=1000, filled=250, requested_usd=490,
             filled_usd=122.5, slippage_cents=0.0, ack_ms=5, fill_ms=510, expected_profit_usd=5.0),
        dict(ts=t + 86400, kind="EXECUTION", simulated=True, venue="novig", requested=20, filled=0,
             requested_usd=9.8, filled_usd=0, slippage_cents=None, ack_ms=5, fill_ms=None, expected_profit_usd=0.0),
        dict(ts=t, kind="EDGE_SURVIVAL", survival_ms=300), dict(ts=t, kind="EDGE_SURVIVAL", survival_ms=6000),
    ]
    ex = execution_summary(data)
    assert ex[("sim", "novig", "$250-1,000")]["fill_ratio"] == 0.25
    assert ex[("sim", "novig", "< $50")]["missed"] == 1.0
    sv = survival_summary(data)
    assert sv["over_500ms"] == 0.5 and sv["over_5000ms"] == 0.5
    pm = projected_monthly(data)
    assert pm["days"] == 1.0 and pm["per_month"] == 150.0
    text = build_report(data)
    for section in ("[EXECUTION]", "[EDGE SURVIVAL]", "[PROJECTED MONTHLY]", "$150/month"):
        assert section in text


def test_config_defaults_to_simulated_paper(tmp_path):
    sup = build_live_supervisor({"NOVIG_BEARER_TOKEN": "t", "RESEARCH_DIR": str(tmp_path)})
    assert sup.sim and "SIMULATED: 500ms delay, 50% of remaining depth" in format_state_report(sup)
    assert not build_live_supervisor({"PAPER_EXECUTION": "instant", "RESEARCH_ENABLED": "0"}).sim
    with pytest.raises(ConfigError):
        build_live_supervisor({"SIM_DEPTH_HAIRCUT": "0", "RESEARCH_ENABLED": "0"})
    with pytest.raises(ValueError):
        Supervisor(paper_execution="vibes")
