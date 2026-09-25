"""
sim_exchange.py — Realistic paper execution: a simulated exchange behind the SAME order path as live trading.

Why: instant paper fills at the displayed price and full displayed size overstate results badly. The edges
worth having (stale prices) are exactly the ones faster traders race for, and displayed depth shrinks the
moment someone trades into it. This gateway makes paper trading pay those costs:

    1. LATENCY    the book is looked at only `latency_ms` after the order is sent (default 500ms: decision +
                  network + matching for a retail setup). Whatever moved or was taken in between is gone.
    2. HAIRCUT    of the depth still there at our price or better, only `depth_haircut` (default 50%) is ours.
                  Other takers hit the same levels, and makers pull quotes as soon as they are hit.
    3. CONSUMED   depth we already took in simulation stays taken until the venue's book next updates, so two
                  of our orders never both fill against the same offers.
    4. PRICE      Kalshi fills at each resting level's price (standard matching); Novig and ProphetX are
                  assumed to fill at the order's limit (conservative; the trader believes Novig works that way).
    5. IOC        whatever cannot be filled at that moment is cancelled; nothing rests.

Everything downstream (reservations, tranches, fees, positions, the execution report) is the live code.
What this cannot know: whether a real competitor's order would reach the matching engine first in the
same millisecond, and how makers react to OUR size. Only live orders, scaled up step by step, show that.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from novig_private import FillSlip

log = logging.getLogger("trading.sim")

DEFAULT_LATENCY_MS = 500.0
DEFAULT_DEPTH_HAIRCUT = 0.5
LEVEL_PRICED_VENUES = {"kalshi"}          # fills at each resting level's own price; others fill at the limit


class SimulatedGateway:
    """Order-gateway interface (place_limit / cancel_orders / get_order / close) for every venue at once."""

    live = False
    api_base = "simulated://paper"
    time_in_force = "immediate_or_cancel"

    def __init__(self, venue: str, get_update: Callable[[str, str], Any],
                 on_fill: Callable[[FillSlip], Awaitable[None]], latency_ms: float = DEFAULT_LATENCY_MS,
                 depth_haircut: float = DEFAULT_DEPTH_HAIRCUT, clock=time.time) -> None:
        self.venue = venue
        self.get_update = get_update
        self.on_fill = on_fill
        self.latency_s = max(0.0, latency_ms / 1000.0)
        self.haircut = min(max(depth_haircut, 0.0), 1.0)
        self.clock = clock
        self._ids = itertools.count(1)
        self.orders: dict[str, dict] = {}
        self._consumed: dict[tuple[str, float], tuple[float, float]] = {}   # (outcome, price) -> (qty, book ts)
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def order_body(ticker: str, price_cents: float, contracts: int, client_id: str, time_in_force=None) -> dict:
        return {"outcome": ticker, "price_cents": price_cents, "contracts": contracts, "client_id": client_id,
                "simulated": True}

    async def place_limit(self, outcome_id: str, side: str, price_cents: float, contracts: int,
                          client_id: str) -> str:
        oid = f"sim-{self.venue}-{next(self._ids)}"
        self.orders[oid] = dict(outcome_id=outcome_id, limit=price_cents / 100.0, contracts=int(contracts),
                                filled=0.0, sent_at=self.clock())
        task = asyncio.get_running_loop().create_task(self._execute(oid))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return oid

    def _available(self, outcome_id: str, limit: float) -> list[tuple[float, float]]:
        """[(price, contracts we could take)] at or better than `limit`, after consumption and the haircut."""
        upd = self.get_update(self.venue, outcome_id)
        if upd is None or upd.price is None:
            return []
        levels = [(p, q) for p, q in (upd.ask_levels or [(upd.price, upd.available_volume)]) if q > 0]
        book_ts = getattr(upd, "received_at", 0.0)
        out = []
        for p, q in sorted(levels):
            if p > limit + 1e-9:
                break
            used, ts = self._consumed.get((outcome_id, round(p, 6)), (0.0, book_ts))
            if ts < book_ts:
                used = 0.0                         # the book changed since: the level is re-quoted
            take = max(0.0, q - used) * self.haircut
            if take >= 1:
                out.append((p, take))
        return out

    async def _execute(self, oid: str) -> None:
        order = self.orders[oid]
        await asyncio.sleep(self.latency_s)
        want = order["contracts"]
        got, cost = 0, 0.0
        for p, take in self._available(order["outcome_id"], order["limit"]):
            n = int(min(want - got, take))
            if n <= 0:
                break
            got += n
            cost += n * (p if self.venue in LEVEL_PRICED_VENUES else order["limit"])
            upd = self.get_update(self.venue, order["outcome_id"])
            key = (order["outcome_id"], round(p, 6))
            used, _ = self._consumed.get(key, (0.0, 0.0))
            self._consumed[key] = (used + n / max(self.haircut, 1e-9), getattr(upd, "received_at", 0.0))
            if got >= want:
                break
        order["filled"] = got
        order["filled_at"] = self.clock()
        avg_cents = (cost / got * 100) if got else None
        log.info("SIM %s %s: wanted %d, filled %d%s", self.venue, oid, want, got,
                 "" if not got else f" avg {avg_cents:.2f}c")
        await self.on_fill(FillSlip(order_id=oid, status="FILLED" if got >= want else "CANCELLED",
                                    filled_volume=got, price_cents=avg_cents, venue=self.venue))

    async def cancel_orders(self, ids) -> None:
        return None                                # IOC: nothing ever rests

    async def get_order(self, oid: str) -> Optional[dict]:
        o = self.orders.get(oid)
        return None if o is None or "filled_at" not in o else {"status": "canceled", "fill_count": o["filled"]}

    @staticmethod
    def filled_count(order: dict) -> Optional[float]:
        return None if not order else order.get("fill_count")

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
