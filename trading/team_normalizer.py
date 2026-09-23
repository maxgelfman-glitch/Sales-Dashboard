"""
team_normalizer.py — The data bridge between feeds that spell teams differently.

    normalize_team_name("NY Knicks")            -> "New York Knicks"
    normalize_team_name("knicks")               -> "New York Knicks"
    normalize_team_name("NYK")                  -> "New York Knicks"
    normalize_team_name("LAC", league="NFL")    -> "Los Angeles Chargers"
    normalize_team_name("LAC", league="NBA")    -> "Los Angeles Clippers"
    normalize_team_name("LAC")                  -> None   (ambiguous: we refuse to guess)
    normalize_team_name(None)                   -> None   (never raises)

Returning None instead of guessing is deliberate: a wrong mapping would
compare two different games and could look like a huge fake edge.

Matching order (first unique hit wins):
    1. exact match on full name / nickname / abbreviation / unique city / alias
    2. same, after expanding shorthand ("NY" -> "new york", "LA" -> "los angeles")
    3. a known nickname appears inside the string ("Knicks (NY) - Home")
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable, Optional

# (City, Nickname, [abbreviations...])
# Bare city shorthand ("NY", "LA", "NO") is deliberately NOT an abbreviation: it is
# expanded to the city instead, so it only resolves when the city is unique in the league.
NBA_TEAMS = [
    ("Atlanta", "Hawks", ["ATL"]), ("Boston", "Celtics", ["BOS"]), ("Brooklyn", "Nets", ["BKN", "BRK"]),
    ("Charlotte", "Hornets", ["CHA", "CHO"]), ("Chicago", "Bulls", ["CHI"]), ("Cleveland", "Cavaliers", ["CLE"]),
    ("Dallas", "Mavericks", ["DAL"]), ("Denver", "Nuggets", ["DEN"]), ("Detroit", "Pistons", ["DET"]),
    ("Golden State", "Warriors", ["GSW", "GS"]), ("Houston", "Rockets", ["HOU"]), ("Indiana", "Pacers", ["IND"]),
    ("Los Angeles", "Clippers", ["LAC"]), ("Los Angeles", "Lakers", ["LAL"]), ("Memphis", "Grizzlies", ["MEM"]),
    ("Miami", "Heat", ["MIA"]), ("Milwaukee", "Bucks", ["MIL"]), ("Minnesota", "Timberwolves", ["MIN"]),
    ("New Orleans", "Pelicans", ["NOP"]), ("New York", "Knicks", ["NYK"]),
    ("Oklahoma City", "Thunder", ["OKC"]), ("Orlando", "Magic", ["ORL"]), ("Philadelphia", "76ers", ["PHI"]),
    ("Phoenix", "Suns", ["PHX", "PHO"]), ("Portland", "Trail Blazers", ["POR"]), ("Sacramento", "Kings", ["SAC"]),
    ("San Antonio", "Spurs", ["SAS", "SA"]), ("Toronto", "Raptors", ["TOR"]), ("Utah", "Jazz", ["UTA", "UTAH"]),
    ("Washington", "Wizards", ["WAS", "WSH"]),
]

NFL_TEAMS = [
    ("Arizona", "Cardinals", ["ARI"]), ("Atlanta", "Falcons", ["ATL"]), ("Baltimore", "Ravens", ["BAL"]),
    ("Buffalo", "Bills", ["BUF"]), ("Carolina", "Panthers", ["CAR"]), ("Chicago", "Bears", ["CHI"]),
    ("Cincinnati", "Bengals", ["CIN"]), ("Cleveland", "Browns", ["CLE"]), ("Dallas", "Cowboys", ["DAL"]),
    ("Denver", "Broncos", ["DEN"]), ("Detroit", "Lions", ["DET"]), ("Green Bay", "Packers", ["GB", "GNB"]),
    ("Houston", "Texans", ["HOU"]), ("Indianapolis", "Colts", ["IND"]), ("Jacksonville", "Jaguars", ["JAX", "JAC"]),
    ("Kansas City", "Chiefs", ["KC", "KAN"]), ("Las Vegas", "Raiders", ["LV", "LVR"]),
    ("Los Angeles", "Chargers", ["LAC"]), ("Los Angeles", "Rams", ["LAR"]), ("Miami", "Dolphins", ["MIA"]),
    ("Minnesota", "Vikings", ["MIN"]), ("New England", "Patriots", ["NE", "NWE"]),
    ("New Orleans", "Saints", ["NOR"]), ("New York", "Giants", ["NYG"]), ("New York", "Jets", ["NYJ"]),
    ("Philadelphia", "Eagles", ["PHI"]), ("Pittsburgh", "Steelers", ["PIT"]), ("San Francisco", "49ers", ["SF", "SFO"]),
    ("Seattle", "Seahawks", ["SEA"]), ("Tampa Bay", "Buccaneers", ["TB", "TAM"]), ("Tennessee", "Titans", ["TEN"]),
    ("Washington", "Commanders", ["WAS", "WSH"]),
]

# Informal nicknames and former names -> canonical full name.
EXTRA_ALIASES = {
    "NBA": {
        "sixers": "Philadelphia 76ers", "blazers": "Portland Trail Blazers", "wolves": "Minnesota Timberwolves",
        "twolves": "Minnesota Timberwolves", "cavs": "Cleveland Cavaliers", "mavs": "Dallas Mavericks",
        "dubs": "Golden State Warriors", "la clippers": "Los Angeles Clippers", "nola": "New Orleans Pelicans",
    },
    "NFL": {
        "niners": "San Francisco 49ers", "bucs": "Tampa Bay Buccaneers", "pats": "New England Patriots",
        "washington football team": "Washington Commanders", "redskins": "Washington Commanders",
        "oakland raiders": "Las Vegas Raiders", "san diego chargers": "Los Angeles Chargers",
        "st louis rams": "Los Angeles Rams", "jags": "Jacksonville Jaguars",
    },
}

# Shorthand tokens expanded before a second lookup attempt.
TOKEN_EXPANSIONS = {
    "ny": "new york", "nyc": "new york", "la": "los angeles", "l a": "los angeles", "gs": "golden state",
    "kc": "kansas city", "tb": "tampa bay", "ne": "new england", "gb": "green bay", "lv": "las vegas",
    "sf": "san francisco", "sa": "san antonio", "no": "new orleans", "okc": "oklahoma city",
    "philly": "philadelphia", "wash": "washington", "st": "saint",
}

LEAGUES = {"NBA": NBA_TEAMS, "NFL": NFL_TEAMS}


def clean(text: str) -> str:
    """Lowercase, '&' -> 'and', drop punctuation, collapse spaces. 'L.A. Lakers!' -> 'la lakers'."""
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[.’']", "", text)          # "L.A." -> "la", "O'Neil" -> "oneil"
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def expand(text: str) -> str:
    return " ".join(TOKEN_EXPANSIONS.get(tok, tok) for tok in text.split())


@lru_cache(maxsize=None)
def _league_index(league: str) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Returns (key -> {canonical names}, nickname/alias phrase -> canonical)."""
    index: dict[str, set[str]] = {}
    phrases: dict[str, str] = {}

    def add(key: str, canonical: str) -> None:
        index.setdefault(clean(key), set()).add(canonical)

    for city, nick, abbrs in LEAGUES[league]:
        canonical = f"{city} {nick}"
        add(canonical, canonical)
        add(nick, canonical)
        add(city, canonical)             # only used if the city is unique (checked at lookup)
        for a in abbrs:
            add(a, canonical)
        phrases[clean(nick)] = canonical
    for alias, canonical in EXTRA_ALIASES[league].items():
        add(alias, canonical)
        phrases[clean(alias)] = canonical
    return index, phrases


def _contains_phrase(haystack: str, phrase: str) -> bool:
    return f" {phrase} " in f" {haystack} "


def _candidates(text: str, leagues: Iterable[str]) -> set[str]:
    cleaned = clean(text)
    if not cleaned:
        return set()
    for form in (cleaned, expand(cleaned)):
        hits: set[str] = set()
        for lg in leagues:
            hits |= _league_index(lg)[0].get(form, set())
        if hits:
            return hits
    # Last resort: a nickname or alias appears somewhere in the string.
    hits = set()
    expanded = expand(cleaned)
    for lg in leagues:
        for phrase, canonical in _league_index(lg)[1].items():
            if _contains_phrase(cleaned, phrase) or _contains_phrase(expanded, phrase):
                hits.add(canonical)
    return hits


def normalize_team_name(raw: object, league: Optional[str] = None) -> Optional[str]:
    """
    Map any team spelling to its canonical full name, or None if unknown/ambiguous.
    `league` ("NBA"/"NFL") is optional but strongly recommended: it resolves
    collisions such as "LAC", "Chicago" or "Miami".
    """
    if not isinstance(raw, str):
        return None
    lg = (league or "").strip().upper()
    leagues = [lg] if lg in LEAGUES else list(LEAGUES)
    hits = _candidates(raw, leagues)
    return next(iter(hits)) if len(hits) == 1 else None


def normalize_outcome(raw: object, league: Optional[str] = None) -> Optional[str]:
    """Like normalize_team_name, but also understands totals sides ('Over', 'U 221.5')."""
    if isinstance(raw, str):
        first = clean(raw).split(" ")[0] if clean(raw) else ""
        if first in {"over", "o"}:
            return "over"
        if first in {"under", "u"}:
            return "under"
    return normalize_team_name(raw, league)
