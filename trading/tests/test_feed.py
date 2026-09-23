"""
Milestone 2 self-test: novig_feed.py

Proves, against a local mock exchange:
    * tick parsing (market_id, price_cents, side, volume) and order-book maths
    * best-ask reporting only for registered NFL/NBA markets
    * bearer-token authentication header and optional subscription messages
    * HARSH connection drop -> stale books cleared -> reconnect in < 3.0s -> data flows again
    * server fully offline for a while -> feed keeps retrying, never crashes, recovers
    * silent (half-dead) stream -> watchdog forces a reconnect
    * a crashing user callback does not take the feed down
"""

import asyncio
import json
import time

import pytest

import novig_feed
from mock_novig_server import MockNovigServer, make_market, make_tick
from novig_feed import MarketRegistry, NovigFeed, OrderBook, TapeTick, parse_message


# ---------------------------------------------------------------- helpers
class Recorder:
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


def registry():
    return MarketRegistry([
        make_market(),                                                   # M-NYK-ML
        make_market(market_id="M-KC-SPR", event_id="NFL-BUF-KC", league="NFL", market_type="Point Spread",
                    home_team="Kansas City Chiefs", away_team="Buffalo Bills",
                    outcome="Kansas City Chiefs", line=-3.5),
        make_market(market_id="M-MLB", league="MLB"),                    # untracked league: not registered
    ])


@pytest.fixture
async def server():
    srv = await MockNovigServer().start()
    yield srv
    await srv.stop()


async def start_feed(url, rec, **kw):
    kw.setdefault("registry", registry())
    feed = NovigFeed(url=url, token="test-token", on_update=rec.on_update, on_state_change=rec.on_state, **kw)
    task = asyncio.create_task(feed.run())
    await asyncio.wait_for(feed.connected.wait(), 5)
    return feed, task


async def shutdown(feed, task):
    await feed.stop()
    await asyncio.wait_for(task, 5)


# ---------------------------------------------------------------- parsing
def test_parse_maps_required_fields():
    [t] = parse_message(json.dumps(make_tick(market_id="M1", price_cents=47, side="SELL", volume=1200, action="place")))
    assert (t.market_id, t.price_cents, t.side, t.volume, t.action) == ("M1", 47, "sell", 1200, "PLACE")


def test_parse_envelope_action_and_numeric_ids():
    raw = json.dumps({"type": "CANCEL", "data": [{"marketId": 123, "priceCents": 52, "side": "buy", "volume": 10}]})
    [t] = parse_message(raw)
    assert (t.market_id, t.price_cents, t.action) == ("123", 52, "CANCEL")


@pytest.mark.parametrize("raw", [
    "not json", "{}", "[1,2,3]", b"\xff\xfe",
    json.dumps(make_tick(price_cents=0)), json.dumps(make_tick(price_cents=100)),
    json.dumps(make_tick(side="hold")), json.dumps(make_tick(volume=-5)),
    json.dumps({"market_id": "M1", "side": "buy", "volume": 1}),        # missing price_cents
])
def test_parse_never_raises_on_garbage(raw):
    assert parse_message(raw) == []


def test_registry_keeps_only_tracked_markets():
    reg = registry()
    assert len(reg) == 2
    assert reg.get("M-KC-SPR").market_type == "spread" and reg.get("M-MLB") is None
    assert reg.register({"market_id": "bad"}) is False


# ---------------------------------------------------------------- order book
def tick(**kw):
    return TapeTick.model_validate(make_tick(**kw))


def test_order_book_best_ask_and_bid():
    b = OrderBook()
    b.apply(tick(price_cents=52, side="sell", volume=100))
    b.apply(tick(price_cents=49, side="sell", volume=300))
    b.apply(tick(price_cents=45, side="buy", volume=50))
    b.apply(tick(price_cents=47, side="buy", volume=70))
    assert b.best_ask() == (49, 300) and b.best_bid() == (47, 70)


def test_order_book_place_cancel_fill_and_trade_print():
    b = OrderBook()
    b.apply(tick(price_cents=49, volume=300, action="PLACE"))
    b.apply(tick(price_cents=49, volume=200, action="PLACE"))    # adds: 500
    b.apply(tick(price_cents=49, volume=150, action="CANCEL"))   # 350
    b.apply(tick(price_cents=49, volume=50, action="FILL"))      # 300
    b.apply(tick(price_cents=48, volume=999, action="TRADE"))    # print only
    assert b.best_ask() == (49, 300) and b.last_trade_cents == 48
    b.apply(tick(price_cents=49, volume=300, action="CANCEL"))   # level emptied
    assert b.best_ask() is None and b.asks == {}


# ---------------------------------------------------------------- network
async def test_sends_bearer_token_and_subscriptions(server, monkeypatch):
    monkeypatch.setenv(novig_feed.TOKEN_ENV_VAR, "secret-abc")
    feed = NovigFeed(url=server.url, subscribe_messages=[{"op": "subscribe", "channel": "tape"}])
    task = asyncio.create_task(feed.run())
    await asyncio.wait_for(feed.connected.wait(), 5)
    await wait_until(lambda: server.received)
    assert server.auth_headers_seen[-1] == "Bearer secret-abc"
    assert json.loads(server.received[0]) == {"op": "subscribe", "channel": "tape"}
    await shutdown(feed, task)


async def test_best_ask_updates_and_line_shift(server):
    rec = Recorder()
    feed, task = await start_feed(server.url, rec)
    await server.broadcast(make_tick(price_cents=50, volume=1000))            # snapshot
    await server.broadcast(make_tick(price_cents=50, volume=1000))            # identical: ignored
    await server.broadcast(make_tick(price_cents=55, volume=500))             # worse level: best ask unchanged
    await server.broadcast(make_tick(price_cents=48, volume=200))             # new best ask -> shift
    await server.broadcast(make_tick(price_cents=45, side="buy", volume=90))  # bid side: best ask unchanged
    await server.broadcast(make_tick(market_id="UNKNOWN", price_cents=10))    # not registered: ignored
    await wait_until(lambda: feed.messages_received == 6)
    await asyncio.sleep(0.05)
    assert [(u.price, u.available_volume) for u, _ in rec.updates] == [(0.50, 1000), (0.48, 200)]
    first, second = rec.updates
    assert first[1] is None and second[1].price == 0.50
    assert second[0].outcome == "New York Knicks" and second[0].event_id == "NBA-BOS-NYK"
    await shutdown(feed, task)


async def test_harsh_drop_recovers_within_3_seconds(server):
    """The headline test: uses the PRODUCTION reconnect delay, no shortcuts."""
    rec = Recorder()
    feed, task = await start_feed(server.url, rec)
    assert feed.reconnect_delay == novig_feed.RECONNECT_DELAY_SECONDS

    await server.broadcast(make_tick(price_cents=50))
    await server.broadcast(make_tick(market_id="M-KC-SPR", price_cents=51))
    await wait_until(lambda: len(feed.latest) == 2)

    t_drop = time.monotonic()
    assert server.drop_all_clients() == 1
    await wait_until(lambda: feed.connect_count == 2, timeout=6)
    recovery = time.monotonic() - t_drop

    drop_states = [d for _, s, d in rec.states if s == "DISCONNECTED"]
    assert drop_states and drop_states[0]["cleared_frames"] == 2 and drop_states[0]["cleared_books"] == 2
    print(f"\n    measured recovery: {recovery:.3f}s (feed self-reported {feed.last_recovery_seconds:.3f}s)")
    assert recovery < 3.0 and feed.last_recovery_seconds < 3.0

    await server.wait_for_clients(1)
    await server.broadcast(make_tick(price_cents=47))
    await wait_until(lambda: "M-NYK-ML" in feed.latest and feed.latest["M-NYK-ML"].price == 0.47)
    assert list(feed.books) == ["M-NYK-ML"]      # the pre-drop KC book did not survive
    assert not task.done()
    await shutdown(feed, task)


async def test_survives_server_outage_and_recovers():
    srv = await MockNovigServer().start()
    port = srv.port
    rec = Recorder()
    feed, task = await start_feed(srv.url, rec, reconnect_delay=0.2)
    await srv.stop()
    await asyncio.sleep(1.0)
    assert not task.done() and not feed.connected.is_set()
    srv2 = await MockNovigServer(port=port).start()
    await wait_until(lambda: feed.connect_count == 2, timeout=5)
    await srv2.broadcast(make_tick(price_cents=55))
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

    feed = NovigFeed(url=server.url, token="t", registry=registry(), on_update=bad_callback)
    task = asyncio.create_task(feed.run())
    await asyncio.wait_for(feed.connected.wait(), 5)
    await server.broadcast(make_tick(price_cents=40))
    await server.broadcast(make_tick(price_cents=41))
    await wait_until(lambda: feed.messages_received == 2)
    assert feed.connected.is_set() and not task.done()
    await shutdown(feed, task)


def test_production_url_is_opt_in():
    assert novig_feed.DEFAULT_NOVIG_WS_URL == "wss://api-qa.novig.us/tape"
    assert novig_feed.NOVIG_PROD_WS_URL == "wss://api.novig.com/tape"
    assert NovigFeed().url == novig_feed.DEFAULT_NOVIG_WS_URL
