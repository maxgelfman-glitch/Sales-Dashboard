"""Self-test: sharp_feed.py — provider mapping, decimal odds, and the strict 30-second freshness rule."""

import json

import pytest
from aiohttp import web

from sharp_feed import (
    SHARP_MAX_AGE_SECONDS,
    MockSharpSource,
    ProviderConfig,
    ProviderSharpSource,
    SharpBook,
    SharpPoller,
    decimal_to_american,
    map_provider_record,
    parse_timestamp,
)


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


KNICKS = dict(league="NBA", home_team="NY Knicks", away_team="Boston", market_type="moneyline",
              side="NY Knicks", odds_for=-120, odds_against=100)
KEY = ("NBA", "New York Knicks", "Boston Celtics", "moneyline", "New York Knicks")


# ---------------------------------------------------------------- freshness rule
def test_rule_is_30_seconds():
    assert SHARP_MAX_AGE_SECONDS == 30.0 and SharpBook().max_age == 30.0


def test_line_expires_exactly_after_30_seconds():
    clock = FakeClock()
    book = SharpBook(clock=clock)
    assert book.ingest([KNICKS]) == (1, 0)
    clock.t += 30.0
    assert book.lookup(*KEY) is not None          # 30.000s old: still valid
    clock.t += 0.001
    assert book.lookup(*KEY) is None              # 30.001s old: dead
    assert book.purge_stale() == 2                # both sides removed from memory
    assert len(book) == 0


def test_age_uses_provider_timestamp_not_receipt_time():
    clock = FakeClock()
    book = SharpBook(clock=clock)
    book.ingest([{**KNICKS, "updated_at": clock.t - 25}])   # already 25s old when received
    assert book.age_of(*KEY) == pytest.approx(25)
    clock.t += 5.5
    assert book.lookup(*KEY) is None                         # 30.5s since the PROVIDER priced it


def test_already_stale_lines_rejected_at_ingest():
    clock = FakeClock()
    book = SharpBook(clock=clock)
    assert book.ingest([{**KNICKS, "updated_at": clock.t - 31}]) == (0, 1)
    assert book.lookup(*KEY) is None


def test_future_timestamp_is_clamped_to_now():
    clock = FakeClock()
    book = SharpBook(clock=clock)
    book.ingest([{**KNICKS, "updated_at": clock.t + 3600}])  # provider clock wildly ahead
    clock.t += 30.5
    assert book.lookup(*KEY) is None                        # cannot live forever


async def test_provider_outage_makes_all_lines_expire():
    clock = FakeClock()
    book = SharpBook(clock=clock)
    calls = {"n": 0}

    async def flaky_fetch():
        calls["n"] += 1
        if calls["n"] > 1:
            raise ConnectionError("provider down")
        return [KNICKS]

    poller = SharpPoller(flaky_fetch, book, interval=1)
    await poller.poll_once()
    assert book.lookup(*KEY) is not None
    for _ in range(31):                   # 31 failed polls, 1s apart
        clock.t += 1
        await poller.poll_once()
    assert poller.failures == 31 and book.lookup(*KEY) is None and len(book) == 0


def test_mirrored_side_and_unmapped_names():
    book = SharpBook()
    book.ingest([KNICKS, {**KNICKS, "home_team": "Seattle SuperSonics", "side": "Seattle SuperSonics"}])
    other = book.lookup("NBA", "New York Knicks", "Boston Celtics", "moneyline", "Boston Celtics")
    assert (other.odds_for, other.odds_against) == (100, -120)
    assert len(book) == 2


def test_ingest_never_raises_on_garbage():
    book = SharpBook()
    assert book.ingest("garbage") == (0, 0)
    assert book.ingest([{"league": "NBA"}, 42, None]) == (0, 3)


# ---------------------------------------------------------------- provider mapping
@pytest.mark.parametrize("dec,american", [(2.5, 150), (2.0, 100), (1 + 100 / 150, -150), (1.9090909, -110)])
def test_decimal_to_american(dec, american):
    assert decimal_to_american(dec) == pytest.approx(american, abs=0.01)


def test_parse_timestamp_formats():
    assert parse_timestamp("2026-09-23T12:00:00Z", "iso") == parse_timestamp(1790164800, "epoch")
    assert parse_timestamp(1790164800000, "epoch_ms") == 1790164800
    assert parse_timestamp(None, "iso") is None


CFG = ProviderConfig(
    url="http://unused",
    list_path="data.events",
    fields=dict(league="sport", home_team="teams.home", away_team="teams.away", market_type="market",
                side="selection", odds_for="prices.0", odds_against="prices.1", line="points",
                updated_at="ts"),
    constants={"source": "pinnacle"},
    odds_format="decimal",
    timestamp_format="iso",
)
RECORD = {"sport": "NBA", "teams": {"home": "NY Knicks", "away": "BOS"}, "market": "Moneyline",
          "selection": "NY Knicks", "prices": [1.8333333, 2.0], "ts": "2026-09-23T12:00:00Z"}


def test_map_provider_record():
    m = map_provider_record(RECORD, CFG)
    assert m["league"] == "NBA" and m["home_team"] == "NY Knicks" and m["market_type"] == "moneyline"
    assert m["odds_for"] == pytest.approx(-120, abs=0.01) and m["odds_against"] == 100
    assert m["source"] == "pinnacle" and m["updated_at"] == 1790164800


def test_config_from_file_substitutes_env(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"url": "https://x/odds?key=${SHARP_API_KEY}",
                                "headers": {"Authorization": "Bearer ${SHARP_API_KEY}"}}))
    monkeypatch.setenv("SHARP_API_KEY", "k123")
    cfg = ProviderConfig.from_file(path)
    assert cfg.url.endswith("key=k123") and cfg.headers["Authorization"] == "Bearer k123"
    monkeypatch.delenv("SHARP_API_KEY")
    with pytest.raises(ValueError, match="SHARP_API_KEY"):
        ProviderConfig.from_file(path)


def test_example_config_is_valid(monkeypatch):
    monkeypatch.setenv("SHARP_API_KEY", "x")
    cfg = ProviderConfig.from_file("config/sharp_provider.example.json")
    assert cfg.timestamp_format == "iso"


async def test_provider_source_end_to_end_over_http():
    fresh_ts = "2099-01-01T00:00:00Z"   # far future -> clamped to now by the book

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer k"
        return web.json_response({"data": {"events": [{**RECORD, "ts": fresh_ts}, {"garbage": True}]}})

    app = web.Application()
    app.router.add_get("/odds", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    src = ProviderSharpSource(CFG.model_copy(update=dict(url=f"http://127.0.0.1:{port}/odds",
                                                         headers={"Authorization": "Bearer k"})))
    try:
        lines = await src()
        book = SharpBook()
        assert book.ingest(lines) == (1, 1)      # good record stored, garbage rejected
        assert book.lookup(*KEY).source == "pinnacle"
    finally:
        await src.close()
        await runner.cleanup()


async def test_mock_source_jitter_stays_valid():
    src = MockSharpSource([{**KNICKS, "odds_for": -101, "odds_against": 101}], jitter_cents=5)
    for _ in range(50):
        [x] = await src()
        assert x["odds_for"] <= -100 and x["odds_against"] >= 100


# ================================================================ per-outcome provider formats
from sharp_feed import pair_fixture_outcomes  # noqa: E402

NOW = 1_790_000_000.0


def optic_fixture(ts=NOW):
    return {
        "id": "fx1", "league": {"id": "nba", "name": "NBA"},
        "home_team_display": "New York Knicks", "away_team_display": "Boston Celtics",
        "odds": [
            {"sportsbook": "Pinnacle", "market": "Moneyline", "name": "New York Knicks", "price": -120,
             "points": None, "timestamp": ts - 3, "is_main": True},
            {"sportsbook": "Pinnacle", "market": "Moneyline", "name": "Boston Celtics", "price": 100,
             "points": None, "timestamp": ts - 1, "is_main": True},
            {"sportsbook": "Pinnacle", "market": "Point Spread", "name": "New York Knicks", "price": -110,
             "points": -2.5, "timestamp": ts, "is_main": True},
            {"sportsbook": "Pinnacle", "market": "Point Spread", "name": "Boston Celtics", "price": -110,
             "points": 2.5, "timestamp": ts, "is_main": True},
            {"sportsbook": "Pinnacle", "market": "Point Spread", "name": "New York Knicks", "price": +140,
             "points": -6.5, "timestamp": ts, "is_main": False},                        # alt line: ignored
            {"sportsbook": "Pinnacle", "market": "Total Points", "name": "Over 221.5", "price": -105,
             "points": 221.5, "timestamp": ts, "is_main": True},
            {"sportsbook": "Pinnacle", "market": "Total Points", "name": "Under 221.5", "price": -115,
             "points": 221.5, "timestamp": ts, "is_main": True},
            {"sportsbook": "DraftKings", "market": "Moneyline", "name": "New York Knicks", "price": -150,
             "points": None, "timestamp": ts, "is_main": True},                         # not our book
            {"sportsbook": "Pinnacle", "market": "Player Points", "name": "Jalen Brunson Over", "price": -110,
             "points": 27.5, "timestamp": ts, "is_main": True},                         # unmapped market
        ]}


def load_cfg(name, monkeypatch):
    monkeypatch.setenv("SHARP_API_KEY", "k")
    return ProviderConfig.from_file(f"config/{name}")


def test_opticodds_fixture_pairs_into_two_way_lines(monkeypatch):
    cfg = load_cfg("sharp_provider.opticodds_v3_outcomes.example.json", monkeypatch)
    lines, skipped = pair_fixture_outcomes(optic_fixture(), cfg)
    by_market = {l["market_type"]: l for l in lines}
    assert set(by_market) == {"moneyline", "spread", "total"} and skipped == 0
    ml = by_market["moneyline"]
    assert (ml["side"], ml["odds_for"], ml["odds_against"], ml["line"]) == ("New York Knicks", -120, 100, None)
    assert ml["updated_at"] == NOW - 3                       # OLDER of the two sides
    sp = by_market["spread"]
    assert (sp["side"], sp["line"], sp["odds_for"], sp["odds_against"]) == ("New York Knicks", -2.5, -110, -110)
    tot = by_market["total"]
    assert (tot["side"], tot["line"], tot["odds_for"], tot["odds_against"]) == ("Over", 221.5, -105, -115)
    assert ml["source"] == "opticodds-pinnacle"


def test_paired_lines_load_into_book_with_freshness(monkeypatch):
    cfg = load_cfg("sharp_provider.opticodds_v3_outcomes.example.json", monkeypatch)
    lines, _ = pair_fixture_outcomes(optic_fixture(), cfg)
    clock = FakeClock(NOW)
    book = SharpBook(clock=clock)
    assert book.ingest(lines) == (3, 0)
    assert book.lookup("NBA", "New York Knicks", "Boston Celtics", "spread", "Boston Celtics").line == 2.5
    clock.t = NOW - 3 + 30.001                               # moneyline's older side crosses 30s
    assert book.lookup(*KEY) is None
    assert book.lookup("NBA", "New York Knicks", "Boston Celtics", "total", "over") is not None


def test_oddsjam_shaped_fixture(monkeypatch):
    cfg = load_cfg("sharp_provider.oddsjam.example.json", monkeypatch)
    fixture = {"league": "NFL", "home_team": "Kansas City Chiefs", "away_team": "Buffalo Bills", "odds": [
        {"sports_book_name": "Pinnacle", "market_name": "Moneyline", "name": "Kansas City Chiefs", "price": -150,
         "bet_points": None, "timestamp": NOW, "is_main": True},
        {"sports_book_name": "Pinnacle", "market_name": "Moneyline", "name": "Buffalo Bills", "price": 130,
         "bet_points": None, "timestamp": NOW, "is_main": True},
        {"sports_book_name": "Pinnacle", "market_name": "Point Spread", "name": "Kansas City Chiefs", "price": -110,
         "bet_points": -3.5, "timestamp": NOW, "is_main": True},
        {"sports_book_name": "Pinnacle", "market_name": "Point Spread", "name": "Buffalo Bills", "price": -110,
         "bet_points": 2.5, "timestamp": NOW, "is_main": True},       # NOT the mirror of -3.5: no pair
    ]}
    lines, skipped = pair_fixture_outcomes(fixture, cfg)
    assert [(l["market_type"], l["odds_for"], l["odds_against"]) for l in lines] == [("moneyline", -150, 130)]
    assert skipped == 0


def test_bad_outcome_records_are_skipped_not_fatal(monkeypatch):
    cfg = load_cfg("sharp_provider.opticodds_v3_outcomes.example.json", monkeypatch)
    fx = optic_fixture()
    fx["odds"] += [{"sportsbook": "Pinnacle", "market": "Moneyline", "name": "Gotham Rogues", "price": 100,
                    "timestamp": NOW}, {"sportsbook": "Pinnacle", "market": "Moneyline", "price": "abc"}]
    lines, skipped = pair_fixture_outcomes(fx, cfg)
    assert len(lines) == 3 and skipped == 2
    assert pair_fixture_outcomes({"nothing": True}, cfg) == ([], 1)


async def test_provider_outcomes_mode_over_http(monkeypatch):
    cfg = load_cfg("sharp_provider.opticodds_v3_outcomes.example.json", monkeypatch)

    async def handler(request):
        assert request.headers["X-Api-Key"] == "k"
        return web.json_response({"data": [optic_fixture(ts=9_999_999_999)]})

    app = web.Application()
    app.router.add_get("/odds", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    src = ProviderSharpSource(cfg.model_copy(update=dict(url=f"http://127.0.0.1:{port}/odds")))
    try:
        lines = await src()
        assert len(lines) == 3
    finally:
        await src.close()
        await runner.cleanup()


# ================================================================ line-move hook (feeds the maker kill-switch)
def test_on_move_fires_with_old_and_new_line():
    moves = []
    book = SharpBook(on_move=lambda key, old, new: moves.append((key, old.line, new.line)))
    spread = dict(league="NBA", home_team="NY Knicks", away_team="Boston", market_type="spread",
                  side="NY Knicks", line=-2.5, odds_for=-110, odds_against=-110)
    book.ingest([spread])
    book.ingest([spread])                                    # unchanged: silent
    book.ingest([{**spread, "line": -3.5}])
    assert moves == [(("NBA", "New York Knicks", "Boston Celtics", "spread", "New York Knicks"), -2.5, -3.5)]


def test_on_move_listener_bug_does_not_break_ingest():
    def boom(*_):
        raise RuntimeError("listener bug")
    book = SharpBook(on_move=boom)
    book.ingest([KNICKS])
    assert book.ingest([{**KNICKS, "odds_for": -130}]) == (1, 0)


# ================================================================ OpticOdds mapping per the brief (nested `odds` object)
from sharp_feed import unwrap_odds  # noqa: E402


def brief_record(updated_at, home=1.8333333, away=2.0, market="moneyline", points=None):
    return {"league": "NBA", "home_team": "NY Knicks", "away_team": "Boston", "market": market,
            "odds": {"home_odds": {"decimal": home}, "away_odds": {"decimal": away},
                     "points": points, "updated_at": updated_at}}


def test_opticodds_brief_mapping_decimal_objects_and_updated_at(monkeypatch):
    cfg = load_cfg("sharp_provider.opticodds.example.json", monkeypatch)
    assert (cfg.mode, cfg.odds_format, cfg.timestamp_format) == ("flat", "decimal", "auto")
    m = map_provider_record(brief_record("2026-09-23T12:00:00Z"), cfg)
    assert m["side"] == "NY Knicks" and m["market_type"] == "moneyline"
    assert m["odds_for"] == pytest.approx(-120, abs=0.01) and m["odds_against"] == 100
    assert m["updated_at"] == 1790164800 and m["source"] == "opticodds-pinnacle"
    spread = map_provider_record(brief_record(1790164800, 1.9090909, 1.9090909, "spread", -2.5), cfg)
    assert (spread["line"], round(spread["odds_for"])) == (-2.5, -110)


def test_opticodds_brief_updated_at_drives_30s_rule(monkeypatch):
    cfg = load_cfg("sharp_provider.opticodds.example.json", monkeypatch)
    clock = FakeClock(1790164800 + 10)
    book = SharpBook(clock=clock)
    assert book.ingest([map_provider_record(brief_record("2026-09-23T12:00:00Z"), cfg)]) == (1, 0)
    assert book.age_of(*KEY) == pytest.approx(10)
    clock.t += 20.001                                             # 30.001s after updated_at
    assert book.lookup(*KEY) is None
    stale = map_provider_record(brief_record(1790164800 - 60), cfg)
    assert SharpBook(clock=FakeClock(1790164800)).ingest([stale]) == (0, 1)   # rejected on arrival


async def test_opticodds_brief_over_http(monkeypatch):
    cfg = load_cfg("sharp_provider.opticodds.example.json", monkeypatch)

    async def handler(request):
        assert request.headers["X-Api-Key"] == "k"
        return web.json_response({"data": [brief_record(9_999_999_999_000), {"league": "NBA", "odds": {}}]})

    app = web.Application()
    app.router.add_get("/odds", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    src = ProviderSharpSource(cfg.model_copy(update=dict(url=f"http://127.0.0.1:{port}/odds")))
    try:
        book = SharpBook()
        assert book.ingest(await src()) == (1, 1)                # good record stored, empty one rejected
    finally:
        await src.close()
        await runner.cleanup()


@pytest.mark.parametrize("obj,fmt,expected", [({"decimal": 1.91}, "decimal", 1.91), ({"value": 2.1}, "decimal", 2.1),
                                              ({"american": -110, "decimal": 1.91}, "american", -110)])
def test_unwrap_odds(obj, fmt, expected):
    assert unwrap_odds(obj, fmt) == expected


def test_unwrap_odds_rejects_unknown_shape():
    with pytest.raises(ValueError):
        unwrap_odds({"fractional": "10/11"}, "decimal")


@pytest.mark.parametrize("value,expected", [(1790164800, 1790164800), (1790164800000, 1790164800),
                                            ("1790164800.5", 1790164800.5), ("2026-09-23T12:00:00Z", 1790164800),
                                            ("2026-09-23T12:00:00+00:00", 1790164800), (None, None)])
def test_auto_timestamps(value, expected):
    assert parse_timestamp(value, "auto") == expected
