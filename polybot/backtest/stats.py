"""Performance statistics with event-level (clustered) uncertainty."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Iterable


def max_drawdown(equity: list[float]) -> tuple[float, float]:
    """Return (max drawdown in $, as fraction of the running peak)."""
    peak = -math.inf
    mdd = 0.0
    mdd_pct = 0.0
    for v in equity:
        peak = max(peak, v)
        dd = peak - v
        if dd > mdd:
            mdd = dd
            mdd_pct = dd / peak if peak > 0 else 0.0
    return mdd, mdd_pct


def trade_stats(pnls: list[float]) -> dict:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "n_trades": len(pnls),
        "net_pnl": round(sum(pnls), 4),
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
        "avg_win": round(gross_win / len(wins), 4) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 4) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else (None if not wins else math.inf),
    }


def cluster_bootstrap_ci(pnl_by_event: dict[str, float], n_boot: int = 5000, alpha: float = 0.05,
                         seed: int = 7) -> dict:
    """Bootstrap CI of mean and total P&L, resampling whole EVENTS (not trades).

    Trades on the same game are correlated, so resampling individual trades
    would understate uncertainty.
    """
    vals = list(pnl_by_event.values())
    n = len(vals)
    if n < 2:
        return {"n_events": n, "mean": vals[0] if vals else None, "ci_mean": None, "ci_total": None}
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        sample = [vals[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(alpha / 2 * n_boot)]
    hi = means[int((1 - alpha / 2) * n_boot) - 1]
    mean = sum(vals) / n
    return {"n_events": n, "mean": round(mean, 5), "ci_mean": [round(lo, 5), round(hi, 5)],
            "ci_total": [round(lo * n, 4), round(hi * n, 4)], "confidence": 1 - alpha}


def calibration(preds: Iterable[tuple[float, int]], bins: int = 10) -> dict:
    """Brier score, log loss and a reliability table for (prob, outcome) pairs."""
    pairs = [(min(max(p, 1e-6), 1 - 1e-6), y) for p, y in preds]
    if not pairs:
        return {"n": 0}
    brier = sum((p - y) ** 2 for p, y in pairs) / len(pairs)
    ll = -sum(y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in pairs) / len(pairs)
    table = defaultdict(lambda: [0, 0.0, 0])
    for p, y in pairs:
        b = min(int(p * bins), bins - 1)
        table[b][0] += 1
        table[b][1] += p
        table[b][2] += y
    rows = [{"bin": f"{b / bins:.1f}-{(b + 1) / bins:.1f}", "n": c, "avg_pred": round(sp / c, 3),
             "freq": round(sy / c, 3)} for b, (c, sp, sy) in sorted(table.items())]
    return {"n": len(pairs), "brier": round(brier, 5), "log_loss": round(ll, 5), "table": rows}
