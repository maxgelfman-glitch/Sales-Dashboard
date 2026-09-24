"""Milestone 4 self-test (data bridge): team_normalizer.py"""

import pytest

from team_normalizer import NBA_TEAMS, NFL_TEAMS, normalize_outcome, normalize_team_name


@pytest.mark.parametrize("raw,league,expected", [
    ("NY Knicks", "NBA", "New York Knicks"),          # the headline case
    ("NY Knicks", None, "New York Knicks"),
    ("New York Knicks", "NBA", "New York Knicks"),
    ("knicks", None, "New York Knicks"),
    ("NYK", None, "New York Knicks"),
    ("  N.Y.  Knicks!! ", None, "New York Knicks"),
    ("Knicks (NY) - Home", None, "New York Knicks"),
    ("L.A. Lakers", "NBA", "Los Angeles Lakers"),
    ("LA Clippers", "NBA", "Los Angeles Clippers"),
    ("GS Warriors", None, "Golden State Warriors"),
    ("Sixers", None, "Philadelphia 76ers"),
    ("Philly 76ers", None, "Philadelphia 76ers"),
    ("Blazers", None, "Portland Trail Blazers"),
    ("Portland Trail Blazers", None, "Portland Trail Blazers"),
    ("OKC", None, "Oklahoma City Thunder"),
    ("Boston", "NBA", "Boston Celtics"),              # unique city inside the league
    ("KC Chiefs", None, "Kansas City Chiefs"),
    ("Niners", None, "San Francisco 49ers"),
    ("SF 49ers", "NFL", "San Francisco 49ers"),
    ("Washington Football Team", "NFL", "Washington Commanders"),
    ("Oakland Raiders", None, "Las Vegas Raiders"),
    ("NY Giants", "NFL", "New York Giants"),
    ("NY Jets", "NFL", "New York Jets"),
    ("LAC", "NFL", "Los Angeles Chargers"),
    ("LAC", "NBA", "Los Angeles Clippers"),
    ("Chicago", "NFL", "Chicago Bears"),
    ("Chicago", "nba", "Chicago Bulls"),
    ("TB Buccaneers", None, "Tampa Bay Buccaneers"),
    ("Green Bay", None, "Green Bay Packers"),
    ("NY", "NBA", "New York Knicks"),               # NBA has one "New York" team
    ("NO", "NFL", "New Orleans Saints"),
    ("NO", "NBA", "New Orleans Pelicans"),
])
def test_maps_known_spellings(raw, league, expected):
    assert normalize_team_name(raw, league) == expected


@pytest.mark.parametrize("raw,league", [
    ("LAC", None),            # Chargers or Clippers? refuse to guess
    ("Chicago", None),        # Bears or Bulls
    ("New York", "NFL"),      # Giants or Jets
    ("Los Angeles", "NBA"),   # Lakers or Clippers
    ("Miami", None),
    ("NY", None),             # Knicks, Nets? Giants, Jets?
    ("LA", "NFL"),            # Rams or Chargers
])
def test_ambiguous_returns_none(raw, league):
    assert normalize_team_name(raw, league) is None


@pytest.mark.parametrize("raw", [None, "", "   ", 123, ["Knicks"], "Seattle SuperSonics", "Manchester United", "!!!"])
def test_unknown_or_bad_input_returns_none_without_raising(raw):
    assert normalize_team_name(raw) is None


@pytest.mark.parametrize("teams,league", [(NBA_TEAMS, "NBA"), (NFL_TEAMS, "NFL")])
def test_every_team_round_trips_by_name_nickname_and_abbreviation(teams, league):
    for city, nick, abbrs in teams:
        full = f"{city} {nick}"
        assert normalize_team_name(full, league) == full
        assert normalize_team_name(full.upper(), league) == full
        assert normalize_team_name(nick, league) == full
        for a in abbrs:
            assert normalize_team_name(a, league) == full, a


def test_league_sizes():
    assert len(NBA_TEAMS) == 30 and len(NFL_TEAMS) == 32


@pytest.mark.parametrize("raw,expected", [
    ("Over", "over"), ("UNDER", "under"), ("o 221.5", "over"), ("U 47.5", "under"),
    ("NY Knicks", "New York Knicks"), (None, None),
])
def test_normalize_outcome(raw, expected):
    assert normalize_outcome(raw, "NBA") == expected


@pytest.mark.parametrize("raw,league,expected", [
    ("NYY", "MLB", "New York Yankees"), ("Yankees", "MLB", "New York Yankees"), ("CWS", "MLB", "Chicago White Sox"),
    ("St. Louis Cardinals", "MLB", "St. Louis Cardinals"), ("Athletics", "MLB", "Oakland Athletics"),
    ("Sacramento Athletics", "MLB", "Oakland Athletics"), ("Cardinals", "NFL", "Arizona Cardinals"),
    ("Montréal Canadiens", "NHL", "Montreal Canadiens"), ("Utah Hockey Club", "NHL", "Utah Mammoth"),
    ("VGK", "NHL", "Vegas Golden Knights"), ("NY Rangers", "NHL", "New York Rangers"),
    ("Rangers", "MLB", "Texas Rangers"), ("Liberty", "WNBA", "New York Liberty"), ("LV Aces", "WNBA", "Las Vegas Aces"),
    ("Giants", "MLB", "San Francisco Giants"), ("Giants", "NFL", "New York Giants"),
    ("Boston", None, None), ("New York", "MLB", None), ("Rangers", None, None),   # ambiguous: refuse to guess
])
def test_new_leagues(raw, league, expected):
    assert normalize_team_name(raw, league) == expected
