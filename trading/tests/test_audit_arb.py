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


def test_only_a_real_doubleheader_blocks_a_matchup():
    h, a = "New York Yankees", "Boston Red Sox"
    gid = ("MLB", h, a)
    # Saturday 7:15pm then Sunday 1:35pm: a series, 18h apart -> both tradeable (Saturday first)
    sat = START
    sup = make_sup(novig=ml_market("novig", "SAT", "MLB", h, a, start=sat)
                   + ml_market("novig", "SUN", "MLB", h, a, start=sat + 18.3 * 3600))
    assert gid not in sup.ambiguous_games and sup.game_start[gid] == sat
    # venues disagreeing by 3h on ONE game: not a doubleheader (the pair check compares the two markets)
    sup = make_sup(novig=ml_market("novig", "G", "MLB", h, a, start=sat),
                   kalshi=ml_market("kalshi", "G", "MLB", h, a, start=sat + 3 * 3600))
    assert gid not in sup.ambiguous_games
    # one event rescheduled 4h later: replaced, not a second game
    sup = make_sup(novig=ml_market("novig", "G", "MLB", h, a, start=sat))
    sup._note_start(gid, sat + 4 * 3600, ("novig", "novig-G-moneyline"))
    assert gid not in sup.ambiguous_games
    # a second event at the same venue 5h after it: a doubleheader
    sup._note_start(gid, sat + 9 * 3600, ("novig", "novig-G2-moneyline"))
    assert gid in sup.ambiguous_games


async def test_a_24_7_session_trades_game_two_of_a_series():
    import time as _t
    h, a = "New York Yankees", "Boston Red Sox"
    gid = ("MLB", h, a)
    sup = make_sup(novig=ml_market("novig", "G1", "MLB", h, a, start=_t.time() - 8 * 3600))   # game 1 is over
    sup.mark_game_live(gid, "started")
    assert sup._trade_blocked(gid)
    sup._note_start(gid, _t.time() + 20 * 3600, ("novig", "G2"))                             # game 2 listed
    assert sup._trade_blocked(gid) is None and gid not in sup.live_games


def test_kalshis_start_hint_is_replaced_by_the_real_start():
    """Kalshi publishes no real start: its time fields are the originally scheduled start + 3h."""
    from kalshi_feed import parse_kalshi_markets
    from datetime import datetime, timezone
    h, a = "Kansas City Chiefs", "Buffalo Bills"
    hint = datetime.fromtimestamp(START + 3 * 3600, timezone.utc).isoformat()
    base = dict(event_ticker="KXNFLGAME-X", title="Buffalo at Kansas City", status="open", occurrence_datetime=hint)
    kal = parse_kalshi_markets({"markets": [dict(base, ticker="KXNFLGAME-X-KC", yes_sub_title="Kansas City"),
                                            dict(base, ticker="KXNFLGAME-X-BUF", yes_sub_title="Buffalo")]}, "NFL")
    assert all(k.start_estimate and abs(k.start_time - START) < 1 for k in kal)
    sup = make_sup(novig=ml_market("novig", "G", "NFL", h, a, start=START + 600),       # Novig: the real start
                   kalshi=[k.model_dump() for k in kal])
    for k in sup.kalshi_registry.all():
        assert k.start_time == START + 600 and not k.start_estimate                     # taken from Novig
    assert ("NFL", h, a) not in sup.ambiguous_games
    alone = make_sup(kalshi=[k.model_dump() for k in kal])                              # Kalshi only: no start
    assert all(k.start_time is None for k in alone.kalshi_registry.all())
