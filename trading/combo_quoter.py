"""
combo_quoter.py — Quote Kalshi combos (parlays) through the RFQ system, with every pressure-test patch built in.

WHY
    Retail loses heavily on Kalshi combos (net ~$294M in 2026 before fees, implied margin ~14.7%) and only a
    handful of bots quote them. Combos are priced by Request For Quote: a user builds a combo, Kalshi broadcasts
    an RFQ, market makers answer with quotes, the user accepts the best, and the QUOTER CONFIRMS (last look)
    before anything executes.

MODES (COMBO_QUOTER)
    shadow  price every open RFQ, send nothing; log what we would have quoted, then compare with the price the
            combo actually traded at and with the result -> would we have won, at what margin, and did it pay?
    demo    real quotes on Kalshi's DEMO exchange (fake money): proves the quote/accept/confirm plumbing
    live    real quotes (TRADING_MODE=live), tiny caps by default

PRICING (per combo)
    * legs: each (market ticker, side). Fair probability from the sharp line when the leg maps to a game we track
      (preferred), else from Kalshi's own single-market book if its bid/ask spread is tight.
    * REJECT: > max legs; any leg without a fresh fair value; two legs on the same game (event ticker OR the game
      code embedded in Kalshi tickers, which catches a player prop + moneyline of one game); settlement beyond
      the horizon; requester YES price outside the price band.
    * fair = product of leg probabilities (legs independent by construction)
    * margin = base + per-leg (+ extra for legs priced only from Kalshi's own book) -> errors compound per leg
    * requester's YES price = fair x (1 + margin); we BUY NO at no_bid = 1 - that price (we are short the combo)
    * expected profit per contract = yes_price - fair - maker fee (assumed 0.0175 x P x (1-P), rounded up)
    * return on collateral = expected profit / no_bid; must clear COMBO_MIN_ROC (kills the 0.1c longshot trap)

RISK BOOK (demo / live)
    * max loss per combo, per leg (sum of every open combo containing that leg+side: popular legs concentrate
      risk), and in total; checked when quoting AND again at confirmation
    * quotes expire after COMBO_QUOTE_TTL_S; last look re-prices every leg with fresh data and declines if the
      combo's fair value moved against us or any leg went stale

ASSUMED (verify on demo first): the requester buying YES pays 1 - no_bid; quotes are for the full RFQ size;
maker fee on RFQ fills. Endpoints and fields are from Kalshi's published OpenAPI spec (/communications/*).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("trading.combos")

MAKER_FEE_RATE = 0.0175
_GAME_CODE = re.compile(r"^\d{2}[A-Z]{3}\d{2}")        # e.g. 26OCT01BOSNYK: date + teams, shared by one game's markets


def _num(v: Any) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _ts(v: Any) -> Optional[float]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def game_code(ticker: str) -> Optional[str]:
    """'KXNBAPTS-26OCT01BOSNYK-...' -> '26OCT01BOSNYK' (the date+teams segment shared by one game's markets)."""
    parts = (ticker or "").split("-")
    return parts[1] if len(parts) > 1 and _GAME_CODE.match(parts[1]) else None


@dataclass(frozen=True)
class Leg:
    market_ticker: str
    event_ticker: str
    side: str                                # "yes" | "no"


@dataclass
class LegFair:
    prob_yes: float                          # fair probability the leg's market resolves YES
    source: str                              # "sharp" | "kalshi_book"
    age_s: float
    game: Optional[tuple] = None             # canonical (league, home, away) when known

    def prob(self, side: str) -> float:
        return self.prob_yes if side == "yes" else 1.0 - self.prob_yes


@dataclass
class ComboConfig:
    mode: str = "shadow"                     # shadow | demo | live
    max_legs: int = 4
    base_margin: float = 0.04
    per_leg_margin: float = 0.02
    book_leg_extra_margin: float = 0.01      # legs priced only from Kalshi's own book are less certain
    min_price: float = 0.03                  # requester's YES price band
    max_price: float = 0.60
    min_roc: float = 0.01                    # expected profit / collateral, per quote
    max_leg_age_s: float = 30.0
    max_book_spread: float = 0.03
    max_horizon_h: float = 36.0
    max_loss_per_combo: float = 25.0
    max_leg_exposure: float = 150.0
    max_total_liability: float = 1000.0
    quote_ttl_s: float = 10.0
    last_look_min_margin: float = 0.02       # at confirmation the combo must still clear this margin on fresh fairs
    poll_s: float = 1.0
    followup_after_s: float = 60.0
    maker_fee_rate: float = MAKER_FEE_RATE


@dataclass
class ComboPrice:
    action: str                              # QUOTE | SKIP
    reason: str
    legs: list[Leg] = field(default_factory=list)
    fair: Optional[float] = None
    margin: Optional[float] = None
    yes_price: Optional[float] = None        # what the requester would pay for YES
    no_bid: Optional[float] = None           # what we pay for NO (our collateral per contract)
    contracts: int = 0
    fee: float = 0.0
    expected_profit: Optional[float] = None  # $ for the whole quote
    roc: Optional[float] = None
    sources: list[str] = field(default_factory=list)


def maker_fee(contracts: int, price: float, rate: float = MAKER_FEE_RATE) -> float:
    return math.ceil(rate * contracts * price * (1 - price) * 100 - 1e-9) / 100.0


def parse_legs(rfq: dict) -> list[Leg]:
    return [Leg(str(l.get("market_ticker")), str(l.get("event_ticker") or ""), str(l.get("side") or "yes").lower())
            for l in rfq.get("mve_selected_legs") or [] if isinstance(l, dict) and l.get("market_ticker")]


def rfq_contracts(rfq: dict, yes_price: Optional[float]) -> int:
    n = _num(rfq.get("contracts_fp")) or _num(rfq.get("contracts"))
    if n:
        return int(n)
    cost = _num(rfq.get("target_cost_dollars"))
    return int(cost / yes_price) if cost and yes_price else 0


class ComboPricer:
    def __init__(self, cfg: ComboConfig, leg_fair: Callable[[Leg], Optional[LegFair]]) -> None:
        self.cfg = cfg
        self.leg_fair = leg_fair

    def price(self, rfq: dict, expires_at: Optional[float] = None) -> ComboPrice:
        cfg = self.cfg
        legs = parse_legs(rfq)
        skip = lambda why, **kw: ComboPrice(action="SKIP", reason=why, legs=legs, **kw)  # noqa: E731
        if not legs:
            return skip("not a combo (no legs)")
        if len(legs) > cfg.max_legs:
            return skip(f"{len(legs)} legs > max {cfg.max_legs}")
        if expires_at is not None and expires_at - time.time() > cfg.max_horizon_h * 3600:
            return skip("settles beyond the horizon (capital tied up too long)")
        seen_events, seen_codes, seen_games = set(), set(), set()
        for leg in legs:
            code = game_code(leg.market_ticker) or game_code(leg.event_ticker)
            if leg.event_ticker in seen_events or (code and code in seen_codes):
                return skip("two legs on the same game (correlated)")
            seen_events.add(leg.event_ticker)
            if code:
                seen_codes.add(code)
        fair, sources, extra = 1.0, [], 0.0
        for leg in legs:
            lf = self.leg_fair(leg)
            if lf is None:
                return skip(f"no fair value for leg {leg.market_ticker}")
            if lf.age_s > cfg.max_leg_age_s:
                return skip(f"stale fair value for leg {leg.market_ticker} ({lf.age_s:.0f}s)")
            if lf.game is not None:
                if lf.game in seen_games:
                    return skip("two legs on the same game (correlated)")
                seen_games.add(lf.game)
            p = lf.prob(leg.side)
            if not 0 < p < 1:
                return skip(f"degenerate leg probability {p}")
            fair *= p
            sources.append(lf.source)
            if lf.source != "sharp":
                extra += cfg.book_leg_extra_margin
        margin = cfg.base_margin + cfg.per_leg_margin * len(legs) + extra
        yes_price = round(min(fair * (1 + margin), 0.99), 4)
        no_bid = round(1 - yes_price, 4)
        if not cfg.min_price <= yes_price <= cfg.max_price:
            return skip(f"price {yes_price:.4f} outside band", fair=fair, margin=margin, yes_price=yes_price,
                        no_bid=no_bid, sources=sources)
        contracts = rfq_contracts(rfq, yes_price)
        if contracts <= 0:
            return skip("no size", fair=fair, margin=margin, yes_price=yes_price, no_bid=no_bid, sources=sources)
        fee = maker_fee(contracts, no_bid, cfg.maker_fee_rate)
        expected = contracts * (yes_price - fair) - fee
        roc = expected / (contracts * no_bid)
        out = ComboPrice(action="QUOTE", reason="ok", legs=legs, fair=fair, margin=margin, yes_price=yes_price,
                         no_bid=no_bid, contracts=contracts, fee=fee, expected_profit=round(expected, 4),
                         roc=round(roc, 5), sources=sources)
        if roc < cfg.min_roc:
            out.action, out.reason = "SKIP", f"return on collateral {roc:.2%} < {cfg.min_roc:.2%}"
        return out


class RiskBook:
    """Open short-combo liabilities: per combo, per leg+side (popular legs concentrate risk), and in total."""

    def __init__(self, cfg: ComboConfig) -> None:
        self.cfg = cfg
        self.open: dict[str, tuple[list[Leg], float]] = {}          # quote/position id -> (legs, max loss $)

    def max_loss(self, cp: ComboPrice) -> float:
        return round(cp.contracts * cp.no_bid + cp.fee, 2)

    def leg_exposure(self, leg: Leg) -> float:
        return round(sum(loss for legs, loss in self.open.values() if leg in legs), 2)

    def total(self) -> float:
        return round(sum(loss for _, loss in self.open.values()), 2)

    def check(self, cp: ComboPrice, exclude: Optional[str] = None) -> Optional[str]:
        loss = self.max_loss(cp)
        if loss > self.cfg.max_loss_per_combo + 1e-9:
            return f"max loss ${loss:.2f} > ${self.cfg.max_loss_per_combo:.0f} per combo"
        others = {k: v for k, v in self.open.items() if k != exclude}
        if sum(l for _, l in others.values()) + loss > self.cfg.max_total_liability + 1e-9:
            return f"total liability would exceed ${self.cfg.max_total_liability:,.0f}"
        for leg in cp.legs:
            if sum(l for legs, l in others.values() if leg in legs) + loss > self.cfg.max_leg_exposure + 1e-9:
                return f"leg {leg.market_ticker} exposure would exceed ${self.cfg.max_leg_exposure:,.0f}"
        return None

    def add(self, key: str, cp: ComboPrice) -> None:
        self.open[key] = (list(cp.legs), self.max_loss(cp))

    def remove(self, key: str) -> None:
        self.open.pop(key, None)


class KalshiComms:
    """The RFQ endpoints on top of the signed Kalshi REST client (kalshi_trading.KalshiOrderGateway._request)."""

    def __init__(self, gateway) -> None:
        self.gw = gateway

    async def _ok(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        status, data = await self.gw._request(method, path, body)
        if status >= 400:
            raise RuntimeError(f"{method} {path}: HTTP {status}: {data}")
        return data

    async def open_rfqs(self) -> list[dict]:
        return (await self._ok("GET", "/communications/rfqs?status=open&limit=100") or {}).get("rfqs") or []

    async def create_quote(self, rfq_id: str, yes_bid: float, no_bid: float) -> str:
        data = await self._ok("POST", "/communications/quotes",
                              {"rfq_id": rfq_id, "yes_bid": f"{yes_bid:.4f}", "no_bid": f"{no_bid:.4f}",
                               "rest_remainder": False})
        return str((data or {}).get("id") or (data or {}).get("quote_id") or ((data or {}).get("quote") or {}).get("id"))

    async def get_quote(self, quote_id: str) -> dict:
        data = await self._ok("GET", f"/communications/quotes/{quote_id}")
        return (data or {}).get("quote") or data or {}

    async def delete_quote(self, quote_id: str) -> None:
        await self._ok("DELETE", f"/communications/quotes/{quote_id}")

    async def confirm_quote(self, quote_id: str) -> None:
        await self._ok("PUT", f"/communications/quotes/{quote_id}/confirm", {})

    async def market(self, ticker: str) -> dict:
        data = await self._ok("GET", f"/markets/{ticker}")
        return (data or {}).get("market") or data or {}

    async def trades(self, ticker: str, min_ts: Optional[float] = None) -> list[dict]:
        q = f"/markets/trades?ticker={ticker}&limit=100" + (f"&min_ts={int(min_ts)}" if min_ts else "")
        return (await self._ok("GET", q) or {}).get("trades") or []


class ComboQuoter:
    def __init__(self, cfg: ComboConfig, comms: KalshiComms, leg_fair: Callable[[Leg], Optional[LegFair]],
                 research=None, allowed: Callable[[], Optional[str]] = lambda: None, clock=time.time) -> None:
        self.cfg = cfg
        self.comms = comms
        self.pricer = ComboPricer(cfg, self._leg_fair_with_fallback(leg_fair))
        self.book = RiskBook(cfg)
        self.research = research
        self.allowed = allowed                           # global kill switches (daily loss stop, halts ...)
        self.clock = clock
        self.seen: set[str] = set()
        self.priced: dict[str, dict] = {}               # rfq_id -> what we priced (for the follow-up)
        self.quotes: dict[str, dict] = {}               # our quote_id -> {rfq, price, created}
        self.positions: dict[str, dict] = {}             # confirmed quote_id -> {price, ticker}
        self._book_cache: dict[str, tuple[float, Optional[LegFair]]] = {}
        self.stats: dict[str, int] = {}

    # ------------------------------------------------------------------ fair values
    def _leg_fair_with_fallback(self, primary):
        self._primary = primary

        def fair(leg: Leg) -> Optional[LegFair]:
            lf = primary(leg)
            if lf is not None:
                return lf
            hit = self._book_cache.get(leg.market_ticker)
            return None if hit is None else hit[1]
        return fair

    async def _refresh_book_fairs(self, legs: list[Leg]) -> None:
        """Kalshi single-market mid for legs the engine does not price (cached 5s), only if the book is tight."""
        now = self.clock()
        for leg in legs:
            if getattr(self, "_primary", None) is not None and self._primary(leg) is not None:
                continue                                  # the sharp line prices it: no API call needed
            cached = self._book_cache.get(leg.market_ticker)
            if cached is not None and now - cached[0] < 5:
                continue
            try:
                m = await self.comms.market(leg.market_ticker)
            except Exception as exc:  # noqa: BLE001
                log.debug("COMBO leg %s market fetch failed: %s", leg.market_ticker, exc)
                continue
            bid, ask = _num(m.get("yes_bid_dollars")), _num(m.get("yes_ask_dollars"))
            lf = None
            if bid and ask and 0 < bid < ask < 1 and ask - bid <= self.cfg.max_book_spread:
                lf = LegFair(prob_yes=(bid + ask) / 2, source="kalshi_book", age_s=0.0)
            self._book_cache[leg.market_ticker] = (now, lf)

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("COMBO step failed (continuing)")
            await asyncio.sleep(self.cfg.poll_s)

    async def step(self) -> None:
        rfqs = await self.comms.open_rfqs()
        for rfq in rfqs:
            rid = str(rfq.get("id"))
            if rid in self.seen:
                continue
            if len(self.seen) > 50_000:
                self.seen = {r for r in self.seen if r in self.priced or r == rid}
            self.seen.add(rid)
            await self.on_rfq(rfq)
        if self.cfg.mode != "shadow":
            await self._manage_quotes()
        await self._followup()

    async def on_rfq(self, rfq: dict) -> ComboPrice:
        legs = parse_legs(rfq)
        await self._refresh_book_fairs(legs)
        expires = None
        try:
            expires = _ts((await self.comms.market(rfq.get("market_ticker"))).get("expected_expiration_time"))
        except Exception:  # noqa: BLE001
            pass
        cp = self.pricer.price(rfq, expires)
        blocked = self.allowed()
        if cp.action == "QUOTE" and blocked:
            cp.action, cp.reason = "SKIP", blocked
        risk = self.book.check(cp) if cp.action == "QUOTE" and self.cfg.mode != "shadow" else None
        if risk:
            cp.action, cp.reason = "SKIP", risk
        self._count(f"rfq_{cp.action.lower()}")
        self._write("COMBO_RFQ", rfq_id=rfq.get("id"), market_ticker=rfq.get("market_ticker"), mode=self.cfg.mode,
                    legs=[[l.market_ticker, l.side] for l in legs], n_legs=len(legs), action=cp.action,
                    reason=cp.reason, fair=cp.fair, margin=cp.margin, yes_price=cp.yes_price, no_bid=cp.no_bid,
                    contracts=cp.contracts, expected_profit=cp.expected_profit, roc=cp.roc, sources=cp.sources,
                    created_ts=rfq.get("created_ts"))
        if cp.action == "QUOTE":
            self.priced[str(rfq.get("id"))] = dict(rfq=rfq, price=cp, at=self.clock(), followed=False)
            if self.cfg.mode in {"demo", "live"}:
                try:
                    qid = await self.comms.create_quote(str(rfq.get("id")), 0.0, cp.no_bid)
                except Exception as exc:  # noqa: BLE001
                    self._count("quote_rejected")
                    log.warning("COMBO quote on %s rejected: %s", rfq.get("id"), exc)
                    return cp
                self.quotes[qid] = dict(rfq=rfq, price=cp, created=self.clock())
                self.book.add(qid, cp)                 # reserve the liability while the quote can be accepted
                self._count("quotes_sent")
        return cp

    async def _manage_quotes(self) -> None:
        now = self.clock()
        for qid, q in list(self.quotes.items()):
            try:
                info = await self.comms.get_quote(qid)
            except Exception as exc:  # noqa: BLE001
                log.debug("COMBO quote %s status failed: %s", qid, exc)
                continue
            status = str(info.get("status") or "").lower()
            if status == "accepted":
                await self._last_look(qid, q, info)
            elif status in {"executed", "confirmed"}:
                self._executed(qid, q, info)
            elif status == "cancelled":
                self._drop(qid, "cancelled by venue/requester")
            elif now - q["created"] > self.cfg.quote_ttl_s:
                try:
                    await self.comms.delete_quote(qid)
                except Exception:  # noqa: BLE001
                    pass
                self._drop(qid, "expired (TTL)")

    async def _last_look(self, qid: str, q: dict, info: dict) -> None:
        """Re-price every leg NOW. Confirm only if the combo still clears the last-look margin on fresh data."""
        side = str(info.get("accepted_side") or "yes").lower()
        old: ComboPrice = q["price"]
        await self._refresh_book_fairs(old.legs)
        fresh = self.pricer.price({**q["rfq"]})
        ok = (side == "yes" and fresh.fair is not None and old.yes_price is not None
              and old.yes_price >= fresh.fair * (1 + self.cfg.last_look_min_margin)
              and not fresh.reason.startswith(("stale", "no fair")))
        blocked = self.allowed() or self.book.check(old, exclude=qid)
        if ok and not blocked:
            await self.comms.confirm_quote(qid)
            self._count("confirmed")
            self._write("COMBO_CONFIRM", quote_id=qid, rfq_id=q["rfq"].get("id"), yes_price=old.yes_price,
                        fair_then=old.fair, fair_now=fresh.fair, contracts=old.contracts)
        else:
            try:
                await self.comms.delete_quote(qid)
            except Exception:  # noqa: BLE001
                pass
            self._count("declined_last_look")
            self._write("COMBO_DECLINE", quote_id=qid, rfq_id=q["rfq"].get("id"), accepted_side=side,
                        yes_price=old.yes_price, fair_then=old.fair, fair_now=fresh.fair,
                        reason=blocked or ("fair value moved against us" if fresh.fair else fresh.reason))
            self._drop(qid, "declined at last look")

    def _executed(self, qid: str, q: dict, info: dict) -> None:
        cp: ComboPrice = q["price"]
        self.quotes.pop(qid, None)
        self.positions[qid] = dict(price=cp, ticker=q["rfq"].get("market_ticker"), at=self.clock())
        self._count("executed")
        self._write("COMBO_FILL", quote_id=qid, market_ticker=q["rfq"].get("market_ticker"), yes_price=cp.yes_price,
                    no_bid=cp.no_bid, contracts=cp.contracts, fair=cp.fair, expected_profit=cp.expected_profit,
                    max_loss=self.book.max_loss(cp))

    def _drop(self, qid: str, why: str) -> None:
        self.quotes.pop(qid, None)
        self.book.remove(qid)
        log.info("COMBO quote %s closed: %s", qid, why)

    # ------------------------------------------------------------------ follow-up: did we win, did it pay?
    async def _followup(self) -> None:
        now = self.clock()
        for rid, p in list(self.priced.items()):
            if p["followed"] or now - p["at"] < self.cfg.followup_after_s:
                continue
            p["followed"] = True
            cp: ComboPrice = p["price"]
            ticker = p["rfq"].get("market_ticker")
            try:
                trades = await self.comms.trades(ticker, min_ts=p["at"] - 5)
            except Exception as exc:  # noqa: BLE001
                log.debug("COMBO trades for %s failed: %s", ticker, exc)
                continue
            prices = [_num(t.get("yes_price_dollars")) for t in trades if _num(t.get("yes_price_dollars"))]
            traded = min(prices) if prices else None
            self._write("COMBO_TRADE", rfq_id=rid, market_ticker=ticker, mode=self.cfg.mode, traded_yes_price=traded,
                        our_yes_price=cp.yes_price, fair=cp.fair, n_trades=len(prices),
                        would_win=None if traded is None else cp.yes_price <= traded + 1e-9,
                        margin_vs_winner=None if traded is None or not cp.fair else round(traded / cp.fair - 1, 4))
            if traded is None or cp.yes_price > traded + 1e-9:
                self.priced.pop(rid, None)            # no trade, or ours would have lost: nothing to settle
            else:
                p["await_result"] = True

    async def check_results(self) -> int:
        """Settle would-have-won shadow quotes and real positions against the combo market's result."""
        done = 0
        items = [(k, v["rfq"].get("market_ticker"), v["price"], "shadow") for k, v in self.priced.items()
                 if v.get("await_result")]
        items += [(k, v["ticker"], v["price"], "position") for k, v in self.positions.items()]
        for key, ticker, cp, kind in items:
            try:
                m = await self.comms.market(ticker)
            except Exception:  # noqa: BLE001
                continue
            result = str(m.get("result") or "").lower()
            status = str(m.get("status") or "").lower()
            if result in {"yes", "no"}:
                won = result == "no"                  # we are short the combo (hold NO)
                pnl = cp.contracts * ((1 - cp.no_bid) if won else -cp.no_bid) - cp.fee
            elif status in {"settled", "finalized", "determined"}:
                # void / scalar settlement: value the NO side at the settlement value when given, else a refund
                value = _num(m.get("settlement_value_dollars"))
                pnl = 0.0 if value is None else cp.contracts * ((1 - value) - cp.no_bid) - cp.fee
                result = result or "void"
            else:
                continue
            self._write("COMBO_RESULT", key=key, position_type=kind, market_ticker=ticker, result=result, pnl=round(pnl, 2),
                        expected_profit=cp.expected_profit, contracts=cp.contracts, yes_price=cp.yes_price, fair=cp.fair)
            if kind == "shadow":
                self.priced.pop(key, None)
            else:
                self.positions.pop(key, None)
                self.book.remove(key)
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
                log.exception("COMBO results check failed")

    # ------------------------------------------------------------------ helpers
    def _count(self, k: str) -> None:
        self.stats[k] = self.stats.get(k, 0) + 1

    def _write(self, kind: str, /, **fields) -> None:
        if self.research is not None:
            self.research.write(kind, **fields)
