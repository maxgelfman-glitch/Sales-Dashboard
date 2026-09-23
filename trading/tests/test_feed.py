"""
Self-test: novig_feed.py (+ the shared ws_base.py reconnect loop)

    * {"event": "subscribe", "data": "tape"} is sent immediately on every (re)connect
    * enveloped ticks keyed by outcomeId (outcomeId, price_cents, side, volume) are parsed
    * hierarchical registry: every outcome knows its market and sibling
    * best ask / best bid reporting; HARSH drop -> state wiped -> reconnect < 3.0s
    * outage, rejected token, silent stream, crashing callback: never fatal
"""

import asyncio
import json
import time

import pytest

import novig_feed
from mock_novig_server import MockNovigServer, make_outcome, make_tick
from novig_feed import MarketRegistry, NovigFeed, OrderBook, TapeTick, parse_message


class Recorder:
    def __init__(self):
        self.updates, self.states = [], []

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
        make_outcome(),                                                            # O-NYK
        make_outcome(outcome_id="O-BOS", sibling="O-NYK", outcome="Boston Celtics"),
        make_outcome(outcome_id="O-KC", sibling="O-BUF", market_id="MK-KC", event_id="NFL-BUF-KC", league="NFL",
                     market_type="Point Spread", home_team="Kansas City Chiefs", away_team="Buffalo Bills",
                     outcome="Kansas City Chiefs", line=-3.5),
        make_outcome(outcome_id="O-MLB", league="MLB"),                            # untracked: dropped
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
def test_parse_enveloped_outcome_tick():
    [t] = parse_message(json.dumps(make_tick("O-1", 47, "SELL", 1200, action="place")))
    assert (t.outcome_id, t.price_cents, t.side, t.volume, t.action) == ("O-1", 47, "sell", 1200, "PLACE")


def test_parse_bare_tick_list_and_envelope_actions():
    ticks = parse_message(json.dumps([make_tick("A", envelope=False), make_tick("B", envelope=False)]))
    assert [t.outcome_id for t in ticks] == ["A", "B"]
    [t] = parse_message(json.dumps({"event": "CANCEL", "data": [{"outcomeId": 9, "price_cents": 52,
                                                                  "side": "buy", "volume": 10}]}))
    assert (t.outcome_id, t.action) == ("9", "CANCEL")
    [t] = parse_message(json.dumps({"event": "tape", "data": {"outcomeId": "X", "price_cents": 50,
                                                               "side": "buy", "volume": 1}}))
    assert t.action is None      # "tape" is a channel name, not an order-book action


def test_control_messages_yield_nothing():
    assert parse_message(json.dumps({"event": "subscribed", "data": "tape"})) == []


@pytest.mark.parametrize("raw", [
    "not json", "{}", "[1,2,3]", b"\xff\xfe",
    json.dumps(make_tick(price_cents=0)), json.dumps(make_tick(price_cents=100)),
    json.dumps(make_tick(side="hold")), json.dumps(make_tick(volume=-5)),
    json.dumps({"event": "tape", "data": {"outcomeId": "O", "side": "buy", "volume": 1}}),
    json.dumps({"event": "tape", "data": {"market_id": "M", "price_cents": 50, "side": "buy", "volume": 1}}),
])
def test_parse_never_raises_on_garbage(raw):
    assert parse_message(raw) == []


def test_registry_hierarchy_and_siblings():
    reg = registry()
    assert len(reg) == 3 and reg.get("O-MLB") is None
    assert reg.get("O-KC").market_type == "spread"
    assert reg.sibling("O-NYK").outcome == "Boston Celtics"
    assert reg.sibling("O-KC") is None                  # sibling not registered
    assert reg.replace_all([make_outcome()]) == 1 and reg.get("O-BOS") is None


# ---------------------------------------------------------------- order book
def tick(**kw):
    return TapeTick.model_validate(make_tick(envelope=False, **kw))


def test_order_book_best_ask_and_bid():
    b = OrderBook()
    for p, s, v in [(52, "sell", 100), (49, "sell", 300), (45, "buy", 50), (47, "buy", 70)]:
        b.apply(tick(price_cents=p, side=s, volume=v))
    assert b.best_ask() == (49, 300) and b.best_bid() == (47, 70)


def test_order_book_place_cancel_fill_and_trade_print():
    b = OrderBook()
    b.apply(tick(price_cents=49, volume=300, action="PLACE"))
    b.apply(tick(price_cents=49, volume=200, action="PLACE"))
    b.apply(tick(price_cents=49, volume=150, action="CANCEL"))
    b.apply(tick(price_cents=49, volume=50, action="FILL"))
    b.apply(tick(price_cents=48, volume=999, action="TRADE"))
    assert b.best_ask() == (49, 300) and b.last_trade_cents == 48
    b.apply(tick(price_cents=49, volume=300, action="CANCEL"))
    assert b.best_ask() is None


# ---------------------------------------------------------------- network
async def test_subscribe_payload_sent_on_every_connect(server, monkeypatch):
    monkeypatch.setenv(novig_feed.TOKEN_ENV_VAR, "secret-abc")
    feed = NovigFeed(url=server.url, reconnect_delay=0.1)
    task = asyncio.create_task(feed.run())
    await wait_until(lambda: len(server.received) == 1)
    assert json.loads(server.received[0]) == {"event": "subscribe", "data": "tape"}
    assert server.auth_headers_seen[-1] == "Bearer secret-abc"
    server.drop_all_clients()
    await wait_until(lambda: len(server.received) == 2)          # resubscribed after reconnect
    assert json.loads(server.received[1]) == {"event": "subscribe", "data": "tape"}
    await shutdown(feed, task)


async def test_top_of_book_updates_and_line_shift(server):
    rec = Recorder()
    feed, task = await start_feed(server.url, rec)
    await server.broadcast(make_tick(price_cents=50, volume=1000))            # snapshot
    await server.broadcast(make_tick(price_cents=50, volume=1000))            # identical: ignored
    await server.broadcast(make_tick(price_cents=55, volume=500))             # worse ask level: no change
    await server.broadcast(make_tick(price_cents=48, volume=200))             # new best ask
    await server.broadcast(make_tick(price_cents=45, side="buy", volume=90))  # new best bid
    await server.broadcast(make_tick("UNKNOWN", 10))                          # not registered
    await wait_until(lambda: feed.messages_received == 6)
    await asyncio.sleep(0.05)
    tops = [(u.price, u.available_volume, u.best_bid) for u, _ in rec.updates]
    assert tops == [(0.50, 1000, None), (0.48, 200, None), (0.48, 200, 0.45)]
    u = rec.updates[-1][0]
    assert (u.outcome_id, u.market_id, u.sibling_outcome_id, u.venue) == ("O-NYK", "MK-NYK-ML", "O-BOS", "novig")
    await shutdown(feed, task)


async def test_harsh_drop_recovers_within_3_seconds(server):
    """Uses the PRODUCTION reconnect delay, no shortcuts."""
    rec = Recorder()
    feed, task = await start_feed(server.url, rec)
    assert feed.reconnect_delay == novig_feed.RECONNECT_DELAY_SECONDS == 2.5
    await server.broadcast(make_tick("O-NYK", 50))
    await server.broadcast(make_tick("O-KC", 51))
    await wait_until(lambda: len(feed.latest) == 2)

    t_drop = time.monotonic()
    assert server.drop_all_clients() == 1
    await wait_until(lambda: feed.connect_count == 2, timeout=6)
    recovery = time.monotonic() - t_drop

    drop = [d for _, s, d in rec.states if s == "DISCONNECTED"][0]
    assert drop["cleared_stale_price_frames"] == 2 and drop["cleared_order_books"] == 2
    print(f"\n    measured recovery: {recovery:.3f}s (feed self-reported {feed.last_recovery_seconds:.3f}s)")
    assert recovery < 3.0 and feed.last_recovery_seconds < 3.0

    await server.wait_for_clients(1)
    await server.broadcast(make_tick("O-NYK", 47))
    await wait_until(lambda: "O-NYK" in feed.latest and feed.latest["O-NYK"].price == 0.47)
    assert list(feed.books) == ["O-NYK"] and not task.done()
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


def test_urls():
    assert novig_feed.DEFAULT_NOVIG_WS_URL == "wss://api-qa.novig.us/tape"
    assert novig_feed.NOVIG_PROD_WS_URL == "wss://api.novig.com/tape"
    assert NovigFeed().url == novig_feed.DEFAULT_NOVIG_WS_URL
