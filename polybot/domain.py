"""Core domain objects shared by every module."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    FAK = "FAK"  # fill-and-kill: take what is available up to the limit, cancel the rest
    FOK = "FOK"  # fill-or-kill: all or nothing, immediately
    GTC = "GTC"  # good-til-cancelled resting limit order
    GTD = "GTD"  # good-til-date resting limit order


class OrderStatus(str, Enum):
    PENDING_SUBMIT = "PENDING_SUBMIT"  # persisted locally, not yet acknowledged
    OPEN = "OPEN"  # accepted / resting
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"  # ambiguous submission; must be reconciled before anything else


TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
    OrderStatus.FAILED,
}


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[Level]  # best (highest) first
    asks: list[Level]  # best (lowest) first
    received_at: float  # local wall-clock time we received it
    exchange_ts: float | None = None
    tick_size: float = 0.01
    min_order_size: float = 5.0
    source: str = "rest"
    hash: str | None = None

    def __post_init__(self) -> None:
        self.bids = sorted((l for l in self.bids if l.size > 0), key=lambda l: -l.price)
        self.asks = sorted((l for l in self.asks if l.size > 0), key=lambda l: l.price)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def age(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.received_at

    def is_crossed(self) -> bool:
        return self.best_bid is not None and self.best_ask is not None and self.best_bid >= self.best_ask


@dataclass(frozen=True)
class FeeSchedule:
    """Platform fee: fee = shares * rate * (p * (1 - p)) ** exponent.

    This is the formula the official SDK uses to budget fees
    (``adjust_buy_amount_for_fees``). ``taker_only`` means makers pay nothing.
    """

    rate: float
    exponent: float
    taker_only: bool = True


@dataclass
class OutcomeToken:
    token_id: str
    label: str


@dataclass
class MarketInfo:
    market_id: str
    condition_id: str | None
    question: str
    tokens: list[OutcomeToken]
    event_id: str | None = None
    event_title: str | None = None
    description: str = ""
    category: str | None = None
    slug: str | None = None
    active: bool = True
    closed: bool = False
    accepting_orders: bool = True
    enable_order_book: bool = True
    end_date: float | None = None  # epoch seconds
    game_start_time: float | None = None
    sports_market_type: str | None = None
    game_id: str | None = None
    tags: list[str] = field(default_factory=list)
    tick_size: float | None = None
    min_order_size: float | None = None
    fees_enabled: bool | None = None
    fee: FeeSchedule | None = None
    liquidity: float | None = None
    volume: float | None = None
    volume_24h: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    neg_risk: bool = False
    resolution_source: str | None = None
    resolution_status: str | None = None
    outcome_prices: list[float] | None = None
    fetched_at: float = field(default_factory=time.time)

    def token(self, token_id: str) -> OutcomeToken | None:
        return next((t for t in self.tokens if t.token_id == token_id), None)

    def other_token(self, token_id: str) -> OutcomeToken | None:
        return next((t for t in self.tokens if t.token_id != token_id), None)


@dataclass
class FairEstimate:
    """An independent probability estimate for one outcome token."""

    token_id: str
    prob: float
    uncertainty: float  # standard deviation of the estimate (probability units)
    source: str
    n_sources: int
    data_as_of: float  # newest underlying data timestamp (epoch s)
    fetched_at: float
    details: dict[str, Any] = field(default_factory=dict)

    def age(self, now: float) -> float:
        return now - max(self.fetched_at, 0)

    def data_age(self, now: float) -> float:
        return now - self.data_as_of


@dataclass
class OrderRequest:
    token_id: str
    market_id: str
    event_id: str | None
    side: Side
    size: float  # shares
    limit_price: float  # worst acceptable price (max for buys, min for sells)
    order_type: OrderType
    purpose: str  # "entry" | "exit"
    strategy: str
    reason: str
    expires_at: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Fill:
    order_id: str
    token_id: str
    side: Side
    price: float
    size: float
    fee: float
    ts: float
    trade_id: str | None = None
    simulated: bool = True
