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

PRICING (per combo): the less sure we are, the wider the quote and the smaller the size
    * legs: each (market ticker, side). Fair probability from the sharp line when the leg maps to a game we track
      (preferred), else from Kalshi's own single-market book if its bid/ask spread is tight.
    * REJECT: > max legs; any leg without a fresh fair value; sharp books disagreeing on a leg by more than
      COMBO_MAX_DISAGREEMENT; settlement beyond the horizon; requester YES price outside the price band; our fair
      above a retail sportsbook's price for the same parlay (then OUR model is the likely error).
    * SAME-GAME legs (event ticker or the game code in Kalshi tickers) are priced as if independent plus a large
      per-leg margin, and are SHADOW-ONLY unless COMBO_LIVE_KINDS includes "sgp": their legs move together.
    * fair = product of leg probabilities
    * margin = base + per-leg + uncertainty, where uncertainty adds per leg for: the league (NFL lines are the
      sharpest), books disagreeing, a single book (no second opinion), the line's age, Kalshi-book pricing,
      same-game legs, and a requester with a winning record
    * fee: Kalshi charges combo makers half the taker fee (0.035 x P x (1-P)) since 2026-08-20, except parlays of
      NFL legs from different games. The fee is added on top of the margin, never taken out of it.
    * requester's YES price = fair x (1 + margin) + fee; we BUY NO at no_bid = 1 - that price (short the combo)
    * size: the max loss allowed per combo shrinks as uncertainty grows (down to COMBO_MIN_SIZE_FACTOR)
    * optional sportsbook price (a feed of retail parlay prices): when we are unsure we quote no tighter than it
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
import json
import dataclasses
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("trading.combos")

LEG_BLOCKED = object()       # leg_fair() result: the engine knows this leg and refuses it (live, cut off, line
                             # mismatch, no fresh sharp line). Never fall back to Kalshi's own book for it.
MAKER_FEE_RATE = 0.035                                   # combo makers: half the 0.07 taker rate (2026-08-20)
LEAGUE_LEG_MARGIN = {"NFL": 0.0, "NBA": 0.005, "MLB": 0.005, "NHL": 0.005, "WNBA": 0.01, "NCAAF": 0.01,
                     "NCAAB": 0.01, "TENNIS": 0.01}
OTHER_LEAGUE_MARGIN = 0.02
_SERIES_LEAGUE = (("NCAAF", "NCAAF"), ("NCAAMB", "NCAAB"), ("NCAAB", "NCAAB"), ("WNBA", "WNBA"), ("NFL", "NFL"),
                  ("NBA", "NBA"), ("MLB", "MLB"), ("NHL", "NHL"), ("ATP", "TENNIS"), ("WTA", "TENNIS"))
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


def league_of(ticker: str) -> str:
    """'KXNBAGAME-...' -> 'NBA', 'KXNCAAMBGAME-...' -> 'NCAAB'; 'OTHER' when the series is not a tracked league."""
    series = (ticker or "").split("-")[0].upper()
    series = series[2:] if series.startswith("KX") else series
    return next((lg for prefix, lg in _SERIES_LEAGUE if series.startswith(prefix)), "OTHER")


@dataclass
class LegFair:
    prob_yes: float                          # fair probability the leg's market resolves YES
    source: str                              # "sharp" | "kalshi_book"
    age_s: float
    game: Optional[tuple] = None             # canonical (league, home, away) when known
    spread: Optional[float] = None           # max - min fair probability across sharp books (None = unknown)
    books: Optional[int] = None              # how many sharp books priced it (1 = no second opinion)
    start: Optional[float] = None            # scheduled start of the leg's game (epoch s), when known

    def prob(self, side: str) -> float:
        return self.prob_yes if side == "yes" else 1.0 - self.prob_yes


@dataclass
class ComboConfig:
    mode: str = "shadow"                     # shadow | demo | live
    max_legs: int = 6                        # priced (shadow) up to this many legs
    live_max_legs: int = 4                   # quoted for real only up to this many
    base_margin: float = 0.04
    per_leg_margin: float = 0.02
    book_leg_extra_margin: float = 0.01      # legs priced only from Kalshi's own book are less certain
    min_price: float = 0.03                  # requester's YES price band
    max_price: float = 0.60
    min_roc: float = 0.01                    # expected profit / collateral, per quote
    max_leg_age_s: float = 30.0
    age_margin: float = 0.01                 # added per leg at max_leg_age_s (linear)
    max_disagreement: float = 0.03           # sharp books further apart than this on a leg -> do not quote
    disagreement_mult: float = 1.0           # margin added per leg = mult x spread / leg probability
    single_book_margin: float = 0.005        # per leg priced by only one sharp book
    league_margin: dict = field(default_factory=lambda: dict(LEAGUE_LEG_MARGIN))
    other_league_margin: float = OTHER_LEAGUE_MARGIN
    same_game_margin: float = 0.08           # per extra leg in the same game (correlation unknown)
    live_kinds: tuple = ("xgame",)           # what may be quoted for real: "xgame" (different games), "sgp"
    live_leagues: Optional[tuple] = None     # None = every league we can price
    fee_exempt_leagues: tuple = ("NFL",)     # cross-game parlays of only these leagues pay no maker fee
    uncertainty_full_cut: float = 0.08       # uncertainty margin at which size falls to min_size_factor
    min_size_factor: float = 0.25
    sportsbook_discount: float = 0.03        # when unsure: quote no tighter than the sportsbook price x (1 - this)
    sportsbook_when_uncertain: float = 0.03  # "unsure" = uncertainty margin at least this (or same-game)
    requester_min_trades: int = 15           # requester record needed before it counts
    requester_widen_roi: float = 0.05        # requester's taker ROI above this -> widen
    requester_widen_margin: float = 0.04
    requester_block_roi: float = 0.15        # ... above this -> decline
    max_book_spread: float = 0.03
    min_book_size: float = 100.0             # contracts on BOTH sides of a Kalshi book before its mid is used
    live_book_legs: bool = False             # legs priced only from Kalshi's own book: shadow-only by default
                                             # (a thin book can be painted to move its mid against us)
    max_horizon_h: float = 36.0
    max_loss_per_combo: float = 25.0
    max_leg_exposure: float = 150.0
    max_total_liability: float = 1000.0
    max_game_exposure: float = 300.0         # every open short parlay with a leg in one game, summed
    settle_after_start_h: float = 4.0        # a leg's game is decided this long after its start (horizon check)
    book_fetches_per_step: int = 10          # REST budget: Kalshi single-market lookups per poll
    followups_per_step: int = 10             # REST budget: trade-price follow-ups per poll
    results_per_check: int = 50              # REST budget: settlement lookups per results check
    max_open_quotes: int = 20                # Kalshi: each open quote is polled every step (REST budget, and a
                                             # win must be seen inside the confirm window)
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
    slice: str = ""                          # e.g. "NFL:2L:x", "NBA+NHL:3L:x", "NBA:2L:sgp"
    leagues: list[str] = field(default_factory=list)
    same_game: bool = False
    fee_exempt: bool = False
    uncertainty: float = 0.0                 # the uncertainty part of the margin
    size_factor: float = 1.0                 # share of max_loss_per_combo this quote may use
    sportsbook_yes: Optional[float] = None


def maker_fee(contracts: int, price: float, rate: float = MAKER_FEE_RATE) -> float:
    return math.ceil(rate * contracts * price * (1 - price) * 100 - 1e-9) / 100.0


def parse_legs(rfq: dict) -> list[Leg]:
    return [Leg(str(l.get("market_ticker")), str(l.get("event_ticker") or ""), str(l.get("side") or "").lower())
            for l in rfq.get("mve_selected_legs") or [] if isinstance(l, dict) and l.get("market_ticker")]


def rfq_contracts(rfq: dict, yes_price: Optional[float]) -> int:
    n = _num(rfq.get("contracts_fp")) or _num(rfq.get("contracts"))
    if n:
        return int(n)
    cost = _num(rfq.get("target_cost_dollars"))
    return int(cost / yes_price) if cost and yes_price else 0


class RequesterBook:
    """How each RFQ creator has done as a TAKER against our prices (real fills and would-have-won shadow quotes).
    Sportsbooks limit winners; we cannot refuse anyone, so we widen or decline instead."""

    def __init__(self, cfg: ComboConfig, path=None) -> None:
        self.cfg = cfg
        self.record: dict[str, list[float]] = {}           # creator id -> [n, taker pnl $, taker stake $]
        self.path = None
        if path is not None:
            self.attach(path)

    def attach(self, path) -> None:
        """Keep the record on disk: a restart must not wipe what we learned about sharp requesters."""
        from pathlib import Path
        import json as _json
        self.path = Path(path)
        try:
            self.record.update({k: list(v) for k, v in _json.loads(self.path.read_text()).items()})
        except (OSError, ValueError):
            pass

    def add(self, creator: Optional[str], taker_pnl: float, taker_stake: float) -> None:
        if not creator:
            return
        r = self.record.setdefault(creator, [0, 0.0, 0.0])
        r[0] += 1
        r[1] += taker_pnl
        r[2] += taker_stake
        if self.path is not None:
            import json as _json
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(_json.dumps(self.record))
                tmp.replace(self.path)
            except OSError as exc:
                log.warning("COMBO requester record not saved: %s", exc)

    def roi(self, creator: Optional[str]) -> Optional[float]:
        r = self.record.get(creator or "")
        if not r or r[0] < self.cfg.requester_min_trades or r[2] <= 0:
            return None
        return r[1] / r[2]

    def adjust(self, creator: Optional[str]) -> tuple[float, Optional[str]]:
        roi = self.roi(creator)
        if roi is None:
            return 0.0, None
        if roi > self.cfg.requester_block_roi:
            return 0.0, f"requester wins {roi:.0%} on our prices"
        return (self.cfg.requester_widen_margin, None) if roi > self.cfg.requester_widen_roi else (0.0, None)


class ComboPricer:
    def __init__(self, cfg: ComboConfig, leg_fair: Callable[[Leg], Optional[LegFair]],
                 sportsbook_price: Optional[Callable[[list], Optional[float]]] = None,
                 requesters: Optional[RequesterBook] = None) -> None:
        self.cfg = cfg
        self.leg_fair = leg_fair
        self.sportsbook_price = sportsbook_price          # retail book's YES price for the same parlay, if known
        self.requesters = requesters or RequesterBook(cfg)

    def price(self, rfq: dict, expires_at: Optional[float] = None) -> ComboPrice:
        cfg = self.cfg
        legs = parse_legs(rfq)
        skip = lambda why, **kw: ComboPrice(action="SKIP", reason=why, legs=legs, **kw)  # noqa: E731
        if not legs:
            return skip("not a combo (no legs)")
        if any(leg.side not in {"yes", "no"} for leg in legs):
            return skip("a leg has no clear side (yes/no)")
        if len(legs) > cfg.max_legs:
            return skip(f"{len(legs)} legs > max {cfg.max_legs}")
        if expires_at is not None and expires_at - time.time() > cfg.max_horizon_h * 3600:
            return skip("settles beyond the horizon (capital tied up too long)")
        fairs: list[LegFair] = []
        for leg in legs:
            lf = self.leg_fair(leg)
            if lf is None or lf is LEG_BLOCKED:
                return skip(f"no fair value for leg {leg.market_ticker}")
            if lf.age_s > cfg.max_leg_age_s:
                return skip(f"stale fair value for leg {leg.market_ticker} ({lf.age_s:.0f}s)")
            if lf.spread is not None and lf.spread > cfg.max_disagreement + 1e-12:
                return skip(f"sharp books disagree by {lf.spread:.3f} on leg {leg.market_ticker}")
            fairs.append(lf)
        if expires_at is None and fairs and all(lf.start for lf in fairs):
            expires_at = max(lf.start for lf in fairs) + cfg.settle_after_start_h * 3600
            if expires_at - time.time() > cfg.max_horizon_h * 3600:
                return skip("settles beyond the horizon (capital tied up too long)")
        # which legs share a game: event ticker, the game code inside Kalshi tickers, or the canonical game
        groups: list[set] = []
        for leg, lf in zip(legs, fairs):
            ids = {("e", leg.event_ticker)} if leg.event_ticker else set()
            code = game_code(leg.market_ticker) or game_code(leg.event_ticker)
            if code:
                ids.add(("c", code))
            if lf.game is not None:
                ids.add(("g", tuple(lf.game)))
            ids.add(("leg", leg.market_ticker, leg.side))
            merged = set(ids)
            for g in [g for g in groups if g & ids]:        # a leg can join two groups: merge them all
                merged |= g
                groups.remove(g)
            groups.append(merged)
        same_game_extra = len(legs) - len(groups)
        same_game = same_game_extra > 0
        leagues = [lf.game[0] if lf.game else league_of(leg.market_ticker) for leg, lf in zip(legs, fairs)]
        fair, sources, uncertainty = 1.0, [], 0.0
        for leg, lf, league in zip(legs, fairs, leagues):
            p = lf.prob(leg.side)
            if not 0 < p < 1:
                return skip(f"degenerate leg probability {p}")
            fair *= p
            sources.append(lf.source)
            uncertainty += cfg.league_margin.get(league, cfg.other_league_margin)
            uncertainty += cfg.age_margin * min(1.0, lf.age_s / max(cfg.max_leg_age_s, 1e-9))
            if lf.source != "sharp":
                uncertainty += cfg.book_leg_extra_margin
            if lf.spread is not None:
                uncertainty += cfg.disagreement_mult * lf.spread / p
            if lf.books == 1:
                uncertainty += cfg.single_book_margin
        uncertainty += cfg.same_game_margin * same_game_extra
        req_add, req_block = self.requesters.adjust(rfq.get("creator_id"))
        if req_block:
            return skip(req_block)
        uncertainty += req_add
        margin = cfg.base_margin + cfg.per_leg_margin * len(legs) + uncertainty
        fee_exempt = not same_game and all(lg in cfg.fee_exempt_leagues for lg in leagues)
        rate = 0.0 if fee_exempt else cfg.maker_fee_rate
        base_yes = fair * (1 + margin)
        yes_price = base_yes + rate * base_yes * (1 - base_yes)          # the fee goes on top of the margin
        sb = self.sportsbook_price(legs) if self.sportsbook_price is not None else None
        if sb is not None:
            if fair > sb:
                return skip("our fair is above the sportsbook's price (model likely wrong)", fair=fair,
                            sportsbook_yes=sb)
            if same_game or uncertainty >= cfg.sportsbook_when_uncertain:
                yes_price = max(yes_price, sb * (1 - cfg.sportsbook_discount))
        yes_price = min(math.ceil(yes_price * 10_000 - 1e-6) / 10_000, 0.99)   # round UP: never the user's way
        no_bid = round(1 - yes_price, 4)
        span = max(cfg.uncertainty_full_cut, 1e-9)
        size_factor = round(max(cfg.min_size_factor, 1 - (1 - cfg.min_size_factor) * min(1.0, uncertainty / span)), 3)
        tags = "+".join(sorted(set(leagues)))
        info = dict(fair=fair, margin=margin, yes_price=yes_price, no_bid=no_bid, sources=sources, leagues=leagues,
                    slice=f"{tags}:{len(legs)}L:{'sgp' if same_game else 'x'}", same_game=same_game,
                    fee_exempt=fee_exempt, uncertainty=round(uncertainty, 4), size_factor=size_factor,
                    sportsbook_yes=sb)
        if not cfg.min_price <= yes_price <= cfg.max_price:
            return skip(f"price {yes_price:.4f} outside band", **info)
        contracts = rfq_contracts(rfq, yes_price)
        if contracts <= 0:
            return skip("no size", **info)
        fee = maker_fee(contracts, no_bid, rate)
        expected = contracts * (yes_price - fair) - fee
        roc = expected / (contracts * no_bid)
        out = ComboPrice(action="QUOTE", reason="ok", legs=legs, contracts=contracts, fee=fee,
                         expected_profit=round(expected, 4), roc=round(roc, 5), **info)
        if roc < cfg.min_roc:
            out.action, out.reason = "SKIP", f"return on collateral {roc:.2%} < {cfg.min_roc:.2%}"
        return out

    def live_allowed(self, cp: ComboPrice) -> Optional[str]:
        """None if this slice may be quoted for real; otherwise why it stays shadow-only."""
        if not self.cfg.live_book_legs and any(src != "sharp" for src in cp.sources):
            return f"slice {cp.slice} is shadow-only (a leg priced from Kalshi's own book)"
        if len(cp.legs) > self.cfg.live_max_legs:
            return f"slice {cp.slice} is shadow-only (more than {self.cfg.live_max_legs} legs)"
        kind = "sgp" if cp.same_game else "xgame"
        if kind not in self.cfg.live_kinds:
            return f"slice {cp.slice} is shadow-only ({kind})"
        if self.cfg.live_leagues is not None and not set(cp.leagues) <= set(self.cfg.live_leagues):
            return f"slice {cp.slice} is shadow-only (league)"
        return None


def leg_game(leg: Leg) -> str:
    """The game a leg belongs to: the date+teams code inside Kalshi tickers, else the event (Novig: event id)."""
    return game_code(leg.market_ticker) or game_code(leg.event_ticker) or leg.event_ticker or leg.market_ticker


class RiskBook:
    """Open short-combo liabilities: per combo, per leg+side (popular legs concentrate risk), per game (one game
    decides every parlay holding any of its legs), and in total."""

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
        cap = self.cfg.max_loss_per_combo * cp.size_factor              # unsure -> smaller
        if loss > cap + 1e-9:
            return f"max loss ${loss:.2f} > ${cap:.2f} per combo (size factor {cp.size_factor:g})"
        others = {k: v for k, v in self.open.items() if k != exclude}
        if sum(l for _, l in others.values()) + loss > self.cfg.max_total_liability + 1e-9:
            return f"total liability would exceed ${self.cfg.max_total_liability:,.0f}"
        for leg in cp.legs:
            if sum(l for legs, l in others.values() if leg in legs) + loss > self.cfg.max_leg_exposure + 1e-9:
                return f"leg {leg.market_ticker} exposure would exceed ${self.cfg.max_leg_exposure:,.0f}"
        for game in {leg_game(l) for l in cp.legs}:
            held = sum(l for legs, l in others.values() if any(leg_game(x) == game for x in legs))
            if held + loss > self.cfg.max_game_exposure + 1e-9:
                return f"game {game} exposure would exceed ${self.cfg.max_game_exposure:,.0f}"
        return None

    def game_exposure(self, game: str) -> float:
        return round(sum(l for legs, l in self.open.values() if any(leg_game(x) == game for x in legs)), 2)

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

    async def get_rfq(self, rfq_id: str) -> dict:
        data = await self._ok("GET", f"/communications/rfqs/{rfq_id}")
        return (data or {}).get("rfq") or data or {}

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

    async def positions(self) -> list[dict]:
        data = await self._ok("GET", "/portfolio/positions?limit=1000&count_filter=position")
        return (data or {}).get("market_positions") or []

    async def market(self, ticker: str) -> dict:
        data = await self._ok("GET", f"/markets/{ticker}")
        return (data or {}).get("market") or data or {}

    async def trades(self, ticker: str, min_ts: Optional[float] = None) -> list[dict]:
        q = f"/markets/trades?ticker={ticker}&limit=100" + (f"&min_ts={int(min_ts)}" if min_ts else "")
        return (await self._ok("GET", q) or {}).get("trades") or []


class KalshiCommsFeed:
    """
    Kalshi's `communications` websocket: rfq_created (every member), quote_accepted / quote_executed (ours only).
    Pushes instead of polling: a win reaches the last look in milliseconds, and RFQs are not sampled 100 at a time.
    `shard_factor` N delivers 1/N of all RFQs to this connection (Kalshi sees ~100+/s; each one we price may need
    a REST call), so volume is chosen, not imposed. Message fields per Kalshi's docs (verify on demo).
    """

    def __init__(self, url: str, key_id: str, private_key, shard_factor: int = 10, shard_key: int = 0) -> None:
        from ws_base import ResilientWebSocketFeed

        outer = self

        class _Feed(ResilientWebSocketFeed):
            venue = "kalshi_rfq"

            def _headers(self):
                from kalshi_feed import WS_SIGN_PATH, auth_headers
                return auth_headers(key_id, private_key, "GET", WS_SIGN_PATH)

            async def _on_open(self, ws):
                params: dict[str, Any] = {"channels": ["communications"]}
                if shard_factor > 1:
                    params.update(shard_factor=shard_factor, shard_key=shard_key)
                await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": params}))
                log.info("COMBO websocket subscribed to communications (shard %d/%d)", shard_key, shard_factor)

            async def _handle_raw(self, raw):
                await outer.dispatch(raw)

        self.feed = _Feed(url, None, stale_after=120.0)
        self.handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}

    @property
    def connected(self) -> bool:
        return self.feed.connected.is_set()

    async def dispatch(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        handler = self.handlers.get(str(msg.get("type") or ""))
        if handler is not None and isinstance(msg.get("msg"), dict):
            try:
                await handler(msg["msg"])
            except Exception:  # noqa: BLE001
                log.exception("COMBO websocket handler failed (continuing)")

    async def run(self) -> None:
        await self.feed.run()


class ComboQuoter:
    def __init__(self, cfg: ComboConfig, comms: KalshiComms, leg_fair: Callable[[Leg], Optional[LegFair]],
                 research=None, allowed: Callable[[], Optional[str]] = lambda: None, clock=time.time,
                 sportsbook_price: Optional[Callable[[list], Optional[float]]] = None,
                 ws: Optional[KalshiCommsFeed] = None) -> None:
        self.cfg = cfg
        self.comms = comms
        self.requesters = RequesterBook(cfg)
        self.pricer = ComboPricer(cfg, self._leg_fair_with_fallback(leg_fair), sportsbook_price, self.requesters)
        self.book = RiskBook(cfg)
        self.research = research
        self.allowed = allowed                           # global kill switches (daily loss stop, halts ...)
        self.on_settled: Callable[[float, str], None] = lambda pnl, key: None   # real P&L -> daily loss stop
        self.clock = clock
        self.seen: set[str] = set()
        self.priced: dict[str, dict] = {}               # rfq_id -> what we priced (for the follow-up)
        self.quotes: dict[str, dict] = {}               # our quote_id -> {rfq, price, created}
        self.positions: dict[str, dict] = {}             # confirmed quote_id -> {price, ticker}
        self._book_cache: dict[str, tuple[float, Optional[LegFair]]] = {}
        self._fetch_budget = cfg.book_fetches_per_step
        self.stats: dict[str, int] = {}
        self.ws = ws
        if ws is not None:
            ws.handlers.update(rfq_created=self.on_ws_rfq, quote_accepted=self.on_ws_accepted,
                               quote_executed=self.on_ws_executed)

    # ------------------------------------------------------------------ fair values
    def _leg_fair_with_fallback(self, primary):
        self._primary = primary

        def fair(leg: Leg) -> Optional[LegFair]:
            lf = primary(leg)
            if lf is LEG_BLOCKED:
                return None                               # known to the engine and refused: no book fallback
            if lf is not None:
                return lf
            hit = self._book_cache.get(leg.market_ticker)
            if hit is None or hit[1] is None:
                return None
            age = self.clock() - hit[0]
            return dataclasses.replace(hit[1], age_s=age)  # its real age: stale mids fail max_leg_age_s
        return fair

    async def _refresh_book_fairs(self, legs: list[Leg]) -> None:
        """Kalshi single-market mid for legs the engine does not price (cached 5s; 60s when unpriceable), only if
        the book is tight. At most `book_fetches_per_step` lookups per poll: at ~100 RFQs a second, unbudgeted
        lookups of player props would exhaust Kalshi's rate limit and starve the quotes themselves."""
        now = self.clock()
        for leg in legs:
            if getattr(self, "_primary", None) is not None and self._primary(leg) is not None:
                continue                                  # the sharp line prices it (or the engine refuses it)
            cached = self._book_cache.get(leg.market_ticker)
            if cached is not None and now - cached[0] < (5 if cached[1] is not None else 60):
                continue
            if self._fetch_budget <= 0:
                return
            self._fetch_budget -= 1
            try:
                m = await self.comms.market(leg.market_ticker)
            except Exception as exc:  # noqa: BLE001
                log.debug("COMBO leg %s market fetch failed: %s", leg.market_ticker, exc)
                continue
            bid, ask = _num(m.get("yes_bid_dollars")), _num(m.get("yes_ask_dollars"))
            bid_n, ask_n = _num(m.get("yes_bid_size_fp")), _num(m.get("yes_ask_size_fp"))
            deep = (bid_n is None or bid_n >= self.cfg.min_book_size) and (ask_n is None or ask_n >= self.cfg.min_book_size)
            lf = None
            if bid and ask and 0 < bid < ask < 1 and ask - bid <= self.cfg.max_book_spread and deep:
                # the width counts as disagreement, so a wider book adds margin
                lf = LegFair(prob_yes=(bid + ask) / 2, source="kalshi_book", age_s=0.0, spread=ask - bid)
            self._book_cache[leg.market_ticker] = (now, lf)

    async def restore(self) -> float:
        """After a restart: every short parlay we still hold counts toward the caps again, with its legs (read from
        the combo market) so the per-leg and per-game caps include it."""
        if self.cfg.mode == "shadow" or not hasattr(self.comms, "positions"):
            return 0.0
        total = 0.0
        for p in await self.comms.positions():
            ticker = str(p.get("ticker") or "")
            held = _num(p.get("position_fp")) if p.get("position_fp") is not None else _num(p.get("position"))
            if not ticker.startswith("KXMVE") or not held or held >= 0:
                continue
            exposure = _num(p.get("market_exposure_dollars")) or 0.0
            key = f"restored-{ticker}"
            if key in self.book.open:
                continue
            legs: list[Leg] = []
            try:                                       # the combo market lists its legs: per-leg/game caps see it
                legs = parse_legs(await self.comms.market(ticker))
            except Exception as exc:  # noqa: BLE001
                log.warning("COMBO restored %s without its legs (%s): total cap only", ticker, exc)
            self.book.open[key] = (legs, exposure)
            n = abs(held)
            no = min(max(exposure / n, 0.0001), 0.9999)
            cp = ComboPrice(action="QUOTE", reason="restored", contracts=n, no_bid=round(no, 4),
                            yes_price=round(1 - no, 4), fee=0.0, slice="restored")
            self.positions[key] = dict(price=cp, ticker=ticker, at=self.clock(), creator=None)   # settles normally
            total += exposure
        if total:
            log.warning("COMBO restored $%.2f of open short-parlay liability from Kalshi", total)
        return total

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        try:
            await self.restore()
        except Exception as exc:  # noqa: BLE001
            log.critical("COMBO could not restore open parlay liability (%s): quoting anyway would ignore it; "
                         "stopping the combo quoter", exc)
            return
        ws_task = asyncio.get_running_loop().create_task(self.ws.run()) if self.ws is not None else None
        try:
            while True:
                try:
                    await self.step()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("COMBO step failed (continuing)")
                await asyncio.sleep(self.cfg.poll_s)
        finally:
            if ws_task is not None:
                ws_task.cancel()

    def _push_mode(self) -> bool:
        return self.ws is not None and self.ws.connected

    async def step(self) -> None:
        self._fetch_budget = self.cfg.book_fetches_per_step
        # with the websocket up, RFQs and wins arrive as events; polling is the fallback when it is down
        rfqs = [] if self._push_mode() else await self.comms.open_rfqs()
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

    # ---------------------------------------------------------------- websocket events
    async def on_ws_rfq(self, msg: dict) -> None:
        rid = str(msg.get("rfq_id") or msg.get("id") or "")
        if not rid or rid in self.seen:
            return
        self.seen.add(rid)
        rfq = {**msg, "id": rid}
        if not rfq.get("mve_selected_legs"):             # the event may not carry the legs: read the RFQ once
            if self._fetch_budget <= 0:
                self._count("rfq_skipped_budget")
                return
            self._fetch_budget -= 1
            try:
                rfq = {**rfq, **await self.comms.get_rfq(rid), "id": rid}
            except Exception as exc:  # noqa: BLE001
                log.debug("COMBO rfq %s lookup failed: %s", rid, exc)
                return
        await self.on_rfq(rfq)

    async def on_ws_accepted(self, msg: dict) -> None:
        qid = str(msg.get("quote_id") or "")
        q = self.quotes.get(qid)
        if q is not None and not q.get("confirm_sent"):
            await self._last_look(qid, q, msg)            # milliseconds after the accept, not on the next poll

    async def on_ws_executed(self, msg: dict) -> None:
        qid = str(msg.get("quote_id") or "")
        q = self.quotes.get(qid)
        if q is not None:
            self._executed(qid, q, msg)

    async def on_rfq(self, rfq: dict) -> ComboPrice:
        legs = parse_legs(rfq)
        await self._refresh_book_fairs(legs)
        cp = self.pricer.price(rfq)                    # horizon from the legs' start times when all are known
        if cp.action == "QUOTE" and not all((self.pricer.leg_fair(l) or LegFair(0.5, "", 0)).start for l in legs):
            expires = None                             # some leg has no start time: ask Kalshi when it settles
            if self._fetch_budget > 0:
                self._fetch_budget -= 1
                try:
                    expires = _ts((await self.comms.market(rfq.get("market_ticker"))).get("expected_expiration_time"))
                except Exception:  # noqa: BLE001
                    expires = None
            cp = self.pricer.price(rfq, expires) if expires is not None else dataclasses.replace(
                cp, action="SKIP", reason="settlement time unknown (horizon cannot be checked)")
        blocked = self.allowed()
        if cp.action == "QUOTE" and blocked:
            cp.action, cp.reason = "SKIP", blocked
        if cp.action == "QUOTE" and self.cfg.mode != "shadow":
            gate = self.pricer.live_allowed(cp) or self.book.check(cp) or (
                f"{len(self.quotes)} quotes already open (max {self.cfg.max_open_quotes})"
                if len(self.quotes) >= self.cfg.max_open_quotes else None)
            if gate:
                cp.action, cp.reason = "SKIP", gate
        self._count(f"rfq_{cp.action.lower()}")
        self._write("COMBO_RFQ", rfq_id=rfq.get("id"), market_ticker=rfq.get("market_ticker"), mode=self.cfg.mode,
                    legs=[[l.market_ticker, l.side] for l in legs], n_legs=len(legs), action=cp.action,
                    reason=cp.reason, fair=cp.fair, margin=cp.margin, yes_price=cp.yes_price, no_bid=cp.no_bid,
                    contracts=cp.contracts, expected_profit=cp.expected_profit, roc=cp.roc, sources=cp.sources,
                    created_ts=rfq.get("created_ts"), slice=cp.slice, same_game=cp.same_game,
                    fee_exempt=cp.fee_exempt, uncertainty=cp.uncertainty, size_factor=cp.size_factor,
                    sportsbook_yes=cp.sportsbook_yes, creator_id=rfq.get("creator_id"))
        if cp.action == "QUOTE":
            self.priced[str(rfq.get("id"))] = dict(rfq=rfq, price=cp, at=self.clock(), followed=False, sent=False)
            if self.cfg.mode in {"demo", "live"}:
                try:
                    qid = await self.comms.create_quote(str(rfq.get("id")), 0.0, cp.no_bid)
                except Exception as exc:  # noqa: BLE001
                    self._count("quote_rejected")
                    log.warning("COMBO quote on %s rejected: %s", rfq.get("id"), exc)
                    return cp
                # no reservation yet: most quotes never win, and reserving each one's full liability would cap
                # how many we can have out at once. The caps are enforced at the last look, where the liability
                # becomes real (and one event loop confirms one at a time, so two wins cannot both slip under).
                self.quotes[qid] = dict(rfq=rfq, price=cp, created=self.clock())
                self.priced[str(rfq.get("id"))]["sent"] = True   # a real quote settles as a position, not shadow
                self._count("quotes_sent")
        return cp

    async def _manage_quotes(self) -> None:
        now = self.clock()
        push = self._push_mode()
        for qid, q in list(self.quotes.items()):
            if push and not q.get("confirm_sent"):     # wins arrive as events: only expire stale quotes here
                if now - q["created"] > self.cfg.quote_ttl_s:
                    try:
                        await self.comms.delete_quote(qid)
                    except Exception:  # noqa: BLE001
                        pass
                    self._drop(qid, "expired (TTL)")
                continue
            try:                                       # one quote's failure never stops the others
                info = await self.comms.get_quote(qid)
                status = str(info.get("status") or "").lower()
                if status == "accepted" and not q.get("confirm_sent"):
                    await self._last_look(qid, q, info)
                elif status == "executed":
                    self._executed(qid, q, info)
                elif status in {"cancelled", "canceled", "expired"}:
                    self._drop(qid, f"{status} by venue/requester")
                elif q.get("confirm_sent"):
                    pass                               # confirmed: waiting for execution; reservation stays
                elif now - q["created"] > self.cfg.quote_ttl_s:
                    try:
                        await self.comms.delete_quote(qid)
                    except Exception:  # noqa: BLE001
                        pass
                    self._drop(qid, "expired (TTL)")
            except Exception as exc:  # noqa: BLE001
                log.warning("COMBO quote %s handling failed (continuing with the others): %s", qid, exc)

    async def _last_look(self, qid: str, q: dict, info: dict) -> None:
        """Re-price every leg NOW. Confirm only if the combo still clears the last-look margin on fresh data."""
        side = str(info.get("accepted_side") or "yes").lower()
        old: ComboPrice = q["price"]
        await self._refresh_book_fairs(old.legs)
        fresh = self.pricer.price({**q["rfq"]})
        net_yes = None if old.yes_price is None else old.yes_price - (old.fee / old.contracts if old.contracts else 0)
        ok = (side == "yes" and fresh.fair is not None and net_yes is not None
              and net_yes >= fresh.fair * (1 + self.cfg.last_look_min_margin)        # after the maker fee
              and not fresh.reason.startswith(("stale", "no fair", "sharp books disagree", "a leg has no")))
        blocked = self.allowed() or self.book.check(old, exclude=qid)
        if ok and not blocked:
            self.book.add(qid, old)                    # reserved BEFORE the await: the next last look sees it
            q["confirm_sent"] = True                   # never confirm twice; the status poll resolves the outcome
            try:
                await self.comms.confirm_quote(qid)
            except Exception as exc:  # noqa: BLE001 — it may have gone through: keep the reservation
                log.critical("COMBO confirm of %s failed (%s): reservation kept until its status resolves", qid, exc)
                return
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
        if qid not in self.book.open:                  # executed without passing our last look: still ours
            self.book.add(qid, cp)
        self.positions[qid] = dict(price=cp, ticker=q["rfq"].get("market_ticker"), at=self.clock(),
                                   creator=q["rfq"].get("creator_id"))
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
        budget = self.cfg.followups_per_step
        for rid, p in list(self.priced.items()):
            if p["followed"] or now - p["at"] < self.cfg.followup_after_s:
                continue
            if budget <= 0:
                break
            budget -= 1
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
                        our_yes_price=cp.yes_price, fair=cp.fair, n_trades=len(prices), slice=cp.slice,
                        would_win=None if traded is None else cp.yes_price <= traded + 1e-9,
                        margin_vs_winner=None if traded is None or not cp.fair else round(traded / cp.fair - 1, 4))
            if traded is None or cp.yes_price > traded + 1e-9 or p.get("sent"):
                self.priced.pop(rid, None)            # nothing to settle in shadow (a real quote settles as a position)
            else:
                p["await_result"] = True

    async def check_results(self) -> int:
        """Settle would-have-won shadow quotes and real positions against the combo market's result."""
        done = 0
        items = [(k, v["rfq"].get("market_ticker"), v["price"], "shadow", v["rfq"].get("creator_id"))
                 for k, v in self.priced.items() if v.get("await_result")]
        items += [(k, v["ticker"], v["price"], "position", v.get("creator")) for k, v in self.positions.items()]
        for key, ticker, cp, kind, creator in items[:self.cfg.results_per_check]:
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
            self.requesters.add(creator, -pnl, cp.contracts * cp.yes_price)       # their gain is our loss
            self._write("COMBO_RESULT", key=key, position_type=kind, market_ticker=ticker, result=result, pnl=round(pnl, 2),
                        expected_profit=cp.expected_profit, contracts=cp.contracts, yes_price=cp.yes_price, fair=cp.fair,
                        slice=cp.slice, creator_id=creator)
            if kind == "shadow":
                self.priced.pop(key, None)
            else:
                self.positions.pop(key, None)
                self.book.remove(key)
                self.on_settled(round(pnl, 2), key)
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
