"""
Self-test: pregame cutoff (never trade or quote into a live game) and the research/measurement layer
(research.py rows, supervisor hooks, research_report.py numbers).

Reference: NYK sharp -120/+100 -> fair 0.52174; NYK ask 0.49 -> +3.17c closing-line value if the
sharp line is unchanged at the cutoff.
"""

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
    PREGAME_CUTOFF_SECONDS,
    ConfigError,
    Supervisor,
    format_state_report,
    pregame_cutoff_from_env,
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
from sharp_feed import MockSharpSource
from tests.test_live_execution import live_sup, upd

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
    starts_in(sup, NYK_GAME, 5)                        # 5 min to start, cutoff is 10
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    assert sup.order_gateway.placed == [] and sup.stats["cutoff_blocked"] == 1
    starts_in(sup, NYK_GAME, 60)
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
    u = upd("O-NYK", 0.60).model_copy(update={"start_time": time.time() + 120})
    await sup.on_market_update(u, None)
    assert sup._trade_blocked(NYK_GAME).startswith("pregame cutoff")


async def test_arb_hedge_blocked_inside_cutoff(tmp_path):
    sup = paper_sup(tmp_path)
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    assert [o.kind for o in sup.orders] == ["DIRECTIONAL"]
    starts_in(sup, NYK_GAME, 3)
    await sup.on_market_update(upd("O-BOS", 0.40), None)       # would lock 11c: still not allowed
    assert [o.kind for o in sup.orders] == ["DIRECTIONAL"]


async def test_cutoff_pulls_only_that_games_quotes_and_logs(tmp_path):
    ledger = tmp_path / "live_ledger.jsonl"
    sup = live_sup(maker=True, ledger_path=ledger, research=ResearchRecorder(tmp_path / "research"))
    await sup.maker.refresh()
    games = {tuple(q.market_key)[:3] for q in sup.maker.quotes.values()}
    assert NYK_GAME in games and len(games) > 1
    starts_in(sup, NYK_GAME, 9)
    assert await sup.run_cutoffs() == [NYK_GAME]
    assert not any(tuple(q.market_key)[:3] == NYK_GAME for q in sup.maker.quotes.values())
    assert any(tuple(q.market_key)[:3] != NYK_GAME for q in sup.maker.quotes.values())
    assert sup.order_gateway.cancels                                           # cancels really sent
    [row] = [json.loads(line) for line in ledger.read_text().splitlines() if '"CUTOFF"' in line]
    assert row["game"] == list(NYK_GAME) and row["quotes_pulled"] >= 2
    assert await sup.run_cutoffs() == []                                        # once per game
    await sup.maker.refresh()                                                   # never re-quoted
    assert not any(tuple(q.market_key)[:3] == NYK_GAME for q in sup.maker.quotes.values())


def test_cutoff_env_parsing():
    assert pregame_cutoff_from_env({}) == PREGAME_CUTOFF_SECONDS == 600
    assert pregame_cutoff_from_env({"PREGAME_CUTOFF_MINUTES": "15"}) == 900
    for bad in ("0", "0.5", "abc", "5000"):
        with pytest.raises(ConfigError):
            pregame_cutoff_from_env({"PREGAME_CUTOFF_MINUTES": bad})
    assert research_from_env({"RESEARCH_ENABLED": "0"}) is None
    assert str(research_from_env({"RESEARCH_DIR": "x/y"}).dir) == "x/y"


def test_check_config_reports_cutoff_and_research(tmp_path):
    report = format_state_report(paper_sup(tmp_path, pregame_cutoff_s=900))
    assert "stop trading + pull quotes 15 min before start" in report
    assert "Games with no start time" in report and str(tmp_path / "research") in report
    assert "NEVER traded" in format_state_report(live_sup())


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
    starts_in(sup, NYK_GAME, 9)
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
    assert [bucket(m) for m in (2000, 600, 100, 45, 15, 5, -3, None)] == \
        ["> 24h", "6-24h", "1-6h", "30-60m", "10-30m", "< 10m", "< 10m", "unknown"]


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
