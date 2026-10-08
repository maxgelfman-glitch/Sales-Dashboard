"""
novig_v3.py — Novig's v3 API (self-serve beta, docs.novig.com): signing, catalog, order book, orders, positions.

Everything here follows Novig's published v3 docs and OpenAPI document (snapshot of 2026-09-28):

SIGNING (NOVIG-V3)   every request, GET and the websocket upgrade included, carries Novig-Key-Id,
                     Novig-Timestamp (unix ms) and Novig-Signature (padded base64 of an Ed25519 or P-256
                     signature) over six lines joined by LF:  NOVIG-V3 / ts / METHOD / path / canonical query /
                     sha256(body) hex. Tested against Novig's 30 official vectors (tests/fixtures).
KEYS                 a MANAGEMENT key (made in the Novig app: Profile -> Settings -> Novig API) opens and funds
                     subaccounts but cannot trade; each subaccount has its own TRADING key. `python novig_v3.py
                     setup` does the subaccount step for you. Keys only work in the environment that made them.
UNITS                one Novig contract pays 1 cent. The engine's contract pays $1, so 1 engine contract =
                     100 Novig contracts. Every conversion happens in this file and nowhere else.
PRICES               decimal strings on a 279-price grid (0.001 steps at the ends, 0.005 in 0.055-0.945).
                     Buys are snapped DOWN to the grid, so we never pay more than we meant.
EVERY ORDER BUYS     there is no sell. A bid resting at 0.665 on one outcome is liquidity for the OTHER outcome
                     at 0.335. So the asks for outcome A are the complements of the bids resting on outcome B.
FILLS                at the resting order's price: "That price can be better than your limit" (Fill.cost docs).
                     Novig does NOT fill everything at the worst price.
FEES                 game markets charge the taker only while the event is OPEN_INGAME. Pregame trading (all we
                     do) is free; resting orders are voided at go-live.
LOCATION             orders need a Novig-app geolocation in a legal state within the last 3 days (else 451
                     GEOLOCATION_EXPIRED): open the Novig app on your phone at least every couple of days.
                     A VPN or proxy is refused (451 ANONYMIZED_NETWORK); a normal data-center server is fine.

ASSUMED (no key to test with yet; `python novig_v3.py probe` prints what is needed to confirm):
    * which team is home: events give only a display `description`; "Away at Home" / "Away @ Home" is parsed,
      and the moneyline's outcome names give the full team names.
    * a spread outcome's display name contains its team name.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional, Union

import aiohttp
from yarl import URL

from novig_feed import TRACKED_LEAGUES, MarketInfo, MarketRegistry, MarketUpdate, canonical_market_type
from novig_private import FillSlip
from settlement import ExchangePosition
from team_normalizer import DYNAMIC_LEAGUES, canonical_league, names_match, normalize_team_name
from ws_base import ResilientWebSocketFeed, StateCallback, safe_call

log = logging.getLogger("trading.novig_v3")

QA_HOST = "https://api.qa.novig.com"
PROD_HOST = "https://api.novig.com"
SCHEME = "NOVIG-V3"
WS_PATH = "/v3/ws"
UNITS_PER_CONTRACT = 100                 # Novig contracts (1 cent each) per engine contract ($1)
MAX_WATCHED_MARKETS = 2048               # Novig's cap per websocket connection
STREAM_CAPACITY, STREAM_REFILL, BOOK_WEIGHT = 512, 4.0, 16
TRACKED_MARKET_TYPES = ("MONEY", "SPREAD", "TOTAL")
MAX_BOOK_LEVELS = 10
DEFAULT_MAKER_TTL_MS = 5 * 60 * 1000     # resting quotes expire on their own if the engine dies


# --------------------------------------------------------------------------
# Price grid and units
# --------------------------------------------------------------------------
def snap_down(price: float) -> Optional[float]:
    """Highest grid price <= price (Novig's own snap_down, in thousandths). None below 0.001."""
    milli = int(math.floor(price * 1000 + 1e-6))
    if milli < 1:
        return None
    milli = min(milli, 999)
    if 51 <= milli <= 949:
        milli -= milli % 5
    return milli / 1000


def on_grid(price: float) -> bool:
    s = snap_down(price)
    return s is not None and abs(s - price) < 1e-9


def price_str(price: float) -> str:
    return f"{price:.3f}"


def to_novig_qty(contracts: float) -> int:
    return int(round(contracts * UNITS_PER_CONTRACT))


def from_novig_qty(qty: float) -> float:
    return qty / UNITS_PER_CONTRACT


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX = re.compile(rb"%([0-9A-Fa-f]{2})")


def _decode(part: str) -> bytes:
    """Only %XX decodes; a bare '+' stays a '+'."""
    return _HEX.sub(lambda m: bytes([int(m.group(1), 16)]), part.encode("utf-8"))


def _encode(raw: bytes) -> str:
    return "".join(chr(b) if b in _UNRESERVED else f"%{b:02X}" for b in raw)


def canonical_query(raw: str) -> str:
    pairs = []
    for pair in raw.split("&"):
        if not pair:
            continue
        k, _, v = pair.partition("=")
        pairs.append((_encode(_decode(k)), _encode(_decode(v))))
    pairs.sort(key=lambda kv: (kv[0].encode(), kv[1].encode()))
    return "&".join(f"{k}={v}" for k, v in pairs)


def build_query(params: Optional[dict]) -> str:
    """dict -> canonical query string (values None are dropped). What we sign is exactly what we send."""
    if not params:
        return ""
    parts = []
    for k, v in params.items():
        if v is None:
            continue
        for item in v if isinstance(v, (list, tuple)) else [v]:
            parts.append(f"{_encode(str(k).encode())}={_encode(str(item).encode())}")
    return canonical_query("&".join(parts))


def string_to_sign(ts_ms: Union[int, str], method: str, path: str, query: str, body: bytes) -> str:
    return "\n".join([SCHEME, str(ts_ms), method.upper(), path, canonical_query(query),
                      hashlib.sha256(body).hexdigest()])


def load_private_key(path: Union[str, Path]):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    return load_pem_private_key(Path(path).expanduser().read_bytes(), None)


class NovigSigner:
    def __init__(self, key_id: str, private_key) -> None:
        if not key_id:
            raise ValueError("Novig signer needs a key id")
        self.key_id = key_id
        self.private_key = private_key

    @classmethod
    def from_pem(cls, key_id: str, path: Union[str, Path]) -> "NovigSigner":
        return cls(key_id, load_private_key(path))

    def sign(self, text: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        if isinstance(self.private_key, ec.EllipticCurvePrivateKey):
            sig = self.private_key.sign(text.encode(), ec.ECDSA(hashes.SHA256()))     # DER, as Novig expects
        else:
            sig = self.private_key.sign(text.encode())                                 # Ed25519
        return base64.b64encode(sig).decode()

    def headers(self, method: str, path: str, query: str = "", body: bytes = b"",
                ts_ms: Optional[int] = None) -> dict[str, str]:
        ts = str(int(time.time() * 1000) if ts_ms is None else ts_ms)
        return {"Novig-Key-Id": self.key_id, "Novig-Timestamp": ts,
                "Novig-Signature": self.sign(string_to_sign(ts, method, path, query, body))}


# --------------------------------------------------------------------------
# REST client
# --------------------------------------------------------------------------
class NovigApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str = "", retry_after: Optional[float] = None) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status, self.code, self.retry_after = status, code, retry_after


GEO_HINT = ("Novig refused on location (HTTP 451 {code}). Open the Novig app on your phone (in New York, no VPN) "
            "so it records a fresh location, then retry. Orders need one in the last 3 days.")


class NovigV3Client:
    """Signed REST over aiohttp. `signer=None` allows only the /v3/public routes."""

    def __init__(self, host: str = PROD_HOST, signer: Optional[NovigSigner] = None, timeout: float = 10.0) -> None:
        self.host = host.rstrip("/")
        self.api_base = self.host
        self.signer = signer
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def request(self, method: str, path: str, params: Optional[dict] = None, body: Any = None,
                      ok: Iterable[int] = (200, 201, 202, 207, 304)) -> Any:
        query = build_query(params)
        data = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.signer is not None:
            headers.update(self.signer.headers(method, path, query, data))
        elif not path.startswith("/v3/public/"):
            raise NovigApiError(0, "NO_KEY", f"{path} needs a Novig API key (NOVIG_KEY_ID / NOVIG_PRIVATE_KEY_PATH)")
        url = URL(self.host + path + (f"?{query}" if query else ""), encoded=True)
        async with self._sess().request(method, url, data=data or None, headers=headers) as resp:
            text = await resp.text()
            try:
                payload = json.loads(text) if text else None
            except json.JSONDecodeError:
                payload = None
            if resp.status in ok:
                return payload
            code = payload.get("code", "") if isinstance(payload, dict) else ""
            msg = payload.get("message", "") if isinstance(payload, dict) else text[:200]
            if resp.status == 403 and not code:
                code, msg = "EDGE_REFUSED", "Novig's edge filter refused the request (rate or body size)"
            if resp.status == 451:
                log.critical(GEO_HINT.format(code=code))
            retry = resp.headers.get("Retry-After")
            raise NovigApiError(resp.status, code or "HTTP_ERROR", msg,
                                float(retry) if retry and retry.replace(".", "", 1).isdigit() else None)

    async def pages(self, path: str, params: Optional[dict] = None, max_pages: int = 50) -> list[dict]:
        items, after = [], None
        for _ in range(max_pages):
            data = await self.request("GET", path, {**(params or {}), "after": after})
            items += (data or {}).get("items") or []
            after = (data or {}).get("next")
            if not after:
                break
        return items

    async def catalog(self, public: bool = False) -> tuple[list[dict], list[dict]]:
        prefix = "/v3/public/catalog" if public or self.signer is None else "/v3/catalog"
        events = await self.pages(prefix + "/events", {"status": "OPEN_PREGAME", "limit": 5000})
        markets = await self.pages(prefix + "/markets", {"eventStatus": "OPEN_PREGAME", "limit": 5000,
                                                          "marketType": ",".join(TRACKED_MARKET_TYPES)})
        return events, markets

    async def fetch_open_markets(self) -> list[MarketInfo]:
        events, markets = await self.catalog()
        return build_market_infos(events, markets)

    @property
    def events_url(self) -> str:
        return self.host + "/v3/catalog/events + /v3/catalog/markets (OPEN_PREGAME)"

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


# --------------------------------------------------------------------------
# Catalog -> MarketInfo
# --------------------------------------------------------------------------
_MATCHUP = [(re.compile(r"\s+@\s+"), True), (re.compile(r"\s+at\s+", re.I), True),
            (re.compile(r"\s+vs?\.?\s+", re.I), False)]


def parse_matchup(description: str) -> Optional[tuple[str, str]]:
    """'Chiefs at Bills, Sun 20:20' -> (home, away) = ('Bills', 'Chiefs'). 'A vs B' is read as home A."""
    text = (description or "").split(",")[0].split(" - ")[0].strip()
    for pattern, away_first in _MATCHUP:
        parts = pattern.split(text, maxsplit=1)
        if len(parts) == 2 and all(p.strip() for p in parts):
            a, b = parts[0].strip(), parts[1].strip()
            return (b, a) if away_first else (a, b)
    return None


def _strip_line(name: str) -> str:
    return re.sub(r"[+-]?\d+(\.\d+)?", " ", name or "").replace("(", " ").replace(")", " ").strip()


def _same_team(league: str, a: str, b: str) -> bool:
    a, b = _strip_line(a), _strip_line(b)
    if not a or not b:
        return False
    if league in DYNAMIC_LEAGUES:
        return names_match(league, a, b)
    na, nb = normalize_team_name(a, league), normalize_team_name(b, league)
    if na is not None and nb is not None:
        return na == nb
    ta, tb = set(a.lower().split()), set(b.lower().split())
    return bool(ta & tb) and (ta <= tb or tb <= ta)


def _side_of(league: str, name: str, home: str, away: str) -> Optional[str]:
    h, a = _same_team(league, name, home), _same_team(league, name, away)
    return "home" if h and not a else "away" if a and not h else None


def build_market_infos(events: list[dict], markets: list[dict]) -> list[MarketInfo]:
    """Join catalog events and markets into tracked MarketInfo rows (one per outcome, each knowing its sibling)."""
    ev_by_id = {e.get("eventId"): e for e in events if isinstance(e, dict)}
    by_event: dict[str, list[dict]] = {}
    for m in markets:
        if isinstance(m, dict):
            by_event.setdefault(m.get("eventId"), []).append(m)
    rows: list[MarketInfo] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for event_id, ms in by_event.items():
        ev = ev_by_id.get(event_id)
        if ev is None:
            skip("market without an open pregame event")
            continue
        league = canonical_league(str(ev.get("league") or ""))
        if league not in TRACKED_LEAGUES or ev.get("status", "OPEN_PREGAME") != "OPEN_PREGAME":
            continue
        teams = parse_matchup(ev.get("description", ""))
        if teams is None:
            skip("event description not 'Away at Home'")
            continue
        home, away = teams
        money = next((m for m in ms if m.get("marketType") == "MONEY" and len(m.get("outcomes") or []) == 2), None)
        if money is not None:                       # the moneyline's outcome names are the full team names
            names = {_side_of(league, o.get("name", ""), home, away): o.get("name", "") for o in money["outcomes"]}
            if names.get("home") and names.get("away"):
                home, away = names["home"], names["away"]
        start = ev.get("startsTs") or None
        for m in ms:
            outcomes = m.get("outcomes") or []
            mtype = m.get("marketType")
            fee = m.get("fee") or {}
            if mtype not in TRACKED_MARKET_TYPES or m.get("status", "OPEN") != "OPEN" or len(outcomes) != 2:
                continue
            if fee.get("charged", "WHEN_LIVE") != "WHEN_LIVE":
                skip("market charges a pregame fee")
                continue
            strike = m.get("strike")
            if mtype in ("SPREAD", "TOTAL") and strike is None:
                skip("line market without a strike")
                continue
            labelled = []
            for o in outcomes:
                name = str(o.get("name") or "")
                if mtype == "TOTAL":
                    low = name.lower()
                    side = "over" if "over" in low and "under" not in low else (
                        "under" if "under" in low and "over" not in low else None)
                    labelled.append((o, side, side, float(strike)))
                else:
                    side = _side_of(league, name, home, away)
                    team = home if side == "home" else away if side == "away" else None
                    line = None if mtype == "MONEY" else (float(strike) if side == "home" else -float(strike))
                    labelled.append((o, side, team, line))
            if len({s for _, s, _, _ in labelled}) != 2 or any(s is None for _, s, _, _ in labelled):
                skip(f"{mtype} outcomes not matched to sides")
                continue
            (o1, _, n1, l1), (o2, _, n2, l2) = labelled
            for o, name, line, sib in ((o1, n1, l1, o2), (o2, n2, l2, o1)):
                try:
                    rows.append(MarketInfo(
                        venue="novig", outcome_id=str(o["outcomeId"]), market_id=str(m["marketId"]),
                        sibling_outcome_id=str(sib["outcomeId"]), league=league, market_type=mtype,
                        event_id=str(event_id), home_team=home, away_team=away, outcome=name, line=line,
                        start_time=None if start is None else float(start) / 1000.0))
                except Exception:  # noqa: BLE001 — one bad node never breaks the bootstrap
                    skip("invalid outcome")
    if skipped:
        log.info("BOOTSTRAP novig v3 skipped: %s", ", ".join(f"{n} x {r}" for r, n in sorted(skipped.items())))
    return [r for r in rows if r.is_tracked()]


# --------------------------------------------------------------------------
# Order gateway
# --------------------------------------------------------------------------
class NovigV3OrderGateway:
    """
    place_limit(outcome, side, price_cents, contracts, client_id) in ENGINE units ($1 contracts, cents).
    side "sell" means: buy the other outcome at 100 - price (Novig has only buys). Takers use IOC; the maker's
    gateway uses GTT so a quote can never outlive the engine by more than its TTL.
    """

    live = True
    reconciles = True            # the engine checks GET /v3/orders/{id} after a taker times out

    def __init__(self, client: NovigV3Client, sibling: Callable[[str], Optional[str]] = lambda oid: None,
                 tif: str = "IOC", ttl_ms: Optional[int] = None,
                 on_order: Optional[Callable[[str], None]] = None) -> None:
        if client.signer is None:
            raise ValueError("Novig order gateway needs a trading key")
        self.client = client
        self.api_base = client.host
        self.sibling = sibling
        self.time_in_force = tif
        self.ttl_ms = ttl_ms if tif == "GTT" else None
        self.on_order = on_order
        self.client_ids: dict[str, str] = {}             # engine client id -> the UUID Novig requires

    def _route(self, outcome_id: str, side: str, price_cents: float) -> tuple[str, Optional[float]]:
        price = price_cents / 100.0
        if side == "sell":
            other = self.sibling(outcome_id)
            if other is None:
                raise ValueError(f"cannot sell {outcome_id}: its other outcome is unknown")
            outcome_id, price = other, 1.0 - price
        return outcome_id, snap_down(price)

    def order_body(self, outcome_id: str, price_cents: float, contracts: int, client_id: str,
                   time_in_force: Optional[str] = None, side: str = "buy") -> dict:
        target, price = self._route(outcome_id, side, price_cents)
        if price is None:
            raise ValueError(f"price {price_cents}c is below Novig's grid")
        cid = self.client_ids.setdefault(client_id, str(uuid.uuid4()))
        body = {"outcomeId": target, "price": price_str(price), "qty": to_novig_qty(contracts),
                "tif": time_in_force or self.time_in_force, "clientId": cid}
        if body["tif"] == "GTT":
            body["ttl"] = int(self.ttl_ms or DEFAULT_MAKER_TTL_MS)
        return body

    async def place_limit(self, outcome_id: str, side: str, price_cents: float, contracts: int,
                          client_id: str) -> str:
        body = self.order_body(outcome_id, price_cents, contracts, client_id, side=side)
        if body["qty"] < 1:
            raise ValueError("order size rounds to zero Novig contracts")
        data = await self.client.request("POST", "/v3/orders", body=body)
        oid = (data or {}).get("orderId")
        if not oid:
            raise NovigApiError(0, "NO_ORDER_ID", f"order response has no orderId: {data}")
        if self.on_order is not None:
            self.on_order(str(oid))
        return str(oid)

    async def cancel_all(self) -> None:
        """Every resting order of this subaccount (the engine's own subaccount: nothing else trades in it)."""
        await self.client.request("DELETE", "/v3/orders")

    async def cancel_orders(self, order_ids: list[str]) -> None:
        ids = list(order_ids)
        for i in range(0, len(ids), 256):
            await self.client.request("DELETE", "/v3/orders/batch", body={"orderIds": ids[i:i + 256]})

    async def get_order(self, order_id: str) -> Optional[dict]:
        try:
            return await self.client.request("GET", f"/v3/orders/{order_id}")
        except NovigApiError as exc:
            if exc.status == 404:
                return None
            raise

    @staticmethod
    def filled_count(order: Optional[dict]) -> Optional[float]:
        """Engine contracts filled, but only once the order is final (otherwise None)."""
        if not order or order.get("status") not in {"FILLED", "CANCELED", "REJECTED"}:
            return None
        return from_novig_qty(float(order.get("qty", 0)) - float(order.get("remaining", 0)))

    async def close(self) -> None:
        await self.client.close()


# --------------------------------------------------------------------------
# Positions and settlement
# --------------------------------------------------------------------------
class NovigV3PositionsClient:
    """
    open_positions() from GET /v3/account/positions. settled_positions(): Novig drops a position once it
    settles, so we remember every outcome we have held (plus whatever `watch()` returns: the engine's open
    Novig legs) and read the market's grade (WIN / LOSS / PUSH / fair-value price) from the catalog.
    """

    def __init__(self, client: NovigV3Client, watch: Callable[[], Iterable[str]] = lambda: (),
                 market_of: Callable[[str], Optional[str]] = lambda oid: None) -> None:
        self.client = client
        self.watch = watch
        self.market_of = market_of
        self.url = client.host + "/v3/account/positions"
        self.status_param, self.open_status, self.settled_status = "-", "open", "graded"
        self.held: dict[str, dict] = {}            # outcome id -> last seen {marketId, qty, cost}

    async def open_positions(self) -> list[ExchangePosition]:
        data = await self.client.request("GET", "/v3/account/positions") or {}
        out = []
        for p in data.get("positions") or []:
            qty = float(p.get("qty") or 0)
            if qty <= 0:
                continue
            oid = str(p["outcomeId"])
            self.held[oid] = {"marketId": p.get("marketId"), "qty": qty, "cost": float(p.get("cost") or 0)}
            out.append(ExchangePosition(settlement_id=f"novig-open-{oid}", outcome_id=oid,
                                        market_id=p.get("marketId"), contracts=from_novig_qty(qty),
                                        cost_usd=float(p.get("cost") or 0), status="OPEN"))
        return out

    async def settled_positions(self) -> list[ExchangePosition]:
        out = []
        outcomes = set(self.held) | {str(o) for o in self.watch()}
        markets: dict[str, Optional[dict]] = {}
        for oid in outcomes:
            mid = (self.held.get(oid) or {}).get("marketId") or self.market_of(oid)
            if not mid:
                continue
            if mid not in markets:
                try:
                    markets[mid] = await self.client.request("GET", f"/v3/catalog/markets/{mid}")
                except NovigApiError as exc:
                    log.warning("SETTLEMENT novig market %s lookup failed: %s", mid, exc)
                    markets[mid] = None
            m = markets[mid]
            if not m or m.get("status") != "SETTLED":
                continue
            status = next((str(o.get("status")) for o in m.get("outcomes") or [] if str(o.get("outcomeId")) == oid),
                          None)
            if status in (None, "TBD"):
                continue
            held = self.held.get(oid) or {}
            contracts = from_novig_qty(held.get("qty", 0))
            if status in {"WIN", "LOSS", "PUSH"}:
                result, payout = status, None
            else:                                   # graded at fair market value: a price, not a win
                result, payout = "FMV", round(contracts * float(status), 2)
            out.append(ExchangePosition(settlement_id=f"novig-settle-{oid}", outcome_id=oid, market_id=mid,
                                        contracts=contracts, cost_usd=held.get("cost"), status="SETTLED",
                                        result=result, payout_usd=payout, settled_at=time.time()))
        for p in out:
            self.held.pop(p.outcome_id, None)
        return out

    async def close(self) -> None:
        await self.client.close()


# --------------------------------------------------------------------------
# Websocket: order books, lifecycle, our own orders
# --------------------------------------------------------------------------
class _Book:
    __slots__ = ("seq", "orders", "resync")

    def __init__(self, seq: int = 0) -> None:
        self.seq = seq
        self.orders: dict[str, tuple[str, int, int]] = {}      # order id -> (outcome id, price milli, qty)
        self.resync = False


class NovigV3Feed(ResilientWebSocketFeed):
    """
    One signed socket: `book` for every tracked market (asks derived from the other outcome's bids) and our
    private `orders` channel (its 15 s heartbeat also keeps a quiet socket alive). In live mode its events
    become FillSlips (incremental: each fill's own quantity).
    Our own resting orders are left out of the book we trade against, so the engine never takes its own quote.
    """

    venue = "novig"

    def __init__(self, client: NovigV3Client, registry: Optional[MarketRegistry] = None,
                 on_update=None, on_state_change: Optional[StateCallback] = None, on_slip=None,
                 on_lifecycle: Optional[Callable[[MarketInfo, str], Union[None, Awaitable[None]]]] = None,
                 private: bool = True, max_markets: int = MAX_WATCHED_MARKETS, **kw: Any) -> None:
        if client.signer is None:
            raise ValueError("the Novig websocket needs an API key (a trading or trading::read key)")
        url = client.host.replace("https://", "wss://").replace("http://", "ws://") + WS_PATH
        kw.setdefault("stale_after", 45.0)       # the private channel's heartbeat arrives every 15 s
        super().__init__(url, on_state_change, logger=log, **kw)
        self.client = client
        self.registry = registry if registry is not None else MarketRegistry()
        self.on_update = on_update
        self.on_slip = on_slip
        self.on_lifecycle = on_lifecycle
        self.private = private
        self.max_markets = max_markets
        self.subscribe_messages = ["book: every tracked market (up to 2,048)", "private: orders"]   # display only
        self.books: dict[str, _Book] = {}
        self.latest: dict[str, MarketUpdate] = {}
        self.subscribed: set[str] = set()
        self.own_orders: set[str] = set()
        self.slips_received = 0
        self._nonce = 0
        self._orders_seq: Optional[int] = None
        self._pending: dict[int, list[str]] = {}            # subscribe nonce -> markets it asked for
        self._tokens, self._tokens_at = float(STREAM_CAPACITY), time.monotonic()
        self._ws_conn = None

    # ---- connection ----
    def _headers(self) -> Optional[dict[str, str]]:
        return self.client.signer.headers("GET", WS_PATH)

    def _next_nonce(self) -> int:
        self._nonce += 1
        return self._nonce

    def wanted_markets(self) -> list[str]:
        """Tracked markets, soonest start first, capped at Novig's per-connection limit."""
        first: dict[str, float] = {}
        for info in self.registry.all():
            if info.venue == "novig" and info.market_id:
                first[info.market_id] = min(first.get(info.market_id, math.inf), info.start_time or math.inf)
        ids = sorted(first, key=lambda m: (first[m], m))
        if len(ids) > self.max_markets:
            log.warning("NOVIG %d tracked markets exceed the %d-per-connection cap: watching the soonest",
                        len(ids), self.max_markets)
        return ids[:self.max_markets]

    def _spend(self, cost: float) -> bool:
        """Client-side model of the `stream` throttle. One request is charged at most the capacity, but
        a request above the capacity passes only when the throttle is full."""
        now = time.monotonic()
        self._tokens = min(STREAM_CAPACITY, self._tokens + (now - self._tokens_at) * STREAM_REFILL)
        self._tokens_at = now
        cost = min(cost, STREAM_CAPACITY)
        if self._tokens + 1e-9 < cost:
            return False
        self._tokens -= cost
        return True

    async def _send(self, ws, msg: dict) -> int:
        nonce = self._next_nonce()
        await ws.send(json.dumps({"nonce": nonce, **msg}, separators=(",", ":")))
        return nonce

    async def _subscribe_markets(self, ws, markets: list[str], private: bool = False) -> None:
        sub: dict[str, Any] = {"markets": {m: "book" for m in markets}} if markets else {}
        if private:
            sub["private"] = ["orders"]
        if not sub:
            return
        nonce = await self._send(ws, {"subscribe": sub})
        self._pending[nonce] = list(markets)
        self.subscribed.update(markets)

    async def _on_open(self, ws) -> None:
        self._ws_conn = ws
        self._nonce, self._orders_seq = 0, None
        self.subscribed.clear()
        self._pending.clear()
        markets = self.wanted_markets()
        if not markets and not self.private:
            log.warning("NOVIG nothing to subscribe yet (empty registry)")
            return
        self._spend(len(markets) * BOOK_WEIGHT + (1 if self.private else 0))
        await self._subscribe_markets(ws, markets, self.private)
        log.info("CONN novig v3 subscribed book for %d markets%s", len(markets),
                 " + private orders" if self.private else "")

    async def sync_subscriptions(self) -> None:
        """After a registry refresh: subscribe new markets and drop finished ones, within the stream throttle."""
        ws = self._ws_conn
        if ws is None or self._ws is None:
            return
        wanted = set(self.wanted_markets())
        gone = sorted(self.subscribed - wanted)
        new = sorted(wanted - self.subscribed)
        if gone and self._spend(len(gone)):
            await self._send(ws, {"unsubscribe": [f"market:{m}" for m in gone]})
            self.subscribed.difference_update(gone)
            for m in gone:
                self._drop_market(m)
        if new:
            if not self._spend(len(new) * BOOK_WEIGHT):
                log.info("NOVIG %d new market(s) wait for the stream throttle to refill", len(new))
                return
            await self._subscribe_markets(ws, new)
            log.info("NOVIG subscribed %d new market(s)", len(new))

    def _drop_market(self, market_id: str) -> None:
        self.books.pop(market_id, None)
        for info in self.registry.all():
            if info.market_id == market_id:
                self.latest.pop(info.outcome_id, None)

    def _clear_state(self) -> dict[str, int]:
        counts = {"stale_price_frames": len(self.latest), "order_books": len(self.books)}
        self.latest.clear()
        self.books.clear()
        self._ws_conn = None
        return counts

    def _heartbeat_extra(self) -> str:
        return f" books={len(self.books)} subscribed={len(self.subscribed)}"

    # ---- messages ----
    async def _handle_raw(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("NOVIG non-JSON frame skipped")
            return
        if not isinstance(msg, dict):
            return
        if "code" in msg and "message" in msg:
            failed = self._pending.pop(msg.get("nonce"), None) if msg.get("nonce") is not None else None
            if failed is not None:                      # that subscribe did nothing: retry at the next refresh
                self.subscribed.difference_update(failed)
                self._tokens = 0.0
            log.log(logging.ERROR if failed else logging.WARNING, "NOVIG websocket error %s: %s (nonce %s)%s",
                    msg.get("code"), msg.get("message"), msg.get("nonce"),
                    f" — {len(failed)} market(s) not subscribed, retrying at the next refresh" if failed else "")
            return
        if isinstance(msg.get("nonce"), int) and "subscribed" in msg:
            self._pending.pop(msg["nonce"], None)
        for mid, s in (msg.get("snapshot") or {}).items():
            if isinstance(s, dict):
                await self._apply_snapshot(mid, s)
        for mid, d in (msg.get("delta") or {}).items():
            if isinstance(d, dict):
                await self._apply_delta(mid, d)
        if isinstance(msg.get("orders"), dict):
            await self._apply_orders(msg["orders"])
        hb = msg.get("heartbeat")
        if isinstance(hb, dict) and self.private and self._orders_seq is not None:
            seq = hb.get("orders")
            if isinstance(seq, int) and seq > self._orders_seq:
                log.critical("NOVIG private orders: heartbeat seq %d > last %d — a fill message was lost; "
                             "re-snapshotting", seq, self._orders_seq)
                await self._request_snapshot({"private": ["orders"]})

    async def _request_snapshot(self, selection: dict) -> None:
        if self._ws_conn is not None:
            await self._send(self._ws_conn, {"snapshot": selection})

    async def _apply_snapshot(self, market_id: str, s: dict) -> None:
        book = s.get("book")
        if isinstance(book, dict):
            b = _Book(int(book.get("seq", 0)))
            for outcome, orders in (book.get("orders") or {}).items():
                for o in orders or []:
                    b.orders[str(o.get("order"))] = (str(outcome), _milli(o.get("price")), int(o.get("qty", 0)))
            self.books[market_id] = b
            await self._emit_market(market_id)
        life = s.get("lifecycle")
        if isinstance(life, dict) and life.get("status") in {"CLOSED", "SETTLED"}:
            await self._lifecycle(market_id, "CLOSE")

    async def _apply_delta(self, market_id: str, d: dict) -> None:
        book = d.get("book")
        if isinstance(book, dict):
            b = self.books.get(market_id)
            seq = int(book.get("seq", 0))
            if b is None:
                b = self.books[market_id] = _Book(seq - 1)       # a market opened under the subscription
            if b.resync:
                pass                                              # waiting for the snapshot we asked for
            elif seq != b.seq + 1:
                log.warning("NOVIG book gap on %s: seq %d after %d — re-snapshotting", market_id, seq, b.seq)
                b.resync = True
                self.latest_clear(market_id)
                if self._spend(BOOK_WEIGHT):
                    await self._request_snapshot({"markets": {market_id: "book"}})
            else:
                b.seq = seq
                for x in book.get("deltas") or []:
                    if x.get("kind") == "add":
                        b.orders[str(x.get("order"))] = (str(x.get("outcome")), _milli(x.get("price")),
                                                         int(x.get("qty", 0)))
                    elif x.get("kind") == "remove":
                        b.orders.pop(str(x.get("order")), None)
                await self._emit_market(market_id)
        life = d.get("lifecycle")
        if isinstance(life, dict):
            for t in life.get("deltas") or []:
                await self._lifecycle(market_id, str(t))

    def latest_clear(self, market_id: str) -> None:
        for info in self.registry.all():
            if info.market_id == market_id:
                self.latest.pop(info.outcome_id, None)

    async def _lifecycle(self, market_id: str, transition: str) -> None:
        if transition not in {"GOLIVE", "START", "CLOSE", "GRADE"}:
            return
        log.warning("NOVIG lifecycle %s on market %s", transition, market_id)
        self.latest_clear(market_id)
        info = next((i for i in self.registry.all() if i.market_id == market_id), None)
        if info is not None and self.on_lifecycle is not None:
            await safe_call(self.on_lifecycle, info, transition, logger=log)

    def levels(self, market_id: str, outcome_id: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """(asks cheapest first, bids best first) for one outcome, engine units, our own orders excluded."""
        b = self.books.get(market_id)
        asks: dict[int, int] = {}
        bids: dict[int, int] = {}
        if b is None:
            return [], []
        for oid, (outcome, milli, qty) in b.orders.items():
            if oid in self.own_orders or qty <= 0 or not 0 < milli < 1000:
                continue
            if outcome == outcome_id:
                bids[milli] = bids.get(milli, 0) + qty
            else:
                asks[1000 - milli] = asks.get(1000 - milli, 0) + qty
        return ([(m / 1000, from_novig_qty(q)) for m, q in sorted(asks.items())],
                [(m / 1000, from_novig_qty(q)) for m, q in sorted(bids.items(), reverse=True)])

    async def _emit_market(self, market_id: str) -> None:
        for info in [i for i in self.registry.all() if i.market_id == market_id]:
            asks, bids = self.levels(market_id, info.outcome_id)
            top = dict(price=asks[0][0] if asks else None, available_volume=asks[0][1] if asks else 0.0,
                       best_bid=bids[0][0] if bids else None, bid_volume=bids[0][1] if bids else 0.0,
                       ask_levels=asks[:MAX_BOOK_LEVELS])
            previous = self.latest.get(info.outcome_id)
            if previous is not None and all(getattr(previous, k) == v for k, v in top.items()):
                continue
            if not asks and not bids:
                self.latest.pop(info.outcome_id, None)
                continue
            update = MarketUpdate.from_info(info, **top)
            self.latest[info.outcome_id] = update
            if self.on_update is not None:
                await safe_call(self.on_update, update, previous, logger=log)

    async def _apply_orders(self, orders: dict) -> None:
        seq = orders.get("seq")
        if "open" in orders:                                     # snapshot
            for o in orders.get("open") or []:
                self.own_orders.add(str(o.get("orderId")))
            self._orders_seq = seq if isinstance(seq, int) else self._orders_seq
            return
        if isinstance(seq, int):
            if self._orders_seq is not None and seq > self._orders_seq + 1:
                log.critical("NOVIG private orders gap: seq %d after %d — re-snapshotting; check fills via "
                             "GET /v3/portfolio/fills", seq, self._orders_seq)
                await self._request_snapshot({"private": ["orders"]})
            self._orders_seq = seq
        for ev in orders.get("deltas") or []:
            slip = event_to_slip(ev)
            kind = ev.get("kind")
            if kind == "open":
                self.own_orders.add(str(ev.get("orderId")))
            elif kind in {"cancel", "reject"} or (kind == "fill" and ev.get("remaining") == 0):
                self.own_orders.discard(str(ev.get("orderId")))
            if slip is not None and self.on_slip is not None:
                self.slips_received += 1
                log.info("FILL_SLIP %s", slip.model_dump_json())
                await safe_call(self.on_slip, slip, logger=log)


def _milli(price: Any) -> int:
    try:
        return int(round(float(price) * 1000))
    except (TypeError, ValueError):
        return 0


def event_to_slip(ev: dict) -> Optional[FillSlip]:
    """A private `orders` event -> FillSlip in engine units. Fill quantities are per fill (incremental)."""
    kind, oid = ev.get("kind"), ev.get("orderId")
    if not oid:
        return None
    if kind == "fill":
        return FillSlip(order_id=str(oid), status="FILLED" if ev.get("remaining") == 0 else "PARTIAL",
                        filled_volume=from_novig_qty(float(ev.get("qty", 0))),
                        price_cents=float(ev["price"]) * 100 if ev.get("price") is not None else None, venue="novig")
    if kind in {"cancel", "reject"}:
        return FillSlip(order_id=str(oid), status="CANCELED" if kind == "cancel" else "REJECTED", filled_volume=0,
                        venue="novig")
    return None


# --------------------------------------------------------------------------
# Command line: echo / probe / setup (run on YOUR machine; keys never leave it)
# --------------------------------------------------------------------------
def _env_signer(env: dict, key_var: str = "NOVIG_KEY_ID", path_var: str = "NOVIG_PRIVATE_KEY_PATH") -> NovigSigner:
    if not env.get(key_var) or not env.get(path_var):
        raise SystemExit(f"set {key_var} and {path_var} (see config/live.env.example)")
    return NovigSigner.from_pem(env[key_var], env[path_var])


async def _echo(env: dict) -> None:
    client = NovigV3Client(env.get("NOVIG_HOST") or PROD_HOST, _env_signer(env))
    try:
        print("echo:", await client.request("POST", "/v3/echo", body={"hello": "novig"}))
        print("OK: host, key, clock and signature are all correct.")
    finally:
        await client.close()


async def _probe(env: dict) -> None:
    """Prints what the engine needs to confirm its assumptions. Paste the output back (it holds no secrets)."""
    client = NovigV3Client(env.get("NOVIG_HOST") or PROD_HOST, _env_signer(env))
    try:
        print("leagues:", await client.request("GET", "/v3/types/leagues"))
        print("market types:", await client.request("GET", "/v3/types/markets"))
        print("limits:", json.dumps(await client.request("GET", "/v3/limits"))[:600])
        events, markets = await client.catalog()
        print(f"\n{len(events)} open pregame events, {len(markets)} main-line markets")
        for e in events[:15]:
            print(f"  {e.get('league'):>8}  {e.get('description')!r}  parsed(home, away)="
                  f"{parse_matchup(e.get('description', ''))}")
        shown = set()
        for m in markets:
            if m.get("marketType") in shown:
                continue
            shown.add(m.get("marketType"))
            print(f"\n{m.get('marketType')} strike={m.get('strike')} fee={m.get('fee')} voids={m.get('voids')}")
            print("   outcomes:", [o.get("name") for o in m.get("outcomes") or []])
            book = await client.request("GET", f"/v3/catalog/markets/{m['marketId']}/book")
            print("   book:", json.dumps(book)[:400])
        infos = build_market_infos(events, markets)
        print(f"\nengine would track {len(infos)} outcomes in "
              f"{len({i.market_id for i in infos})} markets; sample:")
        for i in infos[:8]:
            print(f"   {i.league} {i.away_team} @ {i.home_team} {i.market_type} {i.outcome} {i.line}")
    finally:
        await client.close()


async def _setup(env: dict, label: str, fund: Optional[str], out_dir: Path) -> None:
    """Management key -> new subaccount with a fresh Ed25519 trading key (private key stays on this machine)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    host = env.get("NOVIG_HOST") or PROD_HOST
    mgmt = NovigV3Client(host, _env_signer(env, "NOVIG_MANAGEMENT_KEY_ID", "NOVIG_MANAGEMENT_KEY_PATH"))
    key = Ed25519PrivateKey.generate()
    out_dir.mkdir(parents=True, exist_ok=True)
    pem_path = out_dir / f"novig-trading-{label}.pem"
    if pem_path.exists():
        raise SystemExit(f"{pem_path} already exists: pick another --label")
    pem_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    os.chmod(pem_path, 0o600)
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    try:
        opened = await mgmt.request("POST", "/v3/account/subaccounts",
                                    body={"label": label, "publicKey": public, "algorithm": "Ed25519"})
        key_id = opened["keyId"]
        print(f"subaccount opened. Put these two lines in live.env:\n  NOVIG_KEY_ID={key_id}\n"
              f"  NOVIG_PRIVATE_KEY_PATH={pem_path.resolve()}")
        if fund:
            res = await mgmt.request("POST", f"/v3/account/subaccounts/{key_id}/transfer",
                                     body={"direction": "fund", "amount": f"{float(fund):.5f}",
                                           "clientTransferId": f"{label}-{int(time.time())}"})
            print("funding requested (check it shows Applied before trading):", res)
    except Exception:
        pem_path.unlink(missing_ok=True)
        raise
    finally:
        await mgmt.close()


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Novig v3 API helper (signature test, probe, subaccount setup)")
    ap.add_argument("command", choices=["echo", "probe", "setup"])
    ap.add_argument("--env-file", help="read settings from this file (e.g. live.env)")
    ap.add_argument("--label", default="engine", help="setup: subaccount label")
    ap.add_argument("--fund", help="setup: dollars to move into the new subaccount")
    ap.add_argument("--key-dir", default="keys", help="setup: where the new private key is written")
    args = ap.parse_args(argv)
    env = dict(os.environ)
    if args.env_file:
        for line in Path(args.env_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.command == "echo":
        asyncio.run(_echo(env))
    elif args.command == "probe":
        asyncio.run(_probe(env))
    else:
        asyncio.run(_setup(env, args.label, args.fund, Path(args.key_dir)))


if __name__ == "__main__":
    main()
