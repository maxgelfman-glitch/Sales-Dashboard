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
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, ValidationError

# --------------------------------------------------------------------------
# Risk parameters. These are deliberate, reviewed constants — change with care.
# --------------------------------------------------------------------------
BANKROLL_USD = 100_000.00        # static capital pool used for sizing
KELLY_FRACTION = 0.25            # 1/4 Kelly
MIN_EDGE = 0.025                 # act only when EV per $1 is STRICTLY above 2.5%
MAX_STAKE_USD = 1_000.00         # ABSOLUTE ceiling per position (1% of bankroll)
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
        contracts=int(stake // price),
        suspicious=suspicious,
        **base,
    )
