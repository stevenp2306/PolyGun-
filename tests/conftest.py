"""Shared fixtures. All tests use mocked data; nothing touches a real exchange or real funds."""

from __future__ import annotations

import time

import pytest

from polybot.config import Settings
from polybot.db import Store
from polybot.domain import FairEstimate, FeeSchedule, Level, MarketInfo, OrderBook, OutcomeToken
from polybot.providers.base import ProviderStatus

NOW = 1_800_000_000.0  # fixed clock for deterministic tests


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Never read the developer's real .env / secrets during tests."""
    import os
    for k in list(os.environ):
        if k.startswith("POLYBOT_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)


def make_settings(**kw) -> Settings:
    base = dict(database_url="sqlite://", data_dir="data", log_dir="logs", bankroll_usd=1000.0,
                paper_starting_balance_usd=1000.0, max_position_usd=50.0, max_position_pct=0.05,
                min_cash_reserve_usd=0.0, paper_latency_ms=0, paper_depth_haircut=1.0,
                min_market_liquidity_usd=0, kelly_fraction=1.0, max_book_age_s=60, use_websocket=False)
    base.update(kw)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def store() -> Store:
    return Store("sqlite://")


FEE = FeeSchedule(rate=0.05, exponent=1.0, taker_only=True)


def make_market(mid="M1", start_in_h=24.0, now=NOW, fee=FEE, **kw) -> MarketInfo:
    d = dict(
        market_id=mid, condition_id="0xcond" + mid, question="Will Arsenal win against Chelsea?",
        tokens=[OutcomeToken(f"{mid}-YES", "Yes"), OutcomeToken(f"{mid}-NO", "No")],
        event_id="E-" + mid, event_title="Arsenal vs. Chelsea",
        description="Resolves Yes if Arsenal wins in regular time (90 minutes plus stoppage time).",
        category="Sports", active=True, closed=False, accepting_orders=True, enable_order_book=True,
        end_date=now + (start_in_h + 3) * 3600, game_start_time=now + start_in_h * 3600,
        sports_market_type="moneyline", tags=["epl"], tick_size=0.01, min_order_size=5.0, fees_enabled=True,
        fee=fee, liquidity=50_000, volume=100_000,
    )
    d.update(kw)
    return MarketInfo(**d)


def make_book(token_id, bids, asks, now=NOW, tick=0.01, min_size=5.0) -> OrderBook:
    return OrderBook(token_id, [Level(p, s) for p, s in bids], [Level(p, s) for p, s in asks],
                     received_at=now, tick_size=tick, min_order_size=min_size)


def make_fair(token_id, prob, unc=0.01, now=NOW, age=0.0) -> FairEstimate:
    return FairEstimate(token_id, prob, unc, "test-books", 5, now - age, now - age, {})


class FakeProvider:
    name = "fake"

    def __init__(self, probs: dict[str, float] | None = None, unc=0.01, reason="ok"):
        self.probs = probs or {}
        self.unc = unc
        self.reason = reason
        self.fetched_at = NOW

    def status(self):
        return ProviderStatus(self.name, True)

    def refresh(self, now):
        self.fetched_at = now

    def estimate(self, market, now):
        out = {t.token_id: FairEstimate(t.token_id, self.probs[t.token_id], self.unc, "fake", 5,
                                        self.fetched_at, self.fetched_at, {})
               for t in market.tokens if t.token_id in self.probs}
        return (out, "ok") if out else (None, self.reason)


class FakeSource:
    def __init__(self, markets=None, books=None):
        self.markets = markets or []
        self.books = books or {}

    def fetch_sports_markets(self, limit):
        return list(self.markets)

    def fetch_market(self, mid):
        return next((m for m in self.markets if m.market_id == mid), None)

    def fetch_books(self, toks):
        return [self.books[t] for t in toks if t in self.books]


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def frozen_time(monkeypatch):
    t = {"now": NOW}
    monkeypatch.setattr(time, "time", lambda: t["now"])
    return t
