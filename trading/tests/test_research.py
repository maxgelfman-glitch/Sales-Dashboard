"""
Self-test: pregame cutoff (never trade or quote into a live game) and the research/measurement layer
(research.py rows, supervisor hooks, research_report.py numbers).

Reference: NYK sharp -120/+100 -> fair 0.52174; NYK ask 0.49 -> +3.17c closing-line value if the
sharp line is unchanged at the cutoff.
"""

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest

import main_supervisor
from execution import ExposureMonitor
from kalshi_feed import parse_kalshi_markets
from main_supervisor import (
    DEMO_KALSHI_MARKETS,
    DEMO_MARKETS,
    DEMO_SHARP_LINES,
    ConfigError,
    Supervisor,
    cutoffs_from_env,
    format_state_report,
    research_from_env,
)
from novig_feed import MarketRegistry
from novig_rest import parse_event_hierarchy, parse_start_time
from research import GapTracker, ResearchRecorder, locked_profit
from research_report import (
    build_report,
    bucket,
    clv_summary,
    depth_summary,
    gap_summary,
    load_rows,
    markout_summary,
    near_miss_summary,
)
from sharp_feed import MockSharpSource, ProviderConfig, SharpLine, pair_fixture_outcomes
from execution import SharpQuote
from tests.test_live_execution import live_sup, slip, upd

NYK_GAME = ("NBA", "New York Knicks", "Boston Celtics")
KC_GAME = ("NFL", "Kansas City Chiefs", "Buffalo Bills")


def rows_of(directory, kind=None):
    out = []
    for path in sorted(directory.glob("research-*.jsonl")):
        out += [json.loads(line) for line in path.read_text().splitlines()]
    return [r for r in out if kind is None or r["kind"] == kind]


def paper_sup(tmp_path, **kw):
    sup = Supervisor(feed_url="ws://127.0.0.1:1/unused", token="t", sharp_fetch=MockSharpSource(DEMO_SHARP_LINES),
                     registry=MarketRegistry(DEMO_MARKETS), kalshi_registry=MarketRegistry(DEMO_KALSHI_MARKETS),
                     research=ResearchRecorder(tmp_path / "research"), maker_kwargs=dict(quiet_seconds=0), **kw)
    sup.book.ingest(DEMO_SHARP_LINES)
    sup.feed.connected.set()
    return sup


def starts_in(sup, game, minutes):
    sup.game_start[game] = time.time() + minutes * 60


# ---------------------------------------------------------------------------
# Start times
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    ("2026-10-01T23:30:00Z", datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc).timestamp()),
    ("2026-10-01T19:30:00-04:00", datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc).timestamp()),
    ("2026-10-01T23:30:00", datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc).timestamp()),   # naive = UTC
    (1790897400, 1790897400.0),
    (1790897400000, 1790897400.0),                                                               # milliseconds
    ("1790897400", 1790897400.0),
    (None, None), ("", None), ("tomorrow", None), ({"x": 1}, None),
])
def test_parse_start_time(value, expected):
    assert parse_start_time(value) == expected


def test_event_start_time_reaches_every_outcome():
    ev = {"eventId": "E1", "league": "NBA", "homeTeam": "NY Knicks", "awayTeam": "Boston",
          "startTime": "2026-10-01T23:30:00Z",
          "markets": [{"marketId": "M1", "type": "MONEYLINE",
                       "outcomes": [{"outcomeId": "O1", "name": "New York Knicks"},
                                    {"outcomeId": "O2", "name": "Boston Celtics"}]}]}
    rows = parse_event_hierarchy({"events": [ev]})
    assert {r.start_time for r in rows} == {parse_start_time("2026-10-01T23:30:00Z")}
    ev.pop("startTime")
    assert {r.start_time for r in parse_event_hierarchy({"events": [ev]})} == {None}


def test_kalshi_start_time_parsed():
    m = dict(ticker="KXNBAGAME-26OCT01BOSNYK-NYK", event_ticker="KXNBAGAME-26OCT01BOSNYK",
             yes_sub_title="New York", title="Boston at New York Winner?", status="active",
             occurrence_datetime="2026-10-01T23:30:00Z")
    rows = parse_kalshi_markets({"markets": [m]}, "NBA")
    assert all(r.start_time == parse_start_time("2026-10-01T23:30:00Z") for r in rows)


def test_supervisor_learns_start_times_from_registries(tmp_path):
    sup = paper_sup(tmp_path)
    assert sup.game_start[NYK_GAME] == pytest.approx(main_supervisor.DEMO_START)
    assert 1430 < sup.minutes_to_start(NYK_GAME) <= 1440


# ---------------------------------------------------------------------------
# Pregame cutoff
# ---------------------------------------------------------------------------
async def test_live_taker_blocked_inside_cutoff_allowed_outside():
    sup = live_sup()
    starts_in(sup, NYK_GAME, 0.5)                      # 30s to start, taker cutoff is 1 min
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    assert sup.order_gateway.placed == [] and sup.stats["cutoff_blocked"] == 1
    starts_in(sup, NYK_GAME, 2)                        # inside the maker window, outside the taker one
    await sup.on_market_update(upd("O-NYK", 0.48), None)
    assert len(sup.order_gateway.placed) == 1


async def test_live_never_trades_a_game_without_start_time():
    sup = live_sup()
    sup.game_start.clear()
    await sup.on_market_update(upd("O-NYK", 0.49).model_copy(update={"start_time": None}), None)
    assert sup.order_gateway.placed == []
    assert sup._trade_blocked(NYK_GAME).startswith("no scheduled start time")


async def test_paper_trades_a_game_without_start_time(tmp_path):
    sup = paper_sup(tmp_path)
    sup.game_start.clear()
    assert sup.require_start_time is False
    await sup.on_market_update(upd("O-NYK", 0.49).model_copy(update={"start_time": None}), None)
    assert [o.kind for o in sup.orders] == ["DIRECTIONAL"]


async def test_start_time_on_an_update_is_learned(tmp_path):
    sup = paper_sup(tmp_path)
    sup.game_start.clear()
    u = upd("O-NYK", 0.60).model_copy(update={"start_time": time.time() + 30})
    await sup.on_market_update(u, None)
    assert sup._trade_blocked(NYK_GAME).startswith("taker cutoff")


async def test_arb_hedge_blocked_inside_cutoff(tmp_path):
    sup = paper_sup(tmp_path)
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    assert [o.kind for o in sup.orders] == ["DIRECTIONAL"]
    starts_in(sup, NYK_GAME, 0.5)
    await sup.on_market_update(upd("O-BOS", 0.40), None)       # would lock 11c: still not allowed
    assert [o.kind for o in sup.orders] == ["DIRECTIONAL"]


async def test_maker_then_taker_then_start_phases(tmp_path):
    ledger = tmp_path / "live_ledger.jsonl"
    sup = live_sup(maker=True, ledger_path=ledger, research=ResearchRecorder(tmp_path / "research"))
    await sup.maker.refresh()
    games = {tuple(q.market_key)[:3] for q in sup.maker.quotes.values()}
    assert NYK_GAME in games and len(games) > 1
    for oid in ("O-NYK", "O-BOS"):
        sup.feed.latest[oid] = upd(oid, 0.51)

    starts_in(sup, NYK_GAME, 2.5)                                  # maker window only
    assert await sup.run_cutoffs() == [(NYK_GAME, "maker")]
    assert not any(tuple(q.market_key)[:3] == NYK_GAME for q in sup.maker.quotes.values())
    assert any(tuple(q.market_key)[:3] != NYK_GAME for q in sup.maker.quotes.values())
    assert sup.order_gateway.cancels                                           # cancels really sent
    await sup.maker.refresh()                                                   # never re-quoted
    assert not any(tuple(q.market_key)[:3] == NYK_GAME for q in sup.maker.quotes.values())
    assert sup._trade_blocked(NYK_GAME) is None                                 # takers still allowed
    assert rows_of(tmp_path / "research", "CLOSE") == []

    starts_in(sup, NYK_GAME, 0.5)
    assert await sup.run_cutoffs() == [(NYK_GAME, "taker")]
    assert {c["point"] for c in rows_of(tmp_path / "research", "CLOSE")} == {"cutoff"}
    starts_in(sup, NYK_GAME, -0.1)
    assert await sup.run_cutoffs() == [(NYK_GAME, "start")]
    assert await sup.run_cutoffs() == []                                        # each phase once
    assert {c["point"] for c in rows_of(tmp_path / "research", "CLOSE")} == {"cutoff", "start"}
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if '"CUTOFF"' in line]
    assert [(r["phase"], r["game"]) for r in rows] == [("maker", list(NYK_GAME)), ("taker", list(NYK_GAME))]
    assert rows[0]["quotes_pulled"] >= 2


def test_league_overrides_and_maker_never_before_taker(tmp_path):
    sup = paper_sup(tmp_path, taker_cutoff_s=60, maker_cutoff_s=30, cutoff_overrides={"NFL": (120, 600)})
    assert sup.cutoffs_for("NBA") == (60, 60)                                  # maker raised to the taker cutoff
    assert sup.cutoffs_for("NFL") == (120, 600)
    starts_in(sup, KC_GAME, 5)
    assert sup._trade_blocked(KC_GAME) is None and sup._trade_blocked(KC_GAME, "maker").startswith("maker cutoff")


def test_cutoff_env_parsing():
    assert cutoffs_from_env({}) == dict(taker_cutoff_s=60, maker_cutoff_s=180, cutoff_overrides={})
    assert cutoffs_from_env({"TAKER_CUTOFF_MINUTES": "2", "MAKER_CUTOFF_MINUTES": "5",
                             "CUTOFF_OVERRIDES": "nba=1/4, NFL=0.5/2"}) == dict(
        taker_cutoff_s=120, maker_cutoff_s=300, cutoff_overrides={"NBA": (60, 240), "NFL": (30, 120)})
    for bad in ({"TAKER_CUTOFF_MINUTES": "0"}, {"TAKER_CUTOFF_MINUTES": "abc"}, {"MAKER_CUTOFF_MINUTES": "5000"},
                {"TAKER_CUTOFF_MINUTES": "5", "MAKER_CUTOFF_MINUTES": "2"}, {"CUTOFF_OVERRIDES": "NBA=5"},
                {"CUTOFF_OVERRIDES": "NBA=5/1"}):
        with pytest.raises(ConfigError):
            cutoffs_from_env(bad)
    assert research_from_env({"RESEARCH_ENABLED": "0"}) is None
    assert str(research_from_env({"RESEARCH_DIR": "x/y"}).dir) == "x/y"


def test_check_config_reports_cutoffs_and_research(tmp_path):
    report = format_state_report(paper_sup(tmp_path, taker_cutoff_s=120, maker_cutoff_s=300,
                                           cutoff_overrides={"NFL": (60, 180)}))
    assert "no new orders 2 min before scheduled start" in report
    assert "quotes pulled 5 min before scheduled start" in report and "NFL 1/3" in report
    assert "Games with no start time" in report and str(tmp_path / "research") in report
    assert "NEVER traded" in format_state_report(live_sup())


# ---------------------------------------------------------------------------
# Live-game detection
# ---------------------------------------------------------------------------
def test_sharp_live_flag_parsing():
    base = dict(league="NBA", home_team="NY Knicks", away_team="Boston", market_type="moneyline",
                side="NY Knicks", odds_for=-120, odds_against=100)
    for v, expected in ((True, True), ("in_progress", True), ("Live", True), ("halftime", True), ("final", True),
                        (1, True), (False, False), ("unplayed", False), ("scheduled", False), (None, False), (0, False)):
        assert SharpLine(**base, is_live=v).is_live is expected, v


async def test_sharp_in_play_line_stops_the_game_and_is_never_used(tmp_path):
    ledger = tmp_path / "live_ledger.jsonl"
    sup = live_sup(maker=True, ledger_path=ledger)
    await sup.maker.refresh()
    assert any(tuple(q.market_key)[:3] == NYK_GAME for q in sup.maker.quotes.values())
    live_line = dict(DEMO_SHARP_LINES[0], is_live="in_progress", odds_for=-400, odds_against=300)
    assert sup.book.ingest([live_line]) == (0, 1)
    await asyncio.sleep(0.01)                                                  # quote pull is spawned
    assert NYK_GAME in sup.live_games
    assert sup.book.lookup("NBA", "New York Knicks", "Boston Celtics", "moneyline", "New York Knicks") is None
    assert not any(tuple(q.market_key)[:3] == NYK_GAME for q in sup.maker.quotes.values())
    await sup.on_market_update(upd("O-NYK", 0.30), None)                     # a huge "edge": still ignored
    assert not any(lo.kind == "DIRECTIONAL" for lo in sup.live_orders.values())
    assert '"GAME_LIVE"' in ledger.read_text()
    sup.book.ingest(DEMO_SHARP_LINES)                                          # pregame lines again: still stopped
    assert sup._trade_blocked(NYK_GAME).startswith("game is live")


def test_fixture_is_live_reaches_sharp_lines():
    cfg = ProviderConfig(url="http://x", mode="outcomes", fixture_fields={"is_live": "status"},
                         outcome_fields={"market": "market", "selection": "name", "price": "price"})
    fixture = {"league": "NBA", "home_team": "New York Knicks", "away_team": "Boston Celtics", "status": "live",
               "odds": [{"market": "moneyline", "name": "New York Knicks", "price": -120},
                        {"market": "moneyline", "name": "Boston Celtics", "price": 100}]}
    lines, _ = pair_fixture_outcomes(fixture, cfg)
    assert [SharpLine(**ln).is_live for ln in lines] == [True]


class FakeRest:
    events_url = "http://fake/events"

    def __init__(self, batches):
        self.batches = list(batches)

    async def fetch_open_markets(self):
        return self.batches.pop(0)


async def test_game_gone_from_novig_pregame_list_near_start_is_flagged_live(tmp_path):
    everything = list(DEMO_MARKETS)
    without_nyk = [m for m in DEMO_MARKETS if m["event_id"] != "NBA-BOS-NYK"]
    without_both = [m for m in without_nyk if m["event_id"] != "NBA-LAL-GSW"]
    sup = paper_sup(tmp_path)
    sup.novig_rest = FakeRest([everything, without_nyk, [], without_both])
    await sup.bootstrap()
    assert NYK_GAME in sup.novig_games and not sup.live_games
    starts_in(sup, NYK_GAME, 20)                                               # close to start
    await sup.bootstrap()
    assert sup.live_games == {NYK_GAME: "no longer in Novig's pregame list"}
    await sup.bootstrap()                                                      # empty response: a glitch
    assert len(sup.live_games) == 1
    gsw = ("NBA", "Golden State Warriors", "Los Angeles Lakers")
    await sup.bootstrap()                                                      # far from start: not flagged
    assert gsw not in sup.live_games


def test_refresh_speeds_up_near_start(tmp_path):
    sup = paper_sup(tmp_path)
    assert not sup._any_game_near_start()                                      # demo games start in 24h
    starts_in(sup, NYK_GAME, 10)
    assert sup._any_game_near_start()
    sup.live_games[NYK_GAME] = "x"
    assert not sup._any_game_near_start()


# ---------------------------------------------------------------------------
# Paper "would have traded" inside the cutoff
# ---------------------------------------------------------------------------
async def test_paper_records_shadow_decisions_inside_the_cutoff(tmp_path):
    sup = paper_sup(tmp_path)
    starts_in(sup, NYK_GAME, 0.5)
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    assert sup.orders == []
    [dec] = rows_of(tmp_path / "research", "DECISION")
    assert dec["action"] == "BET" and dec["blocked"].startswith("taker cutoff")
    sup.mark_game_live(NYK_GAME, "test")
    await sup.on_market_update(upd("O-NYK", 0.48), None)
    assert len(rows_of(tmp_path / "research", "DECISION")) == 1                # never once live
    rep = build_report(rows_of(tmp_path / "research"))
    assert "[CLV BY TIME TO START]" in rep


async def test_live_mode_never_shadows(tmp_path):
    sup = live_sup(research=ResearchRecorder(tmp_path / "research"))
    starts_in(sup, NYK_GAME, 0.5)
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    assert rows_of(tmp_path / "research", "DECISION") == []


# ---------------------------------------------------------------------------
# Buying through several price levels
# ---------------------------------------------------------------------------
def book_upd(oid, levels, **kw):
    return upd(oid, levels[0][0], volume=levels[0][1], **kw).model_copy(update={"ask_levels": levels})


async def test_walks_levels_while_each_has_edge(tmp_path):
    sup = paper_sup(tmp_path)
    # fair 0.52174: 0.49 +6.5%, 0.50 +4.3%, 0.51 +2.3% (below the 2.5% threshold -> never bought)
    await sup.on_market_update(book_upd("O-NYK", [(0.49, 300), (0.50, 400), (0.51, 5000)]), None)
    [order] = sup.orders
    assert order.contracts == 700 and order.price == pytest.approx((300 * 0.49 + 400 * 0.50) / 700, abs=1e-6)
    assert order.stake_usd == pytest.approx(300 * 0.49 + 400 * 0.50)
    assert sup.stats["multi_level_takes"] == 1


async def test_walk_is_capped_by_kelly_of_the_worst_level(tmp_path):
    sup = paper_sup(tmp_path)
    await sup.on_market_update(book_upd("O-NYK", [(0.49, 100), (0.50, 100_000)]), None)
    [order] = sup.orders
    fair = 0.5217391
    worst = await Supervisor._evaluate("novig", 0.50, sup_quote(sup), None, "t")
    assert order.stake_usd <= worst.stake_usd + 0.5 and order.contracts > 100
    assert order.stake_usd <= 1000.0 and fair > order.price


def sup_quote(sup):
    line = sup.book.lookup("NBA", "New York Knicks", "Boston Celtics", "moneyline", "New York Knicks")
    return SharpQuote(odds_for=line.odds_for, odds_against=line.odds_against, line=line.line, source=line.source)


async def test_inconsistent_depth_falls_back_to_best_ask(tmp_path):
    sup = paper_sup(tmp_path)
    u = upd("O-NYK", 0.49, volume=300).model_copy(update={"ask_levels": [(0.47, 999), (0.49, 300)]})
    await sup.on_market_update(u, None)
    assert sup.orders[0].contracts == 300 and sup.orders[0].price == 0.49


async def test_live_walk_sends_one_limit_at_the_worst_level_and_reserves_at_it():
    sup = live_sup()
    await sup.on_market_update(book_upd("O-NYK", [(0.49, 300), (0.50, 400)]), None)
    [sent] = sup.order_gateway.placed
    assert (sent["price_cents"], sent["contracts"]) == (50.0, 700)
    assert sup.total_exposure() == pytest.approx(350.0)                       # reserved at the limit
    await sup.on_fill_slip(slip("ex-1", "PARTIAL", 300, 49))
    await sup.on_fill_slip(slip("ex-1", "FILLED", 700, 50))
    leg = sup.orders[0]
    assert leg.contracts == 700 and not leg.pending
    assert sup.total_exposure() == pytest.approx(300 * 0.49 + 400 * 0.50)     # real cost, not the reservation


async def test_live_canary_stake_caps_the_walk():
    sup = live_sup(max_stake=10.0)
    await sup.on_market_update(book_upd("O-NYK", [(0.49, 10), (0.50, 400)]), None)
    [sent] = sup.order_gateway.placed
    assert sent["contracts"] * sent["price_cents"] / 100 <= 10.0 + 1e-9 and sent["contracts"] == 20


# ---------------------------------------------------------------------------
# Hedges across levels and partial hedges
# ---------------------------------------------------------------------------
async def test_hedge_walks_levels_while_every_level_locks_profit(tmp_path):
    sup = paper_sup(tmp_path)
    await sup.on_market_update(upd("O-NYK", 0.49, volume=1000), None)
    first = sup.orders[0]
    assert first.contracts == 1000
    # 0.45 locks 6c, 0.47 locks 4c, 0.50 locks 1c (= the minimum), 0.51 locks 0
    await sup.on_market_update(book_upd("O-BOS", [(0.45, 300), (0.47, 300), (0.50, 300), (0.51, 5000)]), None)
    hedge = sup.orders[1]
    assert hedge.kind == "ARB_HEDGE" and hedge.contracts == 900
    assert sup.positions[("NBA", "New York Knicks", "Boston Celtics", "moneyline")].unhedged() == 100
    assert sup.stats["partial_hedges"] == 1


async def test_partial_hedge_then_completion(tmp_path):
    sup = paper_sup(tmp_path)
    key = ("NBA", "New York Knicks", "Boston Celtics", "moneyline")
    await sup.on_market_update(upd("O-NYK", 0.49, volume=1000), None)
    await sup.on_market_update(upd("O-BOS", 0.45, volume=400), None)
    assert sup.positions[key].unhedged() == 600 and not sup.positions[key].hedged
    await sup.on_market_update(upd("O-BOS", 0.46, volume=5000), None)
    assert sup.positions[key].unhedged() == 0 and sup.positions[key].hedged
    assert [o.contracts for o in sup.orders] == [1000, 400, 600]
    await sup.on_market_update(upd("O-BOS", 0.40, volume=5000), None)          # fully hedged: nothing more
    assert len(sup.orders) == 3


async def test_tiny_partial_hedge_is_skipped(tmp_path):
    sup = paper_sup(tmp_path)
    await sup.on_market_update(upd("O-NYK", 0.49, volume=1000), None)
    await sup.on_market_update(upd("O-BOS", 0.45, volume=5), None)
    assert len(sup.orders) == 1


def test_partial_hedge_scenarios_are_pro_rated():
    from main_supervisor import PaperOrder, arbitrage_scenarios
    first = PaperOrder(order_id=1, kind="DIRECTIONAL", venue="novig", outcome_id="O", event_id="E", league="NBA",
                       market_type="moneyline", side="A", line=None, price=0.49, contracts=1000, stake_usd=490.0,
                       edge=0.05, capped=False, placed_at=0)
    s = arbitrage_scenarios(first, "novig", 400, 180.0, "NBA", "moneyline")
    assert s == {"first_side_wins": round(400 - 196.0 - 180.0, 2), "other_side_wins": round(400 - 196.0 - 180.0, 2)}


# ---------------------------------------------------------------------------
# Locked-profit maths and gap tracking
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("a,pa,b,pb,tie,expected", [
    ("novig", 0.48, "novig", 0.51, None, 0.01),
    ("novig", 0.48, "kalshi", 0.50, None, 0.0025),           # Kalshi taker fee 0.07*.5*.5 = 1.75c
    ("prophetx", 0.48, "novig", 0.50, None, 0.0096),         # ProphetX 2% of the 52c win
    ("novig", 0.48, "novig", 0.51, 0.5, 0.01),               # dead heat pays 2 x 50c = $1: same
    ("novig", 0.48, "novig", 0.51, 0.0, -0.99),              # a tie that pays nothing on either leg
])
def test_locked_profit(a, pa, b, pb, tie, expected):
    assert locked_profit(a, pa, b, pb, tie) == pytest.approx(expected)


class Clock:
    def __init__(self):
        self.t = 1_790_000_000.0

    def __call__(self):
        return self.t


def tracker(tmp_path):
    clock = Clock()
    rec = ResearchRecorder(tmp_path, clock=clock)
    return GapTracker(rec, clock=clock), clock


ML = ("NBA", "New York Knicks", "Boston Celtics", "moneyline")
SIDES = ("New York Knicks", "Boston Celtics")


def test_gap_opens_and_closes_with_duration_profit_and_depth(tmp_path):
    g, clock = tracker(tmp_path)
    g.update(ML, "novig", SIDES[0], 0.48, 1000, None, None, SIDES, 120)
    assert g.update(ML, "kalshi", SIDES[1], 0.52, 400, None, None, SIDES, 120) == []    # 0.48+0.52+fee: no
    opened = g.update(ML, "kalshi", SIDES[1], 0.49, 400, None, None, SIDES, 119)
    assert opened and opened[0]["locked_profit"] == pytest.approx(1 - 0.97 - 0.07 * 0.49 * 0.51, abs=1e-5)
    clock.t += 7.5
    g.update(ML, "kalshi", SIDES[1], 0.47, 900, None, None, SIDES, 118)                  # still open, better
    clock.t += 2.5
    g.update(ML, "novig", SIDES[0], None, 0, None, None, SIDES, 118)                     # novig side gone
    [close] = rows_of(tmp_path, "GAP_CLOSE")
    assert close["duration_s"] == 10.0 and close["max_depth"] == 900
    assert close["best_locked_profit"] == pytest.approx(1 - 0.95 - 0.07 * 0.47 * 0.53, abs=1e-5)
    assert len(rows_of(tmp_path, "GAP_OPEN")) == 1


def test_spreads_must_mirror_and_totals_must_match(tmp_path):
    g, _ = tracker(tmp_path)
    spread = ("NBA", "Golden State Warriors", "Los Angeles Lakers", "spread")
    s = ("Golden State Warriors", "Los Angeles Lakers")
    g.update(spread, "novig", s[0], 0.45, 100, -4.5, None, s, 60)
    assert g.update(spread, "kalshi", s[1], 0.45, 100, 5.5, None, s, 60) == []           # -4.5 vs +5.5
    assert g.update(spread, "prophetx", s[1], 0.45, 100, 4.5, None, s, 60)               # mirrors
    total = ("NFL", "Kansas City Chiefs", "Buffalo Bills", "total")
    g.update(total, "novig", "over", 0.45, 100, 47.5, None, ("over", "under"), 60)
    assert g.update(total, "kalshi", "under", 0.45, 100, 48.5, None, ("over", "under"), 60) == []


def test_drop_venue_closes_only_its_gaps(tmp_path):
    g, _ = tracker(tmp_path)
    g.update(ML, "novig", SIDES[0], 0.45, 100, None, None, SIDES, 60)
    g.update(ML, "novig", SIDES[1], 0.50, 100, None, None, SIDES, 60)
    g.update(ML, "prophetx", SIDES[1], 0.45, 100, None, None, SIDES, 60)
    assert len(g.open_gaps) == 2
    g.drop_venue("prophetx")
    assert [k[1:3] for k in g.open_gaps] == [("novig", "novig")]
    assert [(r["venue_a"], r["venue_b"]) for r in rows_of(tmp_path, "GAP_CLOSE")] == [("novig", "prophetx")]


def test_best_combo_reports_near_misses(tmp_path):
    g, _ = tracker(tmp_path)
    g.update(ML, "novig", SIDES[0], 0.52, 100, None, None, SIDES, 60)
    g.update(ML, "novig", SIDES[1], 0.50, 300, None, None, SIDES, 60)
    g.update(ML, "kalshi", SIDES[1], 0.49, 50, None, None, SIDES, 60)
    best = g.best_combo(ML, None, SIDES)
    assert (best["venue_b"], best["locked_profit"]) == ("novig", -0.02)   # Kalshi's 1c is eaten by its fee


def test_recorder_uses_utc_day_files_and_never_raises(tmp_path):
    clock = Clock()
    rec = ResearchRecorder(tmp_path / "r", clock=clock)
    rec.write("X", a=1)
    clock.t += 86400
    rec.write("X", a=2, kind="ignored")                       # a field named kind can't clobber the row kind
    assert sorted(p.name for p in (tmp_path / "r").iterdir()) == ["research-20260921.jsonl", "research-20260922.jsonl"]
    assert rows_of(tmp_path / "r")[1]["kind"] == "X"
    blocked = tmp_path / "file"
    blocked.write_text("")
    ResearchRecorder(blocked / "sub").write("X")             # a file where the folder should be: logged, not raised


# ---------------------------------------------------------------------------
# Supervisor research hooks -> report
# ---------------------------------------------------------------------------
async def test_decision_entry_close_give_closing_line_value(tmp_path):
    sup = paper_sup(tmp_path)
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    [dec] = rows_of(tmp_path / "research", "DECISION")
    assert (dec["action"], dec["venue"], dec["game"][:3]) == ("BET", "novig", list(NYK_GAME))
    assert dec["edge"] > 0.025 and dec["minutes_to_start"] > 1400 and dec["moved_last"] in ("novig", "sharp")
    [entry] = rows_of(tmp_path / "research", "ENTRY")
    assert (entry["order_kind"], entry["price"], entry["side"]) == ("DIRECTIONAL", 0.49, "New York Knicks")
    sup.feed.latest["O-NYK"] = upd("O-NYK", 0.53)
    sup.feed.latest["O-BOS"] = upd("O-BOS", 0.49)
    starts_in(sup, NYK_GAME, 0.5)
    await sup.run_cutoffs()
    closes = rows_of(tmp_path / "research", "CLOSE")
    assert {c["side"] for c in closes} == set(SIDES)
    clv = clv_summary(rows_of(tmp_path / "research"))
    assert clv["entries_with_close"] == 1 and clv["beat_close"] == 1
    assert clv["mean_clv_cents"] == pytest.approx((0.521739 - 0.49) * 100, abs=0.01)


async def test_paper_maker_fill_records_entry_and_markouts(tmp_path, monkeypatch):
    monkeypatch.setattr(main_supervisor, "MARKOUT_DELAYS", (0.0, 0.01))
    sup = paper_sup(tmp_path)
    await sup.maker.refresh()
    ask = next(q for q in sup.maker.quotes.values() if q.outcome_id == "O-BOS" and q.side == "sell")
    await sup.on_market_update(upd("O-BOS", None, bid=ask.price_cents / 100), None)    # tape crosses our ask
    assert any(o.kind == "MAKER_FILL" for o in sup.orders)
    for _ in range(50):
        if len(rows_of(tmp_path / "research", "MARKOUT")) == 2:
            break
        await main_supervisor.asyncio.sleep(0.01)
    marks = rows_of(tmp_path / "research", "MARKOUT")
    assert [m["delay_s"] for m in marks] == [0.0, 0.01]
    assert marks[0]["markout_per_contract"] == pytest.approx(marks[0]["fair_prob"] - marks[0]["fill_price"], abs=1e-5)
    assert any(e["order_kind"] == "MAKER_FILL" for e in rows_of(tmp_path / "research", "ENTRY"))
    assert set(markout_summary(rows_of(tmp_path / "research"))) == {0.0, 0.01}


async def test_market_updates_feed_gaps_and_depth_sampler(tmp_path):
    sup = paper_sup(tmp_path, exposure=ExposureMonitor(0.01))            # no trading: measurement only
    for oid, price in (("O-NYK", 0.47), ("O-BOS", 0.60)):
        u = upd(oid, price)
        sup.feed.latest[oid] = u
        await sup.on_market_update(u, None)
    kalshi_bos = upd("KXNBAGAME-BOSNYK-BOS", 0.45)
    sup.kalshi.latest[kalshi_bos.outcome_id] = kalshi_bos
    await sup.on_market_update(kalshi_bos, None)
    [gap] = rows_of(tmp_path / "research", "GAP_OPEN")
    assert {gap["venue_a"], gap["venue_b"]} == {"novig", "kalshi"}
    assert gap["locked_profit"] == pytest.approx(1 - 0.92 - 0.07 * 0.45 * 0.55, abs=1e-5)
    assert sup.sample_depth() == 3
    depth = rows_of(tmp_path / "research", "DEPTH")
    assert {d["venue"] for d in depth} == {"novig", "kalshi"} and all(d["minutes_to_start"] > 1400 for d in depth)
    [combo] = rows_of(tmp_path / "research", "BEST_COMBO")
    assert combo["locked_profit"] == gap["locked_profit"]
    await sup.on_feed_state("DISCONNECTED", {"venue": "kalshi"})
    assert len(rows_of(tmp_path / "research", "GAP_CLOSE")) == 1


# ---------------------------------------------------------------------------
# Report maths
# ---------------------------------------------------------------------------
def test_time_buckets():
    assert [bucket(m) for m in (2000, 600, 100, 45, 15, 5, 2, 0.5, -3, None)] == \
        ["> 24h", "6-24h", "1-6h", "30-60m", "10-30m", "3-10m", "1-3m", "< 1m", "< 1m", "unknown"]


def test_report_numbers(tmp_path):
    t = 1_790_000_000.0
    g = ["NBA", "A", "B", "moneyline"]
    rows = [
        dict(ts=t, kind="ENTRY", game=g, side="A", line=None, price=0.40, contracts=100, stake_usd=40.0),
        dict(ts=t, kind="ENTRY", game=g, side="B", line=None, price=0.60, contracts=100, stake_usd=60.0),
        dict(ts=t + 1, kind="CLOSE", game=g, side="A", line=None, venue="novig", close_fair_prob=0.45),
        dict(ts=t + 1, kind="CLOSE", game=g, side="B", line=None, venue="novig", close_fair_prob=0.55),
        dict(ts=t, kind="DEPTH", venue="novig", ask=0.5, ask_size=1000, minutes_to_start=90),
        dict(ts=t, kind="DEPTH", venue="novig", ask=0.5, ask_size=3000, minutes_to_start=100),
        dict(ts=t, kind="GAP_OPEN", venue_a="novig", venue_b="kalshi", minutes_to_start=90),
        dict(ts=t + 4, kind="GAP_CLOSE", venue_a="novig", venue_b="kalshi", duration_s=4.0,
             best_locked_profit=0.01, max_depth=500),
        dict(ts=t, kind="BEST_COMBO", locked_profit=0.005),
        dict(ts=t, kind="BEST_COMBO", locked_profit=-0.015),
        dict(ts=t, kind="BEST_COMBO", locked_profit=-0.03),
        dict(ts=t, kind="MARKOUT", delay_s=10.0, markout_per_contract=-0.01),
        dict(ts=t, kind="MARKOUT", delay_s=10.0, markout_per_contract=0.03),
    ]
    clv = clv_summary(rows)
    assert clv["beat_close"] == 1 and clv["mean_clv_cents"] == pytest.approx(0.0)
    assert clv["total_clv_usd"] == pytest.approx(0.0) and clv["clv_return_on_stake"] == pytest.approx(0.0)
    assert depth_summary(rows)["novig"]["1-6h"] == {"samples": 2, "median_usd": 1000.0}
    gaps = gap_summary(rows)
    assert gaps["by_pair"]["kalshi+novig"]["ceiling_usd"] == pytest.approx(5.0)
    assert gaps["by_time"] == {"1-6h": 1} and gaps["still_open"] == 0
    near = near_miss_summary(rows)
    assert (near["locked"], near["within_1c"], near["within_2c"]) == pytest.approx((1 / 3, 1 / 3, 2 / 3))
    assert markout_summary(rows)[10.0]["adverse_share"] == 0.5
    text = build_report(rows)
    for section in ("[CLOSING LINE VALUE]", "[LIQUIDITY]", "[LOCKED-PROFIT GAPS]", "[NEAR MISSES]", "kalshi+novig"):
        assert section in text


def test_report_loads_files_skips_torn_lines_and_filters_by_date(tmp_path):
    (tmp_path / "research-20260920.jsonl").write_text(json.dumps({"ts": 1, "kind": "DEPTH"}) + "\n")
    (tmp_path / "research-20260921.jsonl").write_text(json.dumps({"ts": 2, "kind": "DEPTH"}) + "\n{\"ts\": 3, \"ki")
    assert [r["ts"] for r in load_rows(tmp_path)] == [1, 2]
    assert [r["ts"] for r in load_rows(tmp_path, since="2026-09-21")] == [2]
    assert "No research rows yet" in build_report([])


# ---------------------------------------------------------------------------
# Worst case: the cheap levels vanish and everything fills at the limit
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("levels,max_stake,cap", [
    ([(0.47, 15), (0.49, 400)], 10.0, 10.0),            # canary
    ([(0.45, 1500), (0.49, 5000)], 1000.0, 1000.0),     # hard per-position ceiling
])
async def test_worst_case_fill_at_limit_never_breaks_a_cap(levels, max_stake, cap):
    sup = live_sup(max_stake=max_stake)
    await sup.on_market_update(book_upd("O-NYK", levels), None)
    [sent] = sup.order_gateway.placed
    assert sent["contracts"] * sent["price_cents"] / 100 <= cap + 1e-9
    assert sup.total_exposure() <= cap + 1e-9                                    # reservation at the limit


async def test_never_buys_a_level_without_edge_and_never_more_than_shown(tmp_path):
    sup = paper_sup(tmp_path)
    # fair 0.52174 -> 0.51 is +2.3%: below the threshold, so the limit can never be 0.51
    await sup.on_market_update(book_upd("O-NYK", [(0.49, 50), (0.50, 60), (0.51, 100_000)]), None)
    [order] = sup.orders
    assert order.contracts == 110                                               # exactly what was shown
    live = live_sup()
    await live.on_market_update(book_upd("O-NYK", [(0.49, 50), (0.50, 60), (0.51, 100_000)]), None)
    assert live.order_gateway.placed[0]["price_cents"] == 50.0


async def test_hedge_worst_case_fits_the_cap(tmp_path):
    sup = paper_sup(tmp_path)
    await sup.on_market_update(upd("O-NYK", 0.30, volume=3000), None)          # big cheap first leg
    first = sup.orders[0]
    await sup.on_market_update(book_upd("O-BOS", [(0.40, 1000), (0.60, 5000)]), None)
    hedge = sup.orders[1]
    assert hedge.contracts * 0.60 <= 1000.0 + 1e-9 or hedge.price == 0.40
    assert hedge.contracts <= first.contracts
