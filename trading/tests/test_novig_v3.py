"""Novig v3: official signing vectors, grid, catalog parsing, the derived book, orders, fills and settlement."""

import asyncio
import base64
import json
import time
from pathlib import Path

import pytest
from aiohttp import web
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import novig_v3 as nv
from main_supervisor import ConfigError, Supervisor, build_live_supervisor
from novig_feed import MarketRegistry

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "novig_signing_vectors.json").read_text())


def _key(name):
    return serialization.load_pem_private_key(VECTORS["keypairs"][name]["private_key_pkcs8_pem"].encode(), None)


# ---------------------------------------------------------------- signing
@pytest.mark.parametrize("vec", VECTORS["vectors"], ids=lambda v: v["id"])
def test_official_signing_vectors(vec):
    i = vec["input"]
    text = nv.string_to_sign(i["timestamp"], i["method"], i["path"], i["query"], i["body"].encode())
    assert text == vec["string_to_sign"]
    signer = nv.NovigSigner("kid", _key(vec["keypair_id"]))
    sig = signer.sign(text)
    if vec["algorithm"] == "ed25519":                       # deterministic: must match byte for byte
        assert sig == vec["signature"]
    else:                                                   # ECDSA is randomised: verify instead
        pub = _key(vec["keypair_id"]).public_key()
        pub.verify(base64.b64decode(sig), text.encode(), ec.ECDSA(hashes.SHA256()))
        pub.verify(base64.b64decode(vec["signature"]), text.encode(), ec.ECDSA(hashes.SHA256()))


def test_build_query_is_canonical_and_drops_none():
    assert nv.build_query({"status": "OPEN_PREGAME", "limit": 5000, "after": None,
                           "marketType": "MONEY,SPREAD"}) == "limit=5000&marketType=MONEY%2CSPREAD&status=OPEN_PREGAME"
    assert nv.build_query(None) == ""


# ---------------------------------------------------------------- grid and units
@pytest.mark.parametrize("p,want", [(0.667, 0.665), (0.665, 0.665), (0.0525, 0.05), (0.0009, None),
                                    (0.9999, 0.999), (0.953, 0.953), (0.947, 0.945), (0.012, 0.012)])
def test_snap_down(p, want):
    assert nv.snap_down(p) == want


def test_grid_has_279_prices_and_is_closed_under_complement():
    grid = [m / 1000 for m in range(1, 1000) if nv.on_grid(m / 1000)]
    assert len(grid) == 279
    assert all(nv.on_grid(round(1 - p, 3)) for p in grid)


def test_units_one_engine_contract_is_100_novig_contracts():
    assert nv.to_novig_qty(3) == 300 and nv.from_novig_qty(110) == 1.1


# ---------------------------------------------------------------- catalog
def _catalog():
    ev = {"eventId": "e1", "sport": "FOOTBALL", "league": "NFL", "status": "OPEN_PREGAME",
          "description": "Chiefs at Bills", "startsTs": 1_900_000_000_000}
    fee = {"coefficient": "0.03", "makerCredit": "0.5", "charged": "WHEN_LIVE"}
    markets = [
        {"marketId": "m-ml", "eventId": "e1", "marketType": "MONEY", "status": "OPEN", "fee": fee,
         "outcomes": [{"outcomeId": "o-buf", "name": "Buffalo Bills"}, {"outcomeId": "o-kc", "name": "Kansas City Chiefs"}]},
        {"marketId": "m-sp", "eventId": "e1", "marketType": "SPREAD", "status": "OPEN", "strike": "-3.5", "fee": fee,
         "outcomes": [{"outcomeId": "s-kc", "name": "Kansas City Chiefs +3.5"}, {"outcomeId": "s-buf", "name": "Buffalo Bills -3.5"}]},
        {"marketId": "m-to", "eventId": "e1", "marketType": "TOTAL", "status": "OPEN", "strike": "48.5", "fee": fee,
         "outcomes": [{"outcomeId": "t-u", "name": "Under 48.5"}, {"outcomeId": "t-o", "name": "Over 48.5"}]},
        {"marketId": "m-fut", "eventId": "e1", "marketType": "MONEY", "status": "OPEN",
         "fee": {"coefficient": "0.06", "makerCredit": "0.7", "charged": "ALWAYS"},
         "outcomes": [{"outcomeId": "f1", "name": "Kansas City Chiefs"}, {"outcomeId": "f2", "name": "Buffalo Bills"}]},
    ]
    return [ev], markets


def test_catalog_maps_home_away_lines_and_siblings():
    rows = {r.outcome_id: r for r in nv.build_market_infos(*_catalog())}
    assert set(rows) == {"o-buf", "o-kc", "s-kc", "s-buf", "t-u", "t-o"}     # the ALWAYS-fee market is skipped
    buf = rows["o-buf"]
    assert (buf.home_team, buf.away_team, buf.outcome) == ("Buffalo Bills", "Kansas City Chiefs", "Buffalo Bills")
    assert buf.market_type == "moneyline" and buf.sibling_outcome_id == "o-kc" and buf.start_time == 1_900_000_000
    assert rows["s-buf"].line == -3.5 and rows["s-kc"].line == 3.5          # strike is the home side's handicap
    assert rows["t-o"].outcome == "over" and rows["t-o"].line == 48.5 and rows["t-o"].sibling_outcome_id == "t-u"


@pytest.mark.parametrize("desc,want", [("Chiefs at Bills", ("Bills", "Chiefs")),
                                       ("Chiefs @ Bills, Sun 20:20", ("Bills", "Chiefs")),
                                       ("Sinner vs. Alcaraz", ("Sinner", "Alcaraz")), ("Chiefs", None)])
def test_parse_matchup(desc, want):
    assert nv.parse_matchup(desc) == want


def test_unparseable_event_is_skipped_not_guessed():
    events, markets = _catalog()
    events[0]["description"] = "Week 6 showdown"
    assert nv.build_market_infos(events, markets) == []


# ---------------------------------------------------------------- feed
def _signer():
    return nv.NovigSigner("kid", Ed25519PrivateKey.generate())


def _feed(**kw):
    reg = MarketRegistry(nv.build_market_infos(*_catalog()))
    updates, slips, life, sent = [], [], [], []
    feed = nv.NovigV3Feed(nv.NovigV3Client("https://api.novig.com", _signer()), registry=reg,
                          on_update=lambda u, p: updates.append(u), on_slip=slips.append,
                          on_lifecycle=lambda info, t: life.append((info.market_id, t)), **kw)

    class WS:
        async def send(self, msg):
            sent.append(json.loads(msg))
    feed._ws_conn = feed._ws = WS()
    return feed, updates, slips, life, sent


async def test_asks_come_from_the_other_outcomes_bids():
    feed, updates, *_ = _feed()
    await feed._handle_raw(json.dumps({"snapshot": {"m-ml": {"eventId": "e1", "book": {"seq": 10, "orders": {
        "o-kc": [{"order": "a", "price": "0.335", "qty": 180}, {"order": "b", "price": "0.325", "qty": 400}],
        "o-buf": [{"order": "c", "price": "0.660", "qty": 250}]}}, "lifecycle": {"seq": 1, "status": "OPEN"}}}}))
    buf = feed.latest["o-buf"]
    assert buf.price == 0.665 and buf.available_volume == 1.8            # 180 Novig contracts = $1.80 payout
    assert buf.ask_levels == [(0.665, 1.8), (0.675, 4.0)]
    assert buf.best_bid == 0.66 and buf.bid_volume == 2.5
    assert feed.latest["o-kc"].price == 0.34


async def test_deltas_gaps_and_own_orders():
    feed, updates, slips, life, sent = _feed()
    await feed._handle_raw(json.dumps({"snapshot": {"m-ml": {"book": {"seq": 10, "orders": {
        "o-kc": [{"order": "a", "price": "0.335", "qty": 180}]}}}}}))
    await feed._handle_raw(json.dumps({"delta": {"m-ml": {"book": {"seq": 11, "deltas": [
        {"kind": "add", "order": "d", "outcome": "o-kc", "price": "0.340", "qty": 100},
        {"kind": "remove", "order": "a", "reason": "fill"}]}}}}))
    assert feed.latest["o-buf"].price == 0.66 and feed.latest["o-buf"].available_volume == 1.0
    feed.own_orders.add("d")                                             # our own quote is never "liquidity"
    await feed._handle_raw(json.dumps({"delta": {"m-ml": {"book": {"seq": 12, "deltas": [
        {"kind": "add", "order": "e", "outcome": "o-kc", "price": "0.300", "qty": 500}]}}}}))
    assert feed.latest["o-buf"].ask_levels == [(0.7, 5.0)]
    await feed._handle_raw(json.dumps({"delta": {"m-ml": {"book": {"seq": 14, "deltas": []}}}}))   # 13 missing
    assert feed.books["m-ml"].resync and "o-buf" not in feed.latest
    feed.subscribed.update({"m-ml", "m-sp", "m-to"})
    feed._tokens = nv.STREAM_CAPACITY
    await feed.sync_subscriptions()                                  # maintenance retries until it goes through
    assert sent[-1]["snapshot"] == {"markets": {"m-ml": "book"}}
    await feed._handle_raw(json.dumps({"snapshot": {"m-ml": {"book": {"seq": 20, "orders": {}}}}}))
    assert not feed.books["m-ml"].resync


async def test_golive_reaches_the_engine_and_clears_prices():
    feed, updates, slips, life, sent = _feed()
    await feed._handle_raw(json.dumps({"snapshot": {"m-ml": {"book": {"seq": 1, "orders": {
        "o-kc": [{"order": "a", "price": "0.335", "qty": 180}]}}}}}))
    await feed._handle_raw(json.dumps({"delta": {"m-ml": {"lifecycle": {"seq": 2, "deltas": ["GOLIVE"]}}}}))
    assert life == [("m-ml", "GOLIVE")] and "o-buf" not in feed.latest


async def test_private_fills_become_engine_slips():
    feed, updates, slips, *_ = _feed()
    await feed._handle_raw(json.dumps({"orders": {"seq": 5, "open": []}}))
    await feed._handle_raw(json.dumps({"delta": {}, "orders": {"seq": 6, "deltas": [
        {"kind": "fill", "orderId": "x", "outcomeId": "o-buf", "price": "0.660", "qty": 40, "remaining": 60},
        {"kind": "fill", "orderId": "x", "outcomeId": "o-buf", "price": "0.665", "qty": 60, "remaining": 0},
        {"kind": "reject", "orderId": "y"}]}}))
    assert [(s.order_id, s.status, s.filled_volume, s.price_cents) for s in slips] == [
        ("x", "PARTIAL", 0.4, 66.0), ("x", "FILLED", 0.6, 66.5), ("y", "REJECTED", 0, None)]


async def test_subscribe_waits_for_a_full_throttle_and_counts_only_acked_markets():
    feed, updates, slips, life, sent = _feed()
    await feed._on_open(feed._ws)
    feed._maintenance.cancel()
    assert sent[0]["subscribe"] == {"private": ["orders"]}          # alone, first
    await feed._handle_raw(json.dumps({"nonce": sent[0]["nonce"], "subscribed": {"private": ["orders"]},
                                       "snapshot": {}, "orders": {"seq": 0, "open": []}}))
    await feed.sync_subscriptions()                                  # 3 markets x 16 = 48 tokens: fits now
    sub = sent[-1]
    assert set(sub["subscribe"]["markets"]) == {"m-ml", "m-sp", "m-to"} and feed.subscribed == set()
    await feed._handle_raw(json.dumps({"nonce": sub["nonce"], "subscribed": {"markets": sub["subscribe"]["markets"]},
                                       "snapshot": {}}))
    assert feed.subscribed == {"m-ml", "m-sp", "m-to"}


def test_a_subscribe_heavier_than_the_bucket_needs_a_full_bucket():
    feed, *_ = _feed()
    feed._tokens, feed._tokens_at = nv.STREAM_CAPACITY - 32, __import__("time").monotonic()
    assert not feed._spend(100 * nv.BOOK_WEIGHT)                     # 1600 > 512: only on a full bucket
    assert 7 < feed._wait_for(100 * nv.BOOK_WEIGHT) <= 8.1
    feed._tokens = nv.STREAM_CAPACITY
    assert feed._spend(100 * nv.BOOK_WEIGHT) and feed._tokens == 0


async def test_rejected_request_without_nonce_is_retried():
    feed, updates, slips, life, sent = _feed()
    feed._ws_conn = feed._ws
    await feed.sync_subscriptions()
    assert feed._pending
    await feed._handle_raw(json.dumps({"code": "RATE_LIMIT_EXCEEDED", "message": "slow down"}))   # no nonce
    assert not feed._pending and feed.subscribed == set()
    feed._tokens = nv.STREAM_CAPACITY
    await feed.sync_subscriptions()
    assert set(sent[-1]["subscribe"]["markets"]) == {"m-ml", "m-sp", "m-to"}


# ---------------------------------------------------------------- REST over a local server
@pytest.fixture
async def novig_server(aiohttp_unused_port=None):
    key = Ed25519PrivateKey.generate()
    seen = []

    async def handle(request):
        body = await request.read()
        text = nv.string_to_sign(request.headers["Novig-Timestamp"], request.method, request.path,
                                 request.query_string, body)
        key.public_key().verify(base64.b64decode(request.headers["Novig-Signature"]), text.encode())
        seen.append((request.method, request.path, request.query_string, body))
        if request.path == "/v3/orders":
            return web.json_response({"orderId": "11111111-1111-1111-1111-111111111111"}, status=201)
        if request.path == "/v3/orders/bad":
            return web.json_response({"code": "GEOLOCATION_EXPIRED", "message": "open the app"}, status=451)
        if request.path == "/v3/catalog/markets":
            if "after=p2" in request.query_string:
                return web.json_response({"items": [{"marketId": "b"}]})
            return web.json_response({"items": [{"marketId": "a"}], "next": "p2"})
        return web.json_response({})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", nv.NovigSigner("kid", key), seen
    await runner.cleanup()


async def test_signed_requests_verify_server_side_and_pages_follow(novig_server):
    host, signer, seen = novig_server
    client = nv.NovigV3Client(host, signer)
    try:
        items = await client.pages("/v3/catalog/markets", {"marketType": "MONEY,SPREAD", "limit": 5000})
        assert [i["marketId"] for i in items] == ["a", "b"]
        assert nv.canonical_query(seen[0][2]) == "limit=5000&marketType=MONEY%2CSPREAD"   # what we signed
        with pytest.raises(nv.NovigApiError) as exc:
            await client.request("GET", "/v3/orders/bad")
        assert exc.value.status == 451 and exc.value.code == "GEOLOCATION_EXPIRED"
    finally:
        await client.close()


async def test_gateway_converts_units_snaps_and_routes_sells(novig_server):
    host, signer, seen = novig_server
    own = set()
    gw = nv.NovigV3OrderGateway(nv.NovigV3Client(host, signer), sibling=lambda o: "other" if o == "o1" else None,
                                on_order=own.add)
    try:
        oid = await gw.place_limit("o1", "buy", 66.7, 3, "tk-1")
        body = json.loads(seen[-1][3])
        assert body["outcomeId"] == "o1" and body["price"] == "0.665" and body["qty"] == 300 and body["tif"] == "IOC"
        assert oid in own and len(body["clientId"]) == 36
        await gw.place_limit("o1", "sell", 60.0, 1, "mk-1")             # sell o1 at 60c = buy the other at 40c
        body = json.loads(seen[-1][3])
        assert (body["outcomeId"], body["price"], body["qty"]) == ("other", "0.400", 100)
        await gw.cancel_orders(["a", "b"])
        assert seen[-1][0] == "DELETE" and json.loads(seen[-1][3]) == {"orderIds": ["a", "b"]}
    finally:
        await gw.close()
    maker = nv.NovigV3OrderGateway(nv.NovigV3Client(host, signer), tif="GTT", ttl_ms=60_000)
    assert maker.order_body("o1", 50, 1, "mk-2")["ttl"] == 60_000
    assert nv.NovigV3OrderGateway.filled_count({"status": "OPEN", "qty": 100, "remaining": 50}) is None
    assert nv.NovigV3OrderGateway.filled_count({"status": "CANCELED", "qty": 300, "remaining": 100}) == 2.0


# ---------------------------------------------------------------- settlement
class FakeClient:
    host = "https://api.novig.com"

    def __init__(self, responses):
        self.responses = responses

    async def request(self, method, path, params=None, body=None):
        return self.responses[path]


async def test_positions_and_grades():
    client = FakeClient({
        "/v3/account/positions": {"seq": 3, "positions": [
            {"marketId": "m1", "outcomeId": "w", "qty": 300, "cost": "1.95000"},
            {"marketId": "m2", "outcomeId": "f", "qty": 100, "cost": "0.50000"}]},
        "/v3/catalog/markets/m1": {"status": "SETTLED", "outcomes": [{"outcomeId": "w", "status": "WIN"}]},
        "/v3/catalog/markets/m2": {"status": "SETTLED", "outcomes": [{"outcomeId": "f", "status": "0.731"}]},
    })
    pc = nv.NovigV3PositionsClient(client)
    opened = await pc.open_positions()
    assert [(p.outcome_id, p.contracts, p.cost_usd) for p in opened] == [("w", 3.0, 1.95), ("f", 1.0, 0.5)]
    settled = {p.outcome_id: p for p in await pc.settled_positions()}
    assert settled["w"].result == "WIN" and settled["w"].contracts == 3.0
    assert settled["f"].result == "FMV" and settled["f"].settle_value == 0.731     # fair value, not a win
    from settlement import settlement_pnl
    assert settlement_pnl(settled["f"], 0.5)[0] == pytest.approx(0.23, abs=0.01)  # 1 x 0.731 - 0.50
    assert await pc.settled_positions() == []                                     # never twice


# ---------------------------------------------------------------- configuration
def _pem(tmp_path):
    path = tmp_path / "k.pem"
    path.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(path)


def test_key_selects_v3_and_paper_needs_no_token(tmp_path):
    sup = build_live_supervisor({"NOVIG_KEY_ID": "kid", "NOVIG_PRIVATE_KEY_PATH": _pem(tmp_path),
                                 "TRADING_LOG_DIR": str(tmp_path), "RESEARCH_ENABLED": "0"})
    assert isinstance(sup.feed, nv.NovigV3Feed) and sup.feed.url == "wss://api.novig.com/v3/ws"
    assert isinstance(sup.novig_rest, nv.NovigV3Client) and sup.feed.registry is sup.registry


def test_v3_without_key_file_is_refused(tmp_path):
    with pytest.raises(ConfigError):
        build_live_supervisor({"NOVIG_API": "v3", "TRADING_LOG_DIR": str(tmp_path)})
    with pytest.raises(ConfigError):
        build_live_supervisor({"NOVIG_KEY_ID": "kid", "NOVIG_PRIVATE_KEY_PATH": str(tmp_path / "missing.pem"),
                               "TRADING_LOG_DIR": str(tmp_path)})


def test_live_v3_uses_ioc_takers_gtt_maker_and_incremental_fills(tmp_path):
    sup = build_live_supervisor({"NOVIG_KEY_ID": "kid", "NOVIG_PRIVATE_KEY_PATH": _pem(tmp_path),
                                 "TRADING_MODE": "live", "LIVE_TRADING_ACKNOWLEDGED": "yes",
                                 "TRADING_LOG_DIR": str(tmp_path), "RESEARCH_ENABLED": "0", "NOVIG_ENV": "qa"})
    assert sup.order_gateway.time_in_force == "IOC" and sup.maker_gateway.time_in_force == "GTT"
    assert sup.fill_volume_mode == "incremental" and sup.feed.on_slip is not None
    assert isinstance(sup.positions_client, nv.NovigV3PositionsClient)
    assert sup.order_gateway.api_base == nv.QA_HOST
    assert list(sup.positions_client.watch()) == []


async def test_golive_marks_the_game_live(tmp_path):
    sup = build_live_supervisor({"NOVIG_KEY_ID": "kid", "NOVIG_PRIVATE_KEY_PATH": _pem(tmp_path),
                                 "TRADING_LOG_DIR": str(tmp_path), "RESEARCH_ENABLED": "0"})
    sup.registry.replace_all(nv.build_market_infos(*_catalog()))
    await sup.on_novig_lifecycle(sup.registry.get("o-buf"), "GOLIVE")
    assert len(sup.live_games) == 1


async def test_live_start_cancels_orders_left_from_earlier_sessions(novig_server):
    host, signer, seen = novig_server
    gw = nv.NovigV3OrderGateway(nv.NovigV3Client(host, signer))
    try:
        await gw.cancel_all()
        assert seen[-1][:3] == ("DELETE", "/v3/orders", "")
    finally:
        await gw.close()


async def test_own_order_seen_after_its_book_add_is_removed_at_once():
    feed, updates, *_ = _feed()
    await feed._handle_raw(json.dumps({"snapshot": {"m-ml": {"book": {"seq": 1, "orders": {
        "o-kc": [{"order": "mine", "price": "0.335", "qty": 180}]}}}}}))
    assert feed.latest["o-buf"].price == 0.665                           # our bid shows as liquidity...
    feed.mark_own("mine")
    await asyncio.sleep(0)
    assert "o-buf" not in feed.latest                                    # ...until we recognise it


def test_spread_names_that_contradict_the_strike_are_skipped():
    events, markets = _catalog()
    markets[1]["outcomes"] = [{"outcomeId": "s-kc", "name": "Kansas City Chiefs -3.5"},
                              {"outcomeId": "s-buf", "name": "Buffalo Bills +3.5"}]    # opposite of strike -3.5 home
    assert not {"s-kc", "s-buf"} & {r.outcome_id for r in nv.build_market_infos(events, markets)}
    assert nv._line_in_name("Buffalo Bills -3.5") == -3.5 and nv._line_in_name("Over 48.5") is None


async def test_lost_fills_are_recovered_from_order_records(tmp_path):
    from main_supervisor import LiveOrder
    sup = build_live_supervisor({"NOVIG_KEY_ID": "kid", "NOVIG_PRIVATE_KEY_PATH": _pem(tmp_path),
                                 "TRADING_MODE": "live", "LIVE_TRADING_ACKNOWLEDGED": "yes",
                                 "TRADING_LOG_DIR": str(tmp_path), "RESEARCH_ENABLED": "0"})
    sup.live_orders["x1"] = LiveOrder(exchange_order_id="x1", kind="MAKER", outcome_id="o", key=("NFL", "a", "b", "m"),
                                      requested=5, limit_price=0.4, maker_side="buy", fill_mode="incremental")
    booked = []

    async def get_order(oid):
        return {"status": "OPEN", "qty": 500, "remaining": 200}           # 3 engine contracts filled
    sup.maker_gateway.get_order = get_order

    async def capture(slip):
        booked.append(slip)
    sup.on_fill_slip = capture
    assert await sup.recover_novig_fills() == 1
    assert booked[0].filled_volume == 3.0 and booked[0].price_cents == 40.0 and booked[0].status == "PARTIAL"
