"""Data-provider interfaces.

A provider either returns an auditable, independently sourced estimate or
nothing. It must never fabricate data: missing credentials disable it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from polybot.domain import FairEstimate, MarketInfo


@dataclass
class ProviderStatus:
    name: str
    enabled: bool
    disabled_reason: str | None = None
    last_refresh: float | None = None
    last_error: str | None = None
    quota_remaining: float | None = None
    events_cached: int = 0
    extra: dict = field(default_factory=dict)


class ProbabilityProvider(Protocol):
    name: str

    def status(self) -> ProviderStatus: ...

    def refresh(self, now: float) -> None: ...

    def estimate(self, market: MarketInfo, now: float) -> tuple[dict[str, FairEstimate] | None, str]:
        """Return {token_id: FairEstimate} for both tokens, or (None, reason)."""
        ...


class LiveScoreProvider(Protocol):
    """Interface for in-play data. No implementation ships with this project:
    without a contracted low-latency feed, live-game strategies stay disabled."""

    name: str

    def status(self) -> ProviderStatus: ...

    def game_state(self, game_id: str) -> dict | None: ...
