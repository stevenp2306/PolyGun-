"""Removing the bookmaker margin ("vig") from decimal odds.

Implied probability of decimal odds o is 1/o. Across all outcomes these sum
to more than 1; the excess is the bookmaker's margin (overround). Methods:

* multiplicative - scale every 1/o by the same factor so they sum to 1.
  Simple, but assumes the margin is spread proportionally, which tends to
  overstate longshots (favourite-longshot bias).
* power - find k so that sum((1/o_i) ** k) = 1. Shrinks longshots more than
  favourites; usually better calibrated than multiplicative in sports data.
* shin - Shin (1993) model that attributes the margin to insider trading;
  also shrinks longshots.

Limitations (important): the three methods can disagree by 1-2 percentage
points on longshots; none of them knows *why* a book priced a line; soft
books shade prices toward public sentiment; and a de-vigged consensus is
still only an estimate. The strategy therefore adds an uncertainty buffer and
requires agreement between several bookmakers.
"""

from __future__ import annotations

import math


def implied(odds: list[float]) -> list[float]:
    if any(o <= 1.0 for o in odds):
        raise ValueError("decimal odds must be > 1.0")
    return [1.0 / o for o in odds]


def overround(odds: list[float]) -> float:
    return sum(implied(odds)) - 1.0


def multiplicative(odds: list[float]) -> list[float]:
    imp = implied(odds)
    s = sum(imp)
    return [p / s for p in imp]


def power(odds: list[float], tol: float = 1e-12) -> list[float]:
    imp = implied(odds)
    if abs(sum(imp) - 1) < tol:
        return imp
    # f(k) = sum(p_i ** k) - 1 is decreasing in k for p_i < 1.
    lo, hi = 0.5, 5.0
    while sum(p ** hi for p in imp) > 1:
        hi *= 2
    while sum(p ** lo for p in imp) < 1:
        lo /= 2
    for _ in range(200):
        mid = (lo + hi) / 2
        if sum(p ** mid for p in imp) > 1:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    k = (lo + hi) / 2
    out = [p ** k for p in imp]
    s = sum(out)
    return [p / s for p in out]


def shin(odds: list[float], tol: float = 1e-12) -> list[float]:
    imp = implied(odds)
    beta = sum(imp)
    n = len(imp)
    if abs(beta - 1) < tol:
        return imp

    def probs(z: float) -> list[float]:
        return [
            (math.sqrt(z * z + 4 * (1 - z) * (p * p) / beta) - z) / (2 * (1 - z))
            for p in imp
        ]

    lo, hi = 0.0, 0.999
    for _ in range(200):
        mid = (lo + hi) / 2
        if sum(probs(mid)) > 1:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    out = probs((lo + hi) / 2)
    s = sum(out)
    return [p / s for p in out] if n else out


METHODS = {"multiplicative": multiplicative, "power": power, "shin": shin}


def devig(odds: list[float], method: str = "power") -> list[float]:
    return METHODS[method](odds)
