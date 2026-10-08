"""
novig_rfq.py — Quote Novig parlays as a registered pricer (liquidity provider), per Novig's RFQ docs (2026-09).

HOW NOVIG'S PARLAY AUCTION WORKS
    A user asks for a parlay (2-20 outcome ids). Novig sends `rfq_created` to every registered pricer; the auction
    lasts 3 seconds; we answer with a price (the user's cost per $1 payout) and a max_wager (largest user stake we
    cover, at least the round's min_wager, $10+). The LOWEST price wins (tie: larger max_wager). The winner gets
    `quote_accepted` and has 1 second to `confirm` (last look); silence rejects. `rfq_executed` reports every
    round's execution price to every pricer, so shadow mode learns where we would have won without quoting.
    Fees: the user pays 0.10 x w x k / (w + k); the quoter pays nothing (and earns no maker credit).
    Settlement: one position; a losing leg settles it as a loss at once; a voided leg counts at its fair market
    value (scalar = product of leg values). The pot = stake / price; the user gets pot x scalar, we keep the rest.

ACCESS (not self-serve like Kalshi): email developers@novig.com to become an LP (W-9; QA within ~2 business
days; production generally needs a $30,000 deposit). Register a pricer once (`python novig_rfq.py register`).
Auth is the OAuth 2.0 client-credentials bearer token of Novig's market-maker API (not the v3 signing keys):
NOVIG_RFQ_ACCESS_TOKEN, or NOVIG_RFQ_CLIENT_ID + NOVIG_RFQ_CLIENT_SECRET + NOVIG_RFQ_TOKEN_URL (Novig gives these
at onboarding; the token URL is not in the public docs).

PRICING reuses combo_quoter.ComboPricer: the same per-leg uncertainty margins, same-game handling (Novig allows
same-game legs; they stay shadow-only unless COMBO_LIVE_KINDS includes sgp), size shrinking with uncertainty, and
the same risk caps, with a SEPARATE risk book for Novig. The quoter fee is zero here.

MODES (NOVIG_RFQ): shadow (subscribe, price, record, never quote) | qa (real quotes on Novig's QA, test money) |
live (TRADING_MODE=live).

ASSUMED, to check on QA: `result` on GET /rfq/executions is the PARLAY's result from the user's side (win = the
parlay hit, we pay); quotes may be sent as soon as the round opens; the price we send is rounded to 0.001 by Novig
(we round UP ourselves, never in the user's favour).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import time
from typing import Any, Callable, Optional

import aiohttp

from combo_quoter import ComboConfig, ComboPrice, ComboPricer, Leg, LegFair, RiskBook
from ws_base import ResilientWebSocketFeed, StateCallback, safe_call

log = logging.getLogger("trading.novig_rfq")

PROD_HOST, QA_HOST = "https://api.novig.com", "https://api-qa.novig.us"
LAST_LOOK_BUDGET_S = 0.6            # answer well inside Novig's 1-second window
ACCEPT_GRACE_S = 10.0               # keep a quote's liability reserved this long after its round closes


def _num(v: Any) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


class NovigRfqAuth:
    """A static access token, or OAuth 2.0 client credentials refreshed before expiry."""

    def __init__(self, access_token: Optional[str] = None, client_id: Optional[str] = None,
                 client_secret: Optional[str] = None, token_url: Optional[str] = None) -> None:
        if not access_token and not (client_id and client_secret and token_url):
            raise ValueError("Novig RFQ needs NOVIG_RFQ_ACCESS_TOKEN, or client id + secret + token URL")
        self._token, self._expires = access_token, math.inf if access_token else 0.0
        self.client_id, self.client_secret, self.token_url = client_id, client_secret, token_url

    @property
    def token(self) -> Optional[str]:
        return self._token

    async def ensure(self, session: Optional[aiohttp.ClientSession] = None) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        own = session is None
        session = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            async with session.post(self.token_url, data={"grant_type": "client_credentials",
                                                          "client_id": self.client_id,
                                                          "client_secret": self.client_secret}) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400 or not isinstance(data, dict) or not data.get("access_token"):
                    raise RuntimeError(f"Novig token request failed: HTTP {resp.status}")
        finally:
            if own:
                await session.close()
        self._token = data["access_token"]
        self._expires = time.time() + float(data.get("expires_in") or 3600)
        return self._token


class NovigRfqRest:
    def __init__(self, auth: NovigRfqAuth, host: str = PROD_HOST, timeout: float = 10.0) -> None:
        self.auth, self.host = auth, host.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def _req(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        token = await self.auth.ensure(self._session)
        async with self._session.request(method, self.host + path, json=body,
                                         headers={"Authorization": f"Bearer {token}"}) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"{method} {path}: HTTP {resp.status}: {data}")
            return data

    async def register(self) -> dict:
        return await self._req("POST", "/rfq/pricer", {})              # websocket-only pricer: no webhook

    async def pricer(self) -> dict:
        return await self._req("GET", "/rfq/pricer")

    async def executions(self, status: Optional[str] = None, limit: int = 500) -> list[dict]:
        q = f"/rfq/executions?limit={limit}" + (f"&status={status}" if status else "")
        return await self._req("GET", q) or []

    async def collateral(self) -> dict:
        return await self._req("GET", "/rfq/collateral")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def settlement_pnl(row: dict) -> Optional[float]:
    """Our P&L on one settled execution (we are the parlay's seller). None if not settled or unreadable."""
    if row.get("status") != "settled":
        return None
    wager, price = _num(row.get("wager")), _num(row.get("price"))
    if not wager or not price:
        return None
    liability = _num(row.get("liability"))
    if liability is None:
        liability = wager * (1 - price) / price
    pot = wager + liability
    result = str(row.get("result") or "").lower()
    scalar = {"win": 1.0, "loss": 0.0}.get(result)
    if result == "fmv":
        scalar = _num(row.get("fmv_value"))
    if result == "push":
        return 0.0
    if scalar is None:
        return None
    return round(pot * (1 - scalar) - liability, 2)


class NovigRfqQuoter(ResilientWebSocketFeed):
    venue = "novig_rfq"

    def __init__(self, cfg: ComboConfig, auth: NovigRfqAuth, host: str = PROD_HOST,
                 leg_lookup: Callable[[str], Optional[tuple[str, LegFair]]] = lambda oid: None,
                 research=None, allowed: Callable[[], Optional[str]] = lambda: None,
                 on_state_change: Optional[StateCallback] = None, clock=time.time, **kw: Any) -> None:
        if cfg.mode not in {"shadow", "qa", "live"}:
            raise ValueError("NOVIG_RFQ mode must be shadow, qa or live")
        super().__init__(host.replace("https://", "wss://").rstrip("/") + "/rfq/ws", on_state_change,
                         logger=log, **{"stale_after": 120.0, **kw})
        self.cfg = dataclasses.replace(cfg, maker_fee_rate=0.0)      # Novig quoters pay no fee
        self.auth = auth
        self.rest = NovigRfqRest(auth, host)
        self.leg_lookup = leg_lookup                                  # outcome id -> (event id, LegFair)
        self.pricer = ComboPricer(self.cfg, self._leg_fair)
        self.book = RiskBook(self.cfg)
        self.research = research
        self.allowed = allowed
        self.clock = clock
        self.rounds: dict[str, dict] = {}            # rfq_id -> {legs, price, wager, quoted, closed_at}
        self.positions: dict[str, dict] = {}         # rfq_id -> {price, wager, liability}
        self.stats: dict[str, int] = {}
        self._ws_conn = None

    # ------------------------------------------------------------ connection
    def _headers(self) -> Optional[dict[str, str]]:
        return {"Authorization": f"Bearer {self.auth.token}"} if self.auth.token else None

    async def run(self) -> None:
        await self.auth.ensure()
        await super().run()

    async def _send(self, event: str, data: Any) -> None:
        if self._ws_conn is not None:
            await self._ws_conn.send(json.dumps({"event": event, "data": data}, separators=(",", ":")))

    async def _on_open(self, ws) -> None:
        self._ws_conn = ws
        await self._send("subscribe", "rfq")
        if self.cfg.mode != "shadow":
            await self._send("subscribe", "quotes")       # without it every win is rejected
        log.info("NOVIG_RFQ connected (%s): subscribed rfq%s", self.cfg.mode,
                 "" if self.cfg.mode == "shadow" else " + quotes")

    def _clear_state(self) -> dict[str, int]:
        self._ws_conn = None
        return {"open_rounds": len(self.rounds)}

    # ------------------------------------------------------------ pricing
    def _leg_fair(self, leg: Leg) -> Optional[LegFair]:
        hit = self.leg_lookup(leg.market_ticker)
        return None if hit is None else hit[1]

    def _as_rfq(self, rid: str, outcome_ids: list[str], payout: float = 1.0) -> dict:
        legs = []
        for oid in outcome_ids:
            hit = self.leg_lookup(oid)
            legs.append({"market_ticker": oid, "event_ticker": hit[0] if hit else "", "side": "yes"})
        return {"id": rid, "mve_selected_legs": legs, "contracts": payout}

    def price_round(self, rid: str, outcome_ids: list[str], min_wager: float) -> ComboPrice:
        """Price one round; sets contracts (= payout) to what our caps allow. action SKIP explains a pass."""
        cp = self.pricer.price(self._as_rfq(rid, outcome_ids))
        if cp.action != "QUOTE":
            return cp
        price = math.ceil(cp.yes_price * 1000 - 1e-9) / 1000          # Novig rounds to 0.001: round up, not down
        if not 0 < price < 1:
            cp.action, cp.reason = "SKIP", "price out of range"
            return cp
        cp.yes_price, cp.no_bid = price, round(1 - price, 4)
        room = self.cfg.max_loss_per_combo * cp.size_factor
        room = min(room, self.cfg.max_total_liability - self.book.total())
        for leg in cp.legs:
            room = min(room, self.cfg.max_leg_exposure - self.book.leg_exposure(leg))
        max_wager = math.floor(max(room, 0.0) * price / (1 - price) * 100) / 100
        if max_wager < max(min_wager, 0.01):
            cp.action, cp.reason = "SKIP", f"caps allow ${max_wager:.2f} < round minimum ${min_wager:.2f}"
            return cp
        cp.contracts = round(max_wager / price, 4)                      # payout if the parlay hits
        cp.expected_profit = round(cp.contracts * (price - cp.fair), 4)
        cp.roc = round(cp.expected_profit / (cp.contracts * cp.no_bid), 5)
        if cp.roc < self.cfg.min_roc:
            cp.action, cp.reason = "SKIP", f"return on collateral {cp.roc:.2%} < {self.cfg.min_roc:.2%}"
        return cp

    # ------------------------------------------------------------ events
    async def _handle_raw(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        event, data = msg.get("event"), msg.get("data") or {}
        handler = {"rfq_created": self.on_rfq_created, "rfq_closed": self.on_rfq_closed,
                   "rfq_executed": self.on_rfq_executed, "quote_accepted": self.on_quote_accepted,
                   "quote_executed": self.on_quote_executed, "quote_dropped": self.on_quote_dropped}.get(event)
        if handler is not None and isinstance(data, dict):
            await handler(data)
        elif event == "error":
            log.warning("NOVIG_RFQ error: %s", data)
        self._expire_rounds()

    async def on_rfq_created(self, d: dict) -> None:
        rid = str(d.get("rfq_id"))
        oids = [str(o) for o in d.get("outcome_ids") or []]
        min_wager = _num(d.get("min_wager")) or 10.0
        cp = self.price_round(rid, oids, min_wager)
        live = self.cfg.mode != "shadow"
        if cp.action == "QUOTE" and live:
            gate = self.allowed() or self.pricer.live_allowed(cp)
            if gate:
                cp.action, cp.reason = "SKIP", gate
        self.rounds[rid] = dict(legs=oids, price=cp, quoted=False, created=self.clock(), closed_at=None)
        self._count(f"rfq_{cp.action.lower()}")
        self._write("COMBO_RFQ", venue="novig", rfq_id=rid, mode=self.cfg.mode, legs=oids, n_legs=len(oids),
                    action=cp.action, reason=cp.reason, fair=cp.fair, margin=cp.margin, yes_price=cp.yes_price,
                    max_wager=None if cp.action != "QUOTE" else round(cp.contracts * cp.yes_price, 2),
                    min_wager=min_wager, expected_profit=cp.expected_profit, roc=cp.roc, slice=cp.slice,
                    same_game=cp.same_game, uncertainty=cp.uncertainty, size_factor=cp.size_factor)
        if cp.action == "QUOTE" and live:
            wager = math.floor(cp.contracts * cp.yes_price * 100) / 100
            await self._send("create_quote", {"rfq_id": rid, "price": f"{cp.yes_price:.3f}",
                                              "max_wager": f"{wager:.2f}", "quote_id": f"q-{rid[:8]}"})
            self.rounds[rid]["quoted"] = True
            self.book.add(rid, cp)                         # reserved until the round resolves
            self._count("quotes_sent")
        elif live:
            await self._send("decline", {"rfq_id": rid})  # tells Novig we are alive, just passing

    async def on_rfq_closed(self, d: dict) -> None:
        r = self.rounds.get(str(d.get("rfq_id")))
        if r is not None:
            r["closed_at"] = self.clock()

    async def on_rfq_executed(self, d: dict) -> None:
        rid = str(d.get("rfq_id"))
        r = self.rounds.get(rid)
        traded = _num(d.get("price"))
        if r is None or traded is None:
            return
        cp: ComboPrice = r["price"]
        if cp.yes_price is None or cp.fair is None:
            return
        self._write("COMBO_TRADE", venue="novig", rfq_id=rid, mode=self.cfg.mode, traded_yes_price=traded,
                    our_yes_price=cp.yes_price, fair=cp.fair, wager=_num(d.get("wager")), slice=cp.slice,
                    would_win=cp.action == "QUOTE" and cp.yes_price <= traded + 1e-9,
                    margin_vs_winner=round(traded / cp.fair - 1, 4))

    async def on_quote_accepted(self, d: dict) -> None:
        """Last look: re-price every leg now; confirm only if the price still clears the margin and the caps."""
        started = time.monotonic()
        rid, qid = str(d.get("rfq_id")), str(d.get("quote_id"))
        wager, price = _num(d.get("wager")) or 0.0, _num(d.get("price")) or 0.0
        r = self.rounds.get(rid)
        ok, why = False, "unknown round"
        if r is not None and 0 < price < 1:
            fresh = self.pricer.price(self._as_rfq(rid, r["legs"]))
            if fresh.fair is None or fresh.reason.startswith(("stale", "no fair", "sharp books disagree")):
                why = f"legs no longer priceable ({fresh.reason})"
            elif price < fresh.fair * (1 + self.cfg.last_look_min_margin):
                why = "fair value moved against us"
            else:
                held: ComboPrice = dataclasses.replace(r["price"], contracts=wager / price, yes_price=price,
                                                       no_bid=round(1 - price, 4))
                why = self.allowed() or self.book.check(held, exclude=rid)
                ok = why is None
        await self._send("confirm", {"rfq_id": rid, "quote_id": qid, "confirmed": ok})
        elapsed = time.monotonic() - started
        self._count("confirmed" if ok else "declined_last_look")
        self._write("COMBO_CONFIRM" if ok else "COMBO_DECLINE", venue="novig", rfq_id=rid, quote_id=qid,
                    wager=wager, yes_price=price, reason=None if ok else why, ms=round(elapsed * 1000, 1))
        if elapsed > LAST_LOOK_BUDGET_S:
            log.warning("NOVIG_RFQ last look took %.0f ms (window 1000 ms)", elapsed * 1000)
        if not ok:
            self.book.remove(rid)

    async def on_quote_executed(self, d: dict) -> None:
        rid = str(d.get("rfq_id"))
        wager, price = _num(d.get("wager")) or 0.0, _num(d.get("price")) or 0.0
        if not 0 < price < 1:
            return
        r = self.rounds.get(rid) or {}
        cp: Optional[ComboPrice] = r.get("price")
        liability = round(wager * (1 - price) / price, 2)
        if cp is not None:
            self.book.add(rid, dataclasses.replace(cp, contracts=wager / price, yes_price=price,
                                                   no_bid=round(1 - price, 4), fee=0.0))
        self.positions[rid] = dict(price=price, wager=wager, liability=liability,
                                   fair=None if cp is None else cp.fair, slice=None if cp is None else cp.slice)
        self.rounds.pop(rid, None)
        self._count("executed")
        self._write("COMBO_FILL", venue="novig", rfq_id=rid, wager=wager, yes_price=price, liability=liability,
                    fair=None if cp is None else cp.fair, slice=None if cp is None else cp.slice)

    async def on_quote_dropped(self, d: dict) -> None:
        self._count(f"dropped_{d.get('reason')}")
        log.warning("NOVIG_RFQ quote on %s dropped: %s %s", d.get("rfq_id"), d.get("reason"),
                    d.get("min_wager") or d.get("required_collateral") or "")
        self.book.remove(str(d.get("rfq_id")))

    def _expire_rounds(self) -> None:
        now = self.clock()
        for rid, r in list(self.rounds.items()):
            closed = r.get("closed_at") or (r["created"] + 3.0)
            if now - closed > ACCEPT_GRACE_S:
                self.rounds.pop(rid, None)
                if rid not in self.positions:
                    self.book.remove(rid)

    # ------------------------------------------------------------ settlement
    async def check_results(self) -> int:
        if not self.positions:
            return 0
        done = 0
        for row in await self.rest.executions(status="settled"):
            rid = str(row.get("rfq_id"))
            pos = self.positions.get(rid)
            pnl = settlement_pnl(row)
            if pos is None or pnl is None:
                continue
            self._write("COMBO_RESULT", venue="novig", key=rid, position_type="position", result=row.get("result"),
                        pnl=pnl, slice=pos.get("slice"),
                        expected_profit=None if pos.get("fair") is None else
                        round(pos["wager"] / pos["price"] * (pos["price"] - pos["fair"]), 4))
            self.positions.pop(rid, None)
            self.book.remove(rid)
            done += 1
        return done

    async def results_loop(self, every_s: float = 300.0) -> None:
        while True:
            await asyncio.sleep(every_s)
            try:
                await self.check_results()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("NOVIG_RFQ results check failed")

    # ------------------------------------------------------------ helpers
    def _count(self, k: str) -> None:
        self.stats[k] = self.stats.get(k, 0) + 1

    def _write(self, kind: str, /, **fields) -> None:
        if self.research is not None:
            self.research.write(kind, **fields)


def auth_from_env(env) -> NovigRfqAuth:
    return NovigRfqAuth(env.get("NOVIG_RFQ_ACCESS_TOKEN"), env.get("NOVIG_RFQ_CLIENT_ID"),
                        env.get("NOVIG_RFQ_CLIENT_SECRET"), env.get("NOVIG_RFQ_TOKEN_URL"))


async def _cli(command: str, env) -> None:
    host = QA_HOST if (env.get("NOVIG_RFQ_ENV") or "qa").lower() == "qa" else PROD_HOST
    rest = NovigRfqRest(auth_from_env(env), env.get("NOVIG_RFQ_HOST") or host)
    try:
        if command == "register":
            print("pricer:", await rest.register())
        elif command == "status":
            print("pricer:", await rest.pricer())
            print("collateral:", await rest.collateral())
    finally:
        await rest.close()


if __name__ == "__main__":
    import argparse
    import os
    ap = argparse.ArgumentParser(description="Novig parlay pricer: register (once) or show status")
    ap.add_argument("command", choices=["register", "status"])
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(_cli(args.command, os.environ))
