"""Broker interface shared by paper and live execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from polybot.domain import FeeSchedule, Fill, OrderBook, OrderRequest

BookLookup = Callable[[str], OrderBook | None]
FeeLookup = Callable[[str], FeeSchedule | None]


@dataclass
class SubmitResult:
    ok: bool
    order_id: str | None  # local order id
    status: str
    message: str = ""
    exchange_order_id: str | None = None


@dataclass
class CancelResult:
    order_id: str
    confirmed: bool
    message: str


class Broker(Protocol):
    mode: str
    simulated: bool

    def submit(self, req: OrderRequest, context: dict) -> SubmitResult: ...

    def cancel(self, order_id: str) -> CancelResult: ...

    def step(self, now: float, books: BookLookup, fees: FeeLookup) -> list[Fill]: ...

    def cash_override(self) -> float | None: ...
