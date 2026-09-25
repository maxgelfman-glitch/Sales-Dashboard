"""
Self-test: college football/basketball — no fixed team table; games are matched venue to venue, conservatively.
"""

import time

import pytest

from main_supervisor import Supervisor
from novig_feed import MarketRegistry, MarketUpdate
from sharp_feed import MockSharpSource
from team_normalizer import (
    canonical_league,
    college_names_match,
    normalize_outcome,
    normalize_team_name,
    orient,
    register_game,
    reset_dynamic_teams,
)


@pytest.fixture(autouse=True)
def clean_teams():
    reset_dynamic_teams()
    yield
    reset_dynamic_teams()


@pytest.mark.parametrize("a,b,expected", [
    ("Texas", "Texas Longhorns", True), ("Texas", "Texas A&M Aggies", False), ("Texas A&M", "Texas A&M Aggies", True),
    ("Georgia", "Georgia Tech Yellow Jackets", False), ("Georgia", "Georgia State Panthers", False),
    ("Georgia", "Georgia Bulldogs", True), ("Ohio St.", "Ohio State Buckeyes", True),
    ("Miami (FL)", "Miami (OH)", False), ("Miami (FL)", "Miami Hurricanes", True),
    ("Ole Miss", "Mississippi Rebels", True), ("UConn", "Connecticut Huskies", True), ("LSU", "LSU Tigers", True),
    ("USC", "Southern California Trojans", True), ("North Carolina", "North Carolina State", False),
    ("Kentucky", "Western Kentucky", False), ("St. John's", "St. John's Red Storm", True),
])
def test_conservative_name_matching(a, b, expected):
    assert college_names_match(a, b) is expected


def test_game_level_matching_resolves_look_alikes():
    t = time.time() + 86400
    assert register_game("NCAAF", "Texas A&M Aggies", "Texas Longhorns", t) == ("Texas A&M Aggies", "Texas Longhorns")
    assert register_game("NCAAF", "Texas A&M", "Texas", t + 600) == ("Texas A&M Aggies", "Texas Longhorns")
    assert normalize_team_name("Texas", "NCAAF") == "Texas Longhorns"
    assert normalize_team_name("texas a&m", "NCAAF") == "Texas A&M Aggies"
    assert normalize_outcome("Texas", "NCAAF") == "Texas Longhorns"


def test_swapped_home_away_keeps_one_orientation():
    t = time.time() + 86400
    register_game("NCAAB", "Duke Blue Devils", "North Carolina Tar Heels", t)          # neutral site
    assert register_game("NCAAB", "North Carolina", "Duke", t) == ("North Carolina Tar Heels", "Duke Blue Devils")
    assert orient("NCAAB", "North Carolina Tar Heels", "Duke Blue Devils") == ("Duke Blue Devils",
                                                                               "North Carolina Tar Heels")


def test_unknown_or_ambiguous_names_stay_unmapped():
    t = time.time() + 86400
    register_game("NCAAF", "Georgia Bulldogs", "Auburn Tigers", t)
    register_game("NCAAF", "Georgia Tech Yellow Jackets", "Clemson Tigers", t)
    assert normalize_team_name("Georgia", "NCAAF") == "Georgia Bulldogs"                 # "Tech" never dropped
    assert normalize_team_name("Tigers", "NCAAF") is None                                 # two Tigers: refuse
    assert normalize_team_name("Alabama", "NCAAF") is None                                # never registered


def test_league_aliases():
    assert canonical_league("CFB") == "NCAAF" and canonical_league("College Basketball") == "NCAAB"
    assert canonical_league("nba") == "NBA"


def outcome(oid, sib, venue, home, away, name, start):
    return dict(venue=venue, outcome_id=oid, sibling_outcome_id=sib, market_id=f"M-{venue}", event_id=f"E-{venue}",
                league="CFB" if venue == "novig" else "NCAAF", market_type="moneyline", home_team=home,
                away_team=away, outcome=name, start_time=start)


async def test_college_game_trades_end_to_end_across_venues():
    t = time.time() + 86400
    novig = [outcome("N-TEX", "N-TAM", "novig", "Texas A&M Aggies", "Texas Longhorns", "Texas Longhorns", t),
             outcome("N-TAM", "N-TEX", "novig", "Texas A&M Aggies", "Texas Longhorns", "Texas A&M Aggies", t)]
    kalshi = [outcome("K-TEX", "K-TAM", "kalshi", "Texas", "Texas A&M", "Texas", t),       # swapped + short names
              outcome("K-TAM", "K-TEX", "kalshi", "Texas", "Texas A&M", "Texas A&M", t)]
    sharp = [dict(league="NCAAF", home_team="Texas A&M Aggies", away_team="Texas Longhorns", market_type="moneyline",
                  side="Texas Longhorns", odds_for=-120, odds_against=100, source="pinnacle")]
    sup = Supervisor(feed_url="ws://127.0.0.1:1/x", token="t", sharp_fetch=MockSharpSource(sharp),
                     registry=MarketRegistry(novig), kalshi_registry=MarketRegistry(kalshi), maker_enabled=False)
    sup.book.ingest(sharp)
    info = {m["outcome_id"]: m for m in novig + kalshi}

    def upd(oid, price):
        return MarketUpdate(**info[oid], price=price, available_volume=500)
    k_tex = sup._canonical(upd("K-TEX", 0.5))
    n_tex = sup._canonical(upd("N-TEX", 0.5))
    assert k_tex == n_tex == (("NCAAF", "Texas A&M Aggies", "Texas Longhorns", "moneyline"), "Texas Longhorns")
    await sup.on_market_update(upd("N-TEX", 0.49), None)                                 # fair 0.5217: edge
    assert [o.side for o in sup.orders] == ["Texas Longhorns"]
    await sup.on_market_update(upd("K-TAM", 0.40), None)                                 # hedge on Kalshi
    assert [o.kind for o in sup.orders] == ["DIRECTIONAL", "ARB_HEDGE"]
