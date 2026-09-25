"""
research_report.py — Turn research/*.jsonl into the numbers that decide whether the strategy works.

    python research_report.py                      # everything in ./research
    python research_report.py --dir logs/research  # another folder
    python research_report.py --since 2026-10-01   # only rows from this UTC date on

WHAT EACH SECTION ANSWERS
    DECISIONS      how often a price beat the sharp line by > 2.5%, and who moved last
    CLOSING LINE   did our entries beat the closing line? (CLV > 0 on average = real edge;
                   this is the fastest honest signal, long before P&L means anything)
    BY TIME        CLV of every BET decision by minutes before the start, INCLUDING the ones the
                   cutoff blocked (paper mode) -> tells you where to set TAKER_CUTOFF_MINUTES
    MARKOUTS       after a maker fill, did fair value move against us? (negative = picked off)
    LIQUIDITY      how much size sits at the best price, by minutes before the game starts
    GAPS           how often a both-sides combination locked a profit after fees, for how
                   long, how much, and how deep  (the core hedged strategy)
    NEAR MISSES    how often the cheapest both-sides combination was within 1c / 2c of a
                   locked profit (the room a hedged MAKER could work in)

Nothing here touches an exchange. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

TIME_BUCKETS = ((24 * 60, "> 24h"), (6 * 60, "6-24h"), (60, "1-6h"), (30, "30-60m"), (10, "10-30m"),
                (3, "3-10m"), (1, "1-3m"), (float("-inf"), "< 1m"))
BUCKET_ORDER = [label for _, label in TIME_BUCKETS] + ["unknown"]


def bucket(minutes: Optional[float]) -> str:
    if minutes is None:
        return "unknown"
    for floor, label in TIME_BUCKETS:
        if minutes >= floor:
            return label
    return "unknown"


def load_rows(directory: Path, since: Optional[str] = None) -> list[dict]:
    """All rows from research-YYYYMMDD.jsonl files (optionally from a UTC date on), oldest first."""
    rows = []
    since_key = since.replace("-", "") if since else None
    for path in sorted(Path(directory).glob("research-*.jsonl")):
        if since_key and path.stem.split("-")[-1] < since_key:
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue                                   # a half-written last line after a crash
    rows.sort(key=lambda r: r.get("ts", 0))
    return rows


def _median(values: Iterable[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def _mean(values: Iterable[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _key(row: dict) -> tuple:
    return (tuple(row.get("game") or ()), row.get("side"), row.get("line"))


# ---------------------------------------------------------------------------
# Sections (each returns plain data so tests can check the numbers)
# ---------------------------------------------------------------------------
SIZE_BUCKETS = ((1000, "$1,000+"), (250, "$250-1,000"), (50, "$50-250"), (0, "< $50"))


def size_bucket(usd: Optional[float]) -> str:
    for floor, label in SIZE_BUCKETS:
        if (usd or 0) >= floor:
            return label
    return "< $50"


def execution_summary(rows: list[dict]) -> dict:
    """
    Per venue and order size: how much of what we asked for actually filled, how often we got nothing, the price
    paid versus the price seen, and speed. Simulated and live orders are reported separately.
    """
    cells = defaultdict(list)
    for r in rows:
        if r["kind"] == "EXECUTION":
            cells[("sim" if r.get("simulated") else "LIVE", r.get("venue"), size_bucket(r.get("requested_usd")))].append(r)
    out = {}
    for key, rs in cells.items():
        req = sum(r.get("requested") or 0 for r in rs)
        got = sum(r.get("filled") or 0 for r in rs)
        slips = [r["slippage_cents"] for r in rs if r.get("slippage_cents") is not None]
        out[key] = {"orders": len(rs), "fill_ratio": got / req if req else None,
                    "missed": sum((r.get("filled") or 0) <= 0 for r in rs) / len(rs),
                    "slippage_cents": _mean(slips), "ack_ms": _median(r.get("ack_ms") for r in rs),
                    "fill_ms": _median(r.get("fill_ms") for r in rs),
                    "requested_usd": sum(r.get("requested_usd") or 0 for r in rs),
                    "filled_usd": sum(r.get("filled_usd") or 0 for r in rs)}
    return out


def survival_summary(rows: list[dict]) -> dict:
    """How long +EV prices stayed available (ms): the share that outlived realistic reaction times."""
    surv = [r["survival_ms"] for r in rows if r["kind"] == "EDGE_SURVIVAL" and r.get("survival_ms") is not None]
    if not surv:
        return {"edges": 0}
    n = len(surv)
    return {"edges": n, "median_ms": _median(surv),
            **{f"over_{t}ms": sum(x > t for x in surv) / n for t in (250, 500, 1000, 5000)}}


def projected_monthly(rows: list[dict]) -> dict:
    """
    Expected profit of what actually FILLED (edge x filled cost for directional trades, locked profit for pairs /
    hedges), per day of data, x 30. Simulated fills already paid latency, depth haircuts and misses.
    This is expected value, not settled P&L: CLV and settlements say whether the edge was real.
    """
    ex = [r for r in rows if r["kind"] == "EXECUTION"]
    if not ex:
        return {"days": 0}
    days = max(1.0, (max(r["ts"] for r in ex) - min(r["ts"] for r in ex)) / 86400)
    by_kind = defaultdict(float)
    for r in ex:
        by_kind["pairs_and_hedges" if r.get("kind") == "ARB_HEDGE" else "directional"] += r.get("expected_profit_usd") or 0
    total = sum(by_kind.values())
    return {"days": round(days, 2), "expected_profit_usd": round(total, 2), "per_day": round(total / days, 2),
            "per_month": round(total / days * 30, 2), "by_kind": dict(by_kind),
            "filled_usd_per_day": round(sum(r.get("filled_usd") or 0 for r in ex) / days, 2)}


def combo_summary(rows: list[dict]) -> dict:
    """
    Combo (parlay) quoting: how many RFQs we could price, how often our price would have beaten the price the
    combo actually traded at, the margin we would have had, and what the would-have-won quotes actually paid.
    """
    rfqs = [r for r in rows if r["kind"] == "COMBO_RFQ"]
    if not rfqs:
        return {"rfqs": 0}
    quotable = [r for r in rfqs if r.get("action") == "QUOTE"]
    trades = [r for r in rows if r["kind"] == "COMBO_TRADE" and r.get("traded_yes_price") is not None]
    wins = [r for r in trades if r.get("would_win")]
    results = [r for r in rows if r["kind"] == "COMBO_RESULT"]
    reasons = Counter(str(r.get("reason", "")).split(" (")[0].split(" for leg")[0] for r in rfqs
                      if r.get("action") == "SKIP")
    return {
        "rfqs": len(rfqs), "quotable": len(quotable),
        "traded_after_pricing": len(trades), "would_win": len(wins),
        "win_rate": len(wins) / len(trades) if trades else None,
        "median_margin_when_winning": _median(r.get("margin_vs_winner") for r in wins),
        "expected_profit_of_wins": sum((q.get("expected_profit") or 0) for q in quotable
                                       if q.get("rfq_id") in {w.get("rfq_id") for w in wins}),
        "settled": len(results), "settled_pnl": sum(r.get("pnl") or 0 for r in results),
        "settled_expected": sum(r.get("expected_profit") or 0 for r in results),
        "declines": sum(r["kind"] == "COMBO_DECLINE" for r in rows),
        "confirms": sum(r["kind"] == "COMBO_CONFIRM" for r in rows),
        "skip_reasons": dict(reasons.most_common(6)),
    }


def decisions_summary(rows: list[dict]) -> dict:
    dec = [r for r in rows if r["kind"] == "DECISION"]
    edges = [r["edge"] for r in dec if r.get("edge") is not None]
    return {
        "evaluations": len(dec),
        "bets": sum(r.get("action") == "BET" for r in dec),
        "edge_over_2_5pct": sum(e > 0.025 for e in edges),
        "median_edge": _median(edges),
        "by_venue": dict(Counter(r.get("venue") for r in dec)),
        "moved_last": dict(Counter(r.get("moved_last") or "unknown" for r in dec if r.get("edge", 0) > 0.025)),
        "median_sharp_age_s": _median(r.get("sharp_age_s") for r in dec),
    }


def _closes(rows: list[dict]) -> dict[tuple, float]:
    """Latest pregame closing fair value per (game, side, line): the scheduled-start snapshot beats the cutoff one."""
    closes: dict[tuple, float] = {}
    for r in rows:                                    # rows are time-ordered: later snapshots overwrite
        if r["kind"] == "CLOSE" and r.get("close_fair_prob") is not None:
            closes[_key(r)] = r["close_fair_prob"]
    return closes


def clv_by_time(rows: list[dict]) -> dict:
    """CLV (cents per contract) of every BET decision, by time to start; `blocked` = stopped by the cutoff."""
    closes = _closes(rows)
    cells = defaultdict(list)
    for r in rows:
        if r["kind"] != "DECISION" or r.get("action") != "BET" or r.get("price") is None:
            continue
        fair = closes.get(_key(r))
        if fair is not None:
            cells[(bucket(r.get("minutes_to_start")), bool(r.get("blocked")))].append(100 * (fair - r["price"]))
    return {k: {"decisions": len(v), "mean_clv_cents": _mean(v), "beat_close": sum(x > 0 for x in v) / len(v)}
            for k, v in cells.items()}


def move_bucket(age: Optional[float]) -> str:
    if age is None:
        return "never moved"
    return "< 5s" if age < 5 else "5-30s" if age < 30 else "30s-5m" if age < 300 else "> 5m"


def clv_by_mover(rows: list[dict]) -> dict:
    """
    CLV of BET decisions split by who moved last and how long ago the sharp moved. If "sharp, < 5s"
    clearly beats "venue", turn on TAKER_REQUIRE_SHARP_MOVED_LAST / TAKER_MAX_SHARP_MOVE_AGE_SECONDS.
    """
    closes = _closes(rows)
    cells = defaultdict(list)
    for r in rows:
        if r["kind"] != "DECISION" or r.get("action") != "BET" or r.get("price") is None:
            continue
        fair = closes.get(_key(r))
        if fair is None:
            continue
        mover = "sharp" if r.get("moved_last") == "sharp" else "venue" if r.get("moved_last") else "unknown"
        cells[(mover, move_bucket(r.get("sharp_move_age_s")))].append(100 * (fair - r["price"]))
    return {k: {"decisions": len(v), "mean_clv_cents": _mean(v), "beat_close": sum(x > 0 for x in v) / len(v)}
            for k, v in cells.items()}


def clv_by_method(rows: list[dict]) -> dict:
    """Mean edge at decision time under each de-vig method vs CLV: which method's edges hold up at the close."""
    closes = _closes(rows)
    cells = defaultdict(list)
    for r in rows:
        if r["kind"] != "DECISION" or r.get("price") is None or not r.get("fair_by_method"):
            continue
        fair_close = closes.get(_key(r))
        if fair_close is None:
            continue
        for method, fair in r["fair_by_method"].items():
            if fair and fair / r["price"] - 1 > 0.025:              # would have been a bet under this method
                cells[method].append(100 * (fair_close - r["price"]))
    return {m: {"would_bet": len(v), "mean_clv_cents": _mean(v)} for m, v in cells.items()}


def clv_summary(rows: list[dict]) -> dict:
    """
    Closing-line value per entry = closing fair probability - entry price (per contract, in $).
    The closing fair value is the sharp's de-vigged probability at our pregame cutoff.
    """
    closes = _closes(rows)
    per_entry = []
    for r in rows:
        if r["kind"] != "ENTRY":
            continue
        fair = closes.get(_key(r))
        if fair is None:
            continue
        clv = fair - r["price"]
        per_entry.append({"order_id": r.get("order_id"), "kind": r.get("order_kind"), "venue": r.get("venue"),
                          "clv_per_contract": clv, "clv_pct": clv / r["price"] if r["price"] else None,
                          "clv_usd": clv * r.get("contracts", 0), "stake_usd": r.get("stake_usd", 0.0)})
    entries = sum(r["kind"] == "ENTRY" for r in rows)
    stake = sum(e["stake_usd"] for e in per_entry)
    return {
        "entries": entries,
        "entries_with_close": len(per_entry),
        "beat_close": sum(e["clv_per_contract"] > 0 for e in per_entry),
        "mean_clv_cents": None if not per_entry else 100 * _mean(e["clv_per_contract"] for e in per_entry),
        "total_clv_usd": sum(e["clv_usd"] for e in per_entry),
        "clv_return_on_stake": None if not stake else sum(e["clv_usd"] for e in per_entry) / stake,
        "per_entry": per_entry,
    }


def markout_summary(rows: list[dict]) -> dict:
    by_delay = defaultdict(list)
    for r in rows:
        if r["kind"] == "MARKOUT" and r.get("markout_per_contract") is not None:
            by_delay[r["delay_s"]].append(r["markout_per_contract"])
    return {delay: {"fills": len(v), "mean_cents": 100 * _mean(v), "adverse_share": sum(x < 0 for x in v) / len(v)}
            for delay, v in sorted(by_delay.items())}


def markout_by_time(rows: list[dict], delay: float = 60.0) -> dict:
    """Mean markout (cents) at `delay` by time to start: where resting quotes get picked off -> MAKER_CUTOFF."""
    cells = defaultdict(list)
    for r in rows:
        if r["kind"] == "MARKOUT" and r.get("delay_s") == delay and r.get("markout_per_contract") is not None:
            cells[bucket(r.get("minutes_to_start"))].append(100 * r["markout_per_contract"])
    return {b: {"fills": len(v), "mean_cents": _mean(v)} for b, v in cells.items()}


def depth_summary(rows: list[dict]) -> dict:
    """Median size (contracts) and dollars at the best ask, per venue, by time to start."""
    cells = defaultdict(list)
    for r in rows:
        if r["kind"] == "DEPTH" and r.get("ask") is not None:
            cells[(r.get("venue"), bucket(r.get("minutes_to_start")))].append((r.get("ask_size") or 0.0) * r["ask"])
    out: dict = defaultdict(dict)
    for (venue, b), dollars in cells.items():
        out[venue][b] = {"samples": len(dollars), "median_usd": _median(dollars)}
    return dict(out)


def gap_summary(rows: list[dict]) -> dict:
    closes = [r for r in rows if r["kind"] == "GAP_CLOSE"]
    opens = [r for r in rows if r["kind"] == "GAP_OPEN"]
    by_pair = defaultdict(list)
    for r in closes:
        by_pair["+".join(sorted((r["venue_a"], r["venue_b"])))].append(r)
    days = {datetime.fromtimestamp(r["ts"], timezone.utc).date() for r in rows} or {None}
    pairs = {}
    for pair, rs in sorted(by_pair.items()):
        pairs[pair] = {
            "gaps": len(rs),
            "per_day": len(rs) / len(days),
            "median_duration_s": _median(r["duration_s"] for r in rs),
            "lasted_over_5s": sum(r["duration_s"] > 5 for r in rs),
            "median_profit_cents": 100 * _median(r["best_locked_profit"] for r in rs),
            "median_depth": _median(r["max_depth"] for r in rs),
            # theoretical ceiling: best profit x deepest size, every gap captured in full (reality is far less)
            "ceiling_usd": sum(r["best_locked_profit"] * r["max_depth"] for r in rs),
        }
    return {"opened": len(opens), "closed": len(closes), "still_open": len(opens) - len(closes),
            "days": len(days), "by_pair": pairs,
            "by_time": dict(Counter(bucket(r.get("minutes_to_start")) for r in opens))}


def near_miss_summary(rows: list[dict]) -> dict:
    combos = [r["locked_profit"] for r in rows if r["kind"] == "BEST_COMBO"]
    if not combos:
        return {"samples": 0}
    n = len(combos)
    return {"samples": n, "locked": sum(c > 0 for c in combos) / n,
            "within_1c": sum(c > -0.01 for c in combos) / n, "within_2c": sum(c > -0.02 for c in combos) / n,
            "median_cents": 100 * _median(combos)}


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------
def _fmt(v, spec=".2f", none="-") -> str:
    return none if v is None else format(v, spec)


def build_report(rows: list[dict]) -> str:
    out = ["=" * 78, "RESEARCH REPORT", "=" * 78]
    if not rows:
        out.append("No research rows yet. Run the engine (paper mode is fine) and come back later.")
        return "\n".join(out)
    first, last = rows[0]["ts"], rows[-1]["ts"]
    out.append(f"{len(rows):,} rows  {datetime.fromtimestamp(first, timezone.utc):%Y-%m-%d %H:%M} -> "
               f"{datetime.fromtimestamp(last, timezone.utc):%Y-%m-%d %H:%M} UTC")

    d = decisions_summary(rows)
    out += ["", "[DECISIONS]  (every time a venue price was compared with the sharp line)",
            f"  evaluations {d['evaluations']:,}   bets {d['bets']:,}   edge > 2.5%: {d['edge_over_2_5pct']:,}"
            f"   median edge {_fmt(d['median_edge'] and d['median_edge'] * 100)}%",
            f"  by venue: {d['by_venue']}   median sharp-data age {_fmt(d['median_sharp_age_s'])}s",
            f"  who moved last on edges > 2.5%: {d['moved_last']}",
            "  (if the VENUE moved last, the edge is often the venue lagging correctly; if the SHARP moved last,",
            "   the venue is stale - that is the edge worth taking)"]

    c = clv_summary(rows)
    out += ["", "[CLOSING LINE VALUE]  (entry price vs the sharp's fair value at the pregame cutoff)",
            f"  entries {c['entries']:,}, with a closing line {c['entries_with_close']:,}, "
            f"beat the close {c['beat_close']:,}",
            f"  mean CLV {_fmt(c['mean_clv_cents'])}c per contract   total ${_fmt(c['total_clv_usd'], ',.2f')}   "
            f"return on stake {_fmt(c['clv_return_on_stake'] and c['clv_return_on_stake'] * 100)}%",
            "  (positive over a few hundred entries = genuine edge; ~0 or negative = the edge is not real)"]

    t = clv_by_time(rows)
    out += ["", "[CLV BY TIME TO START]  (every BET decision; 'blocked' = the cutoff stopped it, paper only)"]
    if t:
        for b in BUCKET_ORDER:
            for blocked in (False, True):
                v = t.get((b, blocked))
                if v:
                    out.append(f"  {b:>7} {'blocked' if blocked else 'traded ':<8} decisions {v['decisions']:>5}   "
                               f"mean CLV {v['mean_clv_cents']:+.2f}c   beat close {v['beat_close']:.0%}")
        out.append("  (tighten the taker cutoff only if the late buckets still show positive CLV)")
    else:
        out.append("  no BET decisions with a closing line yet")

    mv = clv_by_mover(rows)
    out += ["", "[WHO MOVED FIRST]  (CLV of BET decisions by who changed price last / age of the sharp move)"]
    if mv:
        for (mover, age), v in sorted(mv.items()):
            out.append(f"  {mover:<7} sharp moved {age:<12} decisions {v['decisions']:>5}   "
                       f"mean CLV {v['mean_clv_cents']:+.2f}c   beat close {v['beat_close']:.0%}")
        out.append("  (if 'sharp' clearly beats 'venue', set TAKER_REQUIRE_SHARP_MOVED_LAST=1 and a freshness window)")
    else:
        out.append("  no BET decisions with a closing line yet")
    bm = clv_by_method(rows)
    if bm:
        out.append("  by de-vig method (edge > 2.5% under that method): " + "   ".join(
            f"{m} {v['would_bet']} bets {v['mean_clv_cents']:+.2f}c" for m, v in sorted(bm.items())))

    m = markout_summary(rows)
    out += ["", "[MAKER MARKOUTS]  (fair value after each maker fill minus our fill price)"]
    out += [f"  +{delay:>5.0f}s  fills {v['fills']:>5}   mean {v['mean_cents']:+.2f}c   "
            f"moved against us {v['adverse_share']:.0%}" for delay, v in m.items()] or ["  no maker fills yet"]
    mt = markout_by_time(rows)
    if mt:
        out.append("  +60s by time to start: " + "   ".join(
            f"{b} {mt[b]['mean_cents']:+.2f}c ({mt[b]['fills']})" for b in BUCKET_ORDER if b in mt))
        out.append("  (strongly negative late buckets = quotes picked off by late news: raise MAKER_CUTOFF_MINUTES)")

    dep = depth_summary(rows)
    out += ["", "[LIQUIDITY]  median $ at the best ask, by time before the game starts"]
    header = "  " + f"{'venue':<10}" + "".join(f"{b:>10}" for b in BUCKET_ORDER)
    out.append(header)
    for venue, cells in sorted(dep.items()):
        out.append("  " + f"{venue:<10}" + "".join(
            f"{_fmt(cells[b]['median_usd'], ',.0f') if b in cells else '-':>10}" for b in BUCKET_ORDER))

    ex = execution_summary(rows)
    out += ["", "[EXECUTION]  (what we asked for vs what we got; 'sim' = simulated delay + depth haircut)"]
    if ex:
        out.append(f"  {'mode':<5} {'venue':<9} {'size':<11} {'orders':>6} {'filled':>7} {'missed':>7} "
                   f"{'slip':>7} {'ack':>7} {'fill':>7}")
        for (mode, venue, size), v in sorted(ex.items(), key=lambda kv: (kv[0][0], kv[0][1],
                                                                           [b for _, b in SIZE_BUCKETS].index(kv[0][2]))):
            out.append(f"  {mode:<5} {venue:<9} {size:<11} {v['orders']:>6} "
                       f"{_fmt(v['fill_ratio'] and v['fill_ratio'] * 100, '.0f'):>6}% {v['missed'] * 100:>6.0f}% "
                       f"{_fmt(v['slippage_cents'], '+.2f'):>6}c {_fmt(v['ack_ms'], '.0f'):>5}ms "
                       f"{_fmt(v['fill_ms'], '.0f'):>5}ms")
        out.append("  (if 'filled' collapses as size grows, the displayed depth is not really there at size)")
    else:
        out.append("  no orders yet (paper needs PAPER_EXECUTION=simulated, the default)")

    sv = survival_summary(rows)
    out += ["", "[EDGE SURVIVAL]  (how long a +EV price stayed available after we first saw it)"]
    if sv["edges"]:
        out.append(f"  edges {sv['edges']:,}   median {sv['median_ms']:.0f}ms   lasted >250ms {sv['over_250ms']:.0%}   "
                   f">500ms {sv['over_500ms']:.0%}   >1s {sv['over_1000ms']:.0%}   >5s {sv['over_5000ms']:.0%}")
        out.append("  (compare with your real order round-trip from the LIVE execution rows: edges that die faster are "
                   "not catchable)")
    else:
        out.append("  no BET decisions yet")

    pm = projected_monthly(rows)
    out += ["", "[PROJECTED MONTHLY]  (expected profit of what actually filled, per day x 30)"]
    if pm["days"]:
        out.append(f"  over {pm['days']} day(s): ${pm['expected_profit_usd']:,.2f} expected   "
                   f"= ${pm['per_day']:,.2f}/day   = ${pm['per_month']:,.0f}/month   "
                   f"(filled ${pm['filled_usd_per_day']:,.0f}/day)")
        out.append("  by source: " + ", ".join(f"{k} ${v:,.2f}" for k, v in pm["by_kind"].items()))
        out.append("  (expected value, before data costs and taxes; CLV above says whether the edges were real)")
    else:
        out.append("  no filled orders yet")

    c = combo_summary(rows)
    out += ["", "[COMBO QUOTING]  (Kalshi parlays via RFQ; shadow = priced but not sent)"]
    if c["rfqs"]:
        out.append(f"  RFQs {c['rfqs']:,}   we would quote {c['quotable']:,}   traded after pricing "
                   f"{c['traded_after_pricing']:,}   ours would have won {c['would_win']:,} "
                   f"({_fmt(c['win_rate'] and c['win_rate'] * 100, '.1f')}%)")
        out.append(f"  winning price vs our fair value: median "
                   f"{_fmt(c['median_margin_when_winning'] and c['median_margin_when_winning'] * 100, '+.1f')}%   "
                   f"expected profit of would-win quotes ${c['expected_profit_of_wins']:,.2f}")
        out.append(f"  settled {c['settled']:,}: P&L ${c['settled_pnl']:,.2f} vs expected ${c['settled_expected']:,.2f}"
                   f"   live last look: {c['confirms']} confirmed, {c['declines']} declined")
        out.append(f"  top skip reasons: {c['skip_reasons']}")
        out.append("  (Gate 1: win rate >= 5% at a median margin >= 8% before any real quoting)")
    else:
        out.append("  no RFQs seen (COMBO_QUOTER=shadow with Kalshi keys)")

    g = gap_summary(rows)
    out += ["", "[LOCKED-PROFIT GAPS]  (buy both sides, profit after fees in every outcome incl. ties)",
            f"  opened {g['opened']:,}   closed {g['closed']:,}   over {g['days']} day(s)   "
            f"by time to start: {g['by_time']}"]
    for pair, p in g["by_pair"].items():
        out.append(f"  {pair:<18} {p['gaps']:>5} gaps ({p['per_day']:.1f}/day)  median {p['median_duration_s']:.1f}s "
                   f"(>5s: {p['lasted_over_5s']})  median {p['median_profit_cents']:.2f}c x "
                   f"{p['median_depth']:,.0f}  ceiling ${p['ceiling_usd']:,.2f}")
    out.append("  (ceiling assumes every gap is captured in full at its best - real capture is a fraction of it)")

    n = near_miss_summary(rows)
    out += ["", "[NEAR MISSES]  (cheapest both-sides combination, sampled every minute)"]
    if n["samples"]:
        out.append(f"  samples {n['samples']:,}   locked {n['locked']:.1%}   within 1c {n['within_1c']:.1%}   "
                   f"within 2c {n['within_2c']:.1%}   median {n['median_cents']:+.2f}c")
    else:
        out.append("  no samples yet")
    out.append("=" * 78)
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarise research/*.jsonl")
    parser.add_argument("--dir", default="research")
    parser.add_argument("--since", help="UTC date YYYY-MM-DD")
    args = parser.parse_args()
    print(build_report(load_rows(Path(args.dir), args.since)))


if __name__ == "__main__":
    main()
