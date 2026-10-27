"""Order-book state, feed health, WebSocket streaming and REST fallback.

Design:
* The WebSocket market channel (official SDK, async) pushes ``book`` snapshots
  and ``price_change`` deltas. Each update stamps the book with our local
  receive time.
* A REST poller refreshes every monitored book on a schedule regardless, so a
  silently dead socket or dropped messages cannot leave a book frozen.
* A book older than ``max_book_age_s`` is STALE: the strategy and risk engine
  refuse new entries on it.
* If the SDK reports dropped WebSocket messages, every book received over the
  socket is marked suspect until the next REST snapshot replaces it.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from polybot.domain import Level, OrderBook

log = logging.getLogger(__name__)


@dataclass
class FeedHealth:
    ws_connected: bool = False
    ws_last_message: float | None = None
    ws_reconnects: int = 0
    ws_dropped: int = 0
    rest_last_success: float | None = None
    rest_consecutive_failures: int = 0
    last_error: str | None = None
    clock_skew_s: float | None = None
    clock_checked_at: float | None = None
    notes: list[str] = field(default_factory=list)

    def status(self, now: float, max_age: float) -> str:
        fresh_rest = self.rest_last_success is not None and now - self.rest_last_success <= max_age
        fresh_ws = self.ws_connected and self.ws_last_message is not None and now - self.ws_last_message <= max_age
        if fresh_rest or fresh_ws:
            return "ok" if self.rest_consecutive_failures < 3 else "degraded"
        return "stale"


class BookStore:
    def __init__(self, max_age_s: float):
        self.books: dict[str, OrderBook] = {}
        self.max_age_s = max_age_s
        self.suspect: set[str] = set()

    def put(self, book: OrderBook) -> None:
        old = self.books.get(book.token_id)
        if old is not None and old.exchange_ts and book.exchange_ts and book.exchange_ts < old.exchange_ts:
            return  # out-of-order message; keep the newer book
        self.books[book.token_id] = book
        if book.source == "rest":
            self.suspect.discard(book.token_id)

    def get(self, token_id: str) -> OrderBook | None:
        return self.books.get(token_id)

    def fresh(self, token_id: str, now: float | None = None) -> OrderBook | None:
        """Return the book only if it is fresh, not crossed and not suspect."""
        b = self.books.get(token_id)
        if b is None:
            return None
        now = now if now is not None else time.time()
        if b.age(now) > self.max_age_s or b.is_crossed() or token_id in self.suspect:
            return None
        return b

    def quality(self, token_id: str, now: float | None = None) -> tuple[bool, str]:
        b = self.books.get(token_id)
        now = now if now is not None else time.time()
        if b is None:
            return False, "no order book received"
        if b.age(now) > self.max_age_s:
            return False, f"order book stale ({b.age(now):.0f}s > {self.max_age_s:.0f}s)"
        if b.is_crossed():
            return False, "order book crossed (inconsistent data)"
        if token_id in self.suspect:
            return False, "order book suspect after dropped stream messages"
        return True, "ok"

    def apply_price_change(self, token_id: str, side: str, price: float, size: float, now: float,
                           exchange_ts: float | None) -> None:
        b = self.books.get(token_id)
        if b is None:
            return  # deltas without a base snapshot are useless; REST will fill it in
        levels = b.bids if side.upper() == "BUY" else b.asks
        levels = [l for l in levels if abs(l.price - price) > 1e-9]
        if size > 0:
            levels.append(Level(price, size))
        if side.upper() == "BUY":
            b.bids = sorted(levels, key=lambda l: -l.price)
        else:
            b.asks = sorted(levels, key=lambda l: l.price)
        b.received_at = now
        b.exchange_ts = exchange_ts or b.exchange_ts
        b.source = "ws"


def book_from_sdk(ob: Any, now: float, source: str = "rest") -> OrderBook:
    ts = getattr(ob, "timestamp", None)
    return OrderBook(
        token_id=str(ob.asset_id),
        bids=[Level(float(l.price), float(l.size)) for l in ob.bids],
        asks=[Level(float(l.price), float(l.size)) for l in ob.asks],
        received_at=now,
        exchange_ts=ts.timestamp() if ts is not None else None,
        tick_size=float(getattr(ob, "tick_size", None) or 0.01),
        min_order_size=float(getattr(ob, "min_order_size", None) or 5),
        source=source,
        hash=getattr(ob, "hash", None),
    )


class BookSource(Protocol):
    def fetch_books(self, token_ids: list[str]) -> list[OrderBook]: ...


class SdkBookSource:
    def __init__(self, client: Any | None = None):
        if client is None:
            from polymarket import PublicClient
            client = PublicClient()
        self.client = client

    def fetch_books(self, token_ids: list[str]) -> list[OrderBook]:
        out: list[OrderBook] = []
        for i in range(0, len(token_ids), 50):
            chunk = token_ids[i:i + 50]
            now = time.time()
            for ob in self.client.get_order_books(asset_ids=chunk):
                out.append(book_from_sdk(ob, now))
        return out


async def rest_refresh(store: BookStore, source: BookSource, token_ids: list[str],
                       health: FeedHealth) -> None:
    if not token_ids:
        return
    try:
        books = await asyncio.to_thread(source.fetch_books, token_ids)
        for b in books:
            store.put(b)
        health.rest_last_success = time.time()
        health.rest_consecutive_failures = 0
    except Exception as e:
        health.rest_consecutive_failures += 1
        health.last_error = f"REST book refresh failed: {type(e).__name__}: {e}"
        log.warning(health.last_error)


class WebSocketFeed:
    """Market channel subscription with reconnect, backoff and jitter."""

    def __init__(self, store: BookStore, health: FeedHealth,
                 client_factory: Callable[[], Any] | None = None):
        self.store = store
        self.health = health
        self.client_factory = client_factory
        self.token_ids: list[str] = []
        self._resubscribe = asyncio.Event()
        self._stop = False

    def set_tokens(self, token_ids: list[str]) -> None:
        if sorted(token_ids) != sorted(self.token_ids):
            self.token_ids = list(token_ids)
            self._resubscribe.set()

    def stop(self) -> None:
        self._stop = True
        self._resubscribe.set()

    async def run(self) -> None:
        from polymarket import AsyncPublicClient
        from polymarket.streams import MarketSpec

        backoff = 1.0
        while not self._stop:
            if not self.token_ids:
                await asyncio.sleep(2)
                continue
            self._resubscribe.clear()
            try:
                client = self.client_factory() if self.client_factory else AsyncPublicClient()
                async with client:
                    handle = await client.subscribe(MarketSpec(asset_ids=self.token_ids))
                    async with handle:
                        self.health.ws_connected = True
                        backoff = 1.0
                        async for ev in handle:
                            self._on_event(ev)
                            dropped = getattr(handle, "dropped", 0) or 0
                            if dropped > self.health.ws_dropped:
                                self.health.ws_dropped = dropped
                                self.store.suspect.update(
                                    t for t, b in self.store.books.items() if b.source == "ws")
                                self.health.notes.append("ws messages dropped; books marked suspect")
                            if self._resubscribe.is_set() or self._stop:
                                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.health.last_error = f"websocket: {type(e).__name__}: {e}"
                log.warning(self.health.last_error)
            self.health.ws_connected = False
            if self._stop:
                break
            if not self._resubscribe.is_set():
                self.health.ws_reconnects += 1
                await asyncio.sleep(backoff + random.uniform(0, backoff / 2))
                backoff = min(backoff * 2, 60)

    def _on_event(self, ev: Any) -> None:
        now = time.time()
        self.health.ws_last_message = now
        t = getattr(ev, "type", None)
        p = getattr(ev, "payload", None)
        if p is None:
            return
        if t == "book":
            self.store.put(book_from_sdk(p, now, source="ws"))
        elif t == "price_change":
            ts = p.timestamp.timestamp() if getattr(p, "timestamp", None) else None
            for ch in p.price_changes:
                self.store.apply_price_change(str(ch.asset_id), ch.side, float(ch.price),
                                              float(ch.size), now, ts)
        elif t == "tick_size_change":
            b = self.store.get(str(p.asset_id))
            if b is not None:
                b.tick_size = float(p.new_tick_size)


async def check_clock_skew(url: str, health: FeedHealth, timeout: float = 10.0) -> float | None:
    """Compare our clock with the HTTP ``Date`` header of the exchange host."""
    import email.utils

    import httpx

    try:
        t0 = time.time()
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url)
        t1 = time.time()
        server = email.utils.parsedate_to_datetime(r.headers["date"]).timestamp()
        skew = (t0 + t1) / 2 - server  # Date header has 1s resolution
        health.clock_skew_s = skew
        health.clock_checked_at = t1
        return skew
    except Exception as e:
        health.last_error = f"clock check failed: {type(e).__name__}"
        return None
