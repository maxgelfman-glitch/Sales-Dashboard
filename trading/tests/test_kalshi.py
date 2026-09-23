"""Self-test: kalshi_feed.py — translator, RSA-PSS auth, book maths, sequence gaps, live mock stream, bootstrap."""

import asyncio
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import kalshi_feed
from kalshi_feed import (
    KalshiBook,
    KalshiFeed,
    auth_headers,
    kalshi_cents_to_american,
    kalshi_cents_to_probability,
    parse_kalshi_markets,
    series_from_env,
)
from mock_novig_server import MockNovigServer, kalshi_delta, kalshi_snapshot, make_outcome
from novig_feed import MarketRegistry

T_NYK, T_BOS = "KXNBAGAME-BOSNYK-NYK", "KXNBAGAME-BOSNYK-BOS"


def kalshi_registry():
    return MarketRegistry([
        make_outcome(venue="kalshi", outcome_id=T_NYK, sibling=T_BOS, market_id="KXNBAGAME-BOSNYK",
                     event_id="KXNBAGAME-BOSNYK"),
        make_outcome(venue="kalshi", outcome_id=T_BOS, sibling=T_NYK, market_id="KXNBAGAME-BOSNYK",
                     event_id="KXNBAGAME-BOSNYK", outcome="Boston Celtics"),
    ])


async def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


# ---------------------------------------------------------------- translator
@pytest.mark.parametrize("cents,prob,american", [(53, 0.53, -113), (50, 0.50, -100), (40, 0.40, 150),
                                                 (25, 0.25, 300), (80, 0.80, -400)])
def test_cents_translate_to_probability_and_american(cents, prob, american):
    assert kalshi_cents_to_probability(cents) == pytest.approx(prob)
    assert round(kalshi_cents_to_american(cents)) == american


def test_53_cents_exact_american():
    assert kalshi_cents_to_american(53) == -112.77


@pytest.mark.parametrize("bad", [0, 100, -5, 101])
def test_translator_rejects_impossible_prices(bad):
    with pytest.raises(ValueError):
        kalshi_cents_to_probability(bad)


# ---------------------------------------------------------------- auth
def test_rsa_pss_signature_verifies():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    h = auth_headers("key-123", key, "GET", "/trade-api/ws/v2", now_ms=1790000000000)
    assert h["KALSHI-ACCESS-KEY"] == "key-123" and h["KALSHI-ACCESS-TIMESTAMP"] == "1790000000000"
    key.public_key().verify(                       # raises if the signature is wrong
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"1790000000000GET/trade-api/ws/v2",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())


# ---------------------------------------------------------------- book maths
def test_book_yes_ask_is_100_minus_best_no_bid():
    b = KalshiBook()
    b.apply_snapshot(kalshi_snapshot(T_NYK, yes=[(0.50, 300), (0.49, 100)], no=[(0.46, 200), (0.45, 50)])["msg"])
    assert b.yes_ask() == (54.0, 200) and b.yes_bid() == (50.0, 300)
    b.apply_delta(kalshi_delta(T_NYK, "no", 0.47, 120, seq=1)["msg"])     # better NO bid -> cheaper YES
    assert b.yes_ask() == (53.0, 120)
    b.apply_delta(kalshi_delta(T_NYK, "no", 0.47, -120, seq=2)["msg"])    # removed again
    assert b.yes_ask() == (54.0, 200)


def test_book_accepts_legacy_cent_fields():
    b = KalshiBook()
    b.apply_snapshot({"yes": [[50, 10]], "no": [[46, 20]]})
    b.apply_delta({"side": "no", "price": 47, "delta": 5})
    assert b.yes_ask() == (53, 5)


def test_book_rejects_bad_delta():
    with pytest.raises(ValueError):
        KalshiBook().apply_delta({"side": "maybe", "price_dollars": "0.5", "delta_fp": "1"})


# ---------------------------------------------------------------- live mock stream
async def test_feed_subscribes_streams_and_resyncs_on_sequence_gap():
    srv = await MockNovigServer().start()
    updates, states = [], []
    feed = KalshiFeed(url=srv.url, registry=kalshi_registry(), reconnect_delay=0.1,
                      on_update=lambda u, p: updates.append(u), on_state_change=lambda s, d: states.append((s, d)))
    task = asyncio.create_task(feed.run())
    try:
        await wait_until(lambda: srv.received)
        sub = json.loads(srv.received[0])
        assert sub["cmd"] == "subscribe" and sub["params"]["channels"] == ["orderbook_delta"]
        assert sub["params"]["market_tickers"] == sorted([T_NYK, T_BOS])

        await srv.broadcast({"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 1}})   # ack
        await srv.broadcast(kalshi_snapshot(T_NYK, yes=[(0.50, 300)], no=[(0.46, 200)], seq=1))
        await srv.broadcast(kalshi_delta(T_NYK, "no", 0.47, 150, seq=2))
        await wait_until(lambda: len(updates) == 2)
        assert (updates[0].price, updates[1].price, updates[1].available_volume) == (0.54, 0.53, 150)
        assert updates[1].venue == "kalshi" and updates[1].outcome == "New York Knicks"

        await srv.broadcast(kalshi_delta(T_NYK, "no", 0.48, 10, seq=9))     # seq 3..8 missing
        await wait_until(lambda: feed.connect_count == 2)
        gap = [d for s, d in states if s == "DISCONNECTED"][0]
        assert "SequenceGapError" in gap["error"] and gap["cleared_order_books"] == 1
        await wait_until(lambda: len(srv.received) == 2)                    # resubscribed -> fresh snapshot
    finally:
        await feed.stop()
        await asyncio.wait_for(task, 5)
        await srv.stop()


async def test_feed_sends_signed_headers_when_keyed():
    seen = []

    class CapturingServer(MockNovigServer):
        def _check_auth(self, connection, request):
            seen.append({k.lower(): v for k, v in request.headers.raw_items()})
            return super()._check_auth(connection, request)

    server = await CapturingServer().start()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    feed = KalshiFeed(url=server.url, key_id="kid", private_key=key, registry=kalshi_registry())
    task = asyncio.create_task(feed.run())
    try:
        await asyncio.wait_for(feed.connected.wait(), 5)
        h = seen[-1]
        assert h["kalshi-access-key"] == "kid"            # header names are case-insensitive in HTTP
        key.public_key().verify(
            base64.b64decode(h["kalshi-access-signature"]), f"{h['kalshi-access-timestamp']}GET/trade-api/ws/v2".encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    finally:
        await feed.stop()
        await asyncio.wait_for(task, 5)
        await server.stop()


# ---------------------------------------------------------------- REST bootstrap
MARKETS = {"markets": [
    {"ticker": T_NYK, "event_ticker": "KXNBAGAME-BOSNYK", "title": "Boston at New York Winner?",
     "yes_sub_title": "New York", "status": "open"},
    {"ticker": T_BOS, "event_ticker": "KXNBAGAME-BOSNYK", "title": "Boston at New York Winner?",
     "yes_sub_title": "Boston", "status": "open"},
    {"ticker": "KXNBAGAME-X-Y", "event_ticker": "KXNBAGAME-X", "title": "Gotham at Metropolis Winner?",
     "yes_sub_title": "Gotham", "status": "open"},
    {"ticker": "KXNBAGAME-OLD", "event_ticker": "KXNBAGAME-OLD", "title": "Boston at New York Winner?",
     "yes_sub_title": "Boston", "status": "settled"},
], "cursor": ""}


def test_parse_kalshi_markets():
    rows = {r.outcome_id: r for r in parse_kalshi_markets(MARKETS, "NBA")}
    assert set(rows) == {T_NYK, T_BOS}
    nyk = rows[T_NYK]
    assert (nyk.home_team, nyk.away_team, nyk.outcome, nyk.sibling_outcome_id, nyk.venue) == (
        "New York Knicks", "Boston Celtics", "New York Knicks", T_BOS, "kalshi")


def test_series_from_env():
    assert series_from_env(None) == kalshi_feed.DEFAULT_SERIES
    assert series_from_env("KXNFLGAME:nfl") == {"KXNFLGAME": "NFL"}


def test_defaults_are_demo():
    assert KalshiFeed().url == "wss://demo-api.kalshi.co/trade-api/ws/v2"
