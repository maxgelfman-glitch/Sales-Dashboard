"""
Self-test: live execution path (single Novig socket: tape + orders channels; Supervisor live mode;
canary / scale-up gate; live_ledger.jsonl; build_live_supervisor).

Nothing here touches a real exchange: orders go to a recording fake or a local HTTP
mock, fills are injected or pushed over a local WebSocket.

Reference: NYK sharp -120/+100 -> fair 0.52174; NYK ask 0.49 -> 2040 contracts, $999.60 reserved.
"""

import asyncio
import json
import logging

import pytest
from aiohttp import web

from execution import MAKER_CANCEL_BUDGET_MS, ExposureMonitor
from main_supervisor import (
    DEMO_KALSHI_MARKETS,
    DEMO_MARKETS,
    DEMO_SHARP_LINES,
    NOVIG_PROD_EVENTS_URL,
    ConfigError,
    Supervisor,
    build_live_supervisor,
    format_state_report,
    resolve_live_plan,
    setup_logging,
    validate_ws_url,
)
from mock_novig_server import MockNovigServer, make_tick
from novig_feed import NOVIG_PROD_WS_URL, MarketRegistry, MarketUpdate
from novig_private import FillSlip, parse_slips
from sharp_feed import MockSharpSource

INFO = {m["outcome_id"]: m for m in DEMO_MARKETS + DEMO_KALSHI_MARKETS}


def upd(outcome_id, price, volume=5000, bid=None):
    return MarketUpdate(**INFO[outcome_id], price=price, available_volume=volume, best_bid=bid,
                        bid_volume=0 if bid is None else 5000)


def slip(oid, status, filled, cents=None):
    return FillSlip(order_id=oid, status=status, filled_volume=filled, price_cents=cents)


class FakeGateway:
    live = True
    api_base = "fake://novig"

    def __init__(self, fail=False):
        self.placed, self.cancels, self.fail, self.n = [], [], fail, 0

    async def place_limit(self, outcome_id, side, price_cents, contracts, client_id):
        if self.fail:
            raise ConnectionError("exchange rejected")
        self.n += 1
        oid = f"ex-{self.n}"
        self.placed.append(dict(order_id=oid, outcome_id=outcome_id, side=side, price_cents=price_cents,
                                contracts=contracts, client_id=client_id))
        return oid

    async def cancel_orders(self, ids):
        self.cancels.append(list(ids))

    async def close(self):
        pass


def live_sup(mode="cumulative", gateway=None, limit=None, maker=False, timeout=60.0, ready=True,
             confirmed=True, **kw):
    sup = Supervisor(feed_url="ws://127.0.0.1:1/unused", token="t", sharp_fetch=MockSharpSource(DEMO_SHARP_LINES),
                     registry=MarketRegistry(DEMO_MARKETS), kalshi_registry=MarketRegistry(DEMO_KALSHI_MARKETS),
                     live=True, order_gateway=gateway or FakeGateway(),
                     fill_volume_mode=mode, maker_enabled=maker, taker_fill_timeout=timeout,
                     exposure=ExposureMonitor(limit) if limit else None, maker_kwargs=dict(quiet_seconds=0), **kw)
    sup.book.ingest(DEMO_SHARP_LINES)
    sup.orders_channel_confirmed = confirmed        # most tests model a session whose orders channel is proven
    if ready:
        sup.feed.connected.set()
    return sup


# ================================================================ slips
def test_parse_slips_all_shapes():
    one = parse_slips(json.dumps({"order_id": "a", "status": "filled", "filled_volume": 10, "price_cents": 49}))
    env = parse_slips(json.dumps({"event": "orders", "data": [{"orderId": 7, "status": "PARTIAL",
                                                               "filledVolume": "5", "priceCents": 48}]}))
    assert (one[0].order_id, one[0].status, one[0].terminal) == ("a", "FILLED", True)
    assert (env[0].order_id, env[0].filled_volume, env[0].terminal) == ("7", 5, False)


@pytest.mark.parametrize("raw", ["nope", "{}", json.dumps({"event": "subscribed"}),
                                 json.dumps({"order_id": "a", "status": "FILLED", "filled_volume": -1}),
                                 json.dumps({"order_id": "a", "status": "FILLED", "filled_volume": 1,
                                             "price_cents": 150})])
def test_parse_slips_never_raises(raw):
    assert parse_slips(raw) == []


# ================================================================ taker lifecycle
async def test_reserve_then_cumulative_fills_then_exact_exposure():
    sup = live_sup()
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    [sent] = sup.order_gateway.placed
    assert (sent["side"], sent["price_cents"], sent["contracts"]) == ("buy", 49.0, 2040)
    leg = sup.orders[0]
    assert leg.pending and leg.live and sup.total_exposure() == 999.60        # reserved before any fill
    assert ("NBA", "New York Knicks", "Boston Celtics", "moneyline") in sup.positions   # game locked
    await sup.on_fill_slip(slip("ex-1", "PARTIAL", 1000, 49))
    assert (leg.contracts, leg.stake_usd, sup.total_exposure()) == (1000, 490.0, 999.60)
    await sup.on_fill_slip(slip("ex-1", "PARTIAL", 1000, 49))                # replay: ignored
    assert leg.contracts == 1000
    await sup.on_fill_slip(slip("ex-1", "FILLED", 2040, 49))
    assert (leg.contracts, leg.stake_usd, leg.pending, sup.total_exposure()) == (2040, 999.60, False, 999.60)


async def test_incremental_fill_mode_adds_each_slip():
    sup = live_sup(mode="incremental")
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    await sup.on_fill_slip(slip("ex-1", "PARTIAL", 1000, 49))
    await sup.on_fill_slip(slip("ex-1", "PARTIAL", 1000, 49))
    await sup.on_fill_slip(slip("ex-1", "FILLED", 40, 49))
    assert sup.orders[0].contracts == 2040 and sup.total_exposure() == 999.60


async def test_price_improvement_lowers_exposure():
    sup = live_sup()
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    await sup.on_fill_slip(slip("ex-1", "FILLED", 2040, 48))                 # filled 1c better
    assert sup.total_exposure() == 979.20 and sup.orders[0].price == 0.48


async def test_partial_fill_timeout_cancels_remainder_and_releases_reservation():
    sup = live_sup(timeout=0.05)
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    await sup.on_fill_slip(slip("ex-1", "PARTIAL", 500, 49))
    await asyncio.sleep(0.15)
    assert sup.order_gateway.cancels == [["ex-1"]]
    assert sup.total_exposure() == 245.0 and sup.orders[0].contracts == 500 and not sup.orders[0].pending
    assert sup.positions                                                        # 500 contracts still held


async def test_zero_fill_before_channel_confirmed_keeps_lock_and_reservation():
    """The orders channel is unproven: an order that 'saw no fill' may have filled unseen."""
    sup = live_sup(timeout=0.05, confirmed=False)
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    await asyncio.sleep(0.15)
    assert sup.stats["unconfirmed_zero_fill"] == 1
    assert sup.total_exposure() == 999.60 and sup.positions                 # nothing released
    await sup.on_market_update(upd("O-NYK", 0.45), None)                      # game stays locked
    assert len(sup.order_gateway.placed) == 1
    await sup.on_fill_slip(slip("ex-1", "FILLED", 2040, 49))                  # the truth arrives late
    assert sup.orders_channel_confirmed and sup.orders[0].contracts == 2040 and sup.total_exposure() == 999.60


async def test_zero_fill_timeout_releases_lock_and_exposure():
    sup = live_sup(timeout=0.05)
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    await asyncio.sleep(0.15)
    assert sup.total_exposure() == 0 and sup.positions == {}
    await sup.on_market_update(upd("O-NYK", 0.48), None)                      # game tradable again
    assert len(sup.order_gateway.placed) == 2


async def test_late_fill_after_cancel_is_still_booked():
    sup = live_sup(timeout=0.05)
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    await sup.on_fill_slip(slip("ex-1", "PARTIAL", 500, 49))
    await asyncio.sleep(0.15)
    await sup.on_fill_slip(slip("ex-1", "PARTIAL", 600, 49))                  # raced the cancel
    assert sup.orders[0].contracts == 600 and sup.total_exposure() == 294.0


async def test_rejected_order_releases_everything():
    sup = live_sup(gateway=FakeGateway(fail=True))
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    assert sup.total_exposure() == 0 and sup.positions == {} and sup.stats["live_rejected"] == 1


async def test_no_orders_while_socket_down():
    sup = live_sup(ready=False)                                               # single socket down
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    assert sup.order_gateway.placed == [] and sup.total_exposure() == 0 and sup.positions == {}


async def test_kalshi_is_data_only_in_live_mode():
    sup = live_sup()
    await sup.on_market_update(upd("KXNBAGAME-BOSNYK-NYK", 0.45), None)
    assert sup.order_gateway.placed == [] and sup.orders == [] and sup.stats["kalshi_exec_disabled"] == 1


async def test_unknown_order_slip_is_flagged_not_booked():
    sup = live_sup()
    await sup.on_fill_slip(slip("someone-elses", "FILLED", 100, 50))
    assert sup.stats["unknown_fills"] == 1 and sup.total_exposure() == 0


async def test_pending_reservations_count_toward_15000_limit():
    sup = live_sup()
    for i in range(16):
        info = dict(INFO["O-NYK"], outcome_id=f"O{i}", event_id=f"E{i}", home_team="New York Knicks",
                    away_team="Boston Celtics")
        sup.registry.register(info)
        # distinct games so the position lock does not interfere
        info["home_team"], info["away_team"] = "New York Knicks", "Boston Celtics"
        u = MarketUpdate(**info, price=0.40, available_volume=10_000)
        key_override = ("NBA", f"G{i}", "X", "moneyline")
        sup.positions.pop(key_override, None)
        await sup._send_live_taker("DIRECTIONAL", u, key_override, "New York Knicks", 2500, 1000.0, 0.3, True) \
            if sup._kill_switch_allows(1000.0, u.outcome_id) else None
    assert len(sup.order_gateway.placed) == 15 and sup.total_exposure() == 15_000
    assert sup.exposure.taker_halted


async def test_lowered_live_stake_cap():
    sup = live_sup(max_stake=100.0)
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    assert sup.order_gateway.placed[0]["contracts"] == 204                   # floor(100 / 0.49)


# ================================================================ live arbitrage
async def test_live_hedge_waits_for_first_leg_then_hedges():
    sup = live_sup()
    await sup.on_market_update(upd("O-NYK", 0.49, volume=1000), None)          # 1000 contracts
    await sup.on_market_update(upd("O-BOS", 0.478), None)                       # first leg pending: blocked
    assert len(sup.order_gateway.placed) == 1
    await sup.on_fill_slip(slip("ex-1", "FILLED", 1000, 49))
    await sup.on_market_update(upd("O-BOS", 0.478), None)
    assert [p["outcome_id"] for p in sup.order_gateway.placed] == ["O-NYK", "O-BOS"]
    assert sup.order_gateway.placed[1]["contracts"] == 1000
    await sup.on_market_update(upd("O-BOS", 0.40), None)                        # hedge in flight: no second one
    assert len(sup.order_gateway.placed) == 2
    await sup.on_fill_slip(slip("ex-2", "FILLED", 1000, 47.8))
    assert sup.total_exposure() == 490.0 + 478.0


async def test_live_partial_hedge_logs_residual(tmp_path):
    path = setup_logging(tmp_path, console=False)
    sup = live_sup(timeout=0.05)
    await sup.on_market_update(upd("O-NYK", 0.49, volume=1000), None)
    await sup.on_fill_slip(slip("ex-1", "FILLED", 1000, 49))
    await sup.on_market_update(upd("O-BOS", 0.478), None)
    await sup.on_fill_slip(slip("ex-2", "PARTIAL", 600, 47.8))
    await asyncio.sleep(0.15)
    assert ("POSITION_RESIDUAL hedge ex-2 filled 600 of 1000: 400 contracts of the first leg remain unhedged"
            in path.read_text())
    held = next(iter(sup.positions.values()))
    assert not held.hedged and held.unhedged() == 400      # the rest can still be hedged later
    await sup.on_market_update(upd("O-BOS", 0.47), None)
    assert sup.order_gateway.placed[-1]["contracts"] == 400


# ================================================================ live maker
async def test_live_maker_quotes_via_gateway_and_books_real_fills():
    sup = live_sup(maker=True)
    await sup.maker.refresh()
    quotes = {q.order_id: q for q in sup.maker.quotes.values()}
    assert quotes and set(quotes) == set(sup.live_orders)                       # every quote tracked
    bos_bid = next(q for q in quotes.values() if q.outcome_id == "O-BOS" and q.side == "buy")
    bos_ask = next(q for q in quotes.values() if q.outcome_id == "O-BOS" and q.side == "sell")
    # A tape bid crossing our ask would be a simulated fill in paper mode; live books ONLY real slips.
    await sup.on_market_update(upd("O-BOS", None, bid=bos_ask.price_cents / 100), None)
    assert sup.orders == []
    await sup.on_fill_slip(slip(bos_bid.order_id, "PARTIAL", 300, bos_bid.price_cents))
    fill = next(o for o in sup.orders if o.kind == "MAKER_FILL")
    assert (fill.side, fill.contracts, fill.live) == ("Boston Celtics", 300, True)
    assert not any(q.outcome_id == "O-BOS" for q in sup.maker.quotes.values())  # rest of that game pulled
    await sup.on_fill_slip(slip(bos_bid.order_id, "PARTIAL", 400, bos_bid.price_cents))   # race with the cancel
    assert fill.contracts == 400 and sup.total_exposure() == round(400 * bos_bid.price_cents / 100, 2)


async def test_live_maker_sell_fill_is_long_sibling():
    sup = live_sup(maker=True)
    await sup.maker.refresh()
    ask = next(q for q in sup.maker.quotes.values() if q.outcome_id == "O-BOS" and q.side == "sell")
    await sup.on_fill_slip(slip(ask.order_id, "FILLED", ask.contracts, ask.price_cents))
    fill = next(o for o in sup.orders if o.kind == "MAKER_FILL")
    assert fill.side == "New York Knicks" and fill.price == pytest.approx(1 - ask.price_cents / 100)


async def test_socket_drop_pulls_quotes_and_halts():
    sup = live_sup(maker=True)
    await sup.maker.refresh()
    assert sup.maker.quotes
    sup.feed.connected.clear()
    await sup.on_feed_state("DISCONNECTED", {"venue": "novig", "error": "test"})
    assert sup.maker.quotes == {} and sup.maker.last_cancel_ms < MAKER_CANCEL_BUDGET_MS
    await sup.maker.refresh()
    assert sup.maker.quotes == {}                                               # no requoting while down


# ================================================================ end to end over one socket
async def test_end_to_end_live_single_socket(tmp_path):
    """tape tick -> POST /v1/orders (HTTP mock) -> the SAME socket pushes the execution slip."""
    path = setup_logging(tmp_path, console=False)
    ledger = tmp_path / "live_ledger.jsonl"
    novig = await MockNovigServer().start()
    posted = []

    async def post(request):
        body = await request.json()
        posted.append(body)
        oid = f"srv-{len(posted)}"
        asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(novig.broadcast(
            {"channel": "orders", "data": {"order_id": oid, "status": "FILLED",
                                           "filled_volume": body["volume"], "price_cents": body["price_cents"]}})))
        return web.json_response({"orderId": oid})

    async def delete(_request):
        return web.json_response({})

    app = web.Application()
    app.router.add_post("/v1/orders", post)
    app.router.add_delete("/v1/orders", delete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    from novig_rest import NovigOrderGateway
    sup = Supervisor(feed_url=novig.url, token="tok", registry=MarketRegistry(DEMO_MARKETS),
                     sharp_fetch=MockSharpSource(DEMO_SHARP_LINES), sharp_poll_interval=0.1,
                     live=True, order_gateway=NovigOrderGateway(base, "tok"), maker_enabled=False,
                     taker_fill_timeout=2.0, max_stake=10.0, exposure=ExposureMonitor(100.0), ledger_path=ledger)
    run = asyncio.create_task(sup.run())
    try:
        await asyncio.wait_for(sup.feed.connected.wait(), 5)
        while len(novig.received) < 2 or len(sup.book) == 0:
            await asyncio.sleep(0.01)
        assert [json.loads(m) for m in novig.received] == [{"event": "subscribe", "channel": "tape"},
                                                           {"event": "subscribe", "channel": "orders"}]
        await novig.broadcast(make_tick("O-NYK", 49))
        for _ in range(300):
            if sup.orders and not sup.orders[0].pending:
                break
            await asyncio.sleep(0.01)
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await novig.stop()
        await runner.cleanup()
    assert posted[0] == {"outcomeId": "O-NYK", "side": "buy", "price_cents": 49.0, "volume": 20,
                         "order_type": "LIMIT", "clientOrderId": "tk-1"}             # canary: floor($10/0.49)
    leg = sup.orders[0]
    assert (leg.contracts, leg.stake_usd, leg.pending, leg.exchange_order_id) == (20, 9.80, False, "srv-1")
    assert sup.total_exposure() == 9.80 and sup.orders_channel_confirmed
    events = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [e["event"] for e in events] == ["SESSION_START", "ORDER", "SLIP", "FILL", "DONE"]
    assert events[1]["payload"] == posted[0]                                           # exact body sent
    assert (events[3]["filled_total"], events[3]["cost_total_usd"]) == (20, 9.8)
    for h in logging.getLogger("trading").handlers:
        h.flush()
    text = path.read_text()
    for needle in ("LIVE TRADING ENABLED", "ORDER LIVE DIRECTIONAL id=srv-1", "FILL_SLIP",
                   "LIVE orders channel confirmed", "LIVE_DONE DIRECTIONAL srv-1 filled 20/20 cost=$9.80"):
        assert needle in text, needle


async def test_ledger_records_maker_posts_cancels_and_rejections(tmp_path):
    ledger = tmp_path / "l.jsonl"
    sup = live_sup(maker=True, ledger_path=ledger, timeout=0.05)
    await sup.maker.refresh()
    await sup.maker.cancel_all("test kill")
    sup.order_gateway.fail = True
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    sup.order_gateway.fail = False
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    await asyncio.sleep(0.15)                                                        # taker timeout cancel
    ev = [json.loads(line) for line in ledger.read_text().splitlines()]
    kinds = [e["event"] for e in ev]
    assert kinds.count("ORDER") == len([e for e in ev if e.get("kind") == "MAKER"]) + 1
    maker_cancel = next(e for e in ev if e["event"] == "CANCEL" and e["source"] == "maker")
    assert maker_cancel["reason"] == "test kill" and len(maker_cancel["exchange_order_ids"]) >= 2
    assert any(e["event"] == "REJECTED" and e["payload"]["order_type"] == "LIMIT" for e in ev)
    assert any(e["event"] == "CANCEL" and e["source"] == "taker" for e in ev)
    assert all("ts" in e for e in ev)


# ================================================================ configuration gate
@pytest.fixture
def sharp_env(monkeypatch):
    monkeypatch.setenv("SHARP_API_KEY", "k")
    return {"SHARP_PROVIDER_CONFIG": "config/sharp_provider.opticodds.example.json"}


LIVE_ENV = {"TRADING_MODE": "live", "LIVE_TRADING_ACKNOWLEDGED": "yes", "NOVIG_BEARER_TOKEN": "tok"}


def test_live_refused_until_acknowledged_with_token(sharp_env):
    with pytest.raises(ConfigError) as err:
        build_live_supervisor({**sharp_env, "TRADING_MODE": "live"})
    assert "LIVE_TRADING_ACKNOWLEDGED=yes" in str(err.value) and "NOVIG_BEARER_TOKEN" in str(err.value)


def test_malformed_socket_url_from_brief_is_rejected(sharp_env):
    with pytest.raises(ConfigError, match="not a valid WebSocket URL"):
        build_live_supervisor({**sharp_env, **LIVE_ENV, "NOVIG_WS_URL": "wss://://novig.com"})


@pytest.mark.parametrize("url,ok", [("wss://api.novig.com/tape", True), ("wss://://novig.com", False),
                                    ("https://novig.com", False), ("ws://api.novig.com/tape", False),
                                    ("ws://127.0.0.1:9000/x", True), ("", False)])
def test_validate_ws_url(url, ok):
    if ok:
        assert validate_ws_url(url, "X") == url
    else:
        with pytest.raises(ConfigError):
            validate_ws_url(url, "X")


def test_live_starts_on_canary_caps_with_one_socket_two_channels(sharp_env, tmp_path):
    sup = build_live_supervisor({**sharp_env, **LIVE_ENV, "TRADING_LOG_DIR": str(tmp_path)})
    assert sup.live and sup.feed.url == NOVIG_PROD_WS_URL
    assert sup.feed.subscribe_messages == [{"event": "subscribe", "channel": "tape"},
                                           {"event": "subscribe", "channel": "orders"}]
    assert sup.feed.on_slip == sup.on_fill_slip and sup.fill_volume_mode == "cumulative"
    assert (sup.max_stake, sup.exposure.limit, sup.maker) == (10.0, 100.0, None)       # canary, maker off
    assert sup.novig_rest.events_url == NOVIG_PROD_EVENTS_URL == \
        "https://api.novig.us/nbx/v2/emm/events?status=OPEN_PREGAME&limit=100"
    assert sup.order_gateway.api_base == "https://api.novig.us"
    assert not (tmp_path / "live_ledger.jsonl").exists()            # building / --check-config writes nothing
    sup.record_live_plan()                                          # what run() does at launch
    [line] = [json.loads(l) for l in (tmp_path / "live_ledger.jsonl").read_text().splitlines()]
    assert (line["event"], line["max_stake_usd"], line["exposure_limit_usd"], line["maker"]) == \
        ("CANARY_LIMITS", 10.0, 100.0, False)


@pytest.mark.parametrize("override", [{"LIVE_MAX_STAKE_USD": "1000"}, {"LIVE_EXPOSURE_LIMIT_USD": "15000"},
                                      {"MAKER_MODE": "true"}, {"LIVE_MAX_STAKE_USD": "10.01"}])
def test_any_scale_up_without_approval_is_refused(sharp_env, override):
    with pytest.raises(ConfigError, match="LIVE_SCALE_APPROVED_BY"):
        build_live_supervisor({**sharp_env, **LIVE_ENV, **override})


def test_scale_up_with_approval_is_logged_critical_to_ledger(sharp_env, tmp_path):
    sup = build_live_supervisor({**sharp_env, **LIVE_ENV, "TRADING_LOG_DIR": str(tmp_path),
                                 "LIVE_MAX_STAKE_USD": "1000", "LIVE_EXPOSURE_LIMIT_USD": "15000",
                                 "MAKER_MODE": "true", "LIVE_SCALE_APPROVED_BY": "PM 2026-10-01 ledger reconciled"})
    assert (sup.max_stake, sup.exposure.limit, sup.maker is not None) == (1000.0, 15000.0, True)
    sup.record_live_plan()
    [line] = [json.loads(l) for l in (tmp_path / "live_ledger.jsonl").read_text().splitlines()]
    assert (line["event"], line["level"], line["approved_by"]) == ("SCALE_UP_AUTHORIZED", "CRITICAL",
                                                                   "PM 2026-10-01 ledger reconciled")


@pytest.mark.parametrize("name,value", [("LIVE_MAX_STAKE_USD", "5000"), ("LIVE_EXPOSURE_LIMIT_USD", "20000"),
                                        ("LIVE_MAX_STAKE_USD", "abc"), ("LIVE_MAX_STAKE_USD", "0")])
def test_hard_ceilings_hold_even_with_approval(sharp_env, name, value):
    with pytest.raises(ConfigError):
        build_live_supervisor({**sharp_env, **LIVE_ENV, "LIVE_SCALE_APPROVED_BY": "x", name: value})


def test_lower_than_canary_needs_no_approval():
    plan = resolve_live_plan({"LIVE_MAX_STAKE_USD": "5", "LIVE_EXPOSURE_LIMIT_USD": "50"})
    assert (plan.max_stake, plan.exposure_limit, plan.scaled_up) == (5.0, 50.0, False)


def test_fill_mode_defaults_cumulative_and_rejects_nonsense(sharp_env):
    with pytest.raises(ConfigError, match="NOVIG_FILL_VOLUME_MODE"):
        build_live_supervisor({**sharp_env, **LIVE_ENV, "NOVIG_FILL_VOLUME_MODE": "sometimes"})


def test_overrides_for_hosts_and_subscriptions(sharp_env, tmp_path):
    sup = build_live_supervisor({**sharp_env, **LIVE_ENV, "TRADING_LOG_DIR": str(tmp_path),
                                 "NOVIG_API_BASE": "https://novig.us", "NOVIG_WS_URL": "wss://api-qa.novig.us/tape",
                                 "NOVIG_SUBSCRIBE_MESSAGES": '[{"event":"subscribe","channel":"tape"}]'})
    assert sup.order_gateway.api_base == "https://novig.us" and sup.feed.url == "wss://api-qa.novig.us/tape"
    assert sup.novig_rest.events_url.startswith("https://novig.us/nbx/v2/emm/events")
    assert sup.feed.subscribe_messages == [{"event": "subscribe", "channel": "tape"}]


def test_check_config_report_is_plain_text_and_offline(sharp_env, tmp_path):
    sup = build_live_supervisor({**sharp_env, **LIVE_ENV, "TRADING_LOG_DIR": str(tmp_path)})
    report = format_state_report(sup)
    for needle in ("Trading mode", "LIVE", "wss://api.novig.com/tape", '{"event": "subscribe", "channel": "orders"}',
                   "https://api.novig.us/v1/orders", "$10.00", "$100.00", "Taker fill timeout", "2.0s",
                   "Maker                                  off", "kalshi, novig", "UNVERIFIED"):
        assert needle in report, needle
    assert not sup.feed.connected.is_set()                                          # nothing was opened


def test_paper_is_default(sharp_env):
    sup = build_live_supervisor(dict(sharp_env))
    assert not sup.live and sup.order_gateway is None and sup.feed.on_slip is None
    assert sup.feed.subscribe_messages == [{"event": "subscribe", "channel": "tape"}]


def test_committed_provider_schema_is_current():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen", "config/generate_schemas.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert open("config/sharp_provider.schema.json").read() == gen.provider_schema()
