"""
Milestone 2 self-test: novig_feed.py

Proves, against a local mock exchange:
    * JSON parsing and NFL/NBA spread/moneyline/total filtering
    * bearer-token authentication header
    * HARSH connection drop -> stale cache cleared -> reconnect in < 3.0s -> data flows again
    * server fully offline for a while -> feed keeps retrying, never crashes, recovers
    * silent (half-dead) stream -> watchdog forces a reconnect
    * a crashing user callback does not take the feed down
"""

import asyncio
import json
import time

import pytest

import novig_feed
from mock_novig_server import MockNovigServer, make_update
from novig_feed import NovigFeed, parse_message


# ---------------------------------------------------------------- helpers
class Recorder:
    """Collects everything the feed reports, with timestamps."""

    def __init__(self):
        self.updates = []
        self.states = []

    def on_update(self, update, previous):
        self.updates.append((update, previous))

    def on_state(self, state, details):
        self.states.append((time.monotonic(), state, details))


async def wait_until(predicate, timeout=5.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError("condition not met within %.1fs" % timeout)


@pytest.fixture
async def server():
    srv = await MockNovigServer().start()
    yield srv
    await srv.stop()


async def start_feed(url, rec, **kw):
    feed = NovigFeed(url=url, token="test-token", on_update=rec.on_update, on_state_change=rec.on_state, **kw)
    task = asyncio.create_task(feed.run())
    await asyncio.wait_for(feed.connected.wait(), 5)
    return feed, task


async def shutdown(feed, task):
    await feed.stop()
    await asyncio.wait_for(task, 5)


# ---------------------------------------------------------------- parsing
def test_parse_nfl_spread_and_nba_total():
    raw = json.dumps({"type": "tape", "data": [
        make_update(league="nfl", market_type="Point Spread", event_id="E1", home_team="Kansas City Chiefs",
                    away_team="Buffalo Bills", outcome="Kansas City Chiefs", price=0.52, line=-3.5),
        make_update(league="NBA", market_type="totals", outcome="over", price=0.48, line=221.5),
    ]})
    ups = parse_message(raw)
    assert [(u.league, u.market_type, u.line) for u in ups] == [("NFL", "spread", -3.5), ("NBA", "total", 221.5)]


def test_parse_filters_untracked_leagues_and_markets():
    raw = json.dumps([
        make_update(league="MLB"),                      # wrong league
        make_update(market_type="player_points"),      # prop, not a main market
        make_update(market_type="ml"),                  # alias of moneyline -> kept
    ])
    ups = parse_message(raw)
    assert len(ups) == 1 and ups[0].market_type == "moneyline"


@pytest.mark.parametrize("raw", ["not json", "{}", "[1,2,3]", json.dumps(make_update(price=1.7)), b"\xff\xfe"])
def test_parse_never_raises_on_garbage(raw):
    assert parse_message(raw) == []


def test_parse_accepts_camelcase_aliases():
    raw = json.dumps({"league": "NBA", "marketType": "moneyline", "eventId": "E9", "homeTeam": "A",
                      "awayTeam": "B", "selection": "A", "last": 0.61})
    [u] = parse_message(raw)
    assert (u.event_id, u.outcome, u.price) == ("E9", "A", 0.61)


# ---------------------------------------------------------------- network
async def test_sends_bearer_token_from_env(server, monkeypatch):
    monkeypatch.setenv(novig_feed.TOKEN_ENV_VAR, "secret-abc")
    feed = NovigFeed(url=server.url)
    task = asyncio.create_task(feed.run())
    await asyncio.wait_for(feed.connected.wait(), 5)
    assert server.auth_headers_seen[-1] == "Bearer secret-abc"
    await shutdown(feed, task)


async def test_line_shift_detection(server):
    rec = Recorder()
    feed, task = await start_feed(server.url, rec)
    await server.broadcast(make_update(price=0.50))
    await server.broadcast(make_update(price=0.50))   # identical: ignored
    await server.broadcast(make_update(price=0.53))   # shift
    await wait_until(lambda: len(rec.updates) == 2)
    await asyncio.sleep(0.1)
    assert len(rec.updates) == 2
    first, second = rec.updates
    assert first[1] is None                               # snapshot
    assert (second[1].price, second[0].price) == (0.50, 0.53)
    await shutdown(feed, task)


async def test_harsh_drop_recovers_within_3_seconds(server):
    """The headline test: uses the PRODUCTION reconnect delay, no shortcuts."""
    rec = Recorder()
    feed, task = await start_feed(server.url, rec)            # default reconnect_delay=2.5s
    assert feed.reconnect_delay == novig_feed.RECONNECT_DELAY_SECONDS

    await server.broadcast(make_update(price=0.50))
    await wait_until(lambda: len(feed.latest) == 1)

    t_drop = time.monotonic()
    assert server.drop_all_clients() == 1                     # kill TCP, no close frame
    await wait_until(lambda: feed.connect_count == 2, timeout=6)
    recovery = time.monotonic() - t_drop

    # 1) stale memory cleared at drop time
    drop_states = [d for _, s, d in rec.states if s == "DISCONNECTED"]
    assert drop_states and drop_states[0]["cleared_frames"] == 1
    # 2) reconnected inside the 3-second budget
    print(f"\n    measured recovery: {recovery:.3f}s (feed self-reported {feed.last_recovery_seconds:.3f}s)")
    assert recovery < 3.0
    assert feed.last_recovery_seconds < 3.0
    # 3) data flows again after the re-handshake
    await server.wait_for_clients(1)
    await server.broadcast(make_update(price=0.47))
    await wait_until(lambda: len(feed.latest) == 1 and next(iter(feed.latest.values())).price == 0.47)
    assert not task.done()                                     # parent never crashed
    await shutdown(feed, task)


async def test_survives_server_outage_and_recovers():
    srv = await MockNovigServer().start()
    port = srv.port
    rec = Recorder()
    feed, task = await start_feed(srv.url, rec, reconnect_delay=0.2)
    await srv.stop()                                           # exchange goes fully offline
    await asyncio.sleep(1.0)                                   # several failed reconnect attempts
    assert not task.done() and not feed.connected.is_set()
    srv2 = await MockNovigServer(port=port).start()            # exchange comes back
    await wait_until(lambda: feed.connect_count == 2, timeout=5)
    await srv2.broadcast(make_update(price=0.55))
    await wait_until(lambda: len(feed.latest) == 1)
    await shutdown(feed, task)
    await srv2.stop()


async def test_rejected_token_retries_without_crashing():
    srv = await MockNovigServer(required_token="right").start()
    feed = NovigFeed(url=srv.url, token="wrong", reconnect_delay=0.1)
    task = asyncio.create_task(feed.run())
    await wait_until(lambda: len(srv.auth_headers_seen) >= 3)
    assert not task.done() and feed.connect_count == 0
    await shutdown(feed, task)
    await srv.stop()


async def test_silent_stream_watchdog_forces_reconnect(server):
    rec = Recorder()
    feed, task = await start_feed(server.url, rec, stale_after=0.3, reconnect_delay=0.1)
    await wait_until(lambda: feed.connect_count >= 2, timeout=3)
    assert any("StaleStreamError" in d.get("error", "") for _, s, d in rec.states if s == "DISCONNECTED")
    await shutdown(feed, task)


async def test_crashing_callback_does_not_kill_feed(server):
    def bad_callback(update, previous):
        raise RuntimeError("bug in strategy code")

    feed = NovigFeed(url=server.url, token="t", on_update=bad_callback)
    task = asyncio.create_task(feed.run())
    await asyncio.wait_for(feed.connected.wait(), 5)
    await server.broadcast(make_update(price=0.40))
    await server.broadcast(make_update(price=0.41))
    await wait_until(lambda: feed.messages_received == 2)
    assert feed.connected.is_set() and not task.done()
    await shutdown(feed, task)
