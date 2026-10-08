"""
novig_data.py — Summarise Novig's free public trade data (data.novig.com) on YOUR machine.

The daily files are too big to attach, so this downloads them itself and prints a short summary (a few dozen
lines, no personal data) to paste back. Standard library only: no installs needed.

    python novig_data.py                 # last 7 days
    python novig_data.py --days 14
    python novig_data.py --dir ~/novig   # use files you already downloaded (<dir>/<date>/trades.csv)

What it answers
    PARLAYS     how many, how much retail stakes, typical price (= odds), legs, size; how many quoters split
                each trade; by hour (when the flow comes) -> is Novig's parlay program worth its $30k minimum?
    STRAIGHTS   volume by league and market type, trade sizes, maker vs taker -> is there enough flow for the
                pregame market maker, and where?
The data never shows our edge (no fair values in it). It shows whether the pool is big enough to bother.
Columns follow Novig's docs (2026-09): timestamp,outcomeId,marketId,contractSeries,league,marketType,tradeType,
legs,cost,qty,side. One TAKER row per trade plus one MAKER row per counterparty; qty is in $1 contracts.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Optional

BASE = "https://data.novig.com/reporting/trade-data"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "novig-data-summary/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def available_dates(days: int) -> list[str]:
    index = json.loads(_get(f"{BASE}/index.json"))
    return sorted(index.get("dates") or [])[-days:]


def rows_for(date: str, local: Optional[Path], cache: Optional[Path]) -> Iterable[dict]:
    if local is not None:
        path = local / date / "trades.csv"
        if not path.exists():
            path = local / f"{date}-trades.csv"
        text = path.read_text()
    else:
        cached = cache / date / "trades.csv" if cache else None
        if cached is not None and cached.exists():
            text = cached.read_text()
        else:
            text = _get(f"{BASE}/{date}/trades.csv").decode()
            if cached is not None:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(text)
    yield from csv.DictReader(io.StringIO(text))


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _pct(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def summarise(dates: list[str], rows_by_date) -> dict:
    """Aggregate rows into the summary (pure: testable without the network)."""
    combo = dict(trades=0, stake=0.0, payout=0.0, prices=[], legs=Counter(), stakes=[], makers=Counter(),
                 hours=Counter(), per_day=Counter())
    straight = dict(trades=0, notional=0.0, by_league=Counter(), by_type=Counter(), sizes=[], per_day=Counter())
    for date in dates:
        makers_per_trade: dict[tuple, int] = defaultdict(int)
        for r in rows_by_date(date):
            kind, side = (r.get("tradeType") or "").upper(), (r.get("side") or "").upper()
            cost, qty = _f(r.get("cost")), _f(r.get("qty"))
            if kind == "COMBO":
                key = (r.get("timestamp"), r.get("marketId"))
                if side == "MAKER":
                    makers_per_trade[key] += 1
                    continue
                if side != "TAKER" or qty <= 0:
                    continue
                combo["trades"] += 1
                combo["per_day"][date] += 1
                combo["stake"] += cost
                combo["payout"] += qty
                combo["stakes"].append(cost)
                combo["prices"].append(cost / qty)
                combo["legs"][int(_f(r.get("legs")))] += 1
                combo["hours"][(r.get("timestamp") or "T00")[11:13]] += 1
            elif kind == "STRAIGHT" and side == "TAKER" and qty > 0:
                straight["trades"] += 1
                straight["per_day"][date] += qty
                straight["notional"] += qty
                straight["by_league"][r.get("league") or "?"] += qty
                straight["by_type"][r.get("marketType") or "?"] += qty
                straight["sizes"].append(qty)
        combo["makers"].update(makers_per_trade.values())
    n_days = max(1, len(dates))
    prices = combo["prices"]
    out = {
        "dates": f"{dates[0]}..{dates[-1]}" if dates else "none",
        "days": len(dates),
        "parlays": {
            "trades_per_day": round(combo["trades"] / n_days, 1),
            "retail_stake_per_day_usd": round(combo["stake"] / n_days),
            "payout_if_all_won_per_day_usd": round(combo["payout"] / n_days),
            "quoter_collateral_per_day_usd": round((combo["payout"] - combo["stake"]) / n_days),
            "median_price": _pct(prices, 0.5), "p10_price": _pct(prices, 0.1), "p90_price": _pct(prices, 0.9),
            "median_stake_usd": _pct(combo["stakes"], 0.5), "p90_stake_usd": _pct(combo["stakes"], 0.9),
            "legs": dict(sorted(combo["legs"].items())),
            "quoters_per_trade": dict(sorted(combo["makers"].items())),
            "busiest_hours_utc": [h for h, _ in combo["hours"].most_common(5)],
            "by_day": dict(sorted(combo["per_day"].items())),
            # if retail's average hold matches Kalshi's ~15% of stake, this is the pool quoters share:
            "illustrative_quoter_pool_per_day_at_15pct": round(0.15 * combo["stake"] / n_days),
        },
        "straights": {
            "trades_per_day": round(straight["trades"] / n_days, 1),
            "contracts_per_day": round(straight["notional"] / n_days),
            "top_leagues_share": {k: round(v / max(straight["notional"], 1), 3)
                                  for k, v in straight["by_league"].most_common(8)},
            "market_types_share": {k: round(v / max(straight["notional"], 1), 3)
                                   for k, v in straight["by_type"].most_common(6)},
            "median_trade_contracts": _pct(straight["sizes"], 0.5),
            "p90_trade_contracts": _pct(straight["sizes"], 0.9),
        },
    }
    return out


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Summarise Novig's public trade data (paste the output back)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dir", help="read <dir>/<date>/trades.csv instead of downloading")
    ap.add_argument("--cache", default="novig_data_cache", help="keep downloads here (default novig_data_cache)")
    args = ap.parse_args(argv)
    local = Path(args.dir).expanduser() if args.dir else None
    if local is not None:
        dates = sorted(p.name for p in local.iterdir() if p.is_dir() and (p / "trades.csv").exists())[-args.days:]
    else:
        try:
            dates = available_dates(args.days)
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"could not reach {BASE}/index.json ({exc}); download the files and use --dir")
    if not dates:
        sys.exit("no trade files found")
    print(f"reading {len(dates)} day(s): {dates[0]} .. {dates[-1]}", file=sys.stderr)
    summary = summarise(dates, lambda d: rows_for(d, local, None if local else Path(args.cache)))
    print("=== NOVIG PUBLIC DATA SUMMARY (paste everything below) ===")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
