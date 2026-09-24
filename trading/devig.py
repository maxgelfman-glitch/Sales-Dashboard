"""
devig.py — Remove the bookmaker's margin from a two-way (or n-way) price to get fair probabilities.

Three methods (DEVIG_METHOD):
    multiplicative  divide each implied probability by their sum. Simple; spreads the margin evenly,
                    which slightly OVERrates longshots (books put more margin on longshots).
    power           raise each implied probability to the power k so they sum to 1. Takes more margin
                    off the longshot; usually better calibrated on favourite/longshot markets.
    shin            Shin's model: the margin comes from protecting against informed bettors. Similar to
                    power; the standard choice in the betting-market literature.

For near-50/50 markets (most spreads and totals) all three agree to a fraction of a cent. They
differ on lopsided moneylines, which is exactly where a wrong fair value creates fake edges.
Every method returns fair probabilities that sum to 1, plus the overround (sum of implied probs).
"""

from __future__ import annotations

import math

DEVIG_METHODS = ("multiplicative", "power", "shin")


def _implied(decimal_odds: list[float]) -> list[float]:
    if len(decimal_odds) < 2 or any(not math.isfinite(d) or d <= 1.0 for d in decimal_odds):
        raise ValueError(f"need >= 2 decimal odds all > 1.0, got {decimal_odds}")
    return [1.0 / d for d in decimal_odds]


def devig_multiplicative(decimal_odds: list[float]) -> tuple[list[float], float]:
    """Strip the bookmaker margin proportionally. Returns (fair_probs, overround)."""
    raw = _implied(decimal_odds)
    overround = sum(raw)
    return [r / overround for r in raw], overround


def _bisect(f, lo: float, hi: float, iters: int = 100) -> float:
    """Root of an increasing-or-decreasing f on [lo, hi] (f(lo), f(hi) of opposite sign)."""
    flo = f(lo)
    for _ in range(iters):
        mid = (lo + hi) / 2
        fm = f(mid)
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2


def devig_power(decimal_odds: list[float]) -> tuple[list[float], float]:
    """Fair p_i = q_i ** k with k chosen so that sum p_i = 1."""
    raw = _implied(decimal_odds)
    overround = sum(raw)
    if math.isclose(overround, 1.0, abs_tol=1e-12):
        return list(raw), overround
    k = _bisect(lambda k: sum(q ** k for q in raw) - 1.0, 1e-3, 50.0)
    probs = [q ** k for q in raw]
    total = sum(probs)
    return [p / total for p in probs], overround


def devig_shin(decimal_odds: list[float]) -> tuple[list[float], float]:
    """Shin (1993): p_i = (sqrt(z^2 + 4(1-z) q_i^2 / S) - z) / (2(1-z)), z solved so that sum p_i = 1."""
    raw = _implied(decimal_odds)
    overround = sum(raw)
    if overround <= 1.0 + 1e-12:                 # no margin (or an arbitrage): nothing to model
        return [r / overround for r in raw], overround

    def probs(z: float) -> list[float]:
        return [(math.sqrt(z * z + 4 * (1 - z) * q * q / overround) - z) / (2 * (1 - z)) for q in raw]

    z = _bisect(lambda z: sum(probs(z)) - 1.0, 0.0, 0.999)
    p = probs(z)
    total = sum(p)
    return [x / total for x in p], overround


_METHODS = {"multiplicative": devig_multiplicative, "power": devig_power, "shin": devig_shin}


def devig(decimal_odds: list[float], method: str = "multiplicative") -> tuple[list[float], float]:
    try:
        fn = _METHODS[method]
    except KeyError:
        raise ValueError(f"unknown devig method {method!r}; use one of {DEVIG_METHODS}") from None
    return fn(decimal_odds)
