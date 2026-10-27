"""Exit / profit-taking rules.

Profit basis (shown on the dashboard):

    net_exit_value = proceeds from selling ALL held shares into the current
                     visible bids (walking depth), minus taker fees
    cost_basis     = what was paid for those shares including entry fees
    pnl_pct        = (net_exit_value - cost_basis) / cost_basis

So "take profit at 6%" means: selling everything right now, after fees and
walking the bids, would return at least 6% more than the total amount
invested (not 6% on the token price).

A stop-loss is only a *trigger*: it does not guarantee an exit price, and a
sell may not fill at all when bids are thin. The bot never sells below
``min_exit_price`` and never liquidates into an empty book.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from polybot.config import PreStartAction, Settings
from polybot.domain import FairEstimate, MarketInfo, OrderBook
from polybot.pricing import fee_per_share, liquidation_value, round_to_tick


@dataclass
class ExitDecision:
    action: str  # "EXIT" or "HOLD"
    reasons: list[str]
    numbers: dict = field(default_factory=dict)
    shares: float = 0.0
    min_price: float = 0.0
    new_peak_pct: float | None = None


def evaluate_exit(
    s: Settings,
    pos: dict,
    market: MarketInfo | None,
    book: OrderBook | None,
    fair: FairEstimate | None,
    now: float | None = None,
) -> ExitDecision:
    now = now or time.time()
    shares, basis = pos["shares"], pos["cost_basis"]
    nums: dict = {"shares": shares, "cost_basis": round(basis, 4),
                  "avg_cost": round(basis / shares, 4) if shares else None}
    if book is None or not book.bids:
        return ExitDecision("HOLD", ["no bids: cannot exit (risk reported, not liquidating)"], nums)
    fee = market.fee if market else None
    lv = liquidation_value(book.bids, shares, fee)
    if lv.shares < shares - 1e-6:
        nums["unabsorbed_shares"] = round(shares - lv.shares, 2)
    net_value = lv.net_proceeds
    pnl = net_value - basis
    pnl_pct = pnl / basis if basis > 0 else 0.0
    peak = max(pos.get("peak_pnl_pct") or pnl_pct, pnl_pct)
    nums.update(best_bid=book.best_bid, exit_shares_absorbable=round(lv.shares, 2),
                net_exit_value=round(net_value, 4), exit_fees=round(lv.fees, 4),
                pnl=round(pnl, 4), pnl_pct=round(pnl_pct, 4), peak_pnl_pct=round(peak, 4),
                take_profit_pct=s.take_profit_pct)
    fair_fresh = fair is not None and fair.age(now) <= s.max_fair_age_s
    if fair is not None:
        nums.update(fair_prob=round(fair.prob, 4), fair_fresh=fair_fresh)
    reasons: list[str] = []
    exit_now = False

    if s.take_profit_pct > 0 and pnl_pct >= s.take_profit_pct and lv.shares >= shares - 1e-6:
        exit_now = True
        reasons.append(f"take profit: {pnl_pct:.1%} >= {s.take_profit_pct:.1%} of cost basis")

    if s.exit_on_edge_loss and fair_fresh and book.best_bid is not None:
        # Value of holding (per share) vs what we get by selling now (per share, after fees).
        sell_per_share = book.best_bid - fee_per_share(book.best_bid, fee)
        remaining_edge = fair.prob - sell_per_share
        nums["remaining_edge_vs_bid"] = round(remaining_edge, 4)
        if remaining_edge <= s.edge_exit_margin and pnl > 0:
            exit_now = True
            reasons.append(f"edge gone: fair {fair.prob:.3f} <= exit value {sell_per_share:.3f} "
                           f"+ margin {s.edge_exit_margin}")
        elif remaining_edge < -s.edge_exit_margin - 0.02:
            exit_now = True
            reasons.append(f"edge reversed: fair {fair.prob:.3f} well below exit value {sell_per_share:.3f}")

    if s.trailing_enabled and peak >= s.trailing_activation_pct and pnl_pct <= peak - s.trailing_distance_pct:
        exit_now = True
        reasons.append(f"trailing: pnl {pnl_pct:.1%} fell {s.trailing_distance_pct:.1%} from peak {peak:.1%}")

    if s.stop_loss_pct > 0 and pnl_pct <= -s.stop_loss_pct:
        exit_now = True
        reasons.append(f"stop-loss trigger at {pnl_pct:.1%} (fill price NOT guaranteed)")

    if s.max_holding_hours > 0 and pos.get("opened_at") and (now - pos["opened_at"]) / 3600 >= s.max_holding_hours:
        exit_now = True
        reasons.append(f"max holding period {s.max_holding_hours}h reached")

    start = market.game_start_time if market else pos.get("game_start_time")
    if start is not None and now >= start - 60:
        if s.pre_start_action == PreStartAction.EXIT:
            exit_now = True
            reasons.append("pre-start policy: exit before game start")
        elif s.pre_start_action == PreStartAction.EXIT_IF_PROFITABLE and pnl > 0:
            exit_now = True
            reasons.append("pre-start policy: exit profitable position before start")

    if not exit_now:
        why = []
        if s.hold_to_resolution and fair_fresh:
            why.append(f"holding: fair {fair.prob:.3f} still above exit value")
        elif not fair_fresh:
            why.append("estimate stale: not used as a reason to hold; profit/time rules still apply")
        if s.take_profit_pct > 0:
            why.append(f"take-profit at {s.take_profit_pct:.1%} not reached ({pnl_pct:.1%})")
        return ExitDecision("HOLD", why or ["no exit rule triggered"], nums, new_peak_pct=peak)

    tick = book.tick_size or 0.01
    # Sell into the bids we walked; never below the configured floor.
    worst = lv.worst_price if lv.worst_price is not None else book.best_bid
    min_price = max(s.min_exit_price, round_to_tick(worst, tick, "down"))
    sell_shares = lv.shares if lv.shares > 0 else 0.0
    if sell_shares <= 0:
        return ExitDecision("HOLD", reasons + ["no absorbable depth; not liquidating"], nums, new_peak_pct=peak)
    nums.update(exit_min_price=min_price, exit_shares=round(sell_shares, 2))
    return ExitDecision("EXIT", reasons, nums, shares=sell_shares, min_price=min_price, new_peak_pct=peak)
