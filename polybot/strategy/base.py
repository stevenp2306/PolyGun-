"""Strategy framework. Every strategy must publish a StrategyCard."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from polybot.domain import FairEstimate, MarketInfo, OrderBook, OutcomeToken


@dataclass(frozen=True)
class StrategyCard:
    name: str
    information_used: str
    fair_value_method: str
    why_edge_might_exist: str
    assumptions: list[str]
    disabled_when: list[str]
    how_tested: str
    expected_costs: str
    key_risks: list[str]
    validated: bool = False  # flips to True only with out-of-sample evidence (see evidence report)


@dataclass
class Candidate:
    strategy: str
    market: MarketInfo
    token: OutcomeToken
    fair: FairEstimate | None
    book: OrderBook | None
    approved: bool
    reasons: list[str]
    numbers: dict[str, Any] = field(default_factory=dict)
    shares: float = 0.0
    limit_price: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.market.event_title or self.market.question} :: {self.token.label}"
