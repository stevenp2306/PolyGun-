"""Point-in-time backtest over recorded snapshots.

Honesty rules implemented here:
* Decisions use only fields present in the snapshot at its timestamp; the
  outcome (resolutions file) is joined only when settling.
* Events are split CHRONOLOGICALLY into development / validation / test
  blocks. Thresholds are NOT tuned on any block (the configured values are
  used as-is), so the test block stays untouched.
* Walk-forward folds report each consecutive block separately.
* Fills walk the recorded order book with the same depth haircut as paper
  trading. Latency and queue effects cannot be reproduced from snapshots.
* Uncertainty is estimated by resampling EVENTS, not trades.
* Tokens without a recorded resolution are excluded and counted.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from polybot.backtest.data import Snapshot
from polybot.backtest.stats import calibration, cluster_bootstrap_ci, max_drawdown, trade_stats
from polybot.config import Settings
from polybot.domain import MarketInfo, OutcomeToken
from polybot.exits import evaluate_exit
from polybot.marketdata import BookStore
from polybot.pricing import liquidation_value
from polybot.strategy.sports_value import SportsValueStrategy


@dataclass
class BTTrade:
    event_id: str
    token_id: str
    label: str
    entry_ts: float
    shares: float
    cost: float
    fees: float
    fair_prob: float
    entry_price: float
    exit_kind: str = ""
    proceeds: float = 0.0
    pnl: float = 0.0
    settle_ts: float = 0.0


@dataclass
class BTResult:
    name: str
    n_snapshots: int
    n_events_seen: int
    trades: list[BTTrade] = field(default_factory=list)
    excluded_unresolved: int = 0

    def summary(self, bankroll: float) -> dict:
        by_event: dict[str, float] = defaultdict(float)
        for t in self.trades:
            by_event[t.event_id] += t.pnl
        seq = sorted(self.trades, key=lambda t: t.settle_ts)
        eq, run = [bankroll], bankroll
        for t in seq:
            run += t.pnl
            eq.append(run)
        mdd, mdd_pct = max_drawdown(eq)
        cal = calibration([(t.fair_prob, 1 if t.proceeds / t.shares > 0.99 else 0) for t in self.trades
                           if t.exit_kind == "settled" and t.shares > 0])
        return {
            "name": self.name, "snapshots": self.n_snapshots, "events_seen": self.n_events_seen,
            "events_traded": len(by_event), **trade_stats([t.pnl for t in self.trades]),
            "fees": round(sum(t.fees for t in self.trades), 4),
            "capital_deployed": round(sum(t.cost for t in self.trades), 4),
            "total_return_on_bankroll": round(sum(t.pnl for t in self.trades) / bankroll, 5),
            "max_drawdown_usd": round(mdd, 4), "max_drawdown_pct": round(mdd_pct, 5),
            "event_bootstrap": cluster_bootstrap_ci(dict(by_event)),
            "entry_calibration": cal, "excluded_unresolved": self.excluded_unresolved,
            "benchmark_no_trading_pnl": 0.0,
        }


def _market(s: Snapshot) -> MarketInfo:
    return MarketInfo(market_id=s.market_id, condition_id=None, question=s.label,
                      tokens=[OutcomeToken(s.token_id, s.label.split(" :: ")[-1])], event_id=s.event_id,
                      event_title=s.label.split(" :: ")[0], game_start_time=s.game_start_time,
                      tick_size=s.book.tick_size, min_order_size=s.book.min_order_size, fee=s.fee)


def simulate(name: str, snaps: list[Snapshot], resolutions: dict[str, tuple[float, float]],
             settings: Settings, use_exits: bool) -> BTResult:
    strat = SportsValueStrategy(settings)
    res = BTResult(name, len(snaps), len({x.event_id for x in snaps}))
    bankroll = settings.bankroll_usd
    open_pos: dict[str, BTTrade] = {}
    done_tokens: set[str] = set()
    event_exposure: dict[str, float] = defaultdict(float)
    for snap in snaps:
        m = _market(snap)
        now = snap.ts
        bs = BookStore(max_age_s=1e12)
        bs.put(snap.book)
        if snap.token_id in open_pos:
            if use_exits:
                t = open_pos[snap.token_id]
                pos = {"shares": t.shares, "cost_basis": t.cost, "opened_at": t.entry_ts}
                d = evaluate_exit(settings, pos, m, snap.book, snap.fair, now)
                if d.action == "EXIT":
                    lv = liquidation_value(snap.book.bids, d.shares, snap.fee, settings.paper_depth_haircut)
                    if lv.shares >= t.shares - 1e-6:
                        t.proceeds, t.exit_kind, t.settle_ts = lv.net_proceeds, "exit:" + d.reasons[0][:40], now
                        t.pnl = t.proceeds - t.cost
                        t.fees += lv.fees
                        res.trades.append(t)
                        del open_pos[snap.token_id]
                        event_exposure[t.event_id] -= t.cost
            continue
        if snap.token_id in done_tokens:
            continue
        if snap.game_start_time and (snap.game_start_time - now) / 60 < settings.min_minutes_to_start:
            continue
        exposure = sum(t.cost for t in open_pos.values())
        budget = max(0.0, min(settings.max_position_usd, settings.max_position_pct * bankroll,
                              settings.max_event_exposure_pct * bankroll - event_exposure[snap.event_id],
                              settings.max_total_exposure_pct * bankroll - exposure))
        fair = snap.fair
        fair.fetched_at = min(fair.fetched_at, now)
        c = strat.evaluate(m, {snap.token_id: fair}, "", bs, {snap.token_id: budget}, bankroll, now)[0]
        if not c.approved:
            continue
        n = c.numbers
        t = BTTrade(snap.event_id, snap.token_id, snap.label, now, n["shares"], n["total_cost"], n["fees"],
                    fair.prob, n["avg_price"])
        open_pos[snap.token_id] = t
        done_tokens.add(snap.token_id)
        event_exposure[snap.event_id] += t.cost
    for tok, t in open_pos.items():
        r = resolutions.get(tok)
        if r is None:
            res.excluded_unresolved += 1
            continue
        payout, ts = r
        t.proceeds, t.exit_kind, t.settle_ts = payout * t.shares, "settled", ts
        t.pnl = t.proceeds - t.cost
        res.trades.append(t)
    return res


def chronological_split(snaps: list[Snapshot], fractions=(0.6, 0.2, 0.2)) -> dict[str, list[Snapshot]]:
    first_seen: dict[str, float] = {}
    for s in snaps:
        first_seen.setdefault(s.event_id, s.game_start_time or s.ts)
    order = sorted(first_seen, key=lambda e: first_seen[e])
    n = len(order)
    a = int(n * fractions[0])
    b = a + int(n * fractions[1])
    blocks = {"development": set(order[:a]), "validation": set(order[a:b]), "test": set(order[b:])}
    return {k: [s for s in snaps if s.event_id in v] for k, v in blocks.items()}


def walk_forward(snaps: list[Snapshot], resolutions, settings: Settings, folds: int = 5, use_exits: bool = False) -> list[dict]:
    first_seen: dict[str, float] = {}
    for s in snaps:
        first_seen.setdefault(s.event_id, s.game_start_time or s.ts)
    order = sorted(first_seen, key=lambda e: first_seen[e])
    if len(order) < folds * 10:
        return []
    size = len(order) // folds
    out = []
    for i in range(folds):
        ev = set(order[i * size:(i + 1) * size])
        r = simulate(f"fold{i + 1}", [s for s in snaps if s.event_id in ev], resolutions, settings, use_exits)
        out.append(r.summary(settings.bankroll_usd))
    return out


def market_vs_model_calibration(snaps: list[Snapshot], resolutions) -> dict:
    """Is the independent estimate better calibrated than the Polymarket mid?

    Uses the FIRST snapshot per token (pre-match). If the model is not more
    accurate than the market price, there is no reason to expect an edge.
    """
    first: dict[str, Snapshot] = {}
    for s in snaps:
        first.setdefault(s.token_id, s)
    model, market = [], []
    for tok, s in first.items():
        if tok not in resolutions:
            continue
        y = 1 if resolutions[tok][0] > 0.99 else 0
        if s.book.best_bid is None or s.book.best_ask is None:
            continue
        model.append((s.fair.prob, y))
        market.append(((s.book.best_bid + s.book.best_ask) / 2, y))
    return {"model": calibration(model), "market_mid": calibration(market)}
