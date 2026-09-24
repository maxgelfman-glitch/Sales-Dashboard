"""
Self-test: live Kalshi execution (kalshi_trading.py + supervisor wiring). A local aiohttp app plays Kalshi's
REST API; fills are injected as parsed "fill" messages. Nothing touches the real exchange.
"""

import asyncio
import json

import pytest
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import rsa

from execution import kalshi_taker_fee
from kalshi_trading import KalshiOrderGateway, KalshiPositionsClient, parse_kalshi_fill
from main_supervisor import ConfigError, build_live_supervisor, format_state_report
from novig_private import FillSlip
from settlement import ExchangePosition
from tests.test_live_execution import live_sup, slip, upd

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
K_NYK, K_BOS = "KXNBAGAME-BOSNYK-NYK", "KXNBAGAME-BOSNYK-BOS"


# ---------------------------------------------------------------------------
# Gateway against a local fake Kalshi
# ---------------------------------------------------------------------------
@pytest.fixture
async def fake_kalshi():
    state = {"orders": [], "cancels": [], "headers": [], "order_status": {}}

    async def create(request):
        state["headers"].append(dict(request.headers))
        body = await request.json()
        state["orders"].append(body)
        if body.get("count", 0) > 10_000:
            return web.json_response({"error": {"code": "invalid"}}, status=400)
        return web.json_response({"order": {"order_id": f"k{len(state['orders'])}", "status": "executed"}})

    async def cancel(request):
        state["cancels"].append(request.match_info["oid"])
        return web.json_response({}, status=404)        # IOC order already finished

    async def get_order(request):
        return web.json_response({"order": state["order_status"].get(request.match_info["oid"], {})})

    async def positions(request):
        if request.query.get("cursor") == "p2":
            return web.json_response({"market_positions": [{"ticker": K_BOS, "position": -5}], "cursor": ""})
        return web.json_response({"market_positions": [
            {"ticker": K_NYK, "position": 20, "market_exposure": 950}], "cursor": "p2"})

    async def settlements(request):
        return web.json_response({"settlements": [
            {"ticker": K_NYK, "market_result": "yes", "yes_count": 20, "yes_total_cost": 950, "revenue": 2000,
             "settled_time": "2026-10-01T03:00:00Z"},
            {"ticker": "OTHER", "market_result": "no", "yes_count": 0, "no_count": 3}]})

    app = web.Application()
    app.router.add_post("/trade-api/v2/portfolio/orders", create)
    app.router.add_delete("/trade-api/v2/portfolio/orders/{oid}", cancel)
    app.router.add_get("/trade-api/v2/portfolio/orders/{oid}", get_order)
    app.router.add_get("/trade-api/v2/portfolio/positions", positions)
    app.router.add_get("/trade-api/v2/portfolio/settlements", settlements)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state["base"] = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/trade-api/v2"
    yield state
    await runner.cleanup()


async def test_gateway_signs_and_sends_ioc_yes_orders(fake_kalshi):
    gw = KalshiOrderGateway(fake_kalshi["base"], "key-id", KEY)
    try:
        oid = await gw.place_limit(K_NYK, "buy", 45.0, 100, "tk-abc-1")
        assert oid == "k1"
        body, headers = fake_kalshi["orders"][0], fake_kalshi["headers"][0]
        assert body == {"ticker": K_NYK, "action": "buy", "side": "yes", "count": 100, "type": "limit",
                        "client_order_id": "tk-abc-1", "yes_price": 45, "time_in_force": "immediate_or_cancel"}
        assert headers["KALSHI-ACCESS-KEY"] == "key-id" and headers["KALSHI-ACCESS-SIGNATURE"]
        await gw.place_limit(K_NYK, "buy", 45.5, 1, "tk-abc-2")                 # sub-cent price
        assert fake_kalshi["orders"][1]["yes_price_dollars"] == "0.4550" and "yes_price" not in fake_kalshi["orders"][1]
        with pytest.raises(Exception):
            await gw.place_limit(K_NYK, "buy", 45.0, 20_000, "tk-abc-3")        # exchange rejects
        await gw.cancel_orders(["k1"])                                          # 404 = already done: fine
        assert fake_kalshi["cancels"] == ["k1"]
    finally:
        await gw.close()


def test_filled_count_variants():
    f = KalshiOrderGateway.filled_count
    assert f({"fill_count": 7}) == 7 and f({"taker_fill_count": "3"}) == 3
    assert f({"status": "canceled", "count": 10, "remaining_count": 4}) == 6
    assert f({"status": "resting"}) is None


def test_parse_fill_variants():
    s = parse_kalshi_fill({"type": "fill", "msg": {"order_id": "k1", "yes_price": 45, "count": 12}})
    assert (s.order_id, s.filled_volume, s.price_cents, s.venue) == ("k1", 12, 45, "kalshi")
    s = parse_kalshi_fill({"type": "fill", "msg": {"order_id": "k1", "yes_price_dollars": "0.4550", "count_fp": "2"}})
    assert (s.price_cents, s.filled_volume) == (45.5, 2)
    s = parse_kalshi_fill({"type": "fill", "msg": {"order_id": "k1", "no_price": 55, "count": 1}})
    assert s.price_cents == 45
    assert parse_kalshi_fill({"type": "orderbook_delta"}) is None
    assert parse_kalshi_fill({"type": "fill", "msg": {"order_id": "k1", "count": 0}}) is None


async def test_positions_client_reads_open_and_settled(fake_kalshi):
    client = KalshiPositionsClient(fake_kalshi["base"], "key-id", KEY)
    try:
        [open_pos] = await client.open_positions()                             # the NO position is not ours
        assert (open_pos.outcome_id, open_pos.contracts, open_pos.cost_usd) == (K_NYK, 20, 9.5)
        [settled] = await client.settled_positions()
        assert (settled.outcome_id, settled.result, settled.payout_usd, settled.cost_usd) == (K_NYK, "WIN", 20.0, 9.5)
        assert settled.is_settled and settled.settlement_id.startswith("kalshi-")
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Engine: live Kalshi takers, fees, zero-fill confirmation, cross-venue hedges, settlement
# ---------------------------------------------------------------------------
class FakeKalshiGateway:
    live, api_base, time_in_force = True, "fake://kalshi", "immediate_or_cancel"
    order_body = staticmethod(KalshiOrderGateway.order_body)
    filled_count = staticmethod(KalshiOrderGateway.filled_count)

    def __init__(self, reported_fill=0):
        self.placed, self.cancels, self.n = [], [], 0
        self.reported_fill = reported_fill

    async def place_limit(self, outcome_id, side, price_cents, contracts, client_id):
        self.n += 1
        self.placed.append(dict(outcome_id=outcome_id, price_cents=price_cents, contracts=contracts,
                                client_id=client_id))
        return f"k{self.n}"

    async def cancel_orders(self, ids):
        self.cancels.append(list(ids))

    async def get_order(self, oid):
        return {"status": "canceled", "fill_count": self.reported_fill}

    async def close(self):
        pass


def kalshi_sup(gw=None, **kw):
    sup = live_sup(kalshi_gateway=gw or FakeKalshiGateway(), **kw)
    sup.kalshi.connected.set()
    return sup


async def test_kalshi_edge_is_traded_live_with_fee_in_the_cost():
    sup = kalshi_sup()
    await sup.on_market_update(upd(K_NYK, 0.45), None)                          # fair 0.5217
    [sent] = sup.kalshi_gateway.placed
    assert sup.order_gateway.placed == []                                        # nothing went to Novig
    assert sent["outcome_id"] == K_NYK and sent["price_cents"] == 45.0
    assert sent["client_id"].startswith(f"tk-{sup.session_tag}-")
    leg = sup.orders[0]
    assert leg.venue == "kalshi" and leg.pending
    await sup.on_fill_slip(FillSlip(order_id="k1", status="PARTIAL", filled_volume=100, price_cents=45,
                                    venue="kalshi"))
    await sup.on_fill_slip(FillSlip(order_id="k1", status="PARTIAL", filled_volume=sent["contracts"] - 100,
                                    price_cents=45, venue="kalshi"))
    fee = kalshi_taker_fee(100, 45) + kalshi_taker_fee(sent["contracts"] - 100, 45)
    assert not leg.pending and leg.contracts == sent["contracts"]
    assert leg.stake_usd == pytest.approx(round(sent["contracts"] * 0.45 + fee, 2))
    assert sup.total_exposure() == pytest.approx(leg.stake_usd) and sup.kalshi_fills_confirmed


async def test_kalshi_zero_fill_is_confirmed_by_the_order_record_and_released():
    sup = kalshi_sup(timeout=0.05)
    await sup.on_market_update(upd(K_NYK, 0.45), None)
    assert not sup.kalshi_fills_confirmed
    await asyncio.sleep(0.15)
    assert sup.total_exposure() == 0 and not sup.positions                       # released, not UNCONFIRMED


async def test_kalshi_fills_missed_by_the_socket_are_booked_from_the_order_record():
    sup = kalshi_sup(FakeKalshiGateway(reported_fill=30), timeout=0.05)
    await sup.on_market_update(upd(K_NYK, 0.45), None)
    await asyncio.sleep(0.15)
    leg = sup.orders[0]
    assert leg.contracts == 30 and not leg.pending
    assert sup.total_exposure() == pytest.approx(round(30 * 0.45 + kalshi_taker_fee(30, 45), 2))


async def test_novig_first_leg_hedged_on_kalshi_live():
    sup = kalshi_sup()
    await sup.on_market_update(upd("O-NYK", 0.49, volume=1000), None)
    await sup.on_fill_slip(slip("ex-1", "FILLED", 1000, 49))
    await sup.on_market_update(upd(K_BOS, 0.45), None)                          # 49 + 45 + fee < 100
    [hedge] = sup.kalshi_gateway.placed
    assert hedge["outcome_id"] == K_BOS and hedge["contracts"] == 1000


async def test_without_kalshi_live_kalshi_stays_data_only():
    sup = live_sup()
    await sup.on_market_update(upd(K_NYK, 0.45), None)
    assert sup.orders == [] and sup.stats["kalshi_exec_disabled"] == 1


async def test_kalshi_positions_sync_and_settle():
    class FakePositions:
        def __init__(self):
            self.open, self.settled = [], []

        async def open_positions(self):
            return self.open

        async def settled_positions(self):
            return self.settled

        async def close(self):
            pass

    kp = FakePositions()
    kp.open = [ExchangePosition(settlement_id="kalshi-open-x", outcome_id=K_NYK, contracts=20, cost_usd=9.5,
                                status="OPEN")]
    sup = kalshi_sup(kalshi_positions_client=kp)
    sup.synced = False
    assert await sup.startup_sync()
    [restored] = sup.orders
    assert restored.venue == "kalshi" and restored.kind == "RESTORED" and sup.total_exposure() == 9.5
    kp.open, kp.settled = [], [ExchangePosition(settlement_id="kalshi-s1", outcome_id=K_NYK, contracts=20,
                                                status="SETTLED", payout_usd=20.0, result="WIN")]
    [row] = await sup.settlement_sweep()
    assert row["net_profit_usd"] == 10.5 and sup.total_exposure() == 0


async def test_kalshi_feed_routes_fill_messages():
    from kalshi_feed import KalshiFeed
    got = []

    async def on_fill(s):
        got.append(s)
    feed = KalshiFeed(url="ws://127.0.0.1:1/x", on_fill=on_fill)
    await feed._handle_raw(json.dumps({"type": "fill", "sid": 9, "msg": {"order_id": "k1", "yes_price": 45,
                                                                          "count": 3}}))
    assert got[0].order_id == "k1" and feed.fills_received == 1


def test_config_gates(tmp_path):
    pem = tmp_path / "k.pem"
    from cryptography.hazmat.primitives import serialization
    pem.write_bytes(KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    base = {"NOVIG_BEARER_TOKEN": "t", "RESEARCH_ENABLED": "0"}
    with pytest.raises(ConfigError):
        build_live_supervisor({**base, "KALSHI_LIVE_TRADING": "1"})                        # needs KALSHI_ENABLED
    with pytest.raises(ConfigError):
        build_live_supervisor({**base, "KALSHI_ENABLED": "1", "KALSHI_KEY_ID": "k", "KALSHI_PRIVATE_KEY_PATH": str(pem),
                               "KALSHI_LIVE_TRADING": "1"})                                # paper mode
    live = {**base, "TRADING_MODE": "live", "LIVE_TRADING_ACKNOWLEDGED": "yes", "NOVIG_WS_URL": "wss://api.novig.com/tape",
            "KALSHI_ENABLED": "1", "KALSHI_KEY_ID": "k", "KALSHI_PRIVATE_KEY_PATH": str(pem),
            "KALSHI_LIVE_TRADING": "1", "TRADING_LOG_DIR": str(tmp_path)}
    sup = build_live_supervisor(live)
    assert sup.kalshi_live and sup.kalshi_positions_client is not None
    assert "LIVE execution (IOC orders" in format_state_report(sup)
