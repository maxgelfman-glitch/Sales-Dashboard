"""
Milestone 4 self-test: full runtime integration of main_supervisor.py

Runs the real Supervisor against the mock Novig exchange and a mock sharp
feed, drives a scripted scenario (including a harsh connection drop), then
reads trading_engine.log back and proves every required event was recorded
with millisecond timestamps and no unexpected async exceptions.

Expected maths for the scripted scenario (checked by test_execution.py formulas):
    Knicks ML sharp -120/+100 -> fair 0.54545/1.04545 = 0.52174
        Novig 0.490 -> edge +6.48%  -> 1/4 Kelly $1,558 -> CAPPED $1,000  (BET)
        Novig 0.480 -> still +EV    -> duplicate position BLOCKED
    Celtics ML (mirrored side) fair 0.47826; Novig 0.478 -> edge +0.05% -> PASS
    Warriors spread: Novig -3.5 vs sharp -4.5 -> line mismatch -> PASS
    Over 47.5 sharp -105/-115 -> fair 0.48915; Novig 0.44 -> +11.2% -> CAPPED $1,000 (BET)
"""

import asyncio
import logging
import re
from logging.handlers import TimedRotatingFileHandler

import pytest
from aiohttp import web

from main_supervisor import (
    DEMO_SHARP_LINES,
    HttpSharpSource,
    MockSharpSource,
    SharpBook,
    Supervisor,
    setup_logging,
)
from mock_novig_server import MockNovigServer, make_update

TS_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \| ")


async def wait_until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


KNICKS = dict(event_id="NBA-BOS-NYK", league="NBA", market_type="moneyline",
              home_team="New York Knicks", away_team="Boston Celtics")


@pytest.fixture
def log_path(tmp_path):
    path = setup_logging(tmp_path, console=False, level=logging.DEBUG)
    yield path
    for h in list(logging.getLogger("trading").handlers):
        h.close()
        logging.getLogger("trading").removeHandler(h)


async def test_full_runtime_simulation_is_fully_logged(log_path):
    loop = asyncio.get_running_loop()
    async_errors = []
    loop.set_exception_handler(lambda _loop, ctx: async_errors.append(ctx))

    server = await MockNovigServer().start()
    sharp_lines = DEMO_SHARP_LINES + [dict(league="NBA", home_team="Seattle SuperSonics", away_team="Boston",
                                            market_type="moneyline", side="Seattle SuperSonics",
                                            odds_for=-110, odds_against=-110)]
    sup = Supervisor(feed_url=server.url, token="t", sharp_fetch=MockSharpSource(sharp_lines),
                     sharp_poll_interval=0.1, heartbeat_interval=0.2, feed_kwargs={"reconnect_delay": 0.3})
    run = asyncio.create_task(sup.run())
    try:
        await asyncio.wait_for(sup.feed.connected.wait(), 5)
        await wait_until(lambda: len(sup.book) > 0)

        await server.broadcast(make_update(**KNICKS, outcome="New York Knicks", price=0.490))   # BET (capped)
        await server.broadcast(make_update(**KNICKS, outcome="Boston Celtics", price=0.478))    # PASS ~0%
        await server.broadcast(make_update(**KNICKS, outcome="New York Knicks", price=0.480))   # dup blocked
        await server.broadcast(make_update(event_id="NBA-LAL-GSW", market_type="spread", line=-3.5,
                                           home_team="Golden State Warriors", away_team="Los Angeles Lakers",
                                           outcome="Golden State Warriors", price=0.45))          # line mismatch
        await server.broadcast(make_update(event_id="X-1", home_team="Gotham Rogues",
                                           away_team="Boston Celtics", outcome="Gotham Rogues"))   # unmapped
        await wait_until(lambda: sup.stats["updates"] >= 5)

        server.drop_all_clients()                                                               # harsh drop
        await wait_until(lambda: sup.feed.connect_count == 2, timeout=5)
        await server.wait_for_clients(1)
        await server.broadcast(make_update(event_id="NFL-BUF-KC", league="NFL", market_type="total", line=47.5,
                                           home_team="Kansas City Chiefs", away_team="Buffalo Bills",
                                           outcome="Over", price=0.44))                          # BET after reconnect
        await wait_until(lambda: sup.stats["paper_orders"] == 2)
        await asyncio.sleep(0.5)  # allow a few heartbeats
    finally:
        await sup.feed.stop()
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await server.stop()
        loop.set_exception_handler(None)

    # ---- behaviour
    assert sup.stats["paper_orders"] == 2
    assert sup.stats["duplicate_blocked"] == 1
    assert sup.stats["unmapped"] == 1
    assert all(o.stake_usd == 1000.0 and o.capped for o in sup.positions.values())
    assert sup.total_exposure() == 2000.0
    assert not async_errors, async_errors

    # ---- the log file
    for h in logging.getLogger("trading").handlers:
        h.flush()
    text = log_path.read_text()
    lines = text.splitlines()
    print(f"\n    log lines written: {len(lines)}")

    assert lines and all(TS_LINE.match(l) for l in lines), "every line needs a millisecond timestamp"
    required = {
        "connection state change": "CONN_STATE CONNECTED",
        "connection drop": "CONN dropped",
        "stale memory cleared": "cleared 4 stale price frames",  # NYK, BOS, GSW and the unmapped X-1 contract
        "recovery measured": "recovery_seconds",
        "contract update": "FEED_UPDATE NBA NBA-BOS-NYK moneyline New York Knicks",
        "line shift": "LINE_SHIFT",
        "sharp poll": "SHARP_POLL ok",
        "token translation": "BRIDGE 'NY Knicks' -> 'New York Knicks' (NBA)",
        "unmapped sharp name": "BRIDGE unmapped sharp name 'Seattle SuperSonics'",
        "unmapped novig name": "BRIDGE unmapped novig contract X-1",
        "de-vig calc": "CALC NBA-BOS-NYK/moneyline/New York Knicks fair=0.52174",
        "pass decision": "DECISION PASS NBA-BOS-NYK moneyline Boston Celtics",
        "line mismatch": "line mismatch novig=-3.5 sharp=-4.5",
        "+EV trigger": "EV_TRIGGER NBA-BOS-NYK moneyline New York Knicks",
        "duplicate blocked": "EV_TRIGGER skipped: already hold paper position",
        "mock order 1": 'ORDER PAPER {"order_id":1',
        "mock order 2 (post-reconnect)": '"side":"over"',
        "system heartbeat": "HEARTBEAT uptime=",
        "clean shutdown": "SUPERVISOR stopped",
    }
    missing = [name for name, needle in required.items() if needle not in text]
    assert not missing, f"missing from log: {missing}"
    warn_lines = [l for l in lines if "WARNING" in l and "BRIDGE unmapped sharp name" in l]
    assert len(warn_lines) == 1, "repeat unmapped warnings must be de-duplicated (later ones go to DEBUG)"
    assert "Traceback" not in text and "CALLBACK_ERROR" not in text and "SUPERVISOR task" not in text

    # ---- rotation configured daily
    file_handlers = [h for h in logging.getLogger("trading").handlers if isinstance(h, TimedRotatingFileHandler)]
    assert file_handlers and file_handlers[0].when == "MIDNIGHT"


async def test_supervisor_restarts_a_crashed_task(log_path):
    server = await MockNovigServer().start()
    sup = Supervisor(feed_url=server.url, token="t", sharp_fetch=MockSharpSource(DEMO_SHARP_LINES),
                     sharp_poll_interval=0.1, heartbeat_interval=0.1)
    crashes = {"n": 0}
    original = sup._task_factories["heartbeat"]

    async def flaky_heartbeat():
        if crashes["n"] == 0:
            crashes["n"] += 1
            raise RuntimeError("simulated bug")
        await original()

    sup._task_factories["heartbeat"] = flaky_heartbeat
    await sup.run(duration=0.6)
    await server.stop()
    text = log_path.read_text()
    assert sup.stats["task_restarts"] == 1
    assert "SUPERVISOR task heartbeat exited unexpectedly" in text and "HEARTBEAT uptime=" in text


async def test_stale_sharp_data_is_ignored():
    book = SharpBook(max_age_seconds=0.05)
    book.ingest(DEMO_SHARP_LINES)
    args = ("NBA", "New York Knicks", "Boston Celtics", "moneyline", "New York Knicks")
    assert book.lookup(*args) is not None
    mirrored = book.lookup("NBA", "New York Knicks", "Boston Celtics", "moneyline", "Boston Celtics")
    assert (mirrored.odds_for, mirrored.odds_against) == (100, -120)
    await asyncio.sleep(0.1)
    assert book.lookup(*args) is None


def test_book_ingest_never_raises_on_garbage():
    book = SharpBook()
    assert book.ingest("garbage") == (0, 0)
    assert book.ingest([{"league": "NBA"}, 42, None]) == (0, 3)


async def test_http_sharp_source_polls_json_endpoint():
    async def handler(_request):
        return web.json_response(DEMO_SHARP_LINES)

    app = web.Application()
    app.router.add_get("/lines", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    src = HttpSharpSource(f"http://127.0.0.1:{port}/lines")
    try:
        data = await src()
        book = SharpBook()
        assert book.ingest(data) == (len(DEMO_SHARP_LINES), 0)
    finally:
        await src.close()
        await runner.cleanup()
