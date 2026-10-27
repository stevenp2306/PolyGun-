"""Market discovery and eligibility.

Uses the official ``polymarket-client`` SDK (``PublicClient``) for public,
unauthenticated market metadata. Each market's eligibility decision and the
reasons for it are stored so a selection can be reconstructed later.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Protocol

from polybot.config import Settings
from polybot.domain import FeeSchedule, MarketInfo, OutcomeToken

log = logging.getLogger(__name__)

SUPPORTED_SPORTS_MARKET_TYPES = {"moneyline"}


def _ts(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.timestamp()
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def market_from_sdk(m: Any) -> MarketInfo | None:
    """Convert an SDK ``polymarket.Market`` into our MarketInfo.

    Returns None for markets without CTF token IDs (we only trade binary
    markets addressed by token ID in this version).
    """
    yes, no = m.outcomes.yes, m.outcomes.no
    if not yes.token_id or not no.token_id:
        return None
    fs = m.trading.fee_schedule
    fee = None
    if fs is not None:
        fee = FeeSchedule(rate=float(fs.rate), exponent=float(fs.exponent), taker_only=bool(fs.taker_only))
    elif m.trading.fees_enabled is False:
        fee = FeeSchedule(rate=0.0, exponent=0.0, taker_only=True)
    ev = m.events[0] if m.events else None
    prices = None
    if yes.price is not None and no.price is not None:
        prices = [float(yes.price), float(no.price)]
    return MarketInfo(
        market_id=str(m.id),
        condition_id=str(m.condition_id) if m.condition_id else None,
        question=m.question or m.slug or str(m.id),
        tokens=[OutcomeToken(str(yes.token_id), yes.label), OutcomeToken(str(no.token_id), no.label)],
        event_id=str(ev.id) if ev else None,
        event_title=ev.title if ev else None,
        description=m.description or "",
        category=m.category,
        slug=m.slug,
        active=bool(m.state.active),
        closed=bool(m.state.closed),
        accepting_orders=bool(m.state.accepting_orders),
        enable_order_book=bool(m.state.enable_order_book),
        end_date=_ts(m.state.end_date),
        game_start_time=_ts(m.sports.game_start_time),
        sports_market_type=m.sports.sports_market_type,
        game_id=m.sports.game_id,
        tags=[t.slug or t.label or "" for t in m.tags],
        tick_size=_f(m.trading.minimum_tick_size),
        min_order_size=_f(m.trading.minimum_order_size),
        fees_enabled=m.trading.fees_enabled,
        fee=fee,
        liquidity=_f(m.metrics.liquidity_num if m.metrics.liquidity_num is not None else m.metrics.liquidity),
        volume=_f(m.metrics.volume_num if m.metrics.volume_num is not None else m.metrics.volume),
        volume_24h=_f(getattr(m.metrics, "volume_24hr", None)),
        best_bid=_f(m.prices.best_bid),
        best_ask=_f(m.prices.best_ask),
        neg_risk=bool(m.state.neg_risk),
        resolution_source=m.resolution.source,
        resolution_status=(m.resolution.uma_resolution_status.value
                           if m.resolution.uma_resolution_status is not None else None),
        outcome_prices=prices,
        fetched_at=time.time(),
    )


class MarketSource(Protocol):
    def fetch_sports_markets(self, limit: int) -> list[MarketInfo]: ...
    def fetch_market(self, market_id: str) -> MarketInfo | None: ...


class SdkMarketSource:
    """Public market metadata through the official SDK (no credentials)."""

    def __init__(self, client: Any | None = None):
        if client is None:
            from polymarket import PublicClient
            client = PublicClient()
        self.client = client

    def fetch_sports_markets(self, limit: int) -> list[MarketInfo]:
        out: list[MarketInfo] = []
        pag = self.client.list_markets(
            closed=False,
            sports_market_types=sorted(SUPPORTED_SPORTS_MARKET_TYPES),
            order="liquidityNum",
            ascending=False,
            page_size=min(limit, 500),
        )
        for m in pag.iter_items():
            try:
                info = market_from_sdk(m)
            except Exception as e:  # malformed market: skip, never guess
                log.warning("skipping unparseable market %s: %s", getattr(m, "id", "?"), e)
                continue
            if info:
                out.append(info)
            if len(out) >= limit:
                break
        return out

    def fetch_market(self, market_id: str) -> MarketInfo | None:
        m = self.client.get_market(id=market_id)
        return market_from_sdk(m) if m else None


@dataclass
class Eligibility:
    eligible: bool
    reasons: list[str]


def check_eligibility(m: MarketInfo, s: Settings, now: float | None = None) -> Eligibility:
    """Static eligibility checks (metadata only; book quality is checked later)."""
    now = now or time.time()
    r: list[str] = []
    if not m.active:
        r.append("market not active")
    if m.closed:
        r.append("market closed")
    if not m.accepting_orders:
        r.append("market not accepting orders")
    if not m.enable_order_book:
        r.append("order book disabled")
    if len(m.tokens) != 2 or any(not t.token_id for t in m.tokens):
        r.append("not a binary market with two token IDs")
    if m.sports_market_type not in SUPPORTED_SPORTS_MARKET_TYPES:
        r.append(f"unsupported market type '{m.sports_market_type}' (only moneyline is modelled)")
    if m.game_start_time is None:
        r.append("no scheduled game start time")
    else:
        mins = (m.game_start_time - now) / 60
        if mins < s.min_minutes_to_start:
            r.append(f"game starts in {mins:.0f} min (< {s.min_minutes_to_start:.0f} min); "
                     "pre-match strategy only, no reliable live data")
    horizon = m.end_date or m.game_start_time
    if horizon is not None and (horizon - now) / 3600 > s.max_hours_to_resolution:
        r.append(f"resolution more than {s.max_hours_to_resolution:.0f}h away")
    if m.tick_size is None or m.min_order_size is None:
        r.append("missing tick size / minimum order size")
    if m.fee is None:
        r.append("fee schedule unknown (cannot compute costs)")
    if (m.liquidity or 0) < s.min_market_liquidity_usd:
        r.append(f"liquidity {m.liquidity or 0:.0f} < {s.min_market_liquidity_usd:.0f}")
    if not m.description.strip():
        r.append("no resolution criteria text")
    return Eligibility(not r, r)


class Discovery:
    def __init__(self, source: MarketSource, settings: Settings, store: Any | None = None):
        self.source = source
        self.s = settings
        self.store = store
        self.markets: dict[str, MarketInfo] = {}
        self.eligible: dict[str, MarketInfo] = {}
        self.reasons: dict[str, list[str]] = {}
        self.last_refresh: float | None = None
        self.last_error: str | None = None

    def refresh(self, now: float | None = None) -> None:
        now = now or time.time()
        fetched = self.source.fetch_sports_markets(self.s.max_markets_monitored * 3)
        self._ingest(fetched, now)
        self.last_refresh = now
        self.last_error = None

    def _ingest(self, fetched: Iterable[MarketInfo], now: float) -> None:
        markets: dict[str, MarketInfo] = {}
        eligible: dict[str, MarketInfo] = {}
        reasons: dict[str, list[str]] = {}
        for m in fetched:
            markets[m.market_id] = m
            el = check_eligibility(m, self.s, now)
            reasons[m.market_id] = el.reasons
            if el.eligible and len(eligible) < self.s.max_markets_monitored:
                eligible[m.market_id] = m
            if self.store is not None:
                self.store.upsert_market(
                    m.market_id, token_ids=[t.token_id for t in m.tokens], question=m.question,
                    event_title=m.event_title, category=m.category, eligible=el.eligible,
                    reasons=el.reasons, snapshot=_snapshot(m))
        self.markets, self.eligible, self.reasons = markets, eligible, reasons

    def market_for_token(self, token_id: str) -> MarketInfo | None:
        for m in self.markets.values():
            if m.token(token_id):
                return m
        return None


def _snapshot(m: MarketInfo) -> dict:
    return {
        "question": m.question, "event_title": m.event_title, "slug": m.slug,
        "game_start_time": m.game_start_time, "end_date": m.end_date,
        "sports_market_type": m.sports_market_type, "tick_size": m.tick_size,
        "min_order_size": m.min_order_size,
        "fee": None if m.fee is None else vars(m.fee), "liquidity": m.liquidity,
        "volume": m.volume, "best_bid": m.best_bid, "best_ask": m.best_ask,
        "outcomes": [t.label for t in m.tokens], "tags": m.tags,
        "description": m.description[:2000], "fetched_at": m.fetched_at,
    }
