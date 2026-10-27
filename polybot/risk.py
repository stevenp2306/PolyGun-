"""Risk engine: every order passes through here before it is sent.

Loss-limit arithmetic (see docs/STRATEGY_AND_RISK.md for the plain-English version):

    trading_pnl     = equity - net_external_flows
                      (equity = cash + conservative liquidation value of positions;
                       external flows = initial balance + deposits - withdrawals)
    daily_pnl       = trading_pnl_now - trading_pnl_at_00:00_UTC
    daily lock      if daily_pnl <= -daily_loss_limit_pct * bankroll
    high-water mark = max trading_pnl seen
    drawdown_pct    = (hwm - trading_pnl_now) / (net_external_flows + hwm)
    drawdown lock   if drawdown_pct >= max_drawdown_pct

Because positions are marked at what the *visible bids* would pay right now
(after fees, unabsorbed shares at zero), the P&L used here is pessimistic.
Open buy orders are counted as exposure because they could still fill.

Limits are checked on every loop, not continuously: gaps, missing liquidity,
outages and settlement failures mean real losses CAN exceed the configured
limits.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from polybot.config import Settings
from polybot.db import Store
from polybot.domain import MarketInfo, OrderRequest, Side
from polybot.portfolio import PortfolioSnapshot

KILL = "kill_switch"
PAUSE = "paused"
LOCKS = "risk_locks"
HALT = "halt"
HWM = "hwm_trading_pnl"
DAY = "day_start"


def utc_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def correlation_key(m: MarketInfo) -> str:
    """Positions sharing a key are treated as correlated (same league, same UTC day)."""
    tag = next((t for t in m.tags if t and t not in ("sports", "games")), "sports")
    day = utc_date(m.game_start_time) if m.game_start_time else "undated"
    return f"{tag}:{day}"


@dataclass
class RiskStatus:
    can_open: bool
    can_trade_exits: bool
    reasons: list[str]
    daily_pnl: float
    drawdown_pct: float
    hwm: float
    bankroll: float
    triggered_now: list[str] = field(default_factory=list)
    locks: dict = field(default_factory=dict)


class RiskEngine:
    def __init__(self, settings: Settings, store: Store, mode: str):
        self.s = settings
        self.store = store
        self.mode = mode
        self.degraded_reasons: list[str] = []
        self.consecutive_failures = 0
        self.breaker_until = 0.0

    def _k(self, key: str) -> str:
        return f"{self.mode}:{key}"

    # ---------------- manual controls ----------------
    def kill(self, actor: str, reason: str) -> None:
        self.store.set(self._k(KILL), {"active": True, "ts": time.time(), "reason": reason, "actor": actor})
        self.store.audit(actor, "kill_switch", {"mode": self.mode, "reason": reason})

    def clear_kill(self, actor: str) -> None:
        self.store.delete_key(self._k(KILL))
        self.store.audit(actor, "kill_switch_cleared", {"mode": self.mode})

    def pause(self, actor: str) -> None:
        self.store.set(self._k(PAUSE), {"active": True, "ts": time.time(), "actor": actor})
        self.store.audit(actor, "pause_entries", {"mode": self.mode})

    def resume(self, actor: str) -> tuple[bool, str]:
        locks = self.locks()
        if locks:
            return False, f"risk lock active ({', '.join(locks)}): reset it deliberately first"
        if self.kill_active():
            return False, "kill switch active: clear it deliberately first"
        self.store.delete_key(self._k(PAUSE))
        self.store.audit(actor, "resume_entries", {"mode": self.mode})
        return True, "resumed"

    def reset_locks(self, actor: str, snapshot: PortfolioSnapshot | None = None) -> None:
        self.store.delete_key(self._k(LOCKS))
        if snapshot is not None:  # restart daily accounting from now
            self.store.set(self._k(DAY), {"date": utc_date(time.time()),
                                          "trading_pnl": snapshot.trading_equity})
            # The HWM is deliberately NOT reset: drawdown keeps being measured from the peak
            # unless the user also chooses to reset it.
        self.store.audit(actor, "risk_locks_reset", {"mode": self.mode})

    def reset_hwm(self, actor: str, snapshot: PortfolioSnapshot) -> None:
        self.store.set(self._k(HWM), snapshot.trading_equity)
        self.store.audit(actor, "hwm_reset", {"mode": self.mode, "value": snapshot.trading_equity})

    def halt(self, reason: str) -> None:
        self.store.set(self._k(HALT), {"active": True, "ts": time.time(), "reason": reason})
        self.store.audit("system", "halt", {"mode": self.mode, "reason": reason})

    def clear_halt(self, actor: str) -> None:
        self.store.delete_key(self._k(HALT))
        self.store.audit(actor, "halt_cleared", {"mode": self.mode})

    def kill_active(self) -> bool:
        return bool((self.store.get(self._k(KILL)) or {}).get("active"))

    def paused(self) -> bool:
        return bool((self.store.get(self._k(PAUSE)) or {}).get("active"))

    def locks(self) -> dict:
        return self.store.get(self._k(LOCKS)) or {}

    def halted(self) -> dict | None:
        h = self.store.get(self._k(HALT))
        return h if h and h.get("active") else None

    # ---------------- circuit breaker ----------------
    def record_failure(self, now: float | None = None) -> bool:
        now = now or time.time()
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.s.circuit_breaker_failures:
            self.breaker_until = now + self.s.circuit_breaker_cooldown_s
            self.consecutive_failures = 0
            return True
        return False

    def record_success(self) -> None:
        self.consecutive_failures = 0

    # ---------------- periodic update ----------------
    def bankroll(self, snap: PortfolioSnapshot) -> float:
        return max(0.0, min(self.s.bankroll_usd, snap.equity))

    def update(self, snap: PortfolioSnapshot, now: float | None = None) -> RiskStatus:
        now = now or time.time()
        pnl = snap.trading_equity
        day = self.store.get(self._k(DAY))
        if not day or day.get("date") != utc_date(now):
            day = {"date": utc_date(now), "trading_pnl": pnl}
            self.store.set(self._k(DAY), day)
        hwm = self.store.get(self._k(HWM))
        if hwm is None or pnl > hwm:
            hwm = pnl
            self.store.set(self._k(HWM), hwm)
        bankroll = self.bankroll(snap)
        daily_pnl = pnl - day["trading_pnl"]
        peak_equity = snap.net_external_flows + hwm
        dd = (hwm - pnl) / peak_equity if peak_equity > 0 else 0.0
        locks = self.locks()
        triggered = []
        limit_usd = self.s.daily_loss_limit_pct * self.s.bankroll_usd
        if daily_pnl <= -limit_usd and "daily_loss" not in locks:
            locks["daily_loss"] = {"ts": now, "daily_pnl": round(daily_pnl, 4),
                                   "limit": round(-limit_usd, 4)}
            triggered.append("daily_loss")
        if dd >= self.s.max_drawdown_pct and "drawdown" not in locks:
            locks["drawdown"] = {"ts": now, "drawdown_pct": round(dd, 4), "hwm": round(hwm, 4),
                                 "limit": self.s.max_drawdown_pct}
            triggered.append("drawdown")
        if triggered:
            self.store.set(self._k(LOCKS), locks)
            self.store.audit("risk", "lock_triggered", {"mode": self.mode, "locks": triggered})
        can_open, reasons = self.entry_gate(now)
        return RiskStatus(can_open=can_open, can_trade_exits=not self.kill_active(), reasons=reasons,
                          daily_pnl=daily_pnl, drawdown_pct=dd, hwm=hwm, bankroll=bankroll,
                          triggered_now=triggered, locks=locks)

    def entry_gate(self, now: float | None = None) -> tuple[bool, list[str]]:
        now = now or time.time()
        r = []
        if self.kill_active():
            r.append("kill switch active")
        if self.paused():
            r.append("new entries paused by user")
        for k in self.locks():
            r.append(f"risk lock: {k} (manual reset required)")
        h = self.halted()
        if h:
            r.append(f"halted: {h.get('reason')}")
        if now < self.breaker_until:
            r.append(f"circuit breaker open for {self.breaker_until - now:.0f}s after repeated failures")
        r.extend(self.degraded_reasons)
        return not r, r

    # ---------------- sizing caps ----------------
    def budget_for(self, market: MarketInfo, token_id: str, snap: PortfolioSnapshot,
                   reserved_by_event: dict[str, float], open_entry_tokens: set[str]) -> tuple[float, list[str]]:
        """Max additional USDC that may be committed to ``token_id`` right now."""
        s = self.s
        notes = []
        bankroll = self.bankroll(snap)
        held = {p.token_id for p in snap.positions}
        if token_id in held or token_id in open_entry_tokens:
            return 0.0, ["already holding / entering this outcome (no adding, no doubling down)"]
        if any(t.token_id in held or t.token_id in open_entry_tokens for t in market.tokens):
            return 0.0, ["already exposed to the other side of this market"]
        if len(snap.positions) + len(open_entry_tokens) >= s.max_open_positions:
            return 0.0, [f"max open positions ({s.max_open_positions}) reached"]
        caps = {
            "position": min(s.max_position_usd, s.max_position_pct * bankroll),
            "event": s.max_event_exposure_pct * bankroll - snap.event_exposure(market.event_id, reserved_by_event),
            "total": s.max_total_exposure_pct * bankroll - snap.exposure,
            "correlated": s.max_correlated_exposure_pct * bankroll - sum(
                p.cost_basis for p in snap.positions if p.correlation_key == correlation_key(market)),
            "cash_reserve": snap.available_cash - s.min_cash_reserve_usd,
        }
        name, cap = min(caps.items(), key=lambda kv: kv[1])
        if cap <= 0:
            notes.append(f"{name} limit leaves no room")
        else:
            notes.append(f"binding cap: {name} (${cap:.2f})")
        return max(0.0, cap), notes

    def check_order(self, req: OrderRequest, snap: PortfolioSnapshot, market: MarketInfo | None,
                    now: float | None = None) -> tuple[bool, list[str]]:
        """Final pre-submission check. Exits are allowed under loss locks (risk-reducing)."""
        now = now or time.time()
        r: list[str] = []
        if self.kill_active():
            return False, ["kill switch active: no new orders of any kind"]
        if req.size <= 0 or not (0 < req.limit_price < 1):
            return False, ["invalid size or price"]
        if req.side == Side.SELL:
            pos = next((p for p in snap.positions if p.token_id == req.token_id), None)
            if pos is None or req.size > pos.shares + 1e-6:
                r.append("sell size exceeds held shares (no shorting)")
            if req.limit_price < self.s.min_exit_price:
                r.append(f"exit limit {req.limit_price} below floor {self.s.min_exit_price}")
            return not r, r
        ok, gate = self.entry_gate(now)
        r.extend(gate)
        if market is None:
            r.append("unknown market")
            return False, r
        if not market.accepting_orders or market.closed:
            r.append("market not accepting orders")
        if not (self.s.min_order_price <= req.limit_price <= self.s.max_order_price):
            r.append("limit price outside allowed band")
        if market.game_start_time and (market.game_start_time - now) / 60 < self.s.min_minutes_to_start:
            r.append("too close to game start")
        cost = req.size * (req.limit_price + req.meta.get("fee_per_share_est", 0.0))
        budget, notes = self.budget_for(market, req.token_id, snap, req.meta.get("reserved_by_event", {}),
                                        req.meta.get("open_entry_tokens", set()))
        if cost > budget + 1e-6:
            r.append(f"order cost ${cost:.2f} exceeds risk budget ${budget:.2f} ({'; '.join(notes)})")
        if snap.available_cash - cost < self.s.min_cash_reserve_usd - 1e-6:
            r.append("would breach minimum cash reserve")
        return not r, r

    def describe(self) -> dict[str, Any]:
        return {k: getattr(self.s, k) for k in (
            "bankroll_usd", "max_position_usd", "max_position_pct", "max_event_exposure_pct",
            "max_total_exposure_pct", "max_correlated_exposure_pct", "daily_loss_limit_pct",
            "max_drawdown_pct", "max_open_positions", "min_cash_reserve_usd", "max_spread",
            "max_slippage", "max_book_age_s", "max_fair_age_s", "max_order_price", "min_order_price",
            "min_minutes_to_start", "max_hours_to_resolution", "max_depth_fraction", "kelly_fraction")}
