"""Regressions from the independent arbitrage audit: a "locked" pair must not lose in any real outcome."""

import time

from main_supervisor import MarketPosition, PaperOrder, Supervisor
from novig_feed import MarketRegistry, MarketUpdate

START = time.time() + 3 * 86400


def ml_market(venue, prefix, league, home, away, start=START):
    mid = f"{venue}-{prefix}-moneyline"
    a, b = f"{mid}-H", f"{mid}-A"
    return [dict(venue=venue, outcome_id=oid, market_id=mid, sibling_outcome_id=sib, event_id=mid, league=league,
                 market_type="moneyline", home_team=home, away_team=away, outcome=team, line=None, start_time=start)
            for oid, sib, team in ((a, b, home), (b, a, away))]


def make_sup(novig=(), kalshi=()):
    sup = Supervisor(feed_url="ws://127.0.0.1:1/x", token="t", registry=MarketRegistry(list(novig)),
                     kalshi_registry=MarketRegistry(list(kalshi)), maker_enabled=False)
    sup.feed.connected.set()
    if sup.kalshi is not None:
        sup.kalshi.connected.set()
    return sup


async def show(sup, info, price, volume):
    u = MarketUpdate(**info, price=price, available_volume=volume)
    (sup.kalshi if info["venue"] == "kalshi" else sup.feed).latest[u.outcome_id] = u
    await sup.on_market_update(u, None)


async def test_a_doubleheader_is_never_paired_or_traded():
    h, a = "New York Yankees", "Boston Red Sox"
    g2 = START + 5 * 3600 + 5 * 60                       # game 2, five hours later
    nov = ml_market("novig", "G1", "MLB", h, a) + ml_market("novig", "G2", "MLB", h, a, start=g2)
    kal = ml_market("kalshi", "G1", "MLB", h, a) + ml_market("kalshi", "G2", "MLB", h, a, start=g2)
    sup = make_sup(novig=nov, kalshi=kal)
    gid = ("MLB", h, a)
    assert gid in sup.ambiguous_games and "doubleheader" in sup._trade_blocked(gid)
    await show(sup, kal[3], 0.45, 800)                   # game 2 Red Sox
    await show(sup, nov[0], 0.50, 800)                   # game 1 Yankees: 1 - 0.95 looked like a lock
    assert not sup.positions and not sup.orders


def test_the_sharp_book_refuses_a_matchup_with_two_games_the_same_day():
    from sharp_feed import SharpBook
    book = SharpBook()
    line = dict(league="MLB", home_team="New York Yankees", away_team="Boston Red Sox", market_type="moneyline",
                side="New York Yankees")
    book.ingest([dict(line, odds_for=-150, odds_against=130, start_time=START)])
    assert book.lookup("MLB", "New York Yankees", "Boston Red Sox", "moneyline", "New York Yankees") is not None
    book.ingest([dict(line, odds_for=-115, odds_against=-105, start_time=START + 5 * 3600)])
    assert book.lookup("MLB", "New York Yankees", "Boston Red Sox", "moneyline", "New York Yankees") is None


async def test_a_hedge_is_priced_against_every_leg_on_the_held_side():
    h, a = "New York Knicks", "Boston Celtics"
    nov = ml_market("novig", "G1", "NBA", h, a)
    sup = make_sup(novig=nov)

    def leg(oid, kind, price):
        return PaperOrder(order_id=oid, kind=kind, venue="novig", outcome_id=nov[0]["outcome_id"], event_id="e",
                          league="NBA", market_type="moneyline", side=h, line=None, price=price, contracts=100,
                          stake_usd=round(price * 100, 2), edge=None, capped=False, placed_at=time.time(),
                          start_time=START)
    first, second = leg(1, "DIRECTIONAL", 0.40), leg(2, "MAKER_FILL", 0.50)
    sup.orders += [first, second]
    sup.positions[("NBA", h, a, "moneyline")] = MarketPosition(legs=[first, second])
    await show(sup, nov[1], 0.57, 1000)                  # 0.40 + 0.57 locks; the 0.45 average + 0.57 does not
    assert sup.stats["arbs"] == 0 and len(sup.orders) == 2
