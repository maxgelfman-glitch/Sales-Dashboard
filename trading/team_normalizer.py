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
    College leagues have no fixed table: names resolve through the games registered with register_game().
    """
    if not isinstance(raw, str):
        return None
    lg = canonical_league(league or "")
    if lg in DYNAMIC_LEAGUES:
        return _dynamic_resolve(raw, lg)
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


# ==========================================================================
# Leagues without a fixed team table (college): names are matched GAME BY GAME
# ==========================================================================
# Venues spell college teams very differently ("Texas" / "Texas Longhorns" / "Texas A&M Aggies"), and there
# are ~360 schools. Instead of a hand-made table, every game a venue lists is registered; the first venue to
# list a game (Novig, then Kalshi) fixes its canonical team names; later spellings of the same game become
# aliases when EXACTLY ONE pairing of the two teams matches. A name is never matched if the longer spelling
# adds a school-distinguishing word ("Georgia" never matches "Georgia State" or "Georgia Tech").
# Anything ambiguous stays unmapped, and an unmapped game is simply not traded.
DYNAMIC_LEAGUES = frozenset({"NCAAF", "NCAAB", "TENNIS"})
LEAGUE_ALIASES = {
    "CFB": "NCAAF", "NCAAFB": "NCAAF", "NCAA FOOTBALL": "NCAAF", "COLLEGE FOOTBALL": "NCAAF", "NCAA_FOOTBALL": "NCAAF",
    "CBB": "NCAAB", "NCAAM": "NCAAB", "NCAAMB": "NCAAB", "NCAA BASKETBALL": "NCAAB", "COLLEGE BASKETBALL": "NCAAB",
    "NCAA_BASKETBALL": "NCAAB", "MCBB": "NCAAB",
    # ATP and WTA share one league: some venues only say "Tennis"; player names never collide across tours
    "ATP": "TENNIS", "WTA": "TENNIS", "ATP TENNIS": "TENNIS", "WTA TENNIS": "TENNIS", "TENNIS": "TENNIS",
}
DISTINGUISHING = frozenset({"state", "tech", "southern", "northern", "eastern", "western", "central", "north", "south",
                            "east", "west", "a", "and", "m", "international", "atlantic", "christian", "methodist",
                            "baptist", "city", "poly", "polytechnic", "university", "college", "institute", "saint",
                            "st", "upstate", "coastal", "gulf", "valley", "mountain", "pacific", "am"})
COLLEGE_ABBREVIATIONS = {
    "ole miss": "mississippi", "uconn": "connecticut", "usc": "southern california", "lsu": "louisiana state",
    "ucf": "central florida", "smu": "southern methodist", "byu": "brigham young", "tcu": "texas christian",
    "unlv": "nevada las vegas", "utep": "texas el paso", "utsa": "texas san antonio", "uab": "alabama birmingham",
    "fiu": "florida international", "fau": "florida atlantic", "umass": "massachusetts", "pitt": "pittsburgh",
    "vcu": "virginia commonwealth", "unc": "north carolina", "ucla": "ucla", "uva": "virginia", "vt": "virginia tech",
    "ecu": "east carolina", "wku": "western kentucky", "mtsu": "middle tennessee", "niu": "northern illinois",
    "etsu": "east tennessee state", "sfa": "stephen f austin", "liu": "long island", "njit": "njit",
}


def canonical_league(league: str) -> str:
    lg = (league or "").strip().upper()
    return LEAGUE_ALIASES.get(lg, lg)


def _school_tokens(name: str) -> tuple[frozenset[str], Optional[str]]:
    """(core tokens, parenthetical qualifier) — 'Miami (FL)' -> ({'miami'}, 'fl'); 'Ohio St.' -> ({'ohio','state'})."""
    qualifier = None
    m = re.search(r"\(([^)]*)\)", name or "")
    if m:
        qualifier = clean(m.group(1)) or None
        name = name[:m.start()] + name[m.end():]
    text = clean(name)
    for short, full in COLLEGE_ABBREVIATIONS.items():
        text = re.sub(rf"\b{re.escape(short)}\b", full, text)
    toks = text.split()
    if toks and toks[-1] == "st":
        toks[-1] = "state"                      # "Ohio St" -> "ohio state"; a leading "St" stays (Saint)
    return frozenset(toks), qualifier


def player_names_match(a: str, b: str) -> bool:
    """
    Tennis players: 'Carlos Alcaraz' = 'C. Alcaraz' = 'Alcaraz C.' = 'Alcaraz, Carlos' = 'Alcaraz'.
    Every full word of the shorter spelling must appear in the longer one, and every initial must be the first
    letter of a remaining word. Hyphenated names split ('Auger-Aliassime').
    """
    def parts(name: str) -> tuple[list[str], list[str]]:
        toks = clean(name.replace("-", " ").replace(",", " ")).split()
        return [t for t in toks if len(t) > 1], [t for t in toks if len(t) == 1]
    wa, ia = parts(a)
    wb, ib = parts(b)
    if not wa or not wb:
        return False
    # the "shorter" spelling has fewer full words (initials stand in for words it leaves out)
    a_short = (len(wa), -len(ia)) <= (len(wb), -len(ib))
    (ws, is_), (wl, il) = ((wa, ia), (wb, ib)) if a_short else ((wb, ib), (wa, ia))
    if not set(ws) <= set(wl):
        return False
    rest = [t for t in wl if t not in ws]
    for initial in is_:
        hit = next((t for t in rest if t[0] == initial), None)
        if hit is None and initial not in il:
            return False
        if hit is not None:
            rest.remove(hit)
    return True


def names_match(league: str, a: str, b: str) -> bool:
    return player_names_match(a, b) if canonical_league(league) == "TENNIS" else college_names_match(a, b)


def college_names_match(a: str, b: str) -> bool:
    """True if two spellings can be the same school (conservative: see DISTINGUISHING)."""
    ta, qa = _school_tokens(a)
    tb, qb = _school_tokens(b)
    if not ta or not tb:
        return False
    if qa and qb and qa != qb:
        return False
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not small <= big:
        return False
    return not ((big - small) & DISTINGUISHING)


_dyn_alias: dict[str, dict[str, str]] = {}                 # league -> clean(raw) -> canonical
_dyn_games: dict[str, list[tuple[str, str, Optional[float]]]] = {}


def reset_dynamic_teams() -> None:
    _dyn_alias.clear()
    _dyn_games.clear()


def orient(league: str, home: str, away: str) -> tuple[str, str]:
    """
    The registered (home, away) order of a dynamic-league game, so every venue and the sharp feed key the
    game the same way even when one lists the teams the other way round. Other leagues: unchanged.
    """
    lg = canonical_league(league)
    if lg in DYNAMIC_LEAGUES:
        for h, a, _ in _dyn_games.get(lg, []):
            if {h, a} == {home, away}:
                return h, a
    return home, away


def _dynamic_resolve(raw: str, league: str) -> Optional[str]:
    aliases = _dyn_alias.get(league, {})
    hit = aliases.get(clean(raw))
    if hit is not None:
        return hit
    # not seen verbatim: accept only a UNIQUE conservative match among the league's registered teams
    teams = {t for g in _dyn_games.get(league, []) for t in g[:2]}
    matches = {t for t in teams if names_match(league, raw, t)}
    if len(matches) == 1:
        canonical = matches.pop()
        aliases[clean(raw)] = canonical
        return canonical
    return None


def register_game(league: str, home: str, away: str, start: Optional[float] = None,
                  window_s: float = 12 * 3600, create: bool = True) -> Optional[tuple[str, str]]:
    """
    Register one game listed by a venue (dynamic leagues). Returns the canonical (home, away) names, or None
    when it cannot be matched unambiguously (and create=False, or a pairing conflict makes it unsafe).
    """
    lg = canonical_league(league)
    if lg not in DYNAMIC_LEAGUES or not home or not away:
        return None
    aliases = _dyn_alias.setdefault(lg, {})
    games = _dyn_games.setdefault(lg, [])
    candidates = []
    for ch, ca, cstart in games:
        if start is not None and cstart is not None and abs(start - cstart) > window_s:
            continue
        straight = names_match(lg, home, ch) and names_match(lg, away, ca)
        swapped = names_match(lg, home, ca) and names_match(lg, away, ch)
        if straight and swapped:
            continue                            # both pairings fit: ambiguous, never guess
        if straight:
            candidates.append((ch, ca, False))
        elif swapped:
            candidates.append((ch, ca, True))
    if len(candidates) > 1:
        return None
    if candidates:
        ch, ca, swapped = candidates[0]
        h, a = (ca, ch) if swapped else (ch, ca)
        for raw, canon in ((home, h), (away, a)):
            prev = aliases.get(clean(raw))
            if prev is not None and prev != canon:
                return None                     # this spelling already means another team: unsafe
        aliases[clean(home)], aliases[clean(away)] = h, a
        return h, a
    if not create:
        return None
    h, a = home.strip(), away.strip()
    for raw in (h, a):
        if clean(raw) in aliases and aliases[clean(raw)] != raw:
            return None
    games.append((h, a, start))
    aliases[clean(h)], aliases[clean(a)] = h, a
    return h, a
