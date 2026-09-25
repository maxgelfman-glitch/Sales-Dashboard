"""
Self-test: tennis (ATP + WTA as one league) — player-name matching, Kalshi YES-name parsing, and no
cross-venue locked pairs or hedges (retirement settlement differs by venue).
"""

import time

import pytest

from kalshi_feed import parse_kalshi_markets
from main_supervisor import Supervisor
from novig_feed import MarketRegistry, MarketUpdate
from sharp_feed import MockSharpSource
from team_normalizer import normalize_team_name, player_names_match, register_game, reset_dynamic_teams


@pytest.fixture(autouse=True)
def clean_teams():
    reset_dynamic_teams()
    yield
    reset_dynamic_teams()


@pytest.mark.parametrize("a,b,expected", [
    ("Carlos Alcaraz", "C. Alcaraz", True), ("Carlos Alcaraz", "Alcaraz C.", True),
    ("Carlos Alcaraz", "Alcaraz, Carlos", True), ("Carlos Alcaraz", "Alcaraz", True),
    ("Alexander Zverev", "M. Zverev", False), ("Felix Auger-Aliassime", "F. Auger Aliassime", True),
    ("Alex de Minaur", "A. De Minaur", True), ("Jannik Sinner", "Carlos Alcaraz", False),
])
def test_player_matching(a, b, expected):
    assert player_names_match(a, b) is expected


def test_kalshi_tennis_markets_use_the_yes_names():
    t = "2026-10-01T15:00:00Z"
    payload = {"markets": [
        {"ticker": "KXATPMATCH-26OCT01SINALC-SIN", "event_ticker": "KXATPMATCH-26OCT01SINALC",
         "title": "Will Jannik Sinner win the Sinner vs Alcaraz: Round of 16 match?", "yes_sub_title": "Jannik Sinner",
         "status": "active", "occurrence_datetime": t},
        {"ticker": "KXATPMATCH-26OCT01SINALC-ALC", "event_ticker": "KXATPMATCH-26OCT01SINALC",
         "title": "Will Carlos Alcaraz win the Sinner vs Alcaraz: Round of 16 match?", "yes_sub_title": "Carlos Alcaraz",
         "status": "active", "occurrence_datetime": t}]}
    rows = parse_kalshi_markets(payload, "TENNIS")
    assert len(rows) == 2 and {r.outcome for r in rows} == {"Jannik Sinner", "Carlos Alcaraz"}
    assert all(r.league == "TENNIS" and r.sibling_outcome_id for r in rows)


def test_kalshi_college_markets_keep_raw_names_for_matching():
    payload = {"markets": [
        {"ticker": "KXNCAAFGAME-26SEP19PURUCLA-PUR", "event_ticker": "KXNCAAFGAME-26SEP19PURUCLA",
         "title": "Purdue at UCLA Winner?", "yes_sub_title": "Purdue", "status": "active"},
        {"ticker": "KXNCAAFGAME-26SEP19PURUCLA-UCLA", "event_ticker": "KXNCAAFGAME-26SEP19PURUCLA",
         "title": "Purdue at UCLA Winner?", "yes_sub_title": "UCLA", "status": "active"}]}
    rows = parse_kalshi_markets(payload, "NCAAF")
    assert {(r.home_team, r.away_team, r.outcome) for r in rows} == {("UCLA", "Purdue", "Purdue"),
                                                                      ("UCLA", "Purdue", "UCLA")}


def m(oid, sib, venue, p1, p2, name, start):
    return dict(venue=venue, outcome_id=oid, sibling_outcome_id=sib, market_id=f"M-{venue}", event_id=f"E-{venue}",
                league="ATP" if venue == "novig" else "TENNIS", market_type="moneyline", home_team=p1, away_team=p2,
                outcome=name, start_time=start)


async def test_tennis_directional_yes_cross_venue_pair_no():
    t = time.time() + 86400
    novig = [m("N-S", "N-A", "novig", "Jannik Sinner", "Carlos Alcaraz", "Jannik Sinner", t),
             m("N-A", "N-S", "novig", "Jannik Sinner", "Carlos Alcaraz", "Carlos Alcaraz", t)]
    kalshi = [m("K-S", "K-A", "kalshi", "J. Sinner", "C. Alcaraz", "J. Sinner", t),
              m("K-A", "K-S", "kalshi", "J. Sinner", "C. Alcaraz", "C. Alcaraz", t)]
    sharp = [dict(league="ATP", home_team="Jannik Sinner", away_team="Carlos Alcaraz", market_type="moneyline",
                  side="Jannik Sinner", odds_for=-120, odds_against=100, source="pinnacle")]
    sup = Supervisor(feed_url="ws://127.0.0.1:1/x", token="t", sharp_fetch=MockSharpSource(sharp),
                     registry=MarketRegistry(novig), kalshi_registry=MarketRegistry(kalshi), maker_enabled=False)
    sup.book.ingest(sharp)
    info = {x["outcome_id"]: x for x in novig + kalshi}

    def upd(oid, price):
        u = MarketUpdate(**info[oid], price=price, available_volume=500)
        (sup.kalshi.latest if u.venue == "kalshi" else sup.feed.latest)[oid] = u
        return u
    assert normalize_team_name("C. Alcaraz", "TENNIS") == "Carlos Alcaraz"
    await sup.on_market_update(upd("K-A", 0.46), None)          # not an edge on its own (fair 0.478 < 0.46 + fee)
    await sup.on_market_update(upd("N-S", 0.51), None)          # 51 + 44 + fee < 99: would lock ... but tennis
    assert sup.stats["arb_pairs"] == 0 and sup.orders == []
    await sup.on_market_update(upd("N-S", 0.49), None)          # a real edge vs the sharp: directional is fine
    assert [o.kind for o in sup.orders] == ["DIRECTIONAL"]
    await sup.on_market_update(upd("K-A", 0.40), None)          # cross-venue hedge refused
    assert len(sup.orders) == 1
