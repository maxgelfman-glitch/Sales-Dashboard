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
