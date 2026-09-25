"""
Self-test: ProphetX as a price feed (paper + research only): market parsing (American odds -> contract prices,
stake -> contracts), the polling feed, the 2%-of-winnings fee in edges and locked pairs, and that ProphetX is
never traded live.
"""

import time
from datetime import datetime, timezone

import pytest

from main_supervisor import (
    ConfigError,
    DEMO_KALSHI_MARKETS,
    DEMO_MARKETS,
    DEMO_SHARP_LINES,
    Supervisor,
    build_live_supervisor,
    win_payout,
)
from novig_feed import MarketRegistry
from prophetx_feed import ProphetXFeed, american_to_price, event_teams, parse_markets
from sharp_feed import MockSharpSource
from tests.test_live_execution import live_sup, upd

START = time.time() + 86400
EVENT = {"event_id": 101, "name": "Boston Celtics at New York Knicks", "scheduled": datetime.fromtimestamp(START, timezone.utc).isoformat(),
         "status": "not_started"}


def lvl(price, qty, name, oid):
    return {"strike_id": f"s-{oid}", "outcome_id": oid, "name": name, "price": price, "quantity": qty}


MARKETS = [
    {"id": 1, "type": "moneyline", "status": "active",
     "selections": [[lvl(110, 476.19, "New York Knicks", 1), lvl(105, 200, "New York Knicks", 1)],
                    [lvl(-130, 1300, "Boston Celtics", 2)]]},
    {"id": 2, "type": "spread", "status": "active", "market_strikes": [
        {"strike": -4.5, "selections": [[lvl(-110, 550, "New York Knicks -4.5", 3)],
                                        [lvl(-110, 550, "Boston Celtics +4.5", 4)]]}]},
    {"id": 3, "type": "total", "status": "active", "strike": 221.5,
     "selections": [[lvl(-105, 100, "Over 221.5", 5)], [lvl(-115, 100, "Under 221.5", 6)]]},
    {"id": 4, "type": "player_points", "status": "active", "selections": [[lvl(-110, 5, "x", 7)], [lvl(-110, 5, "y", 8)]]},
    {"id": 5, "type": "moneyline", "status": "suspended", "selections": [[lvl(100, 5, "a", 9)], [lvl(100, 5, "b", 10)]]},
]


def test_american_odds_to_contract_price():
    assert american_to_price(100) == pytest.approx(0.5)
    assert american_to_price(-150) == pytest.approx(0.6)
    assert american_to_price(150) == pytest.approx(0.4)
    assert american_to_price(50) is None and american_to_price("x") is None


def test_event_teams_from_name_or_competitors():
    assert event_teams(EVENT) == ("New York Knicks", "Boston Celtics")
    ev = {"competitors": [{"name": "A", "side": "home"}, {"name": "B", "side": "away"}]}
    assert event_teams(ev) == ("A", "B")


def test_parse_markets():
    rows = parse_markets(EVENT, "NBA", MARKETS, START)
    by = {(i.market_type, i.outcome): (i, asks) for i, asks in rows}
    assert len(rows) == 6                                                  # 3 markets x 2 sides; prop/suspended out
    ml, asks = by[("moneyline", "New York Knicks")]
    assert asks[0] == (pytest.approx(0.47619, abs=1e-5), pytest.approx(1000.0, abs=0.1))   # $476.19 @ +110
    assert asks[1][0] == pytest.approx(0.487805, abs=1e-5)                                 # +105 is worse
    assert (ml.venue, ml.home_team, ml.away_team, ml.sibling_outcome_id is not None) == (
        "prophetx", "New York Knicks", "Boston Celtics", True)
    sp, _ = by[("spread", "Boston Celtics")]
    assert sp.line == 4.5
    tot, _ = by[("total", "over")]
    assert tot.line == 221.5


class FakeClient:
    base = "fake://prophetx"

    async def get(self, path, **params):
        if path == "mm/get_tournaments":
            return {"tournaments": [{"id": 7, "name": "NBA"}, {"id": 8, "name": "EPL"}]}
        if path == "mm/get_sport_events":
            return {"sport_events": [EVENT]} if params["tournament_id"] == 7 else {"sport_events": []}
        if path == "v4/mm/get_multiple_markets":
            return {"101": MARKETS}
        raise AssertionError(path)

    async def close(self):
        pass


async def test_feed_polls_and_emits_updates_once_per_change():
    seen = []

    async def on_update(u, prev):
        seen.append(u)
    feed = ProphetXFeed(FakeClient(), on_update=on_update)
    assert await feed.poll_once() == 6
    assert await feed.poll_once() == 0                                     # nothing changed
    assert len(feed.registry.all()) == 6 and all(u.venue == "prophetx" for u in seen)


def test_win_fee_math():
    assert win_payout("prophetx", 0.5) == pytest.approx(0.99)
    assert win_payout("novig", 0.5) == 1.0


def paper_with_prophetx():
    sup = Supervisor(feed_url="ws://127.0.0.1:1/x", token="t", sharp_fetch=MockSharpSource(DEMO_SHARP_LINES),
                     registry=MarketRegistry(DEMO_MARKETS), kalshi_registry=MarketRegistry(DEMO_KALSHI_MARKETS),
                     maker_enabled=False, prophetx_feed=ProphetXFeed(FakeClient()))
    sup.book.ingest(DEMO_SHARP_LINES)
    sup.feed.connected.set()
    return sup


async def test_paper_edge_on_prophetx_includes_the_win_fee():
    sup = paper_with_prophetx()
    await sup.prophetx.poll_once()                                         # Knicks +110 = 47.6c, fair 52.17c
    [order] = sup.orders
    assert order.venue == "prophetx" and order.side == "New York Knicks"
    effective = 0.47619 / win_payout("prophetx", 0.47619)
    assert order.edge == pytest.approx(0.521739 / effective - 1, abs=1e-3)


async def test_locked_pair_with_prophetx_accounts_for_its_fee():
    sup = paper_with_prophetx()
    sup.arb_pairs_enabled = True
    u = upd("O-BOS", 0.505, volume=1000)                                   # no edge alone vs fair 0.478
    sup.feed.latest["O-BOS"] = u
    await sup.on_market_update(u, None)
    sup.book._lines.clear(); sup.book._by_line.clear(); sup.book._src.clear(); sup.book._main_src.clear()
    await sup.prophetx.poll_once()                                         # Knicks 47.6c on ProphetX
    pairs = [o for o in sup.orders if o.kind == "ARB_PAIR"]
    # 50.5 + 47.62 = 98.12c; worst case pays 1 - 2% x 52.4c = 98.95c -> 0.83c < 1c minimum: NO pair
    assert pairs == []


async def test_prophetx_is_never_traded_live():
    sup = live_sup(prophetx_feed=ProphetXFeed(FakeClient()))
    await sup.prophetx.poll_once()
    assert sup.order_gateway.placed == [] and sup.orders == []


def test_config():
    with pytest.raises(ConfigError):
        build_live_supervisor({"PROPHETX_ENABLED": "1", "RESEARCH_ENABLED": "0"})
    sup = build_live_supervisor({"PROPHETX_ENABLED": "1", "PROPHETX_ACCESS_KEY": "a", "PROPHETX_SECRET_KEY": "b",
                                 "RESEARCH_ENABLED": "0"})
    assert sup.prophetx is not None and "prophetx_feed" in sup._task_factories


async def test_locked_pair_with_prophetx_when_the_gap_clears_its_fee():
    sup = paper_with_prophetx()
    sup.book._lines.clear(); sup.book._by_line.clear(); sup.book._src.clear(); sup.book._main_src.clear()
    u = upd("O-BOS", 0.49, volume=1000)
    sup.feed.latest["O-BOS"] = u
    await sup.on_market_update(u, None)
    await sup.prophetx.poll_once()
    pair = [o for o in sup.orders if o.kind == "ARB_PAIR"]
    assert {o.venue for o in pair} == {"novig", "prophetx"}
    n = pair[0].contracts
    worst = min(n * win_payout("prophetx", pair[1].price if pair[1].venue == "prophetx" else pair[0].price), n)
    assert worst - sum(o.stake_usd for o in pair) >= 0.01 * n - 0.01
