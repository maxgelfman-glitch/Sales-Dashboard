"""
Self-test: TheRundown v2 adapter (payload shapes from TheRundown's published v2 OpenAPI description).
REST is served by a local aiohttp app; the WebSocket by the local mock server. No real network.
"""

import asyncio
import copy

import pytest
from aiohttp import web

from main_supervisor import ConfigError, Supervisor, build_live_supervisor, format_state_report, sharp_source_from_env
from mock_novig_server import MockNovigServer
from sharp_feed import SharpBook
from therundown_feed import TheRundownSource


def price(p, main=True):
    return {"price": p, "is_main_line": main, "updated_at": "2026-01-14T22:30:00Z"}


EVENT = {
    "event_id": "ev1", "sport_id": 4, "event_date": "2026-01-15T00:00:00Z",
    "score": {"event_status": "STATUS_SCHEDULED"},
    "teams": [{"team_id": 145, "name": "Los Angeles", "mascot": "Lakers", "is_away": True, "is_home": False},
              {"team_id": 153, "name": "Boston", "mascot": "Celtics", "is_away": False, "is_home": True}],
    "markets": [
        {"market_id": 1, "period_id": 0, "name": "moneyline", "participants": [
            {"id": 145, "type": "TYPE_TEAM", "name": "Los Angeles Lakers",
             "lines": [{"value": "", "prices": {"3": price(150), "19": price(155)}}]},
            {"id": 153, "type": "TYPE_TEAM", "name": "Boston Celtics",
             "lines": [{"value": "", "prices": {"3": price(-180), "19": price(-185)}}]}]},
        {"market_id": 2, "period_id": 0, "name": "handicap", "participants": [
            {"id": 145, "type": "TYPE_TEAM", "name": "Los Angeles Lakers",
             "lines": [{"value": "4.5", "prices": {"3": price(-110)}}]},
            {"id": 153, "type": "TYPE_TEAM", "name": "Boston Celtics",
             "lines": [{"value": "-4.5", "prices": {"3": price(-110)}}]}]},
        {"market_id": 3, "period_id": 0, "name": "totals", "participants": [
            {"id": 1001, "type": "TYPE_RESULT", "name": "Over",
             "lines": [{"value": "224.5", "prices": {"3": price(-105)}}]},
            {"id": 1002, "type": "TYPE_RESULT", "name": "Under",
             "lines": [{"value": "224.5", "prices": {"3": price(-115)}}]}]},
        {"market_id": 1, "period_id": 1, "name": "moneyline 1H", "participants": []},          # halves ignored
    ],
}


class Clock:
    t = 1_768_435_200.0

    def __call__(self):
        return self.t


def source(clock=None, **kw):
    kw.setdefault("use_websocket", False)
    return TheRundownSource("key", affiliate_ids=(3, 19), clock=clock or Clock(), **kw)


def by(lines, market, src="pinnacle"):
    return [ln for ln in lines if ln["market_type"] == market and ln["source"] == src]


def test_snapshot_becomes_two_way_lines_per_book():
    s = source()
    s.load_events([EVENT])
    lines = s.lines()
    [ml] = by(lines, "moneyline")
    assert (ml["home_team"], ml["away_team"], ml["side"]) == ("Boston Celtics", "Los Angeles Lakers", "Boston Celtics")
    assert (ml["odds_for"], ml["odds_against"], ml["line"], ml["is_live"]) == (-180, 150, None, False)
    [dk] = by(lines, "moneyline", "draftkings")
    assert (dk["odds_for"], dk["odds_against"]) == (-185, 155)
    [spread] = by(lines, "spread")
    assert (spread["line"], spread["odds_for"], spread["odds_against"]) == (-4.5, -110, -110)
    [total] = by(lines, "total")
    assert (total["side"], total["line"], total["odds_for"], total["odds_against"]) == ("Over", 224.5, -105, -115)


def test_lines_are_accepted_by_the_sharp_book_under_canonical_names():
    s = source()
    s.load_events([EVENT])
    book = SharpBook()
    stored, rejected = book.ingest(s.lines())
    assert rejected == 0 and stored == 4
    got = book.lookup("NBA", "Boston Celtics", "Los Angeles Lakers", "spread", "Los Angeles Lakers", line=4.5)
    assert got is not None and got.line == 4.5


def test_off_board_and_closed_prices_are_skipped():
    ev = copy.deepcopy(EVENT)
    ev["markets"][0]["participants"][0]["lines"][0]["prices"]["3"]["price"] = 0.0001
    s = source()
    s.load_events([ev])
    assert by(s.lines(), "moneyline") == []                    # one side off the board: no two-way line


def test_game_not_scheduled_is_flagged_live():
    ev = copy.deepcopy(EVENT)
    ev["score"]["event_status"] = "STATUS_IN_PROGRESS"
    s = source()
    s.load_events([ev])
    assert all(ln["is_live"] for ln in s.lines())


def test_ws_rows_update_and_close_prices():
    s = source()
    s.load_events([EVENT])
    assert s.handle_message('{"meta":{"type":"heartbeat"},"data":{"now":""}}') == 0
    row = {"event_id": "ev1", "affiliate_id": 3, "market_id": 1, "participant_id": 153,
           "participant_name": "Boston Celtics", "line": "", "price": "-200", "change_type": "price_change"}
    assert s.handle_message('{"meta":{},"data":' + __import__("json").dumps(row) + "}") == 1
    assert by(s.lines(), "moneyline")[0]["odds_for"] == -200
    s.handle_message('{"data":' + __import__("json").dumps(dict(row, change_type="close")) + "}")
    assert by(s.lines(), "moneyline") == []
    assert s.handle_message('{"data":{"event_id":"unknown","affiliate_id":3,"market_id":1}}') == 0
    assert s.handle_message("not json") == 0


async def test_prices_are_withheld_when_the_source_is_not_confirmed_alive():
    clock = Clock()
    s = source(clock=clock, use_websocket=True)
    s.load_events([EVENT])
    s.last_snapshot = clock.t
    s._ws_task = asyncio.get_running_loop().create_future()      # pretend the WS task is running
    assert s.alive()
    s.handle_message('{"meta":{"type":"heartbeat"}}')
    clock.t += 25
    assert s.alive()                                                # heartbeat 25s ago
    clock.t += 10
    assert not s.alive()
    with pytest.raises(RuntimeError):
        s.last_snapshot = clock.t - 100                            # snapshot not due yet, WS silent
        await s()


async def test_rest_snapshot_and_websocket_end_to_end():
    seen = []

    async def events(request):
        seen.append((request.match_info["sport"], request.match_info["date"], request.headers.get("X-TheRundown-Key"),
                     request.query.get("affiliate_ids")))
        return web.json_response({"meta": {}, "events": [EVENT] if request.match_info["sport"] == "4" else []})

    app = web.Application()
    app.router.add_get("/api/v2/sports/{sport}/events/{date}", events)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/api/v2"
    ws = await MockNovigServer().start()
    s = TheRundownSource("secret", leagues=("NBA",), rest_base=base, ws_url=ws.url, use_websocket=True)
    try:
        lines = await s()
        assert seen and seen[0][0] == "4" and seen[0][2] == "secret" and seen[0][3] == "3"
        assert by(lines, "moneyline")[0]["odds_for"] == -180
        for _ in range(100):
            if ws.clients:
                break
            await asyncio.sleep(0.02)
        await ws.broadcast({"meta": {"type": "market_price"},
                            "data": {"event_id": "ev1", "affiliate_id": 3, "market_id": 1, "participant_id": 153,
                                     "line": "", "price": "-190", "change_type": "price_change"}})
        for _ in range(100):
            if by(s.lines(), "moneyline")[0]["odds_for"] == -190:
                break
            await asyncio.sleep(0.02)
        assert by(await s(), "moneyline")[0]["odds_for"] == -190
    finally:
        await s.close()
        await ws.stop()
        await runner.cleanup()


def test_env_selection():
    assert sharp_source_from_env({}) is None
    src = sharp_source_from_env({"SHARP_PROVIDER": "therundown", "THERUNDOWN_API_KEY": "k",
                                 "THERUNDOWN_AFFILIATE_IDS": "3,19", "THERUNDOWN_WEBSOCKET": "0"})
    assert isinstance(src, TheRundownSource) and src.affiliate_ids == (3, 19) and not src.use_websocket
    with pytest.raises(ConfigError):
        sharp_source_from_env({"SHARP_PROVIDER": "therundown"})
    with pytest.raises(ConfigError):
        sharp_source_from_env({"SHARP_PROVIDER": "therundown", "THERUNDOWN_API_KEY": "k",
                               "THERUNDOWN_AFFILIATE_IDS": "pinnacle"})
    sup = build_live_supervisor({"SHARP_PROVIDER": "therundown", "THERUNDOWN_API_KEY": "k", "RESEARCH_ENABLED": "0"})
    assert sup.sharp_enabled and "therundown" in format_state_report(sup)


def test_supervisor_accepts_the_source():
    sup = Supervisor(sharp_fetch=source())
    assert sup.sharp_enabled
