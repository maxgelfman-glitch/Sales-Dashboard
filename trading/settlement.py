"""
settlement.py — Exchange positions: parsing, settlement P&L, and the REST client.

WHY THIS EXISTS
    The engine reserves exposure when it trades, but only the exchange knows when a
    game has settled. Without this module exposure only ever grows (the canary halts
    after ~10 fills) and a restart forgets positions that are still live on Novig.

WHAT THE SUPERVISOR DOES WITH IT (main_supervisor.py)
    * startup sweep   — fetch OPEN positions and restore them as real liabilities
                        before any order can be sent (live mode refuses to trade if
                        this fails)
    * settlement loop — every 15 minutes fetch SETTLED positions; release the exposure
                        of every tracked position that settled and append one SETTLE
                        row to live_ledger.jsonl (idempotent across sweeps and restarts)

NET PROFIT (per settled position), first rule that applies:
    1. exchange reports realised P&L                   -> use it
    2. exchange reports the payout                     -> payout - stake
    3. exchange reports the result                     -> payout = contracts x
           WIN 1.00 | LOSS 0.00 | TIE/DEAD_HEAT 0.50 | PUSH/VOID/CANCELLED refund of stake
    4. none of the above                               -> exposure released, P&L recorded
                                                          as unknown (null) and logged CRITICAL
    stake = what WE paid (our fill cost) when we hold the order; else the exchange's cost.

ASSUMED — Novig's positions API is not in the brief or reachable docs:
    path  NOVIG_POSITIONS_PATH (default /v1/positions), status filter parameter
    NOVIG_POSITIONS_STATUS_PARAM (default "status") with values NOVIG_OPEN_STATUS
    ("OPEN") / NOVIG_SETTLED_STATUS ("SETTLED"); several common field spellings are
    accepted below. Money fields are dollars; prices > 1 are read as cents.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
from pydantic import BaseModel

log = logging.getLogger("trading.settlement")

BASELINE_CAPITAL_USD = 100_000.0
SETTLEMENT_SWEEP_SECONDS = 15 * 60
DEFAULT_POSITIONS_PATH = "/v1/positions"
DEFAULT_STATUS_PARAM = "status"
DEFAULT_OPEN_STATUS = "OPEN"
DEFAULT_SETTLED_STATUS = "SETTLED"
PAGE_WARN_SIZE = 100

WIN_RESULTS = {"WIN", "WON", "WINNER", "YES"}
LOSS_RESULTS = {"LOSS", "LOST", "LOSE", "LOSER", "NO"}
HALF_RESULTS = {"TIE", "DEAD_HEAT", "DEADHEAT", "HALF", "DRAW"}
REFUND_RESULTS = {"PUSH", "VOID", "CANCELLED", "CANCELED", "REFUND", "NO_ACTION"}
SETTLED_STATUSES = {"SETTLED", "CLOSED", "RESOLVED", "FINAL", "COMPLETED", "PAID"}


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _epoch(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    n = _num(v)
    if n is not None:
        return n / 1000.0 if n > 1e11 else n
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


class ExchangePosition(BaseModel):
    """One position as reported by the exchange (normalised)."""
    settlement_id: str                   # stable id used to never process the same settlement twice
    outcome_id: str
    market_id: Optional[str] = None
    event_id: Optional[str] = None
    contracts: float = 0.0
    cost_usd: Optional[float] = None     # what the position cost (exchange's view)
    status: str = ""
    result: Optional[str] = None
    payout_usd: Optional[float] = None
    pnl_usd: Optional[float] = None
    settled_at: Optional[float] = None   # epoch seconds

    @property
    def is_settled(self) -> bool:
        return self.status in SETTLED_STATUSES or self.settled_at is not None or self.result is not None \
            or self.payout_usd is not None


def parse_position(item: dict) -> Optional[ExchangePosition]:
    if not isinstance(item, dict):
        return None
    outcome = _first(item, "outcomeId", "outcome_id", "outcome")
    if isinstance(outcome, dict):
        outcome = _first(outcome, "id", "outcomeId")
    if outcome is None:
        return None
    contracts = _num(_first(item, "contracts", "quantity", "size", "volume", "filled_volume", "shares")) or 0.0
    cost = _num(_first(item, "cost_usd", "cost", "costBasis", "cost_basis", "amount", "stake"))
    if cost is None:
        avg = _num(_first(item, "avg_price", "avgPrice", "average_price", "price", "price_cents", "priceCents"))
        if avg is not None and contracts:
            cost = contracts * (avg / 100.0 if avg > 1 else avg)
    settled_at = _epoch(_first(item, "settled_at", "settledAt", "closed_at", "closedAt", "resolved_at"))
    result = _first(item, "result", "outcome_result", "resolution", "settlement_result")
    status = str(_first(item, "status", "state") or "").upper()
    pid = _first(item, "position_id", "positionId", "id")
    # the settlement id must be stable across sweeps: exchange id if present, else a content hash
    sid = str(pid) if pid is not None else hashlib.sha1(
        json.dumps([str(outcome), status, settled_at, contracts], sort_keys=True).encode()).hexdigest()[:16]
    return ExchangePosition(
        settlement_id=sid, outcome_id=str(outcome),
        market_id=None if _first(item, "marketId", "market_id") is None else str(_first(item, "marketId", "market_id")),
        event_id=None if _first(item, "eventId", "event_id") is None else str(_first(item, "eventId", "event_id")),
        contracts=contracts, cost_usd=None if cost is None else round(cost, 6), status=status,
        result=None if result is None else str(result).upper(),
        payout_usd=_num(_first(item, "payout_usd", "payout", "settlement_amount", "settlementAmount", "proceeds")),
        pnl_usd=_num(_first(item, "pnl_usd", "pnl", "realized_pnl", "realizedPnl", "net_profit", "netProfit")),
        settled_at=settled_at)


def parse_positions(payload: Any) -> list[ExchangePosition]:
    items = payload if isinstance(payload, list) else (
        _first(payload, "positions", "data", "results", "orders", "items") if isinstance(payload, dict) else None)
    out = []
    for item in items if isinstance(items, list) else []:
        pos = parse_position(item)
        if pos is not None:
            out.append(pos)
        else:
            log.debug("SETTLEMENT unparseable position skipped: %s", item)
    return out


def settlement_pnl(pos: ExchangePosition, stake_usd: float) -> tuple[Optional[float], Optional[float], str]:
    """(net_profit_usd, payout_usd, method). net/payout are None when they cannot be determined."""
    if pos.pnl_usd is not None:
        payout = pos.payout_usd if pos.payout_usd is not None else stake_usd + pos.pnl_usd
        return round(pos.pnl_usd, 2), round(payout, 2), "exchange_pnl"
    if pos.payout_usd is not None:
        return round(pos.payout_usd - stake_usd, 2), round(pos.payout_usd, 2), "exchange_payout"
    r = (pos.result or "").upper()
    if r in WIN_RESULTS:
        payout = pos.contracts * 1.0
    elif r in LOSS_RESULTS:
        payout = 0.0
    elif r in HALF_RESULTS:
        payout = pos.contracts * 0.5
    elif r in REFUND_RESULTS:
        payout = stake_usd
    else:
        return None, None, "unknown"
    return round(payout - stake_usd, 2), round(payout, 2), f"result_{r.lower()}"


def load_ledger_settlements(path: Optional[Path]) -> tuple[set[str], float]:
    """(already-processed settlement ids, cumulative net profit) from an existing ledger."""
    ids: set[str] = set()
    total = 0.0
    if path is None or not Path(path).exists():
        return ids, total
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") != "SETTLE":
                continue
            if row.get("settlement_id"):
                ids.add(str(row["settlement_id"]))
            total += float(row.get("net_profit_usd") or 0.0)
    return ids, round(total, 2)


class PositionsClient:
    """GET {api_base}{path}?{status_param}={status} with the bearer token. One persistent session."""

    def __init__(self, api_base: str, token: str, path: str = DEFAULT_POSITIONS_PATH,
                 status_param: str = DEFAULT_STATUS_PARAM, open_status: str = DEFAULT_OPEN_STATUS,
                 settled_status: str = DEFAULT_SETTLED_STATUS, timeout: float = 10.0) -> None:
        self.url = api_base.rstrip("/") + "/" + path.lstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.status_param, self.open_status, self.settled_status = status_param, open_status, settled_status
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get(self, status: str) -> list[ExchangePosition]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout, headers=self.headers)
        started = time.monotonic()
        async with self._session.get(self.url, params={self.status_param: status}) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        rows = parse_positions(payload)
        raw = payload if isinstance(payload, list) else (payload or {}).get("positions") or (payload or {}).get("data")
        if isinstance(raw, list) and len(raw) >= PAGE_WARN_SIZE:
            log.warning("SETTLEMENT %s positions response has %d rows: pagination is not confirmed in Novig's "
                        "docs — some positions may be missing", status, len(raw))
        log.info("SETTLEMENT fetched %d %s position(s) in %.0fms", len(rows), status,
                 (time.monotonic() - started) * 1000)
        return rows

    async def open_positions(self) -> list[ExchangePosition]:
        return await self._get(self.open_status)

    async def settled_positions(self) -> list[ExchangePosition]:
        return await self._get(self.settled_status)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
