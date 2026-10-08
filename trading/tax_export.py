"""
tax_export.py — One CSV row per settled wager, from the engine's ledger, for your accountant.

    python tax_export.py                          # ledger.jsonl -> tax_2026.csv (current year)
    python tax_export.py --ledger path/to/ledger.jsonl --year 2026 --out tax_2026.csv

Why per wager: if these contracts are taxed as gambling (the IRS has not ruled on event contracts), winnings
are reported gross and losses are only deductible as an itemised deduction (90% of them from 2026), so wins
and losses must be listed separately, never netted. If they are treated as something else (e.g. Section 1256
or ordinary income) the same rows still give your accountant everything needed. Not tax advice.

Columns: date (New York), venue, kind (STRAIGHT / PARLAY), id, description, stake, payout, net, result,
win_or_loss. The summary at the end (stderr) gives gross wins, gross losses and the net.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
COLUMNS = ["date", "venue", "kind", "id", "description", "stake_usd", "payout_usd", "net_usd", "result",
           "win_or_loss"]


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _date(row: dict) -> str:
    ts = row.get("ts")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, timezone.utc).astimezone(NY).date().isoformat()
    text = str(row.get("timestamp") or "")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(NY).date().isoformat()
    except ValueError:
        return text[:10]


def wager_rows(lines: Iterable[str], year: Optional[int] = None) -> list[dict]:
    """Every SETTLE row as one wager. Rows with unknown P&L are kept and flagged (they must be filled in by hand)."""
    out, seen = [], set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") != "SETTLE":
            continue
        kind = "PARLAY" if row.get("kind") == "PARLAY" else "STRAIGHT"
        ident = str(row.get("position") or row.get("settlement_id") or row.get("outcome_id") or "")
        if (kind, ident) in seen:                   # a replayed settlement is still one wager
            continue
        seen.add((kind, ident))
        date = _date(row)
        if year is not None and not date.startswith(str(year)):
            continue
        net = _num(row.get("net_profit_usd"))
        stake, payout = _num(row.get("stake_usd")), _num(row.get("payout_usd"))
        if payout is None and stake is not None and net is not None:
            payout = stake + net
        desc = row.get("market_ticker") or " ".join(str(row.get(k)) for k in ("game_id", "market_type", "side")
                                                     if row.get(k))
        out.append(dict(date=date, venue=row.get("venue") or "unknown",
                        kind=kind, id=ident, description=desc,
                        stake_usd="" if stake is None else round(stake, 2),
                        payout_usd="" if payout is None else round(payout, 2),
                        net_usd="" if net is None else round(net, 2), result=row.get("result") or "",
                        win_or_loss="UNKNOWN - check venue statement" if net is None else
                        "WIN" if net > 0 else "LOSS" if net < 0 else "PUSH"))
    return sorted(out, key=lambda r: (r["date"], r["venue"], r["id"]))


def totals(rows: list[dict]) -> dict:
    nets = [r["net_usd"] for r in rows if r["net_usd"] != ""]
    return dict(wagers=len(rows), gross_wins=round(sum(n for n in nets if n > 0), 2),
                gross_losses=round(-sum(n for n in nets if n < 0), 2), net=round(sum(nets), 2),
                unknown=sum(1 for r in rows if r["net_usd"] == ""))


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Per-wager tax CSV from the engine ledger")
    ap.add_argument("--ledger", default="ledger.jsonl")
    ap.add_argument("--year", type=int, default=datetime.now(NY).year)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    path = Path(args.ledger).expanduser()
    if not path.exists():
        sys.exit(f"no ledger at {path} (set --ledger to the LEDGER_PATH the engine writes)")
    rows = wager_rows(path.read_text(encoding="utf-8").splitlines(), args.year)
    out = Path(args.out or f"tax_{args.year}.csv")
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    t = totals(rows)
    print(f"wrote {out}: {t['wagers']} wagers; gross wins ${t['gross_wins']:,.2f}, gross losses "
          f"${t['gross_losses']:,.2f}, net ${t['net']:,.2f}", file=sys.stderr)
    if t["unknown"]:
        print(f"WARNING: {t['unknown']} wager(s) have unknown P&L: fill them in from the venue statement",
              file=sys.stderr)
    print("Keep the venues' own annual statements too; this file is your per-wager record, not tax advice.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
