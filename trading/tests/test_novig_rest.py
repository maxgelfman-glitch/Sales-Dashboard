"""Self-test: novig_rest.py — hierarchical bootstrap parsing and the order gateways."""

import pytest
from aiohttp import web

from novig_feed import MarketRegistry
from novig_rest import NovigOrderGateway, NovigRestClient, PaperOrderGateway, parse_event_hierarchy

EVENTS = {"events": [
    {"eventId": "E1", "league": "NBA", "homeTeam": "NY Knicks", "awayTeam": "Boston", "status": "OPEN",
     "markets": [
         {"marketId": "M1", "type": "MONEYLINE",
          "outcomes": [{"outcomeId": "O1", "name": "New York Knicks"}, {"outcomeId": "O2", "name": "Boston Celtics"}]},
         {"marketId": "M2", "type": "SPREAD", "strike": -4.5,               # market-level line, home view
          "outcomes": [{"outcomeId": "O3", "name": "New York Knicks"}, {"outcomeId": "O4", "name": "Boston Celtics"}]},
         {"marketId": "M3", "type": "TOTAL", "line": 221.5,
          "outcomes": [{"outcomeId": "O5", "name": "Over"}, {"outcomeId": "O6", "name": "Under"}]},
         {"marketId": "M4", "type": "PLAYER_POINTS",                       # untracked market type
          "outcomes": [{"outcomeId": "O7", "name": "Over"}, {"outcomeId": "O8", "name": "Under"}]},
         {"marketId": "M5", "type": "MONEYLINE", "outcomes": [{"outcomeId": "O9", "name": "x"}]},  # malformed
     ]},
    {"id": "E2", "league": {"name": "NFL"}, "homeTeam": {"name": "Kansas City Chiefs"},
     "awayTeam": {"name": "Buffalo Bills"},
     "markets": [{"id": "M6", "marketType": "spread",
                  "outcomes": [{"id": "O10", "name": "Buffalo Bills", "line": 2.5},
                               {"id": "O11", "name": "Kansas City Chiefs", "line": -2.5}]}]},
    {"eventId": "E3", "league": "NBA", "homeTeam": "A", "awayTeam": "B", "status": "SETTLED", "markets": []},
    {"eventId": "E4", "league": "MLB", "homeTeam": "Yankees", "awayTeam": "Mets",
     "markets": [{"marketId": "M9", "type": "moneyline",
                  "outcomes": [{"outcomeId": "O90", "name": "Yankees"}, {"outcomeId": "O91", "name": "Mets"}]}]},
]}


def test_parse_hierarchy_flattens_and_links_siblings():
    rows = {r.outcome_id: r for r in parse_event_hierarchy(EVENTS)}
    assert set(rows) == {"O1", "O2", "O3", "O4", "O5", "O6", "O10", "O11"}
    assert rows["O1"].sibling_outcome_id == "O2" and rows["O2"].sibling_outcome_id == "O1"
    assert rows["O1"].market_type == "moneyline" and rows["O1"].market_id == "M1" and rows["O1"].event_id == "E1"
    assert (rows["O3"].line, rows["O4"].line) == (-4.5, 4.5)      # home -4.5, away mirrored
    assert rows["O5"].line == rows["O6"].line == 221.5
    assert (rows["O10"].league, rows["O10"].line, rows["O11"].line) == ("NFL", 2.5, -2.5)
    reg = MarketRegistry(rows.values())
    assert reg.sibling("O3").outcome == "Boston Celtics"


@pytest.mark.parametrize("payload", [None, "x", [], {}, {"events": "nope"}, [{"markets": 5}], [42]])
def test_parse_hierarchy_never_raises(payload):
    assert parse_event_hierarchy(payload) == []


async def _serve(routes):
    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


async def test_rest_client_bootstrap_over_http():
    seen = {}

    async def events(request):
        seen["auth"] = request.headers.get("Authorization")
        return web.json_response(EVENTS)

    runner, base = await _serve([("GET", "/v1/events", events)])
    client = NovigRestClient(f"{base}/v1/events", token="tok")
    try:
        rows = await client.fetch_open_markets()
        assert len(rows) == 8 and seen["auth"] == "Bearer tok"
    finally:
        await client.close()
        await runner.cleanup()


async def test_live_gateway_posts_limit_orders_and_bulk_deletes():
    posted, deleted = [], []

    async def post(request):
        posted.append(await request.json())
        return web.json_response({"orderId": f"srv-{len(posted)}"})

    async def delete(request):
        deleted.append(await request.json())
        return web.json_response({"cancelled": len(deleted[-1]["orderIds"])})

    runner, base = await _serve([("POST", "/v1/orders", post), ("DELETE", "/v1/orders", delete)])
    gw = NovigOrderGateway(base, token="tok")
    try:
        a = await gw.place_limit("O1", "buy", 48, 100, "mk-1")
        b = await gw.place_limit("O1", "sell", 52, 90, "mk-2")
        await gw.cancel_orders([a, b])
    finally:
        await gw.close()
        await runner.cleanup()
    assert (a, b) == ("srv-1", "srv-2")
    assert posted[0] == {"outcomeId": "O1", "side": "buy", "price_cents": 48, "volume": 100,
                         "order_type": "LIMIT", "clientOrderId": "mk-1"}
    assert deleted == [{"orderIds": ["srv-1", "srv-2"]}]       # ONE bulk request


def test_live_gateway_requires_token():
    with pytest.raises(ValueError):
        NovigOrderGateway("https://x", token="")


async def test_paper_gateway_records_without_sending():
    gw = PaperOrderGateway()
    oid = await gw.place_limit("O1", "buy", 48, 10, "c1")
    assert gw.open_orders[oid]["order_type"] == "LIMIT" and gw.live is False
    await gw.cancel_orders([oid])
    assert gw.open_orders == {} and gw.cancel_calls == [[oid]]
