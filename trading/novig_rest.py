"""
novig_rest.py — Novig REST: startup market bootstrap + order gateway.

1) BOOTSTRAP (fetch_open_markets)
   GET NOVIG_EVENTS_URL -> events -> markets -> exactly two outcomes, flattened
   into MarketInfo rows (one per outcomeId, each knowing its sibling) for the
   in-memory MarketRegistry. The supervisor runs it at startup and refreshes
   it periodically so new games appear without restarts.

2) ORDER GATEWAY
   PaperOrderGateway  — DEFAULT. Records orders in memory, sends nothing.
   NovigOrderGateway  — real HTTP, used only when TRADING_MODE=live:
       POST   {NOVIG_API_BASE}/v1/orders   {..., "order_type": "LIMIT"}
       DELETE {NOVIG_API_BASE}/v1/orders   {"orderIds": [...]}   (bulk cancel)

VERIFIED vs ASSUMED
   Verified (brief / docs search): events contain markets, markets contain two
   outcomes keyed by outcomeId; orders reference outcomeId; endpoint paths
   /v1/orders with order_type="LIMIT".
   ASSUMED — confirm against docs.novig.com (unreachable from the build sandbox):
     * the events endpoint URL (no default: set NOVIG_EVENTS_URL)
     * JSON key spellings below (several common spellings are accepted)
     * a market-level `line`/`strike` on a spread is from the HOME team's view
     * order body keys (ORDER_BODY_KEYS) and the bulk-cancel body key "orderIds"
"""

from __future__ import annotations

import itertools
import logging
import time
from typing import Any, Optional, Protocol
from urllib.parse import parse_qs, urlparse

import aiohttp

from novig_feed import MarketInfo, canonical_market_type
from team_normalizer import normalize_team_name

log = logging.getLogger("trading.novig_rest")

DEFAULT_NOVIG_API_BASE = "https://api.novig.com"
CLOSED_STATUSES = {"CLOSED", "SETTLED", "ENDED", "FINAL", "CANCELLED", "CANCELED", "RESOLVED"}

# The ONE place to change order field names once confirmed against Novig's docs.
ORDER_BODY_KEYS = dict(outcome="outcomeId", side="side", price="price_cents", size="volume",
                       order_type="order_type", client_id="clientOrderId")
BULK_CANCEL_KEY = "orderIds"


# ==========================================================================
# Bootstrap parsing
# ==========================================================================
def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def _name(v: Any) -> Optional[str]:
    """Team/league fields may be plain strings or objects like {"name": ..., "abbreviation": ...}."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return _first(v, "name", "abbreviation", "displayName", "shortName")
    return None


def parse_event_hierarchy(payload: Any) -> list[MarketInfo]:
    """Flatten events -> markets -> outcomes into MarketInfo rows. Never raises; bad nodes are skipped."""
    events = payload if isinstance(payload, list) else (_first(payload, "events", "data", "results") or [])
    rows: list[MarketInfo] = []
    skipped = 0
    for ev in events if isinstance(events, list) else []:
        if not isinstance(ev, dict):
            continue
        status = str(_first(ev, "status", "state") or "").upper()
        if status in CLOSED_STATUSES:
            continue
        event_id = _first(ev, "eventId", "event_id", "id")
        league = _name(_first(ev, "league", "leagueName", "league_name", "competition"))
        home = _name(_first(ev, "homeTeam", "home_team", "home"))
        away = _name(_first(ev, "awayTeam", "away_team", "away"))
        if not all((event_id, league, home, away)):
            skipped += 1
            continue
        for m in _first(ev, "markets") or []:
            if not isinstance(m, dict):
                continue
            market_id = _first(m, "marketId", "market_id", "id")
            mtype = _first(m, "type", "marketType", "market_type", "name")
            outcomes = _first(m, "outcomes") or []
            if market_id is None or mtype is None or len(outcomes) != 2:
                skipped += 1      # the data model guarantees exactly two outcomes per market
                continue
            mtype = canonical_market_type(str(mtype))
            mline = _first(m, "line", "strike", "points")
            ids = [_first(o, "outcomeId", "outcome_id", "id") for o in outcomes]
            if None in ids:
                skipped += 1
                continue
            for o, oid, sib in zip(outcomes, ids, reversed(ids)):
                name = _name(_first(o, "name", "description", "label", "team", "title"))
                line = _first(o, "line", "points", "strike")
                if line is None and mline is not None:
                    if mtype == "spread":
                        is_home = normalize_team_name(name, league) == normalize_team_name(home, league)
                        line = float(mline) if is_home else -float(mline)
                    elif mtype == "total":
                        line = float(mline)
                try:
                    rows.append(MarketInfo(venue="novig", outcome_id=str(oid), market_id=str(market_id),
                                           sibling_outcome_id=str(sib), league=league, market_type=mtype,
                                           event_id=str(event_id), home_team=home, away_team=away,
                                           outcome=name or "", line=None if line is None else float(line)))
                except Exception:  # noqa: BLE001 — one bad node never breaks the bootstrap
                    skipped += 1
    if skipped:
        log.warning("BOOTSTRAP skipped %d malformed event/market node(s)", skipped)
    return [r for r in rows if r.is_tracked()]


class NovigRestClient:
    """Read-only REST access used for the bootstrap. One persistent session."""

    def __init__(self, events_url: str, token: Optional[str] = None, timeout: float = 10.0) -> None:
        self.events_url = events_url
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def fetch_open_markets(self) -> list[MarketInfo]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout, headers=self.headers)
        started = time.monotonic()
        async with self._session.get(self.events_url) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        rows = parse_event_hierarchy(payload)
        events = payload if isinstance(payload, list) else (_first(payload, "events", "data", "results") or [])
        n_events = len(events) if isinstance(events, list) else 0
        limit = parse_qs(urlparse(self.events_url).query).get("limit", [None])[0]
        if limit and limit.isdigit() and n_events >= int(limit):
            log.warning("BOOTSTRAP novig returned %d events = the page limit (%s): some games may be missing "
                        "(pagination not confirmed in Novig's docs)", n_events, limit)
        if n_events and not any(isinstance(e, dict) and _first(e, "markets") for e in events):
            log.warning("BOOTSTRAP novig: %d events but none embed markets/outcomes — the markets may need a "
                        "separate endpoint (e.g. /nbx/v2/emm/markets/open); nothing is tradable yet", n_events)
        log.info("BOOTSTRAP novig loaded %d tracked outcomes from %d events in %.0fms", len(rows), n_events,
                 (time.monotonic() - started) * 1000)
        return rows

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


# ==========================================================================
# Order gateways
# ==========================================================================
class OrderGateway(Protocol):
    live: bool

    async def place_limit(self, outcome_id: str, side: str, price_cents: float, contracts: int,
                          client_id: str) -> str: ...

    async def cancel_orders(self, order_ids: list[str]) -> None: ...

    async def close(self) -> None: ...


class PaperOrderGateway:
    """Default gateway. Remembers orders; sends nothing anywhere."""

    live = False

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.open_orders: dict[str, dict] = {}
        self.placed: list[dict] = []
        self.cancel_calls: list[list[str]] = []

    async def place_limit(self, outcome_id, side, price_cents, contracts, client_id) -> str:
        oid = f"paper-{next(self._ids)}"
        order = dict(order_id=oid, outcome_id=outcome_id, side=side, price_cents=price_cents,
                     contracts=contracts, client_id=client_id, order_type="LIMIT")
        self.open_orders[oid] = order
        self.placed.append(order)
        return oid

    async def cancel_orders(self, order_ids: list[str]) -> None:
        self.cancel_calls.append(list(order_ids))
        for oid in order_ids:
            self.open_orders.pop(oid, None)

    async def close(self) -> None:
        return None


class NovigOrderGateway:
    """REAL order routing. Only constructed when TRADING_MODE=live."""

    live = True

    def __init__(self, api_base: str, token: str, timeout: float = 2.0) -> None:
        if not token:
            raise ValueError("NovigOrderGateway needs a bearer token")
        self.api_base = api_base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout, headers=self.headers)
        return self._session

    async def place_limit(self, outcome_id, side, price_cents, contracts, client_id) -> str:
        k = ORDER_BODY_KEYS
        body = {k["outcome"]: outcome_id, k["side"]: side, k["price"]: price_cents, k["size"]: contracts,
                k["order_type"]: "LIMIT", k["client_id"]: client_id}
        async with self._sess().post(f"{self.api_base}/v1/orders", json=body) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        order_id = _first(data if isinstance(data, dict) else {}, "orderId", "order_id", "id")
        if order_id is None:
            raise ValueError(f"order response has no id: {data}")
        return str(order_id)

    async def cancel_orders(self, order_ids: list[str]) -> None:
        if not order_ids:
            return
        async with self._sess().delete(f"{self.api_base}/v1/orders", json={BULK_CANCEL_KEY: list(order_ids)}) as resp:
            resp.raise_for_status()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


