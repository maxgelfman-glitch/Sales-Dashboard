"""
research_report.py — Turn research/*.jsonl into the numbers that decide whether the strategy works.

    python research_report.py                      # everything in ./research
    python research_report.py --dir logs/research  # another folder
    python research_report.py --since 2026-10-01   # only rows from this UTC date on

WHAT EACH SECTION ANSWERS
    DECISIONS      how often a price beat the sharp line by > 2.5%, and who moved last
    CLOSING LINE   did our entries beat the closing line? (CLV > 0 on average = real edge;
                   this is the fastest honest signal, long before P&L means anything)
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
                (float("-inf"), "< 10m"))
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


def clv_summary(rows: list[dict]) -> dict:
    """
    Closing-line value per entry = closing fair probability - entry price (per contract, in $).
    The closing fair value is the sharp's de-vigged probability at our pregame cutoff.
    """
    closes: dict[tuple, float] = {}
    for r in rows:
        if r["kind"] == "CLOSE" and r.get("close_fair_prob") is not None:
            closes[_key(r)] = r["close_fair_prob"]
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

    m = markout_summary(rows)
    out += ["", "[MAKER MARKOUTS]  (fair value after each maker fill minus our fill price)"]
    out += [f"  +{delay:>5.0f}s  fills {v['fills']:>5}   mean {v['mean_cents']:+.2f}c   "
            f"moved against us {v['adverse_share']:.0%}" for delay, v in m.items()] or ["  no maker fills yet"]

    dep = depth_summary(rows)
    out += ["", "[LIQUIDITY]  median $ at the best ask, by time before the game starts"]
    header = "  " + f"{'venue':<10}" + "".join(f"{b:>10}" for b in BUCKET_ORDER)
    out.append(header)
    for venue, cells in sorted(dep.items()):
        out.append("  " + f"{venue:<10}" + "".join(
            f"{_fmt(cells[b]['median_usd'], ',.0f') if b in cells else '-':>10}" for b in BUCKET_ORDER))

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
