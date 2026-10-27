"""Executable pricing: fees, order-book walking, slippage and expected value.

Everything here works on *executable* prices from the order book, never on a
displayed midpoint.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from polybot.domain import FeeSchedule, Level

EPS = 1e-9


def fee_per_share(price: float, fee: FeeSchedule | None, is_taker: bool = True) -> float:
    """Platform fee per share at ``price`` (USDC).

    fee = rate * (p * (1 - p)) ** exponent per share. When a market's fee
    schedule is unknown, callers must decide whether to treat it as
    ineligible; this function returns 0 only when ``fee`` is None.
    """
    if fee is None or fee.rate <= 0:
        return 0.0
    if not is_taker and fee.taker_only:
        return 0.0
    p = min(max(price, 0.0), 1.0)
    return fee.rate * (p * (1.0 - p)) ** fee.exponent


def fee_for(shares: float, price: float, fee: FeeSchedule | None, is_taker: bool = True) -> float:
    return shares * fee_per_share(price, fee, is_taker)


@dataclass
class WalkResult:
    shares: float = 0.0
    notional: float = 0.0  # sum(price * shares), excluding fees
    fees: float = 0.0
    worst_price: float | None = None
    fully_filled: bool = False
    fills: list[Level] = field(default_factory=list)

    @property
    def avg_price(self) -> float | None:
        return self.notional / self.shares if self.shares > EPS else None

    @property
    def all_in_per_share(self) -> float | None:
        """Average cost (buy) or proceeds (sell) per share including fees."""
        if self.shares <= EPS:
            return None
        return (self.notional + self.fees) / self.shares

    @property
    def net_proceeds(self) -> float:
        """For sells: what we actually receive."""
        return self.notional - self.fees

    @property
    def total_cost(self) -> float:
        """For buys: what we actually pay."""
        return self.notional + self.fees


def walk_book(
    levels: list[Level],
    side: str,
    shares: float | None = None,
    budget: float | None = None,
    limit_price: float | None = None,
    fee: FeeSchedule | None = None,
    depth_fraction: float = 1.0,
    lot: float = 0.01,
) -> WalkResult:
    """Simulate taking liquidity.

    ``levels`` must be the opposite side of the book, best first (asks for a
    BUY, bids for a SELL). Stops at ``shares``, at ``budget`` (buys: total
    cost incl. fees), or at ``limit_price``. ``depth_fraction`` assumes only
    part of the displayed size is really available (competition, cancels).
    """
    side = side.upper()
    res = WalkResult()
    remaining = shares if shares is not None else math.inf
    money_left = budget if budget is not None else math.inf
    for lvl in levels:
        if remaining <= EPS or money_left <= EPS:
            break
        if limit_price is not None:
            if side == "BUY" and lvl.price > limit_price + EPS:
                break
            if side == "SELL" and lvl.price < limit_price - EPS:
                break
        avail = lvl.size * depth_fraction
        take = min(avail, remaining)
        if side == "BUY" and budget is not None:
            per_share = lvl.price + fee_per_share(lvl.price, fee)
            take = min(take, money_left / per_share)
        take = math.floor(take / lot + EPS) * lot
        if take <= EPS:
            continue
        f = fee_for(take, lvl.price, fee)
        res.shares += take
        res.notional += take * lvl.price
        res.fees += f
        res.worst_price = lvl.price
        res.fills.append(Level(lvl.price, take))
        remaining -= take
        if side == "BUY":
            money_left -= take * lvl.price + f
    if shares is not None:
        res.fully_filled = res.shares >= shares - EPS
    return res


def round_to_tick(price: float, tick: float, direction: str) -> float:
    """Round a price onto the tick grid. direction 'down' or 'up'."""
    if tick <= 0:
        return price
    n = price / tick
    n = math.floor(n + 1e-9) if direction == "down" else math.ceil(n - 1e-9)
    decimals = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return round(n * tick, decimals + 1)


@dataclass
class EntryQuote:
    """Everything needed to judge a BUY at executable prices."""

    shares: float
    avg_price: float
    fees: float
    total_cost: float
    all_in_per_share: float
    best_ask: float
    slippage: float  # avg fill price minus best ask
    worst_price: float


def quote_buy(
    asks: list[Level],
    budget: float,
    limit_price: float,
    fee: FeeSchedule | None,
    depth_fraction: float,
    lot: float = 0.01,
    max_shares: float | None = None,
) -> EntryQuote | None:
    if not asks:
        return None
    w = walk_book(asks, "BUY", shares=max_shares, budget=budget, limit_price=limit_price, fee=fee,
                  depth_fraction=depth_fraction, lot=lot)
    if w.shares <= EPS:
        return None
    best = asks[0].price
    return EntryQuote(
        shares=w.shares,
        avg_price=w.avg_price or best,
        fees=w.fees,
        total_cost=w.total_cost,
        all_in_per_share=w.all_in_per_share or best,
        best_ask=best,
        slippage=(w.avg_price or best) - best,
        worst_price=w.worst_price or best,
    )


def expected_value_per_share(prob: float, all_in_cost: float) -> float:
    """EV of buying one share that pays $1 if the outcome happens."""
    return prob - all_in_cost


def kelly_fraction_binary(prob: float, all_in_cost: float) -> float:
    """Full-Kelly bankroll fraction for a $1-payout contract bought at cost c.

    f* = (p - c) / (1 - c). Returned value is clipped to [0, 1].
    """
    if all_in_cost >= 1 or all_in_cost <= 0:
        return 0.0
    return max(0.0, min(1.0, (prob - all_in_cost) / (1 - all_in_cost)))


def liquidation_value(
    bids: list[Level], shares: float, fee: FeeSchedule | None, depth_fraction: float = 1.0
) -> WalkResult:
    """Conservative value of selling ``shares`` into the visible bids now.

    Shares that cannot be absorbed by visible depth are valued at zero.
    """
    return walk_book(bids, "SELL", shares=shares, fee=fee, depth_fraction=depth_fraction)
