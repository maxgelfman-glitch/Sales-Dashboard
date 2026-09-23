"""
execution.py — Mathematical risk core.

PIPELINE (one call to `evaluate_market_edge`):
    1. De-vig the sharp book (Pinnacle/Circa) two-way odds with the
       MULTIPLICATIVE method -> the "true" fair probability of our side.
    2. Compare to the Novig contract price (a Novig contract costs `price`
       dollars and pays $1 if it wins, so `price` IS Novig's implied probability).
    3. Edge (expected value per $1 staked) = fair_prob / price - 1.
       Only act if edge is STRICTLY greater than MIN_EDGE (2.5%).
    4. Size with 1/4 Kelly against the static $100,000 bankroll.
    5. Apply the HARD SAFETY CEILING: never more than $1,000 on one position,
       regardless of what Kelly says.

GLOBAL EXPOSURE KILL-SWITCH (ExposureMonitor, bottom of this file)
    Tracks every open, un-settled position. A NEW TAKER order is refused if it
    would take total open exposure above $15,000 (15% of bankroll); once
    exposure reaches $15,000 all new taker orders halt until positions settle.
    It only gates taker orders: it never blocks cancelling resting (maker)
    orders, because cancelling only ever reduces risk.

KEY FORMULAS
    American -> decimal:  +150 -> 2.50      -150 -> 1.6667
    Multiplicative de-vig for a two-way market with decimal odds d1, d2:
        raw_i       = 1 / d_i                   (includes the book's margin)
        overround   = raw_1 + raw_2             (e.g. 1.0476 for -110/-110)
        fair_i      = raw_i / overround         (sums to exactly 1.0)
    Kelly for a binary contract bought at price c with win probability p:
        f* = (p - c) / (1 - c)                  (fraction of bankroll)
        stake = KELLY_FRACTION * f* * BANKROLL, then min(stake, MAX_STAKE)

This module does pure math only. It places NO orders.
"""

from __future__ import annotations

import logging
import math
import asyncio
import time
from typing import Any, Callable, Literal, Optional, Protocol, Union

from pydantic import BaseModel, Field, ValidationError

# --------------------------------------------------------------------------
# Risk parameters. These are deliberate, reviewed constants — change with care.
# --------------------------------------------------------------------------
BANKROLL_USD = 100_000.00        # static capital pool used for sizing
KELLY_FRACTION = 0.25            # 1/4 Kelly
MIN_EDGE = 0.025                 # act only when EV per $1 is STRICTLY above 2.5%
MAX_STAKE_USD = 1_000.00         # ABSOLUTE ceiling per position (1% of bankroll)
GLOBAL_EXPOSURE_LIMIT_USD = 15_000.00  # max total open (un-settled) exposure (15% of bankroll)
SUSPICIOUS_EDGE = 0.20           # edges above this are usually bad data; flagged in the log
_EDGE_EPSILON = 1e-9             # absorbs floating-point noise at the 2.5% boundary

log = logging.getLogger("trading.execution")


# --------------------------------------------------------------------------
# Input / output models
# --------------------------------------------------------------------------
class NovigQuote(BaseModel):
    """The Novig contract we could buy."""
    price: float = Field(gt=0, lt=1, description="Cost per $1 payout (= implied probability)")
    fee_per_contract: float = Field(default=0.0, ge=0, description="Commission in $ per contract, if any")
    line: Optional[float] = None
    label: str = ""

    @property
    def effective_price(self) -> float:
        return self.price + self.fee_per_contract


class SharpQuote(BaseModel):
    """Two-way American odds from a sharp book for the SAME market."""
    odds_for: float = Field(description="American odds on our side, e.g. -110")
    odds_against: float = Field(description="American odds on the other side, e.g. -110")
    line: Optional[float] = None
    source: str = "sharp"


class EdgeDecision(BaseModel):
    action: Literal["BET", "PASS"]
    reason: str
    fair_prob: Optional[float] = None
    novig_price: Optional[float] = None
    edge: Optional[float] = None               # EV per $1 staked, e.g. 0.05 = +5%
    sharp_overround: Optional[float] = None    # book margin, e.g. 1.0476
    full_kelly_fraction: Optional[float] = None
    kelly_stake_usd: float = 0.0               # 1/4 Kelly BEFORE the safety ceiling
    stake_usd: float = 0.0                     # what we would actually risk
    capped: bool = False                       # True if the $1,000 ceiling overrode Kelly
    contracts: int = 0                         # whole contracts purchasable with stake_usd
    fee_usd: float = 0.0                       # venue transaction fee included in stake_usd (Kalshi)
    suspicious: bool = False                   # edge so large it is probably a data error


# --------------------------------------------------------------------------
# Pure math helpers
# --------------------------------------------------------------------------
def american_to_decimal(odds: float) -> float:
    """Convert American odds to decimal odds. Valid American odds are >= +100 or <= -100."""
    if odds >= 100:
        return 1.0 + odds / 100.0
    if odds <= -100:
        return 1.0 + 100.0 / abs(odds)
    raise ValueError(f"invalid American odds {odds!r}: must be >= +100 or <= -100")


def devig_multiplicative(decimal_odds: list[float]) -> tuple[list[float], float]:
    """Strip the bookmaker margin proportionally. Returns (fair_probs, overround)."""
    if len(decimal_odds) < 2 or any(d <= 1.0 for d in decimal_odds):
        raise ValueError(f"need >= 2 decimal odds all > 1.0, got {decimal_odds}")
    raw = [1.0 / d for d in decimal_odds]
    overround = sum(raw)
    return [r / overround for r in raw], overround


def kelly_fraction_for_contract(p: float, price: float) -> float:
    """Full-Kelly bankroll fraction for buying a $1-payout contract at `price`."""
    return (p - price) / (1.0 - price)


def apply_safety_ceiling(stake: float) -> tuple[float, bool]:
    """
    THE HARD CAP. Returns (safe_stake, was_capped).
    Also rejects NaN/inf/negative values outright (returns 0).
    """
    if not math.isfinite(stake) or stake <= 0:
        return 0.0, False
    if stake > MAX_STAKE_USD:
        return MAX_STAKE_USD, True
    return stake, False


def _floor_cents(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100.0


def _whole_contracts(stake: float, price: float) -> int:
    """Whole contracts affordable with `stake`. (Plain `stake // price` gives 1000 // 0.40 = 2499.)"""
    n = math.floor(stake / price + 1e-9)
    return n - 1 if n * price > stake + 0.005 else n


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------
async def evaluate_market_edge(
    novig_data: Union[NovigQuote, dict[str, Any]],
    sharp_data: Union[SharpQuote, dict[str, Any]],
) -> EdgeDecision:
    """
    Decide whether a Novig contract is +EV versus the de-vigged sharp price,
    and if so how much to stake. Never raises: bad inputs return a PASS.
    """
    try:
        novig = novig_data if isinstance(novig_data, NovigQuote) else NovigQuote.model_validate(novig_data)
        sharp = sharp_data if isinstance(sharp_data, SharpQuote) else SharpQuote.model_validate(sharp_data)
        dec_for = american_to_decimal(sharp.odds_for)
        dec_against = american_to_decimal(sharp.odds_against)
    except (ValidationError, ValueError) as exc:
        log.warning("CALC invalid input -> PASS: %s", exc)
        return EdgeDecision(action="PASS", reason=f"invalid input: {exc}")

    # Spreads/totals are only comparable at the identical number.
    if novig.line is not None and sharp.line is not None and not math.isclose(novig.line, sharp.line):
        return EdgeDecision(action="PASS", reason=f"line mismatch novig={novig.line} sharp={sharp.line}")

    (fair_prob, _), overround = devig_multiplicative([dec_for, dec_against])
    price = novig.effective_price
    if price >= 1.0:
        return EdgeDecision(action="PASS", reason="price incl. fees >= $1", fair_prob=fair_prob, novig_price=price)

    edge = fair_prob / price - 1.0
    base = dict(fair_prob=fair_prob, novig_price=price, edge=edge, sharp_overround=overround)
    log.info("CALC %s fair=%.5f price=%.5f edge=%+.4f%% overround=%.5f",
             novig.label or "-", fair_prob, price, edge * 100, overround)

    if edge <= MIN_EDGE + _EDGE_EPSILON:
        return EdgeDecision(action="PASS", reason=f"edge {edge:+.4%} not above {MIN_EDGE:.1%}", **base)

    full_kelly = kelly_fraction_for_contract(fair_prob, price)
    kelly_stake = KELLY_FRACTION * full_kelly * BANKROLL_USD
    stake, capped = apply_safety_ceiling(kelly_stake)
    stake = _floor_cents(stake)
    suspicious = edge > SUSPICIOUS_EDGE
    if suspicious:
        log.warning("CALC suspicious edge %+.2f%% on %s — verify data/name mapping", edge * 100, novig.label or "-")
    if stake <= 0:
        return EdgeDecision(action="PASS", reason="stake rounds to $0", full_kelly_fraction=full_kelly, **base)

    return EdgeDecision(
        action="BET",
        reason=("kelly capped at safety ceiling" if capped else "quarter kelly"),
        full_kelly_fraction=full_kelly,
        kelly_stake_usd=round(kelly_stake, 2),
        stake_usd=stake,
        capped=capped,
        contracts=_whole_contracts(stake, price),
        suspicious=suspicious,
        **base,
    )


# --------------------------------------------------------------------------
# Global exposure kill-switch
# --------------------------------------------------------------------------
class ExposureLimitError(RuntimeError):
    """Raised if code tries to record a position that would breach the global limit."""


class ExposureMonitor:
    """
    Tracks open, un-settled positions and gates NEW TAKER orders.

        ok, reason = monitor.check_taker(stake)   # ask before sending a taker order
        monitor.record_open(position_id, stake)   # after it is placed (re-checks!)
        monitor.settle(position_id)               # when the market settles -> exposure released

    Maker-order cancellation is deliberately NOT gated: `allows_cancellation()`
    is always True, even while the kill-switch is engaged.
    """

    def __init__(self, limit_usd: float = GLOBAL_EXPOSURE_LIMIT_USD) -> None:
        self.limit = limit_usd
        self._open: dict[str, float] = {}
        self._was_halted = False

    @property
    def open_exposure(self) -> float:
        return round(sum(self._open.values()), 2)

    @property
    def headroom(self) -> float:
        return round(max(0.0, self.limit - self.open_exposure), 2)

    @property
    def taker_halted(self) -> bool:
        return self.open_exposure >= self.limit - 1e-9

    def open_positions(self) -> dict[str, float]:
        return dict(self._open)

    def check_taker(self, stake_usd: float) -> tuple[bool, str]:
        """May a new taker order of this size be sent right now?"""
        if not math.isfinite(stake_usd) or stake_usd <= 0:
            return False, f"invalid stake {stake_usd!r}"
        if self.taker_halted:
            return False, (f"KILL_SWITCH engaged: open exposure ${self.open_exposure:,.2f} "
                           f">= ${self.limit:,.2f}; new taker orders halted")
        if self.open_exposure + stake_usd > self.limit + 1e-9:
            return False, (f"KILL_SWITCH: ${stake_usd:,.2f} would lift exposure to "
                           f"${self.open_exposure + stake_usd:,.2f} > ${self.limit:,.2f} (headroom ${self.headroom:,.2f})")
        return True, "ok"

    def record_open(self, position_id: str, stake_usd: float) -> None:
        ok, reason = self.check_taker(stake_usd)
        if not ok:
            raise ExposureLimitError(reason)
        if position_id in self._open:
            raise ValueError(f"position {position_id} already open")
        self._open[position_id] = stake_usd
        self._log_transition()

    def record_fill(self, position_id: str, stake_usd: float) -> None:
        """Record a MAKER fill. A fill has already happened, so it is recorded even if it breaches
        the limit (logged loudly); MakerEngine sizes resting quotes to the headroom to prevent this."""
        if position_id in self._open:
            raise ValueError(f"position {position_id} already open")
        if self.open_exposure + stake_usd > self.limit + 1e-9:
            log.error("KILL_SWITCH maker fill %s ($%.2f) takes exposure over the limit", position_id, stake_usd)
        self._open[position_id] = stake_usd
        self._log_transition()

    def adjust(self, position_id: str, stake_usd: float) -> None:
        """Set a live position's exposure to its actual value (e.g. reservation -> filled cost).
        0 removes it. Used when fills confirm less (or, rarely, more) than was reserved."""
        if stake_usd <= 0:
            self._open.pop(position_id, None)
        else:
            if self.open_exposure - self._open.get(position_id, 0.0) + stake_usd > self.limit + 1e-9:
                log.error("KILL_SWITCH adjustment of %s to $%.2f takes exposure over the limit", position_id, stake_usd)
            self._open[position_id] = stake_usd
        self._log_transition()

    def settle(self, position_id: str) -> float:
        """Release a settled position's exposure. Returns the amount released (0 if unknown)."""
        released = self._open.pop(position_id, 0.0)
        self._log_transition()
        return released

    @staticmethod
    def allows_cancellation() -> bool:
        """Cancelling resting maker orders is always allowed: it can only reduce risk."""
        return True

    def _log_transition(self) -> None:
        halted = self.taker_halted
        if halted and not self._was_halted:
            log.warning("KILL_SWITCH ENGAGED open exposure $%.2f >= $%.2f: new taker orders halted "
                        "(maker cancellations still allowed)", self.open_exposure, self.limit)
        elif self._was_halted and not halted:
            log.warning("KILL_SWITCH RELEASED open exposure $%.2f < $%.2f: taker orders resume",
                        self.open_exposure, self.limit)
        self._was_halted = halted


# ==========================================================================
# Cross-venue price translation (Kalshi cents <-> probability <-> American)
# ==========================================================================
def cents_to_probability(price_cents: float) -> float:
    """A contract paying 100c costing 53c implies a 53% probability."""
    if not 0 < price_cents < 100:
        raise ValueError(f"price must be strictly between 0 and 100 cents, got {price_cents}")
    return price_cents / 100.0


def probability_to_american(p: float) -> float:
    """0.53 -> -112.77 (~-113); 0.40 -> +150. Rounded to 2 decimals."""
    if not 0 < p < 1:
        raise ValueError(f"probability must be strictly between 0 and 1, got {p}")
    return round(-100.0 * p / (1 - p), 2) if p >= 0.5 else round(100.0 * (1 - p) / p, 2)


def cents_to_american(price_cents: float) -> float:
    return probability_to_american(cents_to_probability(price_cents))


# ==========================================================================
# Kalshi taker fee
#   Fees = 0.07 * Contracts * P * (1 - P), P = price_cents / 100,
#   rounded UP to the next cent per order (Kalshi's published fee schedule
#   rounds up; rounding up is also the conservative direction for us).
# ==========================================================================
KALSHI_TAKER_FEE_RATE = 0.07


def kalshi_taker_fee(contracts: int, price_cents: float) -> float:
    p = price_cents / 100.0
    raw = KALSHI_TAKER_FEE_RATE * contracts * p * (1.0 - p)
    return math.ceil(raw * 100 - 1e-9) / 100.0


def kalshi_fee_per_contract(price_cents: float) -> float:
    p = price_cents / 100.0
    return KALSHI_TAKER_FEE_RATE * p * (1.0 - p)


async def evaluate_kalshi_edge(price_cents: float, sharp_data: Union[SharpQuote, dict[str, Any]],
                               line: Optional[float] = None, label: str = "") -> EdgeDecision:
    """
    Edge on a Kalshi YES contract AFTER the taker fee.
      1. Size with the fee folded into the price (cost per contract = P + fee/contract).
      2. Recompute the exact, rounded-up fee for that many contracts and shrink the
         order until total cash out (contracts x P + fee) fits the $1,000 ceiling.
      3. Net edge = (contracts x fair - total cost) / total cost. BET only if > 2.5%.
    """
    try:
        fee_pc = kalshi_fee_per_contract(price_cents)
        base = await evaluate_market_edge(
            NovigQuote(price=cents_to_probability(price_cents), fee_per_contract=fee_pc, line=line, label=label),
            sharp_data)
    except ValueError as exc:
        return EdgeDecision(action="PASS", reason=f"invalid input: {exc}")
    if base.action != "BET":
        return base.model_copy(update=dict(reason=f"after Kalshi fee: {base.reason}"))

    p = price_cents / 100.0
    contracts = base.contracts
    while contracts > 0 and contracts * p + kalshi_taker_fee(contracts, price_cents) > MAX_STAKE_USD + 1e-9:
        contracts -= 1
    if contracts <= 0:
        return base.model_copy(update=dict(action="PASS", reason="no whole contract fits after fees",
                                           stake_usd=0.0, contracts=0))
    fee = kalshi_taker_fee(contracts, price_cents)
    cost = round(contracts * p + fee, 2)
    net_edge = (contracts * base.fair_prob - cost) / cost
    if net_edge <= MIN_EDGE + _EDGE_EPSILON:
        return base.model_copy(update=dict(action="PASS", edge=net_edge, stake_usd=0.0, contracts=0, fee_usd=fee,
                                           reason=f"net edge {net_edge:+.4%} after ${fee:.2f} Kalshi fee not above "
                                                  f"{MIN_EDGE:.1%}"))
    log.info("CALC kalshi %s %d contracts @ %.1fc fee=$%.2f cost=$%.2f net_edge=%+.4f%%",
             label or "-", contracts, price_cents, fee, cost, net_edge * 100)
    return base.model_copy(update=dict(edge=net_edge, stake_usd=cost, contracts=contracts, fee_usd=fee))


# ==========================================================================
# Maker (passive) engine
# ==========================================================================
MAKER_REFRESH_SECONDS = 2.0      # how often quotes are re-evaluated
MAKER_QUIET_SECONDS = 5.0        # only quote if no taker order fired in this window
MAKER_LINE_MOVE_POINTS = 0.5     # sharp spread/total move GREATER than this => cancel ALL quotes
MAKER_ML_FAIR_MOVE = 0.02        # sharp moneyline fair-prob move greater than 2c => cancel ALL quotes
MAKER_CANCEL_BUDGET_MS = 200.0   # bulk cancel must complete inside this budget


def maker_quote_prices(fair_prob: float, min_edge: float = MIN_EDGE) -> tuple[Optional[int], Optional[int]]:
    """
    Two-sided quote just outside fair value, in whole cents, so that EVERY fill
    carries an edge strictly above `min_edge`:
      bid b: buying at b has edge fair/b - 1 > min_edge            -> b < fair / (1 + min_edge)
      ask a: selling at a == buying the other side at 1-a;
             edge (1-fair)/(1-a) - 1 > min_edge                    -> a > 1 - (1-fair)/(1 + min_edge)
    Example: fair 0.50 -> bid 48c / ask 52c.
    """
    bid_limit = 100 * fair_prob / (1 + min_edge)
    # The 1e-9 guards absorb float noise: 100*(1 - 0.82/1.025) evaluates to 19.999999999999996,
    # which without them would quote 20c = EXACTLY 2.5% edge instead of strictly more.
    bid = math.ceil(bid_limit - 1e-9) - 1            # strictly below the limit
    ask_limit = 100 * (1 - (1 - fair_prob) / (1 + min_edge))
    ask = math.floor(ask_limit + 1e-9) + 1           # strictly above the limit
    return (bid if 1 <= bid <= 99 else None), (ask if 1 <= ask <= 99 else None)


def maker_quote_contracts(fair_prob: float, price_cents: int, side: str, max_stake: float = MAX_STAKE_USD) -> int:
    """1/4 Kelly size for one quote, capped at the $1,000 ceiling (cost = collateral at risk)."""
    c = price_cents / 100.0
    if side == "buy":
        p, cost = fair_prob, c                       # buy this outcome at c
    else:
        p, cost = 1.0 - fair_prob, 1.0 - c           # selling at c == buying the other side at 1-c
    f = kelly_fraction_for_contract(p, cost)
    stake, _ = apply_safety_ceiling(KELLY_FRACTION * f * BANKROLL_USD)
    stake = min(stake, max_stake)
    return _whole_contracts(stake, cost) if stake > 0 else 0


class MakerTarget(BaseModel):
    outcome_id: str
    market_key: tuple
    fair_prob: float
    label: str = ""


class RestingQuote(BaseModel):
    order_id: str
    outcome_id: str
    market_key: tuple
    side: Literal["buy", "sell"]
    price_cents: int
    contracts: int
    fair_prob: float
    placed_at: float

    @property
    def worst_case_cost(self) -> float:
        """Collateral at risk if this quote is completely filled."""
        c = self.price_cents / 100.0
        return self.contracts * (c if self.side == "buy" else 1.0 - c)


class QuoteGateway(Protocol):
    async def place_limit(self, outcome_id: str, side: str, price_cents: float, contracts: int,
                          client_id: str) -> str: ...

    async def cancel_orders(self, order_ids: list[str]) -> None: ...


class MakerEngine:
    """
    Keeps two-sided LIMIT quotes resting on eligible outcomes while the taker side is quiet.

    Safety:
      * cancel_all() — ONE bulk cancel call for every resting quote; timed against
        the 200ms budget and retried once. Triggered by the supervisor on a
        WebSocket drop or a sharp line move beyond the thresholds above.
      * Never posts while the exposure kill-switch is engaged (and pulls all quotes then).
      * Worst-case cost of all resting quotes never exceeds the exposure headroom.
    """

    def __init__(self, gateway: QuoteGateway, exposure: "ExposureMonitor",
                 targets: Callable[[], list[MakerTarget]], refresh_interval: float = MAKER_REFRESH_SECONDS,
                 quiet_seconds: float = MAKER_QUIET_SECONDS, clock: Callable[[], float] = time.monotonic,
                 max_stake: float = MAX_STAKE_USD,
                 on_posted: Optional[Callable[["RestingQuote"], None]] = None,
                 can_quote: Optional[Callable[[], bool]] = None) -> None:
        self.gateway = gateway
        self.max_stake = min(max_stake, MAX_STAKE_USD)   # may only LOWER the ceiling
        self.on_posted = on_posted                        # live mode: register the order for fill tracking
        self.can_quote = can_quote                        # live mode: False while the private channel is down
        self.exposure = exposure
        self.targets = targets
        self.refresh_interval = refresh_interval
        self.quiet_seconds = quiet_seconds
        self.clock = clock
        self.quotes: dict[str, RestingQuote] = {}
        self.last_taker_at = -math.inf
        self.kill_count = 0
        self.last_cancel_ms: Optional[float] = None
        self._client_ids = 0
        self._lock = asyncio.Lock()

    # ---------------- state ----------------
    def note_taker_activity(self) -> None:
        self.last_taker_at = self.clock()

    def is_quiet(self) -> bool:
        return self.clock() - self.last_taker_at >= self.quiet_seconds

    def resting_worst_case(self) -> float:
        return round(sum(q.worst_case_cost for q in self.quotes.values()), 2)

    def quotes_for(self, outcome_id: str) -> list[RestingQuote]:
        return [q for q in self.quotes.values() if q.outcome_id == outcome_id]

    # ---------------- cancellation (never gated by anything) ----------------
    async def _cancel(self, order_ids: list[str], reason: str, kill: bool) -> float:
        if not order_ids:
            return 0.0
        started = time.perf_counter()
        for attempt in (1, 2):
            try:
                await self.gateway.cancel_orders(order_ids)
                break
            except Exception as exc:  # noqa: BLE001
                log.error("MAKER_CANCEL attempt %d failed (%s: %s)", attempt, type(exc).__name__, exc)
                if attempt == 2:
                    log.critical("MAKER_CANCEL FAILED for %d quote(s) — they may still be resting; will retry",
                                 len(order_ids))
                    return (time.perf_counter() - started) * 1000
        for oid in order_ids:
            self.quotes.pop(oid, None)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if kill:
            self.kill_count += 1
            self.last_cancel_ms = elapsed_ms
            level = logging.WARNING if elapsed_ms <= MAKER_CANCEL_BUDGET_MS else logging.ERROR
            log.log(level, "MAKER_KILL bulk-cancelled %d quote(s) in %.1fms (budget %.0fms): %s",
                    len(order_ids), elapsed_ms, MAKER_CANCEL_BUDGET_MS, reason)
        else:
            log.info("MAKER_CANCEL %d quote(s) in %.1fms: %s", len(order_ids), elapsed_ms, reason)
        return elapsed_ms

    async def cancel_all(self, reason: str) -> float:
        """Maker kill-switch: pull every resting quote in one bulk call. Returns elapsed ms."""
        return await self._cancel(list(self.quotes), reason, kill=True)

    async def cancel_market(self, market_key: tuple, reason: str) -> float:
        ids = [oid for oid, q in self.quotes.items() if q.market_key == market_key]
        return await self._cancel(ids, reason, kill=False)

    def on_fill(self, order_id: str) -> Optional[RestingQuote]:
        """Remove a filled quote from the book and return it (the supervisor books the position)."""
        return self.quotes.pop(order_id, None)

    # ---------------- quoting ----------------
    def _desired(self, t: MakerTarget) -> list[tuple[str, int, int]]:
        bid, ask = maker_quote_prices(t.fair_prob)
        out = []
        for side, price in (("buy", bid), ("sell", ask)):
            if price is None:
                continue
            n = maker_quote_contracts(t.fair_prob, price, side, self.max_stake)
            if n > 0:
                out.append((side, price, n))
        return out

    async def refresh(self) -> None:
        async with self._lock:
            if self.exposure.taker_halted:
                if self.quotes:
                    await self._cancel(list(self.quotes), "exposure kill-switch engaged", kill=True)
                return
            if self.can_quote is not None and not self.can_quote():
                if self.quotes:
                    await self._cancel(list(self.quotes), "quoting not allowed (private fill channel down)", kill=True)
                return
            if not self.is_quiet():
                return
            targets = {t.outcome_id: t for t in self.targets()}
            gone = [oid for oid, q in self.quotes.items() if q.outcome_id not in targets]
            await self._cancel(gone, "outcome no longer eligible (position, stale sharp or delisted)", kill=False)

            for t in targets.values():
                desired = self._desired(t)
                current = self.quotes_for(t.outcome_id)
                if sorted((q.side, q.price_cents, q.contracts) for q in current) == sorted(desired):
                    continue
                await self._cancel([q.order_id for q in current], f"requote {t.outcome_id}", kill=False)
                for side, price, n in desired:
                    cost = n * (price / 100 if side == "buy" else 1 - price / 100)
                    if self.resting_worst_case() + cost > self.exposure.headroom + 1e-9:
                        log.info("MAKER_SKIP %s %s %dc: worst case $%.2f exceeds exposure headroom $%.2f",
                                 t.outcome_id, side, price, self.resting_worst_case() + cost, self.exposure.headroom)
                        continue
                    self._client_ids += 1
                    try:
                        oid = await self.gateway.place_limit(t.outcome_id, side, price, n, f"mk-{self._client_ids}")
                    except Exception as exc:  # noqa: BLE001
                        log.error("MAKER_POST failed %s %s %dc: %s", t.outcome_id, side, price, exc)
                        continue
                    self.quotes[oid] = RestingQuote(order_id=oid, outcome_id=t.outcome_id, market_key=t.market_key,
                                                    side=side, price_cents=price, contracts=n,
                                                    fair_prob=t.fair_prob, placed_at=time.time())
                    if self.on_posted is not None:
                        self.on_posted(self.quotes[oid])
                    log.info("MAKER_POST LIMIT %s %s %d @ %dc (fair %.2fc) id=%s %s", t.outcome_id, side, n, price,
                             t.fair_prob * 100, oid, t.label)

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("MAKER_ERROR refresh failed (loop continues)")
            await asyncio.sleep(self.refresh_interval)
