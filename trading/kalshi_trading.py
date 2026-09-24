"""
kalshi_trading.py — Kalshi order placement, fills and positions (live execution on a second venue).

ORDERS   POST   {base}/portfolio/orders            (signed: KALSHI-ACCESS-KEY / -TIMESTAMP / -SIGNATURE)
         body   {"ticker", "action": "buy", "side": "yes", "count", "type": "limit",
                 "yes_price" (whole cents) | "yes_price_dollars" (sub-cent), "client_order_id",
                 "time_in_force": "immediate_or_cancel"}
         Each game-winner market is one team's YES contract, so buying an outcome = buying YES on its ticker.
         Immediate-or-cancel: the order fills what it can at once and the rest is cancelled by Kalshi.
         Nothing ever rests on Kalshi from a taker order.
CANCEL   DELETE {base}/portfolio/orders/{order_id} (404 = already finished: fine for IOC orders)
STATUS   GET    {base}/portfolio/orders/{order_id} (used to confirm a zero fill before releasing money)
FILLS    WebSocket channel "fill" on the same authenticated socket as the order book. One message per
         fill, INCREMENTAL counts (not a running total): {"type": "fill", "msg": {"order_id", "market_ticker",
         "side", "yes_price" | "yes_price_dollars", "count" | "count_fp", "action", "is_taker", "ts"}}
POSITIONS GET   {base}/portfolio/positions         open  (market_positions[].position > 0 = YES held)
          GET   {base}/portfolio/settlements       settled (revenue = payout, market_result yes/no/void)

FEES     Kalshi charges the taker fee per fill (ceil(0.07 x C x P x (1-P)) per order). The engine adds it to
         each fill's cost (rounded up per fill, so never understated).

ASSUMED: field names above follow Kalshi's public v2 API; newer responses add *_dollars / *_fp variants,
which are accepted too. The first canary order on Kalshi confirms them. Keep KALSHI_LIVE_TRADING off
until Novig's canary has been reconciled.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
from yarl import URL

from kalshi_feed import auth_headers
from novig_private import FillSlip
from settlement import ExchangePosition

log = logging.getLogger("trading.kalshi_exec")

SIGN_PREFIX = "/trade-api/v2"


def _num(v: Any) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _cents(msg: dict, key: str) -> Optional[float]:
    """Price in cents from either "<key>" (cents) or "<key>_dollars" (dollars)."""
    dollars = _num(msg.get(f"{key}_dollars"))
    if dollars is not None:
        return round(dollars * 100, 4)
    return _num(msg.get(key))


class KalshiError(RuntimeError):
    pass


class KalshiOrderGateway:
    """Same interface as NovigOrderGateway: place_limit / cancel_orders / close (+ get_order)."""

    live = True

    def __init__(self, base_url: str, key_id: str, private_key, time_in_force: Optional[str] = "immediate_or_cancel",
                 timeout: float = 5.0) -> None:
        if not (key_id and private_key is not None):
            raise ValueError("Kalshi trading needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
        self.api_base = base_url.rstrip("/")
        self.key_id, self.private_key = key_id, private_key
        self.time_in_force = time_in_force or None
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self.last_orders: dict[str, dict] = {}        # order_id -> latest order object seen (for diagnostics)

    def _sign_path(self, path: str) -> str:
        # Kalshi signs the path from /trade-api/v2 onward, without the query string
        base_path = URL(self.api_base).path.rstrip("/")
        return (base_path if base_path.startswith(SIGN_PREFIX) else SIGN_PREFIX) + path

    async def _request(self, method: str, path: str, json_body: Optional[dict] = None) -> tuple[int, Any]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        headers = auth_headers(self.key_id, self.private_key, method, self._sign_path(path))
        headers["Content-Type"] = "application/json"
        async with self._session.request(method, self.api_base + path, json=json_body, headers=headers) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                data = await resp.text()
            return resp.status, data

    @staticmethod
    def order_body(ticker: str, price_cents: float, contracts: int, client_id: str,
                   time_in_force: Optional[str]) -> dict:
        body = {"ticker": ticker, "action": "buy", "side": "yes", "count": int(contracts), "type": "limit",
                "client_order_id": client_id}
        if abs(price_cents - round(price_cents)) < 1e-9:
            body["yes_price"] = int(round(price_cents))
        else:
            body["yes_price_dollars"] = f"{price_cents / 100:.4f}"
        if time_in_force:
            body["time_in_force"] = time_in_force
        return body

    async def place_limit(self, outcome_id: str, side: str, price_cents: float, contracts: int,
                          client_id: str) -> str:
        if side != "buy":
            raise KalshiError("only buying YES is supported")
        body = self.order_body(outcome_id, price_cents, contracts, client_id, self.time_in_force)
        status, data = await self._request("POST", "/portfolio/orders", body)
        if status >= 400 or not isinstance(data, dict):
            raise KalshiError(f"HTTP {status}: {data}")
        order = data.get("order") if isinstance(data.get("order"), dict) else data
        oid = order.get("order_id") or order.get("id")
        if not oid:
            raise KalshiError(f"no order_id in response: {data}")
        self.last_orders[str(oid)] = order
        return str(oid)

    async def cancel_orders(self, ids: list[str]) -> None:
        for oid in ids:
            status, data = await self._request("DELETE", f"/portfolio/orders/{oid}")
            if status == 404 or (status < 400):
                continue          # 404: an IOC order is already finished — nothing left to cancel
            raise KalshiError(f"cancel {oid}: HTTP {status}: {data}")

    async def get_order(self, oid: str) -> Optional[dict]:
        status, data = await self._request("GET", f"/portfolio/orders/{oid}")
        if status >= 400 or not isinstance(data, dict):
            return None
        order = data.get("order") if isinstance(data.get("order"), dict) else data
        self.last_orders[oid] = order
        return order

    @staticmethod
    def filled_count(order: dict) -> Optional[float]:
        """Contracts filled according to an order object (several spellings across API versions)."""
        for key in ("fill_count", "taker_fill_count", "fill_count_fp", "filled_count"):
            v = _num(order.get(key))
            if v is not None:
                return v
        count, remaining = _num(order.get("count") or order.get("initial_count")), _num(order.get("remaining_count"))
        if count is not None and remaining is not None and str(order.get("status", "")).lower() in {
                "canceled", "cancelled", "executed"}:
            return count - remaining
        return None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def parse_kalshi_fill(payload: Any) -> Optional[FillSlip]:
    """One Kalshi "fill" WebSocket message -> FillSlip (incremental count, venue kalshi). None if not a fill."""
    if not isinstance(payload, dict) or payload.get("type") != "fill":
        return None
    msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else {}
    oid = msg.get("order_id")
    count = _num(msg.get("count_fp")) if msg.get("count_fp") is not None else _num(msg.get("count"))
    if not oid or count is None or count <= 0:
        return None
    price = _cents(msg, "yes_price")
    if price is None:                       # a NO-side print: YES price is its complement
        no = _cents(msg, "no_price")
        price = None if no is None else 100 - no
    return FillSlip(order_id=str(oid), status="PARTIAL", filled_volume=count, price_cents=price, venue="kalshi")


class KalshiPositionsClient:
    """open_positions() / settled_positions() in the settlement module's ExchangePosition shape."""

    def __init__(self, base_url: str, key_id: str, private_key, timeout: float = 10.0) -> None:
        self.gateway = KalshiOrderGateway(base_url, key_id, private_key, timeout=timeout)
        self.url = self.gateway.api_base + "/portfolio/{positions|settlements}"
        self.status_param, self.open_status, self.settled_status = "-", "positions", "settlements"

    async def _pages(self, path: str, list_key: str) -> list[dict]:
        rows, cursor = [], None
        for _ in range(50):
            status, data = await self.gateway._request("GET", path + (f"?cursor={cursor}" if cursor else ""))
            if status >= 400 or not isinstance(data, dict):
                raise KalshiError(f"GET {path}: HTTP {status}: {data}")
            rows += data.get(list_key) or []
            cursor = data.get("cursor")
            if not cursor:
                break
        return rows

    async def open_positions(self) -> list[ExchangePosition]:
        out = []
        for p in await self._pages("/portfolio/positions", "market_positions"):
            held = _num(p.get("position_fp")) if p.get("position_fp") is not None else _num(p.get("position"))
            if not held or held <= 0:          # we only ever buy YES; a NO position is not ours to manage
                continue
            exposure = _num(p.get("market_exposure_dollars"))
            if exposure is None:
                cents = _num(p.get("market_exposure"))
                exposure = None if cents is None else cents / 100
            out.append(ExchangePosition(settlement_id=f"kalshi-open-{p.get('ticker')}", outcome_id=str(p.get("ticker")),
                                        contracts=held, cost_usd=exposure, status="OPEN"))
        return out

    async def settled_positions(self) -> list[ExchangePosition]:
        out = []
        for s in await self._pages("/portfolio/settlements", "settlements"):
            yes = _num(s.get("yes_count_fp")) if s.get("yes_count_fp") is not None else _num(s.get("yes_count"))
            if not yes:
                continue
            revenue = _num(s.get("revenue_dollars"))
            if revenue is None:
                cents = _num(s.get("revenue"))
                revenue = None if cents is None else cents / 100
            cost = _num(s.get("yes_total_cost_dollars"))
            if cost is None:
                cents = _num(s.get("yes_total_cost"))
                cost = None if cents is None else cents / 100
            result = str(s.get("market_result") or "").upper()
            when = s.get("settled_time")
            try:
                settled_at = datetime.fromisoformat(str(when).replace("Z", "+00:00")).timestamp() if when else time.time()
            except ValueError:
                settled_at = time.time()
            out.append(ExchangePosition(
                settlement_id=f"kalshi-{s.get('ticker')}-{when}", outcome_id=str(s.get("ticker")), contracts=yes,
                cost_usd=cost, status="SETTLED", payout_usd=revenue, settled_at=settled_at,
                result={"YES": "WIN", "NO": "LOSS", "VOID": "VOID"}.get(result, result or None)))
        return out

    async def close(self) -> None:
        await self.gateway.close()


def session_tag() -> str:
    """Short unique prefix so Kalshi client_order_ids never repeat across restarts."""
    return uuid.uuid4().hex[:8]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
