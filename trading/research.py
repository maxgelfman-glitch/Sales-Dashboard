"""
research.py — Measurement layer: does this strategy have an edge, and how much can it trade?

Writes append-only JSON lines to research/research-YYYYMMDD.jsonl (UTC day). This is kept
separate from live_ledger.jsonl on purpose: the ledger is the record you reconcile against
the exchange; research data is high-volume and exists only to be analysed
(python research_report.py).

ROW KINDS
    DECISION   every edge evaluation: venue, game, side, price, depth, fair probability, edge,
               sharp-data age, which side moved last (Novig vs sharp), minutes to start, BET/PASS
    ENTRY      every position we actually took (paper or live): price, contracts, kind
    CLOSE      at each game's pregame cutoff: the sharp fair probability and venue prices for
               every side -> closing-line value (CLV) = close_fair / entry_price - 1
    MARKOUT    for maker fills: fair value 10s / 60s / 300s after the fill vs our fill price
    DEPTH      once a minute: top of book (price and size) for every outcome, with minutes to start
               -> the pregame liquidity curve
    GAP_OPEN / GAP_CLOSE
               a cross-venue (or same-venue) pair of asks that locks a profit after fees in every
               settlement scenario; CLOSE carries how long it lasted, its best profit and depth
    BEST_COMBO once a minute per game+market: the cheapest both-sides cost across venues
               -> how often prices are NEAR a locked profit (the room a hedged maker would have)

FEES USED FOR GAPS (per contract, pregame straight trades; see brief/help centres)
    novig      0
    kalshi     taker 0.07 x P x (1-P)   (per-order rounding up ignored in research)
    prophetx   2% of net profit, charged only if that leg wins
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("trading.research")

DEPTH_SAMPLE_SECONDS = 60.0
MARKOUT_DELAYS = (10.0, 60.0, 300.0)


# ---------------------------------------------------------------------------
# Fees and locked-profit maths for a pair of opposite outcomes
# ---------------------------------------------------------------------------
def upfront_fee(venue: str, price: float) -> float:
    """Fee paid when buying one contract at `price` as a taker (pregame straight)."""
    if venue == "kalshi":
        return 0.07 * price * (1 - price)
    return 0.0


def win_fee(venue: str, price: float) -> float:
    """Fee charged only if the contract wins (ProphetX: 2% of net profit)."""
    if venue == "prophetx":
        return 0.02 * (1 - price)
    return 0.0


def locked_profit(a_venue: str, a_price: float, b_venue: str, b_price: float,
                  tie_payout: Optional[float] = None) -> float:
    """
    Worst-case profit per contract of buying side A on one venue and side B on another.
    tie_payout: per-contract payout of EACH leg if the game ties (0.5 for dead-heat venues,
    None when a tie is impossible, e.g. NBA or half-point lines).
    """
    cost = a_price + upfront_fee(a_venue, a_price) + b_price + upfront_fee(b_venue, b_price)
    scenarios = [1.0 - win_fee(a_venue, a_price) - cost, 1.0 - win_fee(b_venue, b_price) - cost]
    if tie_payout is not None:
        scenarios.append(2 * tie_payout - cost)
    return min(scenarios)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------
class ResearchRecorder:
    """Append-only JSON lines, one file per UTC day. Never raises into the trading path."""

    def __init__(self, directory: str | Path, clock=time.time) -> None:
        self.dir = Path(directory)
        self.clock = clock
        self.rows_written = 0

    def path_for(self, ts: float) -> Path:
        return self.dir / f"research-{datetime.fromtimestamp(ts, timezone.utc):%Y%m%d}.jsonl"

    def write(self, kind: str, /, **fields: Any) -> None:
        ts = self.clock()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.path_for(ts).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": round(ts, 3), **fields, "kind": kind}, default=str) + "\n")
            self.rows_written += 1
        except OSError as exc:
            log.error("RESEARCH write failed: %s", exc)


# ---------------------------------------------------------------------------
# Cross-venue gap tracking
# ---------------------------------------------------------------------------
class GapTracker:
    """
    Keeps the latest ask per (game-market, venue, side); on every update re-checks every pair of
    opposite sides across venues and records when a locked-profit gap opens and closes.
    """

    def __init__(self, recorder: ResearchRecorder, clock=time.time) -> None:
        self.rec = recorder
        self.clock = clock
        self.books: dict[tuple, dict[tuple[str, str], dict]] = {}      # market_key -> {(venue, side): quote}
        self.open_gaps: dict[tuple, dict] = {}                          # gap_key -> state

    def update(self, market_key: tuple, venue: str, side: str, price: Optional[float], volume: float,
               line: Optional[float], tie_payout: Optional[float], sides: tuple[str, str],
               minutes_to_start: Optional[float]) -> list[dict]:
        book = self.books.setdefault(market_key, {})
        if price is None or volume <= 0:
            book.pop((venue, side), None)
        else:
            book[(venue, side)] = {"price": price, "volume": volume, "line": line}
        return self._check(market_key, tie_payout, sides, minutes_to_start)

    def drop_venue(self, venue: str) -> None:
        """A venue disconnected: forget its prices and close every gap that relied on them."""
        for book in self.books.values():
            for k in [k for k in book if k[0] == venue]:
                del book[k]
        for gkey in [k for k in self.open_gaps if venue in (k[1], k[2])]:
            self._close_gap(gkey, None)

    def best_combo(self, market_key: tuple, tie_payout: Optional[float], sides: tuple[str, str]) -> Optional[dict]:
        """Cheapest both-sides combination across venues (even when it is NOT a locked profit)."""
        best = None
        for combo in self._pairs(market_key, sides):
            profit = locked_profit(combo["venue_a"], combo["price_a"], combo["venue_b"], combo["price_b"], tie_payout)
            if best is None or profit > best["locked_profit"]:
                best = {**combo, "locked_profit": round(profit, 5)}
        return best

    def _pairs(self, market_key: tuple, sides: tuple[str, str]):
        book = self.books.get(market_key, {})
        a_side, b_side = sides
        for (va, sa), qa in book.items():
            if sa != a_side:
                continue
            for (vb, sb), qb in book.items():
                if sb != b_side:
                    continue
                if qa["line"] is not None and qb["line"] is not None and abs(qa["line"] + qb["line"]) > 1e-9 \
                        and market_key[3] == "spread":
                    continue                                        # spreads must mirror (-x vs +x)
                if market_key[3] == "total" and qa["line"] != qb["line"]:
                    continue
                yield {"venue_a": va, "side_a": sa, "price_a": qa["price"], "venue_b": vb, "side_b": sb,
                       "price_b": qb["price"], "depth": min(qa["volume"], qb["volume"]), "line": qa["line"]}

    def _check(self, market_key: tuple, tie_payout: Optional[float], sides: tuple[str, str],
               minutes_to_start: Optional[float]) -> list[dict]:
        now = self.clock()
        live_keys = set()
        opened = []
        for combo in self._pairs(market_key, sides):
            profit = locked_profit(combo["venue_a"], combo["price_a"], combo["venue_b"], combo["price_b"], tie_payout)
            gkey = (market_key, combo["venue_a"], combo["venue_b"], combo["line"])
            if profit <= 0:
                continue
            live_keys.add(gkey)
            state = self.open_gaps.get(gkey)
            if state is None:
                state = {"opened": now, "best": profit, "depth": combo["depth"]}
                self.open_gaps[gkey] = state
                row = {**combo, "game": list(market_key), "locked_profit": round(profit, 5),
                       "minutes_to_start": minutes_to_start}
                self.rec.write("GAP_OPEN", **row)
                opened.append(row)
            else:
                state["best"] = max(state["best"], profit)
                state["depth"] = max(state["depth"], combo["depth"])
        for gkey in [k for k in self.open_gaps if k[0] == market_key and k not in live_keys]:
            self._close_gap(gkey, minutes_to_start)
        return opened

    def _close_gap(self, gkey: tuple, minutes_to_start: Optional[float]) -> None:
        state = self.open_gaps.pop(gkey)
        self.rec.write("GAP_CLOSE", game=list(gkey[0]), venue_a=gkey[1], venue_b=gkey[2], line=gkey[3],
                       duration_s=round(self.clock() - state["opened"], 3), best_locked_profit=round(state["best"], 5),
                       max_depth=state["depth"], minutes_to_start=minutes_to_start)
