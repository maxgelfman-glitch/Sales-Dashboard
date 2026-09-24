"""
Self-test: "who moved first" — re-checking stale venue prices when the sharp moves, the optional taker
filters, and the report sections that decide whether to turn them on.
"""

import asyncio
import time

import pytest

from main_supervisor import ConfigError, taker_filters_from_env
from research_report import clv_by_method, clv_by_mover, move_bucket
from tests.test_research import paper_sup, upd

NYK_ML = ("NBA", "New York Knicks", "Boston Celtics", "moneyline")
MOVED = dict(league="NBA", home_team="NY Knicks", away_team="Boston", market_type="moneyline",
             side="NY Knicks", odds_for=-160, odds_against=140, source="mock-pinnacle")     # fair ~0.60


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_sharp_move_rechecks_a_stale_venue_price_immediately(tmp_path):
    sup = paper_sup(tmp_path)
    stale = upd("O-NYK", 0.53)                         # fair 0.5217: no edge yet
    sup.feed.latest["O-NYK"] = stale
    await sup.on_market_update(stale, None)
    assert sup.orders == []
    sup.book.ingest([MOVED])                           # sharp moves; Novig does not tick
    await settle()
    assert [o.kind for o in sup.orders] == ["DIRECTIONAL"] and sup.orders[0].price == 0.53
    assert sup.stats["sharp_move_reevaluations"] >= 1


async def test_moved_last_tracks_real_changes_not_polls(tmp_path):
    sup = paper_sup(tmp_path)
    u = upd("O-NYK", 0.53)
    await sup.on_market_update(u, None)
    assert sup.moved_last(u, NYK_ML)[0] == "novig"     # no sharp move seen yet
    sup.book.ingest([MOVED])
    assert sup.moved_last(u, NYK_ML)[0] == "sharp"
    sup.book.ingest([MOVED])                           # re-polled, unchanged: still the same move
    mover, age = sup.moved_last(u, NYK_ML)
    assert mover == "sharp" and age < 1
    later = upd("O-NYK", 0.54)
    sup.last_price_change[("novig", "O-NYK")] = time.time() + 0.001
    assert sup.moved_last(later, NYK_ML)[0] == "novig"


async def test_filter_blocks_edges_where_the_venue_moved_last(tmp_path):
    sup = paper_sup(tmp_path, taker_require_sharp_moved_last=True)
    await sup.on_market_update(upd("O-NYK", 0.49), None)          # venue tick creates the "edge": blocked
    assert sup.orders == [] and sup.stats["moved_first_blocked"] == 1
    sup.feed.latest["O-NYK"] = upd("O-NYK", 0.53)
    sup.book.ingest([MOVED])                                       # sharp moved after the venue: allowed
    await settle()
    assert [o.kind for o in sup.orders] == ["DIRECTIONAL"]


async def test_freshness_window(tmp_path):
    sup = paper_sup(tmp_path, taker_require_sharp_moved_last=True, taker_max_sharp_move_age_s=5.0)
    sup.last_sharp_change[NYK_ML] = time.time() - 10              # moved, but 10s ago
    sup.last_price_change[("novig", "O-NYK")] = time.time() - 60
    u = upd("O-NYK", 0.49)
    await sup.on_market_update(u, u)                               # price unchanged: venue is stale
    assert sup.orders == [] and "5s freshness" in (sup._moved_first_filter(u, NYK_ML) or "")
    sup.last_sharp_change[NYK_ML] = time.time() - 1
    await sup.on_market_update(u, u)
    assert len(sup.orders) == 1


def test_filters_env():
    assert taker_filters_from_env({}) == dict(taker_require_sharp_moved_last=False, taker_max_sharp_move_age_s=None,
                                              arb_pairs_enabled=True)
    assert taker_filters_from_env({"TAKER_REQUIRE_SHARP_MOVED_LAST": "1", "TAKER_MAX_SHARP_MOVE_AGE_SECONDS": "4",
                                   "ARB_PAIRS_ENABLED": "0"}) \
        == dict(taker_require_sharp_moved_last=True, taker_max_sharp_move_age_s=4.0, arb_pairs_enabled=False)
    for bad in ({"TAKER_MAX_SHARP_MOVE_AGE_SECONDS": "x"}, {"TAKER_MAX_SHARP_MOVE_AGE_SECONDS": "0"}):
        with pytest.raises(ConfigError):
            taker_filters_from_env(bad)


def test_report_splits_clv_by_mover_and_method():
    g = ["NBA", "A", "B", "moneyline"]
    rows = [
        dict(ts=1, kind="DECISION", action="BET", game=g, side="A", line=None, price=0.40, moved_last="sharp",
             sharp_move_age_s=2.0, fair_by_method={"multiplicative": 0.44, "power": 0.41}),
        dict(ts=2, kind="DECISION", action="BET", game=g, side="A", line=None, price=0.46, moved_last="novig",
             sharp_move_age_s=120.0, fair_by_method={"multiplicative": 0.48, "power": 0.475}),
        dict(ts=3, kind="CLOSE", game=g, side="A", line=None, close_fair_prob=0.45),
    ]
    mv = clv_by_mover(rows)
    assert mv[("sharp", "< 5s")]["mean_clv_cents"] == pytest.approx(5.0)
    assert mv[("venue", "30s-5m")]["mean_clv_cents"] == pytest.approx(-1.0)
    bm = clv_by_method(rows)
    assert bm["multiplicative"]["would_bet"] == 2 and bm["power"]["would_bet"] == 1
    assert [move_bucket(x) for x in (None, 1, 10, 100, 1000)] == ["never moved", "< 5s", "5-30s", "30s-5m", "> 5m"]
