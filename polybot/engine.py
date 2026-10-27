"""The trading engine: monitors 24/7, trades only when every rule passes.

One ``tick()`` (every ``loop_interval_s``):
 1. process kill-switch requests (cancel the bot's own open orders)
 2. advance the broker (paper fills / live reconciliation)
 3. settle resolved markets
 4. mark the portfolio conservatively and update risk limits
 5. derive data-quality status (feeds, clock, providers)
 6. cancel entry orders close to game start
 7. evaluate exits for open positions
 8. evaluate entries (only if the entry gate is open)
 9. persist equity snapshot and dashboard state

Background tasks refresh market metadata, odds, order books (REST) and the
WebSocket feed, and in live mode run reconciliation, geoblock checks and the
optional order heartbeat.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from polybot.alerts import Alerter
from polybot.backtest.data import ResearchRecorder
from polybot.brokers.live_gate import LiveGate, fetch_geoblock
from polybot.brokers.paper import PaperBroker
from polybot.config import Mode, Settings
from polybot.db import Store
from polybot.discovery import Discovery, MarketSource
from polybot.domain import FairEstimate, MarketInfo, OrderRequest, OrderStatus, OrderType, Side
from polybot.exits import evaluate_exit
from polybot.marketdata import BookSource, BookStore, FeedHealth, WebSocketFeed, check_clock_skew, rest_refresh
from polybot.portfolio import Accounting, PortfolioSnapshot
from polybot.pricing import fee_per_share
from polybot.risk import RiskEngine, correlation_key
from polybot.strategy.base import Candidate
from polybot.strategy.sports_value import SportsValueStrategy

log = logging.getLogger(__name__)

_NETWORK_ERRORS = {"TransportError", "TimeoutError", "RateLimitError", "ConnectError", "ConnectTimeout",
                   "ReadTimeout", "ProxyError", "RemoteProtocolError", "ConnectionLostError"}

ACTIVE = [OrderStatus.PENDING_SUBMIT.value, OrderStatus.OPEN.value, OrderStatus.PARTIAL.value,
          OrderStatus.UNKNOWN.value]


class Engine:
    def __init__(self, settings: Settings, store: Store, market_source: MarketSource,
                 book_source: BookSource, providers: list[Any], broker: Any | None = None,
                 alerter: Alerter | None = None, live_client_factory: Any = None):
        self.s = settings
        self.store = store
        self.mode = settings.mode.value
        self.acct = Accounting(store, self.mode)
        self.risk = RiskEngine(settings, store, self.mode)
        self.gate = LiveGate(settings)
        self.alert = alerter or Alerter(settings, store)
        self.discovery = Discovery(market_source, settings, store)
        self.book_source = book_source
        self.books = BookStore(settings.max_book_age_s)
        self.health = FeedHealth()
        self.providers = providers
        self.strategy = SportsValueStrategy(settings)
        self.recorder = ResearchRecorder(settings.data_dir / "research")
        self.ws: WebSocketFeed | None = None
        self.held_markets: dict[str, MarketInfo] = {}
        self.fairs: dict[str, tuple[dict[str, FairEstimate] | None, str]] = {}
        self.last_candidates: list[dict] = []
        self.last_exit_views: dict[str, dict] = {}
        self.last_decision_key: dict[str, tuple] = {}
        self.state = "starting"
        self.started_at = time.time()
        self.last_tick: float | None = None
        self.last_tick_error: str | None = None
        self.tick_errors = 0
        self.last_equity_snap = 0.0
        self.last_snapshot: PortfolioSnapshot | None = None
        self.last_risk: Any = None
        self.stopping = False
        if broker is not None:
            self.broker = broker
        elif settings.mode == Mode.PAPER:
            self.broker = PaperBroker(settings, store, self.acct)
        else:
            from polybot.brokers.live import LiveBroker
            self.broker = LiveBroker(settings, store, self.acct, self.gate, client_factory=live_client_factory,
                                     alert=self.alert, on_failure=self._on_failure,
                                     on_success=self.risk.record_success, halt=self._halt)

    # ---------------- helpers ----------------
    def _on_failure(self) -> bool:
        tripped = self.risk.record_failure()
        if tripped:
            self.alert("circuit_breaker", f"{self.s.circuit_breaker_failures} consecutive order/API failures: "
                       f"new orders paused for {self.s.circuit_breaker_cooldown_s:.0f}s", level="error")
        return tripped

    def _halt(self, reason: str) -> None:
        self.risk.halt(reason)
        self.alert("halt", f"New orders halted: {reason}. Manual attention required.", level="critical")

    def market_for_token(self, token_id: str) -> MarketInfo | None:
        m = self.discovery.market_for_token(token_id)
        if m is None:
            for hm in self.held_markets.values():
                if hm.token(token_id):
                    return hm
        return m

    def fee_for(self, token_id: str):
        m = self.market_for_token(token_id)
        return m.fee if m else None

    def monitored_tokens(self) -> list[str]:
        toks = [t.token_id for m in self.discovery.eligible.values() for t in m.tokens]
        toks += [p["token_id"] for p in self.store.list_positions(self.mode)]
        return sorted(set(toks))

    def snapshot(self, now: float | None = None) -> PortfolioSnapshot:
        return self.acct.snapshot(lambda t: self.books.get(t), self.fee_for,
                                  cash_override=self.broker.cash_override(), now=now)

    # ---------------- lifecycle ----------------
    def startup(self) -> None:
        if self.mode == "paper":
            self.acct.ensure_initial_balance(self.s.paper_starting_balance_usd)
        self.store.audit("system", "startup", {"mode": self.mode})
        self.alert("restart", f"Bot started in {self.mode.upper()} mode.", level="info", dedupe=False)
        for p in self.provider_statuses():
            if not p["enabled"]:
                self.alert("provider_disabled", f"Data provider {p['name']} disabled: {p['disabled_reason']}",
                           level="warning")

    def provider_statuses(self) -> list[dict]:
        out = []
        for p in self.providers:
            st = p.status()
            out.append({"name": st.name, "enabled": st.enabled, "disabled_reason": st.disabled_reason,
                        "last_refresh": st.last_refresh, "last_error": st.last_error,
                        "quota_remaining": st.quota_remaining, "events_cached": st.events_cached})
        return out

    # ---------------- fair value ----------------
    def estimate_fair(self, m: MarketInfo, now: float) -> tuple[dict[str, FairEstimate] | None, str]:
        if not self.providers:
            return None, "no probability provider configured"
        results = []
        reasons = []
        for p in self.providers:
            est, why = p.estimate(m, now)
            if est:
                results.append(est)
            else:
                reasons.append(f"{p.name}: {why}")
        if not results:
            return None, "; ".join(reasons)
        if len(results) == 1:
            return results[0], "ok"
        combined: dict[str, FairEstimate] = {}
        for tok in results[0]:
            ps = [r[tok].prob for r in results if tok in r]
            if max(ps) - min(ps) > self.s.max_bookmaker_disagreement:
                return None, f"providers disagree ({min(ps):.3f}..{max(ps):.3f})"
            base = results[0][tok]
            combined[tok] = FairEstimate(tok, sum(ps) / len(ps),
                                         max(r[tok].uncertainty for r in results if tok in r),
                                         "+".join(r[tok].source for r in results if tok in r),
                                         sum(r[tok].n_sources for r in results if tok in r),
                                         min(r[tok].data_as_of for r in results if tok in r),
                                         min(r[tok].fetched_at for r in results if tok in r), base.details)
        return combined, "ok"

    # ---------------- control ----------------
    def kill(self, actor: str, reason: str = "manual kill switch") -> list[dict]:
        self.risk.kill(actor, reason)
        return self._process_kill(force=True)

    def _process_kill(self, force: bool = False) -> list[dict]:
        k = self.store.get(f"{self.mode}:kill_switch") or {}
        if not k.get("active"):
            return []
        done = self.store.get(f"{self.mode}:kill_processed")
        if done == k.get("ts") and not force:
            return []
        results = []
        for o in self.store.list_orders(self.mode, statuses=ACTIVE, limit=10_000):
            r = self.broker.cancel(o["id"])
            results.append({"order_id": o["id"], "exchange_order_id": o.get("exchange_order_id"),
                            "confirmed": r.confirmed, "message": r.message})
        self.store.set(f"{self.mode}:kill_processed", k.get("ts"))
        self.store.set(f"{self.mode}:kill_result", {"ts": time.time(), "results": results})
        failed = [r for r in results if not r["confirmed"]]
        self.alert("kill_switch", f"KILL SWITCH: new orders stopped; {len(results) - len(failed)} cancels "
                   f"confirmed, {len(failed)} NOT confirmed. Positions remain open and monitored.",
                   level="critical", dedupe=False)
        return results

    def cancel_entries(self, why: str) -> None:
        for o in self.store.list_orders(self.mode, statuses=ACTIVE, limit=10_000):
            if o["purpose"] == "entry":
                r = self.broker.cancel(o["id"])
                self.store.audit("system", "cancel_entry", {"order": o["id"], "why": why,
                                                            "confirmed": r.confirmed, "msg": r.message})

    # ---------------- main tick ----------------
    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        if self.risk.kill_active():
            self._process_kill()

        fills = self.broker.step(now, lambda t: self.books.fresh(t, now), self.fee_for)
        for f in fills:
            m = self.market_for_token(f.token_id)
            what = f"{m.event_title or m.question} / {m.token(f.token_id).label}" if m and m.token(f.token_id) else f.token_id[:10]
            self.alert("fill", f"{'SIMULATED ' if f.simulated else ''}{f.side.value} fill: {f.size:.2f} sh "
                       f"@ {f.price:.3f} (fee {f.fee:.4f}) - {what}", level="info", dedupe=False)

        self._settle_resolved(now)

        snap = self.snapshot(now)
        rs = self.risk.update(snap, now)
        self.last_snapshot, self.last_risk = snap, rs
        if rs.triggered_now:
            self.cancel_entries("risk lock")
            self.alert("risk_limit", f"Risk limit hit: {', '.join(rs.triggered_now)} "
                       f"(daily P&L {rs.daily_pnl:+.2f}, drawdown {rs.drawdown_pct:.1%}). New entries "
                       "stopped, entry orders cancelled; manual reset required.", level="critical", dedupe=False)

        self._update_quality(now)
        self._pre_start_cancels(now)
        if not self.risk.kill_active():
            self._manage_exits(snap, now)
        can_open, gate_reasons = self.risk.entry_gate(now)
        if self.mode == "live":
            g = self.gate.check()
            if not g.allowed:
                can_open, gate_reasons = False, gate_reasons + g.failures
        self._evaluate_entries(snap, now, can_open, gate_reasons)

        if now - self.last_equity_snap >= 60:
            snap = self.snapshot(now)
            self.store.add_equity_snapshot(self.mode, snap.cash, snap.equity, snap.exposure,
                                           snap.net_external_flows, ts=now)
            self.last_equity_snap = now
            if now - getattr(self, "_last_prune", 0) > 3600:  # keep SKIP history bounded (TRADE/EXIT kept)
                self.store.prune_decisions(now - 7 * 86400)
                self._last_prune = now
        self.last_tick = now
        self.state = "degraded" if self.risk.degraded_reasons else "running"
        self.store.set("engine_heartbeat", {"ts": now, "mode": self.mode, "state": self.state})

    def _update_quality(self, now: float) -> None:
        r = []
        if self.discovery.last_refresh is None:
            r.append("market discovery has not completed yet")
        elif now - self.discovery.last_refresh > 3 * self.s.discovery_interval_s:
            r.append("market metadata stale")
        fs = self.health.status(now, self.s.max_book_age_s)
        if fs == "stale" and self.monitored_tokens():
            r.append("market data feed stale")
        if self.health.clock_skew_s is not None and abs(self.health.clock_skew_s) > self.s.max_clock_skew_s:
            r.append(f"clock skew {self.health.clock_skew_s:+.1f}s exceeds {self.s.max_clock_skew_s}s")
        newly = set(r) - set(self.risk.degraded_reasons)
        for reason in newly:
            self.alert("data_quality", f"Degraded: {reason}. No new positions until it clears.", level="warning")
        self.risk.degraded_reasons = r

    def _pre_start_cancels(self, now: float) -> None:
        for o in self.store.list_orders(self.mode, statuses=ACTIVE, limit=10_000):
            if o["purpose"] != "entry":
                continue
            m = self.market_for_token(o["token_id"])
            if m and m.game_start_time and m.game_start_time - now <= self.s.cancel_entries_before_start_min * 60:
                r = self.broker.cancel(o["id"])
                self.store.audit("system", "cancel_before_start", {"order": o["id"], "confirmed": r.confirmed})

    def _settle_resolved(self, now: float) -> None:
        for p in self.store.list_positions(self.mode):
            m = self.held_markets.get(p.get("market_id") or "") or self.market_for_token(p["token_id"])
            if m is None or not m.closed or m.resolution_status != "resolved" or not m.outcome_prices:
                continue
            idx = next((i for i, t in enumerate(m.tokens) if t.token_id == p["token_id"]), None)
            if idx is None:
                continue
            payout = m.outcome_prices[idx]
            if payout not in (0.0, 0.5, 1.0) or abs(sum(m.outcome_prices) - 1) > 1e-6:
                continue  # not a clean final resolution yet
            proceeds = self.acct.settle(p["token_id"], payout, now)
            self.recorder.record_resolution(p["token_id"], payout, now)
            for t in m.tokens:  # record the other side too, for research
                if t.token_id != p["token_id"]:
                    self.recorder.record_resolution(t.token_id, m.outcome_prices[m.tokens.index(t)], now)
            note = (" Redeem on polymarket.com if it is not redeemed automatically." if self.mode == "live" else "")
            self.alert("settlement", f"Position settled at {payout:.2f}/share (proceeds ${proceeds or 0:.2f}): "
                       f"{m.event_title or m.question}.{note}", level="info", dedupe=False)

    # ---------------- exits ----------------
    def _manage_exits(self, snap: PortfolioSnapshot, now: float) -> None:
        open_exit_tokens = {o["token_id"] for o in self.store.list_orders(self.mode, statuses=ACTIVE, limit=10_000)
                            if o["purpose"] == "exit"}
        views = {}
        for pv in snap.positions:
            pos = pv.row
            m = self.market_for_token(pv.token_id)
            book = self.books.fresh(pv.token_id, now)
            fairs, _ = self.fairs.get(m.market_id, (None, "")) if m else (None, "")
            fair = (fairs or {}).get(pv.token_id)
            d = evaluate_exit(self.s, pos, m, book, fair, now)
            if d.new_peak_pct is not None and d.new_peak_pct != pos.get("peak_pnl_pct"):
                self.store.upsert_position({**pos, "peak_pnl_pct": d.new_peak_pct})
            views[pv.token_id] = {"action": d.action, "reasons": d.reasons, "numbers": d.numbers,
                                  "stale_book": book is None}
            if d.action != "EXIT" or pv.token_id in open_exit_tokens or m is None:
                continue
            req = OrderRequest(token_id=pv.token_id, market_id=m.market_id, event_id=m.event_id, side=Side.SELL,
                               size=round(d.shares, 2), limit_price=d.min_price, order_type=OrderType.FAK,
                               purpose="exit", strategy=pos.get("strategy") or "unknown",
                               reason="; ".join(d.reasons))
            ok, why = self.risk.check_order(req, snap, m, now)
            self._record_decision(now, m, pv.token_id, "EXIT" if ok else "HOLD",
                                  d.reasons + ([] if ok else why), d.numbers, force=True)
            if ok:
                res = self.broker.submit(req, self._context(m, pv.token_id))
                if not res.ok and res.status not in ("CANCELED",):
                    self.alert("order_failed", f"Exit order not placed: {res.message}", level="error")
        self.last_exit_views = views

    # ---------------- entries ----------------
    def _context(self, m: MarketInfo, token_id: str) -> dict:
        tok = m.token(token_id)
        book = self.books.get(token_id)
        return {"market_id": m.market_id, "event_id": m.event_id, "event_title": m.event_title,
                "question": m.question, "outcome_label": tok.label if tok else None,
                "correlation_key": correlation_key(m), "tick_size": (book.tick_size if book else m.tick_size),
                "min_order_size": max((book.min_order_size if book else 0) or 0, m.min_order_size or 0),
                "game_start_time": m.game_start_time, "end_date": m.end_date, "neg_risk": m.neg_risk}

    def _evaluate_entries(self, snap: PortfolioSnapshot, now: float, can_open: bool, gate_reasons: list[str]) -> None:
        reserved, by_event = self.acct.reserved_for_open_buys()
        open_entry_tokens = {o["token_id"] for o in self.store.list_orders(self.mode, statuses=ACTIVE, limit=10_000)
                             if o["purpose"] == "entry"}
        bankroll = self.risk.bankroll(snap)
        cands: list[Candidate] = []
        for m in list(self.discovery.eligible.values()):
            fairs, why = self.estimate_fair(m, now)
            self.fairs[m.market_id] = (fairs, why)
            budgets, notes = {}, {}
            for t in m.tokens:
                budgets[t.token_id], notes[t.token_id] = self.risk.budget_for(m, t.token_id, snap, by_event,
                                                                              open_entry_tokens)
            for c in self.strategy.evaluate(m, fairs, why, self.books, budgets, bankroll, now, notes):
                if c.fair is not None:
                    self.recorder.record(m, c.token.token_id, c.fair, self.books.get(c.token.token_id), now)
                cands.append(c)
        approved = sorted([c for c in cands if c.approved], key=lambda c: -c.numbers.get("ev_per_dollar", 0))
        for c in cands:
            if c.approved and not can_open:
                c.approved = False
                c.reasons.append("entry gate closed: " + "; ".join(gate_reasons))
        for c in approved:
            if not c.approved:
                continue
            fee_est = fee_per_share(c.limit_price, c.market.fee)
            req = OrderRequest(
                token_id=c.token.token_id, market_id=c.market.market_id, event_id=c.market.event_id,
                side=Side.BUY, size=round(c.shares, 2), limit_price=c.limit_price, order_type=OrderType.FAK,
                purpose="entry", strategy=c.strategy, reason="; ".join(c.reasons),
                meta={"fee_per_share_est": fee_est, "reserved_by_event": by_event,
                      "open_entry_tokens": open_entry_tokens, "numbers": _plain(c.numbers),
                      "entry_reason": "; ".join(c.reasons),
                      "exit_plan": self._exit_plan_text(), "strategy": c.strategy},
            )
            ok, why = self.risk.check_order(req, snap, c.market, now)
            if not ok:
                c.approved = False
                c.reasons.append("risk check: " + "; ".join(why))
                continue
            req.meta.pop("reserved_by_event", None)
            req.meta.pop("open_entry_tokens", None)
            ctx = {**self._context(c.market, c.token.token_id), "strategy": c.strategy,
                   "entry_reason": "; ".join(c.reasons), "exit_plan": self._exit_plan_text()}
            res = self.broker.submit(req, ctx)
            c.numbers["order_status"] = res.status
            if res.ok:
                open_entry_tokens.add(c.token.token_id)
                by_event[c.market.event_id or ""] = by_event.get(c.market.event_id or "", 0) + req.size * (req.limit_price + fee_est)
                self.alert("order_placed", f"{'SIMULATED ' if self.broker.simulated else ''}BUY {req.size:.2f} sh "
                           f"of '{c.token.label}' in {c.market.event_title or c.market.question} at <= "
                           f"{req.limit_price:.3f} (edge {c.numbers.get('edge')})", level="info", dedupe=False)
                snap = self.snapshot(now)
            else:
                c.approved = False
                c.reasons.append(f"order not placed: {res.message}")
                if res.status in ("FAILED", "UNKNOWN", "REJECTED"):
                    self.alert("order_failed", f"Entry order failed: {res.message}", level="error")
        for c in cands:
            self._record_decision(now, c.market, c.token.token_id, "TRADE" if c.approved else "SKIP",
                                  c.reasons, c.numbers, strategy=c.strategy)
        self.last_candidates = [self._candidate_view(c) for c in cands]

    def _exit_plan_text(self) -> str:
        s = self.s
        parts = [f"take profit at {s.take_profit_pct:.1%} of cost basis (net of exit fees)"]
        if s.exit_on_edge_loss:
            parts.append("exit when fair value no longer exceeds exit value")
        if s.trailing_enabled:
            parts.append(f"trailing {s.trailing_distance_pct:.1%} after {s.trailing_activation_pct:.1%}")
        if s.stop_loss_pct > 0:
            parts.append(f"stop-loss trigger {s.stop_loss_pct:.1%} (no price guarantee)")
        if s.max_holding_hours > 0:
            parts.append(f"max hold {s.max_holding_hours}h")
        parts.append(f"pre-start: {s.pre_start_action.value}")
        parts.append("otherwise hold to resolution" if s.hold_to_resolution else "no hold-to-resolution")
        return "; ".join(parts)

    def _candidate_view(self, c: Candidate) -> dict:
        return {"market_id": c.market.market_id, "event": c.market.event_title or c.market.question,
                "question": c.market.question, "outcome": c.token.label, "token_id": c.token.token_id,
                "game_start_time": c.market.game_start_time, "approved": c.approved, "reasons": c.reasons,
                "numbers": _plain(c.numbers), "strategy": c.strategy}

    def _record_decision(self, now: float, m: MarketInfo, token_id: str, decision: str, reasons: list[str],
                         numbers: dict, strategy: str | None = None, force: bool = False) -> None:
        key = (decision, tuple(r.split(" (")[0][:60] for r in reasons))
        last = self.last_decision_key.get(token_id)
        if not force and last and last[0] == key and now - last[1] < 300:
            return
        self.last_decision_key[token_id] = (key, now)
        tok = m.token(token_id)
        self.store.add_decision({"ts": now, "mode": self.mode, "strategy": strategy, "market_id": m.market_id,
                                 "event_id": m.event_id, "token_id": token_id,
                                 "label": f"{m.event_title or m.question} :: {tok.label if tok else '?'}",
                                 "decision": decision, "reasons": reasons, "numbers": _plain(numbers)})

    # ---------------- async runners ----------------
    async def _every(self, interval: float, fn, name: str) -> None:
        while not self.stopping:
            try:
                r = fn()
                if asyncio.iscoroutine(r):
                    await r
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if type(e).__name__ in _NETWORK_ERRORS:
                    log.warning("%s failed: %s: %s", name, type(e).__name__, e)
                else:
                    log.exception("%s failed", name)
                self.alert(f"{name}_error", f"{name} failed: {type(e).__name__}: {e}", level="error")
            await asyncio.sleep(interval)

    async def _discovery_job(self) -> None:
        await asyncio.to_thread(self.discovery.refresh)
        # Keep metadata for markets we hold even if they dropped out of the eligible set.
        for p in self.store.list_positions(self.mode):
            if p.get("market_id"):
                m = await asyncio.to_thread(self.discovery.source.fetch_market, p["market_id"])
                if m:
                    self.held_markets[p["market_id"]] = m
        if self.ws:
            self.ws.set_tokens(self.monitored_tokens())

    async def _odds_job(self) -> None:
        now = time.time()
        for p in self.providers:
            await asyncio.to_thread(p.refresh, now)
            st = p.status()
            if st.last_error:
                self.alert("provider_error", f"{st.name}: {st.last_error}", level="warning")

    async def _books_job(self) -> None:
        await rest_refresh(self.books, self.book_source, self.monitored_tokens(), self.health)

    async def _tick_job(self) -> None:
        try:
            await asyncio.to_thread(self.tick)
            self.last_tick_error = None
        except Exception as e:
            self.tick_errors += 1
            self.last_tick_error = f"{type(e).__name__}: {e}"
            self.state = "degraded"
            raise

    async def _live_checks_job(self) -> None:
        try:
            self.gate.geoblock = await fetch_geoblock(self.s.geoblock_url)
        except Exception as e:
            self.gate.geoblock = None
            self.alert("geoblock_check_failed", f"Geoblock check failed ({type(e).__name__}); live orders paused.")
        if self.gate.geoblock and self.gate.geoblock.get("blocked") is not False:
            self.alert("geoblocked", "Polymarket reports this location as restricted: live trading stopped. "
                       "Do not use a VPN to get around this.", level="critical")
        if self.mode == "live":
            self.gate.closed_only = await asyncio.to_thread(self.broker.closed_only)
            problems = await asyncio.to_thread(self.broker.reconcile_account, time.time(),
                                               self.store.list_positions(self.mode))
            if problems:
                self._halt("reconciliation failed: " + " | ".join(problems))

    async def _clock_job(self) -> None:
        await check_clock_skew(self.s.clob_url, self.health)

    async def _heartbeat_job(self) -> None:
        if self.mode == "live" and self.s.use_order_heartbeat and self.gate.check().allowed:
            await asyncio.to_thread(self.broker.send_heartbeat)

    async def run(self, extra_tasks: list | None = None) -> None:
        self.startup()
        tasks = [
            asyncio.create_task(self._every(self.s.discovery_interval_s, self._discovery_job, "discovery")),
            asyncio.create_task(self._every(self.s.odds_poll_interval_s, self._odds_job, "odds")),
            asyncio.create_task(self._every(self.s.book_poll_interval_s, self._books_job, "books")),
            asyncio.create_task(self._every(self.s.loop_interval_s, self._tick_job, "tick")),
            asyncio.create_task(self._every(3600, self._clock_job, "clock")),
        ]
        if self.mode == "live":
            tasks.append(asyncio.create_task(self._every(900, self._live_checks_job, "live_checks")))
            if self.s.use_order_heartbeat:
                tasks.append(asyncio.create_task(self._every(5, self._heartbeat_job, "heartbeat")))
        if self.s.use_websocket:
            self.ws = WebSocketFeed(self.books, self.health)
            tasks.append(asyncio.create_task(self.ws.run()))
        tasks += list(extra_tasks or [])
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()

    def shutdown(self) -> None:
        """Graceful stop: no new orders; cancel the bot's open ENTRY orders; leave positions."""
        self.stopping = True
        if self.ws:
            self.ws.stop()
        try:
            self.cancel_entries("shutdown")
        except Exception:
            log.exception("cancel on shutdown failed")
        self.state = "stopped"
        self.store.audit("system", "shutdown", {"mode": self.mode})
        self.store.set("engine_heartbeat", {"ts": time.time(), "mode": self.mode, "state": "stopped"})


def _plain(d: Any) -> Any:
    if isinstance(d, dict):
        return {str(k): _plain(v) for k, v in d.items()}
    if isinstance(d, (list, tuple, set)):
        return [_plain(x) for x in d]
    if isinstance(d, (str, int, float, bool)) or d is None:
        return d
    return str(d)
