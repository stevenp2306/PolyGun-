"""Paper trading: real market data, simulated orders. Results are SIMULATED.

Fill model (deliberately conservative):
* Orders become active only after ``paper_latency_ms`` and then trade against
  the book as it is *at that later time*, not at decision time.
* Marketable orders (FAK/FOK, or the crossing part of a limit order) walk the
  opposite side of the book up to the limit price, but assume only
  ``paper_depth_haircut`` of each displayed level is really available, plus
  optional extra adverse ticks of slippage. Taker fees apply.
* A resting limit order is NOT filled just because the market touches its
  price. It fills only when the opposite best price trades *through* it by at
  least one tick, and then only ``paper_passive_fill_fraction`` of the
  through-volume, at the order's own price (queue position is unknown).
* Partial fills, cancellations and expirations are supported.

Limitations: no market impact on later quotes, no queue modelling, no
settlement failures, no exchange outages or rejections beyond basic checks.
"""

from __future__ import annotations

import math
import time

from polybot.brokers.base import BookLookup, CancelResult, FeeLookup, SubmitResult
from polybot.config import Settings
from polybot.db import Store
from polybot.domain import Fill, OrderRequest, OrderStatus, OrderType, Side
from polybot.portfolio import Accounting
from polybot.pricing import fee_for, walk_book

ACTIVE = [OrderStatus.OPEN.value, OrderStatus.PARTIAL.value]


class PaperBroker:
    mode = "paper"
    simulated = True

    def __init__(self, settings: Settings, store: Store, accounting: Accounting):
        self.s = settings
        self.store = store
        self.acct = accounting

    def cash_override(self) -> float | None:
        return None

    def submit(self, req: OrderRequest, context: dict) -> SubmitResult:
        now = time.time()
        tick = context.get("tick_size") or 0.01
        min_size = context.get("min_order_size") or 0
        if abs(round(req.limit_price / tick) * tick - req.limit_price) > 1e-9:
            return self._reject(req, context, f"price {req.limit_price} not on tick grid {tick}")
        if req.size < min_size - 1e-9:
            return self._reject(req, context, f"size {req.size} below minimum {min_size}")
        oid = self.store.insert_order({
            "mode": self.mode, "token_id": req.token_id, "market_id": req.market_id,
            "event_id": req.event_id, "side": req.side.value, "order_type": req.order_type.value,
            "purpose": req.purpose, "strategy": req.strategy, "limit_price": req.limit_price,
            "size": req.size, "status": OrderStatus.OPEN.value, "reason": req.reason,
            "expires_at": req.expires_at, "activate_at": now + self.s.paper_latency_ms / 1000.0,
            "exchange_order_id": f"SIM-{int(now * 1000)}", "meta": {**req.meta, **_jsonable(context)},
        })
        return SubmitResult(True, oid, OrderStatus.OPEN.value, "simulated order accepted",
                            exchange_order_id=f"SIM-{oid[:8]}")

    def _reject(self, req: OrderRequest, context: dict, msg: str) -> SubmitResult:
        oid = self.store.insert_order({
            "mode": self.mode, "token_id": req.token_id, "market_id": req.market_id,
            "event_id": req.event_id, "side": req.side.value, "order_type": req.order_type.value,
            "purpose": req.purpose, "strategy": req.strategy, "limit_price": req.limit_price,
            "size": req.size, "status": OrderStatus.REJECTED.value, "reason": req.reason, "error": msg,
            "meta": {**req.meta, **_jsonable(context)},
        })
        return SubmitResult(False, oid, OrderStatus.REJECTED.value, msg)

    def cancel(self, order_id: str) -> CancelResult:
        o = self.store.get_order(order_id)
        if o is None or o["status"] not in ACTIVE:
            return CancelResult(order_id, True, "not open")
        self.store.update_order(order_id, status=OrderStatus.CANCELED.value)
        return CancelResult(order_id, True, "simulated cancel confirmed")

    def step(self, now: float, books: BookLookup, fees: FeeLookup) -> list[Fill]:
        out: list[Fill] = []
        for o in self.store.list_orders(self.mode, statuses=ACTIVE, limit=10_000):
            if o["expires_at"] and now >= o["expires_at"]:
                self.store.update_order(o["id"], status=OrderStatus.EXPIRED.value)
                continue
            if o["activate_at"] and now < o["activate_at"]:
                continue
            book = books(o["token_id"])
            otype = OrderType(o["order_type"])
            if book is None:
                if otype in (OrderType.FAK, OrderType.FOK) and now - (o["activate_at"] or now) > self.s.max_book_age_s:
                    self.store.update_order(o["id"], status=OrderStatus.CANCELED.value,
                                            error="no fresh book; immediate order killed")
                continue
            out.extend(self._match(o, book, fees(o["token_id"]), now, otype))
        return out

    def _match(self, o: dict, book, fee, now: float, otype: OrderType) -> list[Fill]:
        side = Side(o["side"])
        remaining = o["size"] - (o["filled_size"] or 0.0)
        if remaining <= 1e-9:
            return []
        levels = book.asks if side == Side.BUY else book.bids
        meta = o.get("meta") or {}
        fills: list[Fill] = []
        tick = book.tick_size or 0.01
        if otype in (OrderType.FAK, OrderType.FOK) or not meta.get("marketable_checked"):
            w = walk_book(levels, side.value, shares=remaining, limit_price=o["limit_price"],
                          fee=None, depth_fraction=self.s.paper_depth_haircut)
            if otype == OrderType.FOK and not w.fully_filled:
                self.store.update_order(o["id"], status=OrderStatus.CANCELED.value,
                                        error="FOK not fully fillable (simulated)")
                return []
            for lvl in w.fills:
                px = lvl.price + (tick * self.s.paper_slippage_ticks if side == Side.BUY
                                  else -tick * self.s.paper_slippage_ticks)
                px = min(px, o["limit_price"]) if side == Side.BUY else max(px, o["limit_price"])
                fills.append(Fill(o["id"], o["token_id"], side, px, lvl.size,
                                  fee_for(lvl.size, px, fee, is_taker=True), now, simulated=True))
            if otype not in (OrderType.FAK, OrderType.FOK):
                meta["marketable_checked"] = True
                self.store.update_order(o["id"], meta=meta)
        else:
            # Passive resting order: require a trade-through by >= 1 tick.
            lim = o["limit_price"]
            if side == Side.BUY:
                through = [l for l in book.asks if l.price <= lim - tick + 1e-9]
            else:
                through = [l for l in book.bids if l.price >= lim + tick - 1e-9]
            vol = sum(l.size for l in through) * self.s.paper_passive_fill_fraction
            qty = math.floor(min(vol, remaining) * 100) / 100
            if qty > 0:
                fills.append(Fill(o["id"], o["token_id"], side, lim, qty,
                                  fee_for(qty, lim, fee, is_taker=False), now, simulated=True))
        filled_now = sum(f.size for f in fills)
        total_filled = (o["filled_size"] or 0.0) + filled_now
        if fills:
            prev_notional = (o["avg_fill_price"] or 0.0) * (o["filled_size"] or 0.0)
            avg = (prev_notional + sum(f.price * f.size for f in fills)) / total_filled
            ctx = dict(meta)
            for f in fills:
                f.trade_id = f"SIMTRADE-{o['id'][:10]}-{int(now * 1e6)}-{fills.index(f)}"
                self.acct.apply_fill(f, ctx)
            status = OrderStatus.FILLED if total_filled >= o["size"] - 1e-6 else OrderStatus.PARTIAL
            self.store.update_order(o["id"], filled_size=total_filled, avg_fill_price=avg,
                                    fees=(o["fees"] or 0.0) + sum(f.fee for f in fills), status=status.value)
        if otype in (OrderType.FAK, OrderType.FOK) and total_filled < o["size"] - 1e-6:
            # Remainder of an immediate order is killed; filled_size shows any partial fill.
            self.store.update_order(o["id"], status=OrderStatus.CANCELED.value,
                                    error=("remainder killed after partial fill (simulated)" if total_filled > 0
                                           else "no liquidity within limit (simulated)"))
        return fills


def _jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [x for x in v if isinstance(x, (str, int, float, bool))]
        elif isinstance(v, dict):
            out[k] = _jsonable(v)
        elif isinstance(v, set):
            out[k] = sorted(str(x) for x in v)
    return out
