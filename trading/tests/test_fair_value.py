"""
Self-test: margin removal methods (devig.py) and the multi-book consensus fair value (SharpBook).
"""

import pytest

import execution
from devig import devig, devig_multiplicative, devig_power, devig_shin
from execution import american_to_decimal, evaluate_market_edge, NovigQuote, SharpQuote
from main_supervisor import ConfigError, Supervisor, fair_value_from_env, format_state_report
from sharp_feed import SharpBook

FAV_DOG = [american_to_decimal(-300), american_to_decimal(250)]     # lopsided moneyline with margin


@pytest.mark.parametrize("fn", [devig_multiplicative, devig_power, devig_shin])
def test_every_method_sums_to_one_and_reports_overround(fn):
    probs, over = fn(FAV_DOG)
    assert sum(probs) == pytest.approx(1.0) and over == pytest.approx(0.75 + 1 / 3.5)


@pytest.mark.parametrize("fn", [devig_multiplicative, devig_power, devig_shin])
def test_no_margin_is_left_untouched(fn):
    probs, _ = fn([2.0, 2.0])
    assert probs == pytest.approx([0.5, 0.5])
    probs, _ = fn([1 / 0.7, 1 / 0.3])
    assert probs == pytest.approx([0.7, 0.3])


def test_power_and_shin_take_more_margin_off_the_longshot():
    mult, power, shin = (devig(FAV_DOG, m)[0] for m in ("multiplicative", "power", "shin"))
    assert power[1] < mult[1] and shin[1] < mult[1]          # longshot fair value lower than proportional
    assert power[0] > mult[0] and shin[0] > mult[0]


def test_methods_agree_on_a_coin_flip():
    even = [american_to_decimal(-110), american_to_decimal(-110)]
    assert all(devig(even, m)[0] == pytest.approx([0.5, 0.5]) for m in ("multiplicative", "power", "shin"))


def test_unknown_method_and_bad_odds_raise():
    with pytest.raises(ValueError):
        devig([2.0, 2.0], "vibes")
    with pytest.raises(ValueError):
        devig_power([1.0, 2.0])


async def test_engine_wide_method_changes_the_edge():
    quote = NovigQuote(price=0.26, line=None, label="dog")
    sharp = SharpQuote(odds_for=250, odds_against=-300, line=None, source="t")
    try:
        execution.set_devig_method("multiplicative")
        mult = await evaluate_market_edge(quote, sharp)
        execution.set_devig_method("power")
        power = await evaluate_market_edge(quote, sharp)
    finally:
        execution.set_devig_method("multiplicative")
    assert power.fair_prob < mult.fair_prob and power.edge < mult.edge


# ---------------------------------------------------------------------------
# Consensus across books
# ---------------------------------------------------------------------------
class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


def line(source, odds_for, odds_against, market="moneyline", side="New York Knicks", ln=None):
    return dict(league="NBA", home_team="New York Knicks", away_team="Boston Celtics", market_type=market, side=side,
                odds_for=odds_for, odds_against=odds_against, line=ln, source=source)


def fair_of(sl):
    return execution.fair_devig([american_to_decimal(sl.odds_for), american_to_decimal(sl.odds_against)])[0][0]


KEY = ("NBA", "New York Knicks", "Boston Celtics", "moneyline", "New York Knicks")


def test_single_book_returns_its_raw_line():
    book = SharpBook()
    book.ingest([line("Pinnacle", -120, 100)])
    got = book.lookup(*KEY)
    assert (got.source, got.odds_for) == ("Pinnacle", -120)


def test_two_books_blend_by_weight():
    book = SharpBook(weights={"pinnacle": 3, "circa": 1})
    book.ingest([line("Pinnacle", -120, 100), line("Circa", -130, 110)])
    p_pin = devig_multiplicative([american_to_decimal(-120), american_to_decimal(100)])[0][0]
    p_cir = devig_multiplicative([american_to_decimal(-130), american_to_decimal(110)])[0][0]
    got = book.lookup(*KEY)
    assert got.source == "consensus:circa+pinnacle"
    assert fair_of(got) == pytest.approx((3 * p_pin + p_cir) / 4, abs=1e-4)
    other = book.lookup("NBA", "New York Knicks", "Boston Celtics", "moneyline", "Boston Celtics")
    assert fair_of(other) == pytest.approx(1 - fair_of(got), abs=1e-4)      # the mirrored side agrees


def test_unlisted_books_are_ignored_when_weights_are_set():
    book = SharpBook(weights={"pinnacle": 1})
    book.ingest([line("Pinnacle", -120, 100), line("SoftBook", -300, 250)])
    assert book.lookup(*KEY).source == "Pinnacle"
    book = SharpBook(weights={"pinnacle": 1, "*": 1})
    book.ingest([line("Pinnacle", -120, 100), line("SoftBook", -125, 105)])
    assert book.lookup(*KEY).source.startswith("consensus:")


def test_a_stale_book_drops_out_of_the_blend():
    clock = Clock()
    book = SharpBook(clock=clock, max_age_seconds=30)
    book.ingest([line("Circa", -130, 110)])
    clock.t += 25
    book.ingest([line("Pinnacle", -120, 100)])
    assert book.lookup(*KEY).source.startswith("consensus:")
    clock.t += 10                                                        # Circa now 35s old
    assert book.lookup(*KEY).source == "Pinnacle"


def test_books_on_different_lines_are_not_blended():
    book = SharpBook(weights={"pinnacle": 2, "circa": 1})
    book.ingest([line("Pinnacle", -110, -110, "spread", ln=-4.5), line("Circa", -105, -115, "spread", ln=-5.0)])
    key = ("NBA", "New York Knicks", "Boston Celtics", "spread", "New York Knicks")
    assert book.lookup(*key).line == -4.5 and book.lookup(*key).source == "Pinnacle"   # heaviest book's line
    assert book.lookup(*key, line=-5.0).source == "Circa"                              # exact-number request


def test_another_book_arriving_is_not_a_line_move():
    moves = []
    book = SharpBook(on_move=lambda k, old, new: moves.append((old.source, new.source)))
    book.ingest([line("Pinnacle", -120, 100)])
    book.ingest([line("Circa", -160, 140)])                               # different book: NOT a move
    book.ingest([line("Pinnacle", -120, 100)])
    assert moves == []
    book.ingest([line("Pinnacle", -130, 110)])                            # same book changed: a move
    assert moves == [("Pinnacle", "Pinnacle")]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def test_fair_value_env_parsing():
    assert fair_value_from_env({}) == dict(devig_method="multiplicative", sharp_weights=None)
    assert fair_value_from_env({"DEVIG_METHOD": "Shin", "SHARP_BOOK_WEIGHTS": "Pinnacle:2, circa sports:1"}) == dict(
        devig_method="shin", sharp_weights={"pinnacle": 2.0, "circa sports": 1.0})
    for bad in ({"DEVIG_METHOD": "vibes"}, {"SHARP_BOOK_WEIGHTS": "pinnacle"}, {"SHARP_BOOK_WEIGHTS": "pin:-1"}):
        with pytest.raises(ConfigError):
            fair_value_from_env(bad)


def test_supervisor_applies_method_and_reports_it():
    try:
        sup = Supervisor(devig_method="power", sharp_weights={"pinnacle": 2})
        assert execution.devig_method() == "power"
        report = format_state_report(sup)
        assert "power" in report and "pinnacle:2" in report
    finally:
        execution.set_devig_method("multiplicative")


def test_the_same_matchup_on_another_day_is_a_different_game():
    import time as _t
    from sharp_feed import SharpBook
    now = _t.time()
    book = SharpBook()
    today = dict(league="MLB", home_team="New York Yankees", away_team="Boston Red Sox", market_type="moneyline",
                 side="New York Yankees", odds_for=-200, odds_against=170, start_time=now + 3 * 3600)
    tomorrow = {**today, "odds_for": 130, "odds_against": -150, "start_time": now + 27 * 3600}
    book.ingest([today, tomorrow])                                       # the later game is not stored over it
    hit = book.lookup("MLB", "New York Yankees", "Boston Red Sox", "moneyline", "New York Yankees",
                      start=now + 3 * 3600)
    assert hit is not None and hit.odds_for == -200
    assert book.lookup("MLB", "New York Yankees", "Boston Red Sox", "moneyline", "New York Yankees",
                       start=now + 27 * 3600) is None                    # tomorrow's market is never priced by it


def test_lines_for_a_team_not_in_the_game_or_without_a_number_are_refused():
    from sharp_feed import SharpBook
    book = SharpBook()
    base = dict(league="NBA", home_team="Los Angeles Lakers", away_team="Boston Celtics", odds_for=300,
                odds_against=-400)
    assert book.ingest([{**base, "market_type": "moneyline", "side": "Los Angeles Clippers"}]) == (0, 1)
    assert book.ingest([{**base, "market_type": "spread", "side": "Los Angeles Lakers", "line": None}]) == (0, 1)
    assert book.ingest([{**base, "market_type": "moneyline", "side": "Over"}]) == (0, 1)


def test_pairs_and_hedges_never_join_two_games():
    from main_supervisor import same_game_time
    assert same_game_time(1000.0, 1000.0 + 3600) and same_game_time(None, 5.0)
    assert not same_game_time(1000.0, 1000.0 + 24 * 3600)


def test_a_broken_or_disagreeing_book_never_creates_a_consensus_edge():
    # Bovada with the teams swapped: averaging it in made a -6.6% bet look like +4.6%
    p = {o: devig_multiplicative([american_to_decimal(o[0]), american_to_decimal(o[1])])[0][0]
         for o in ((-150, 130), (-155, 135))}
    book = SharpBook(weights={"pinnacle": 2, "*": 1})
    book.ingest([line("Pinnacle", -150, 130), line("DraftKings", -155, 135), line("Bovada", 130, -150)])
    got = book.lookup(*KEY)                                            # swapped Bovada dropped, never averaged in
    assert got.source == "consensus:draftkings+pinnacle"
    assert fair_of(got) == pytest.approx((2 * p[(-150, 130)] + p[(-155, 135)]) / 3, abs=1e-4)
    # a -1000/-1000 book (overround 1.82) is left out instead of being blended into a clean-looking line
    book = SharpBook(weights={"pinnacle": 2, "*": 1})
    book.ingest([line("Pinnacle", -150, 130), line("Junk", -1000, -1000)])
    assert book.lookup(*KEY).source == "Pinnacle"
    # the anchor itself broken: no price at all
    book = SharpBook(weights={"pinnacle": 2, "*": 1})
    book.ingest([line("Pinnacle", -1000, -1000), line("Circa", -150, 130)])
    assert book.lookup(*KEY) is None


def test_an_extreme_longshot_consensus_is_refused_not_clamped():
    book = SharpBook(weights={"*": 1})
    book.ingest([line("Pinnacle", -40000, 29000), line("Circa", -42000, 29500)])
    assert book.lookup(*KEY) is None
