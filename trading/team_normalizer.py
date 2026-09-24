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
import unicodedata
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

MLB_TEAMS = [
    ("Arizona", "Diamondbacks", ["ARI", "AZ"]), ("Atlanta", "Braves", ["ATL"]), ("Baltimore", "Orioles", ["BAL"]),
    ("Boston", "Red Sox", ["BOS"]), ("Chicago", "Cubs", ["CHC"]), ("Chicago", "White Sox", ["CWS", "CHW"]),
    ("Cincinnati", "Reds", ["CIN"]), ("Cleveland", "Guardians", ["CLE"]), ("Colorado", "Rockies", ["COL"]),
    ("Detroit", "Tigers", ["DET"]), ("Houston", "Astros", ["HOU"]), ("Kansas City", "Royals", ["KC", "KCR"]),
    ("Los Angeles", "Angels", ["LAA"]), ("Los Angeles", "Dodgers", ["LAD"]), ("Miami", "Marlins", ["MIA"]),
    ("Milwaukee", "Brewers", ["MIL"]), ("Minnesota", "Twins", ["MIN"]), ("New York", "Mets", ["NYM"]),
    ("New York", "Yankees", ["NYY"]), ("Oakland", "Athletics", ["OAK", "ATH"]),
    ("Philadelphia", "Phillies", ["PHI"]), ("Pittsburgh", "Pirates", ["PIT"]), ("San Diego", "Padres", ["SD", "SDP"]),
    ("San Francisco", "Giants", ["SF", "SFG"]), ("Seattle", "Mariners", ["SEA"]),
    ("St. Louis", "Cardinals", ["STL"]), ("Tampa Bay", "Rays", ["TB", "TBR"]), ("Texas", "Rangers", ["TEX"]),
    ("Toronto", "Blue Jays", ["TOR"]), ("Washington", "Nationals", ["WSH", "WSN", "WAS"]),
]

NHL_TEAMS = [
    ("Anaheim", "Ducks", ["ANA"]), ("Boston", "Bruins", ["BOS"]), ("Buffalo", "Sabres", ["BUF"]),
    ("Calgary", "Flames", ["CGY"]), ("Carolina", "Hurricanes", ["CAR"]), ("Chicago", "Blackhawks", ["CHI"]),
    ("Colorado", "Avalanche", ["COL"]), ("Columbus", "Blue Jackets", ["CBJ"]), ("Dallas", "Stars", ["DAL"]),
    ("Detroit", "Red Wings", ["DET"]), ("Edmonton", "Oilers", ["EDM"]), ("Florida", "Panthers", ["FLA"]),
    ("Los Angeles", "Kings", ["LAK"]), ("Minnesota", "Wild", ["MIN"]), ("Montreal", "Canadiens", ["MTL", "MON"]),
    ("Nashville", "Predators", ["NSH"]), ("New Jersey", "Devils", ["NJD", "NJ"]),
    ("New York", "Islanders", ["NYI"]), ("New York", "Rangers", ["NYR"]), ("Ottawa", "Senators", ["OTT"]),
    ("Philadelphia", "Flyers", ["PHI"]), ("Pittsburgh", "Penguins", ["PIT"]), ("San Jose", "Sharks", ["SJS", "SJ"]),
    ("Seattle", "Kraken", ["SEA"]), ("St. Louis", "Blues", ["STL"]), ("Tampa Bay", "Lightning", ["TBL", "TB"]),
    ("Toronto", "Maple Leafs", ["TOR"]), ("Utah", "Mammoth", ["UTA", "UTAH"]), ("Vancouver", "Canucks", ["VAN"]),
    ("Vegas", "Golden Knights", ["VGK", "VEG"]), ("Washington", "Capitals", ["WSH", "WAS"]),
    ("Winnipeg", "Jets", ["WPG"]),
]

WNBA_TEAMS = [
    ("Atlanta", "Dream", ["ATL"]), ("Chicago", "Sky", ["CHI"]), ("Connecticut", "Sun", ["CON", "CONN"]),
    ("Dallas", "Wings", ["DAL"]), ("Golden State", "Valkyries", ["GSV", "GS"]), ("Indiana", "Fever", ["IND"]),
    ("Las Vegas", "Aces", ["LVA", "LV"]), ("Los Angeles", "Sparks", ["LAS"]), ("Minnesota", "Lynx", ["MIN"]),
    ("New York", "Liberty", ["NYL"]), ("Phoenix", "Mercury", ["PHX", "PHO"]), ("Seattle", "Storm", ["SEA"]),
    ("Washington", "Mystics", ["WAS", "WSH"]), ("Portland", "Fire", ["POR"]), ("Toronto", "Tempo", ["TOR"]),
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
    "MLB": {
        "athletics": "Oakland Athletics", "as": "Oakland Athletics", "sacramento athletics": "Oakland Athletics",
        "las vegas athletics": "Oakland Athletics", "dbacks": "Arizona Diamondbacks", "d backs": "Arizona Diamondbacks",
        "cleveland indians": "Cleveland Guardians", "chi sox": "Chicago White Sox", "yanks": "New York Yankees",
        "saint louis cardinals": "St. Louis Cardinals", "jays": "Toronto Blue Jays", "nats": "Washington Nationals",
        "halos": "Los Angeles Angels", "la angels": "Los Angeles Angels", "anaheim angels": "Los Angeles Angels",
        "la dodgers": "Los Angeles Dodgers",
    },
    "NHL": {
        "habs": "Montreal Canadiens", "leafs": "Toronto Maple Leafs", "caps": "Washington Capitals",
        "utah hockey club": "Utah Mammoth", "utah hc": "Utah Mammoth", "vegas": "Vegas Golden Knights",
        "las vegas golden knights": "Vegas Golden Knights", "saint louis blues": "St. Louis Blues",
        "bolts": "Tampa Bay Lightning", "canes": "Carolina Hurricanes", "sens": "Ottawa Senators",
        "la kings": "Los Angeles Kings", "pens": "Pittsburgh Penguins",
    },
    "WNBA": {
        "ny liberty": "New York Liberty", "la sparks": "Los Angeles Sparks", "vegas aces": "Las Vegas Aces",
    },
}

# Shorthand tokens expanded before a second lookup attempt.
TOKEN_EXPANSIONS = {
    "ny": "new york", "nyc": "new york", "la": "los angeles", "l a": "los angeles", "gs": "golden state",
    "kc": "kansas city", "tb": "tampa bay", "ne": "new england", "gb": "green bay", "lv": "las vegas",
    "sf": "san francisco", "sa": "san antonio", "no": "new orleans", "okc": "oklahoma city",
    "philly": "philadelphia", "wash": "washington", "st": "saint",
}

LEAGUES = {"NBA": NBA_TEAMS, "NFL": NFL_TEAMS, "MLB": MLB_TEAMS, "NHL": NHL_TEAMS, "WNBA": WNBA_TEAMS}


def clean(text: str) -> str:
    """Lowercase, '&' -> 'and', drop punctuation, collapse spaces. 'L.A. Lakers!' -> 'la lakers'."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()   # "Montréal" -> "Montreal"
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
