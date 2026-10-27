"""Accounting shared by paper and live modes.

Definitions (all USDC):

* cost_basis   - what was paid for the shares still held, *including* entry fees.
* realized_pnl - sell/settlement proceeds net of exit fees minus the cost basis
                 of the shares disposed of.
* cash         - external cash flows (initial balance, deposits, withdrawals)
                 + sell/settlement proceeds - buy costs - all fees.
* reserved     - cash that open BUY orders could still consume (limit price
                 * remaining size + estimated taker fee).
* liquidation value - what the position would fetch if sold into the visible
                 bids *right now*, after fees. Shares the visible bids cannot
                 absorb are valued at ZERO (conservative).
* equity       - cash + liquidation value of all positions.
* exposure     - cost basis of open positions + reserved cash for open buys
                 (the most the bot can lose on positions it holds or may soon
                 hold, ignoring settlement failures).

Deposits and withdrawals are recorded as cash flows and are never counted as
trading profit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from polybot.db import Store
from polybot.domain import FeeSchedule, Fill, OrderBook, OrderStatus, Side
from polybot.pricing import fee_per_share, liquidation_value

EPS = 1e-9


@dataclass
class PositionView:
    token_id: str
    shares: float
    cost_basis: float
    avg_cost: float
    event_id: str | None
    market_id: str | None
    correlation_key: str | None
    best_bid: float | None
    liquidation_value: float
    liquidation_shares: float
    unrealized_conservative: float
    unrealized_pct: float
    row: dict = field(default_factory=dict)


@dataclass
class PortfolioSnapshot:
    mode: str
    ts: float
    cash: float
    reserved: float
    positions: list[PositionView]
    net_external_flows: float
    realized_pnl: float
    fees_paid: float

    @property
    def available_cash(self) -> float:
        return self.cash - self.reserved

    @property
    def positions_value(self) -> float:
        return sum(p.liquidation_value for p in self.positions)

    @property
    def equity(self) -> float:
        return self.cash + self.positions_value

    @property
    def open_cost(self) -> float:
        return sum(p.cost_basis for p in self.positions)

    @property
    def exposure(self) -> float:
        return self.open_cost + self.reserved

    @property
    def unrealized_conservative(self) -> float:
        return sum(p.unrealized_conservative for p in self.positions)

    @property
    def trading_equity(self) -> float:
        """Equity with external flows removed (used for drawdown / returns)."""
        return self.equity - self.net_external_flows

    def event_exposure(self, event_id: str | None, reserved_by_event: dict | None = None) -> float:
        if event_id is None:
            return 0.0
        v = sum(p.cost_basis for p in self.positions if p.event_id == event_id)
        return v + (reserved_by_event or {}).get(event_id, 0.0)


class Accounting:
    def __init__(self, store: Store, mode: str):
        self.store = store
        self.mode = mode

    # ---------------- cash flows ----------------
    def ensure_initial_balance(self, amount: float) -> None:
        if not self.store.list_cash_flows(self.mode):
            self.store.add_cash_flow(self.mode, "initial", amount, "starting balance")
            self.store.audit("system", "initial_balance", {"mode": self.mode, "amount": amount})

    def record_external_flow(self, kind: str, amount: float, note: str = "") -> None:
        assert kind in ("deposit", "withdrawal")
        signed = abs(amount) if kind == "deposit" else -abs(amount)
        self.store.add_cash_flow(self.mode, kind, signed, note)
        self.store.audit("user", f"cash_{kind}", {"mode": self.mode, "amount": signed})

    def net_external_flows(self) -> float:
        return sum(f["amount"] for f in self.store.list_cash_flows(self.mode))

    def net_external_flows_since(self, since: float) -> float:
        return sum(f["amount"] for f in self.store.list_cash_flows(self.mode)
                   if f["ts"] >= since and f["kind"] != "initial")

    def cash(self) -> float:
        total = self.net_external_flows()
        for f in self.store.list_fills(self.mode, limit=10_000_000):
            if f["side"] == Side.BUY.value:
                total -= f["price"] * f["size"] + f["fee"]
            else:
                total += f["price"] * f["size"] - f["fee"]
        return total

    # ---------------- fills ----------------
    def apply_fill(self, fill: Fill, context: dict) -> None:
        """Record a fill and update the position. ``context`` carries market labels."""
        if fill.trade_id and self.store.fill_exists(self.mode, fill.trade_id):
            return  # idempotent: the same exchange trade is never counted twice
        self.store.insert_fill({
            "mode": self.mode, "order_id": fill.order_id, "trade_id": fill.trade_id,
            "token_id": fill.token_id, "side": fill.side.value, "price": fill.price,
            "size": fill.size, "fee": fill.fee, "ts": fill.ts,
            "settlement_status": context.get("settlement_status"),
        })
        pos = self.store.get_position(self.mode, fill.token_id)
        now = fill.ts
        if fill.side == Side.BUY:
            if pos is None or pos["status"] != "OPEN":
                pos = {
                    "mode": self.mode, "token_id": fill.token_id, "shares": 0.0, "cost_basis": 0.0,
                    "realized_pnl": (pos or {}).get("realized_pnl", 0.0) if pos else 0.0,
                    "fees_paid": (pos or {}).get("fees_paid", 0.0) if pos else 0.0,
                    "opened_at": now, "closed_at": None, "status": "OPEN", "peak_pnl_pct": None,
                }
            pos["shares"] += fill.size
            pos["cost_basis"] += fill.price * fill.size + fill.fee
            pos["fees_paid"] = pos.get("fees_paid", 0.0) + fill.fee
            for k in ("market_id", "event_id", "event_title", "outcome_label", "question",
                      "correlation_key", "strategy", "entry_reason", "exit_plan",
                      "game_start_time", "end_date"):
                if context.get(k) is not None and not pos.get(k):
                    pos[k] = context[k]
        else:
            if pos is None or pos["shares"] <= EPS:
                # Selling something we do not hold locally: record, flag, never invent a position.
                self.store.audit("system", "sell_without_position",
                                 {"token_id": fill.token_id, "size": fill.size})
                return
            qty = min(fill.size, pos["shares"])
            basis_out = pos["cost_basis"] * (qty / pos["shares"])
            proceeds = fill.price * qty - fill.fee
            pos["realized_pnl"] = pos.get("realized_pnl", 0.0) + proceeds - basis_out
            pos["fees_paid"] = pos.get("fees_paid", 0.0) + fill.fee
            pos["shares"] -= qty
            pos["cost_basis"] -= basis_out
            if pos["shares"] <= 1e-6:
                pos["shares"] = 0.0
                pos["cost_basis"] = 0.0
                pos["status"] = context.get("close_status", "CLOSED")
                pos["closed_at"] = now
        self.store.upsert_position(pos)

    def settle(self, token_id: str, payout_per_share: float, ts: float | None = None) -> float | None:
        """Settle a resolved position. Returns proceeds, or None if nothing held."""
        pos = self.store.get_position(self.mode, token_id)
        if not pos or pos["status"] != "OPEN" or pos["shares"] <= EPS:
            return None
        shares = pos["shares"]
        fill = Fill(order_id="settlement", token_id=token_id, side=Side.SELL,
                    price=payout_per_share, size=shares, fee=0.0, ts=ts or time.time(),
                    trade_id=f"settle:{token_id}", simulated=self.mode == "paper")
        self.apply_fill(fill, {"close_status": "SETTLED", "settlement_status": "resolved"})
        return payout_per_share * shares

    # ---------------- snapshot ----------------
    def reserved_for_open_buys(self) -> tuple[float, dict[str, float]]:
        total = 0.0
        by_event: dict[str, float] = {}
        live = [OrderStatus.PENDING_SUBMIT.value, OrderStatus.OPEN.value,
                OrderStatus.PARTIAL.value, OrderStatus.UNKNOWN.value]
        for o in self.store.list_orders(self.mode, statuses=live, limit=100_000):
            if o["side"] != Side.BUY.value:
                continue
            remaining = max(0.0, o["size"] - (o["filled_size"] or 0.0))
            fee_est = (o.get("meta") or {}).get("fee_per_share_est", 0.0)
            amt = remaining * (o["limit_price"] + fee_est)
            total += amt
            if o.get("event_id"):
                by_event[o["event_id"]] = by_event.get(o["event_id"], 0.0) + amt
        return total, by_event

    def snapshot(
        self,
        book_for: Callable[[str], OrderBook | None],
        fee_for: Callable[[str], FeeSchedule | None],
        cash_override: float | None = None,
        depth_fraction: float = 1.0,
        now: float | None = None,
    ) -> PortfolioSnapshot:
        now = now or time.time()
        views: list[PositionView] = []
        realized = 0.0
        fees = 0.0
        for row in self.store.list_positions(self.mode, status=None):
            realized += row.get("realized_pnl") or 0.0
            fees += row.get("fees_paid") or 0.0
            if row["status"] != "OPEN" or row["shares"] <= EPS:
                continue
            book = book_for(row["token_id"])
            fee = fee_for(row["token_id"])
            if book is not None:
                lv = liquidation_value(book.bids, row["shares"], fee, depth_fraction)
                liq, liq_sh, bid = lv.net_proceeds, lv.shares, book.best_bid
            else:
                liq, liq_sh, bid = 0.0, 0.0, None  # no book -> assume nothing recoverable now
            unreal = liq - row["cost_basis"]
            views.append(PositionView(
                token_id=row["token_id"], shares=row["shares"], cost_basis=row["cost_basis"],
                avg_cost=row["cost_basis"] / row["shares"], event_id=row.get("event_id"),
                market_id=row.get("market_id"), correlation_key=row.get("correlation_key"),
                best_bid=bid, liquidation_value=liq, liquidation_shares=liq_sh,
                unrealized_conservative=unreal,
                unrealized_pct=unreal / row["cost_basis"] if row["cost_basis"] > EPS else 0.0,
                row=row,
            ))
        reserved, _ = self.reserved_for_open_buys()
        cash = cash_override if cash_override is not None else self.cash()
        return PortfolioSnapshot(
            mode=self.mode, ts=now, cash=cash, reserved=reserved, positions=views,
            net_external_flows=self.net_external_flows(), realized_pnl=realized, fees_paid=fees,
        )


def estimate_fee_per_share(price: float, fee: FeeSchedule | None) -> float:
    return fee_per_share(price, fee, is_taker=True)
