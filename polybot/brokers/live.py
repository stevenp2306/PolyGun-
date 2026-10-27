"""Live execution through the official Polymarket SDK (``polymarket-client``).

DISABLED BY DEFAULT. Every submission re-checks the LiveGate; nothing reaches
the exchange unless the gate passes.

Order handling:
* Entries are sent as FAK "market" orders with a hard ``max_price`` (a
  marketable limit): they take available liquidity up to the limit and the
  rest is cancelled, so the price can never be worse than the limit.
* Exits are FAK sells with a hard ``min_price``.
* Orders are signed with ``create_*_order`` and posted with ``post_order``.
  We deliberately do NOT use ``place_*_order``: that helper can automatically
  submit token approval transactions when an order is rejected for allowance,
  and this bot never grants approvals on its own.
* The intent is persisted (status PENDING_SUBMIT) BEFORE posting. If the post
  times out or the connection drops, the order becomes UNKNOWN and is never
  blindly retried: reconciliation searches the exchange's open orders and
  trades for it first. Unresolvable ambiguity halts new orders and alerts.
* Fills are booked only from exchange trade records (idempotent by trade id)
  once their settlement status is MINED/CONFIRMED. A trade that FAILS to
  settle is reported and not booked.
* Only orders whose exchange IDs are stored in this bot's database are ever
  cancelled. Manually created orders/positions are reported as "external"
  and never touched.
"""

from __future__ import annotations

import logging
import random
import time
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable

from polybot.brokers.base import BookLookup, CancelResult, FeeLookup, SubmitResult
from polybot.brokers.live_gate import LiveGate
from polybot.config import Mode, Settings
from polybot.db import Store
from polybot.domain import Fill, OrderRequest, OrderStatus, OrderType, Side, TERMINAL_STATUSES
from polybot.portfolio import Accounting
from polybot.pricing import fee_for

log = logging.getLogger(__name__)

NON_TERMINAL = [OrderStatus.PENDING_SUBMIT.value, OrderStatus.OPEN.value, OrderStatus.PARTIAL.value,
                OrderStatus.UNKNOWN.value]
SETTLED_TRADE_STATES = {"MINED", "CONFIRMED"}
USDC_DECIMALS = Decimal(10) ** 6


class LiveTradingBlocked(RuntimeError):
    pass


def _err_types():
    from polymarket import errors as e
    return e


def call_with_retry(fn: Callable[[], Any], attempts: int = 4, base: float = 0.5,
                    sleep: Callable[[float], None] = time.sleep) -> Any:
    """Retry an IDEMPOTENT read on rate limits / transport errors with backoff + jitter.

    Never use this for order submission.
    """
    e = _err_types()
    for i in range(attempts):
        try:
            return fn()
        except e.RateLimitError as ex:
            if i == attempts - 1:
                raise
            delay = ex.retry_after if ex.retry_after else base * 2 ** i
            sleep(delay + random.uniform(0, delay / 2))
        except (e.TransportError, e.TimeoutError):
            if i == attempts - 1:
                raise
            delay = base * 2 ** i
            sleep(delay + random.uniform(0, delay / 2))
    raise RuntimeError("unreachable")


class LiveBroker:
    mode = "live"
    simulated = False

    def __init__(self, settings: Settings, store: Store, accounting: Accounting, gate: LiveGate,
                 client_factory: Callable[[], Any] | None = None, alert: Callable[..., None] | None = None,
                 on_failure: Callable[[], bool] | None = None, on_success: Callable[[], None] | None = None,
                 halt: Callable[[str], None] | None = None):
        if settings.mode != Mode.LIVE:
            raise LiveTradingBlocked("LiveBroker can only be constructed in POLYBOT_MODE=live")
        self.s = settings
        self.store = store
        self.acct = accounting
        self.gate = gate
        self.client_factory = client_factory
        self.client: Any = None
        self.alert = alert or (lambda *a, **k: None)
        self.on_failure = on_failure or (lambda: False)
        self.on_success = on_success or (lambda: None)
        self.halt = halt or (lambda reason: None)
        self._cash: float | None = None
        self.blocked_tokens: set[str] = set()
        self.external_orders: list[dict] = []
        self.external_positions: list[dict] = []
        self.heartbeat_id = ""
        self.last_reconcile: float | None = None
        self.last_reconcile_ok: bool | None = None

    # ---------------- connection ----------------
    def connect(self) -> None:
        if self.client is not None:
            return
        if self.client_factory is not None:
            self.client = self.client_factory()
            return
        from polymarket import ApiKeyCreds, PublicClient, SecureClient

        s = self.s
        if s.polymarket_private_key is None or not s.polymarket_wallet_address:
            raise LiveTradingBlocked("private key / wallet address not configured")
        # Preflight without the key: the wallet must already be onboarded (approvals done on
        # polymarket.com). This prevents the SDK from deploying a wallet on our behalf.
        with PublicClient() as pub:
            state = pub.get_trading_approvals_state(wallet=s.polymarket_wallet_address)
        if not state.is_fully_approved:
            raise LiveTradingBlocked(
                "wallet trading approvals incomplete: finish onboarding on polymarket.com "
                "(this bot never grants approvals)")
        creds = None
        if s.polymarket_api_key and s.polymarket_api_secret and s.polymarket_api_passphrase:
            creds = ApiKeyCreds(key=s.polymarket_api_key.get_secret_value(),
                                secret=s.polymarket_api_secret.get_secret_value(),
                                passphrase=s.polymarket_api_passphrase.get_secret_value())
        self.client = SecureClient.create(
            private_key=s.polymarket_private_key.get_secret_value(),
            wallet=s.polymarket_wallet_address,
            credentials=creds,
        )

    def close(self) -> None:
        if self.client is not None and hasattr(self.client, "close"):
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None

    def cash_override(self) -> float | None:
        return self._cash

    # ---------------- account reads ----------------
    def refresh_cash(self) -> float:
        self.connect()
        ba = call_with_retry(lambda: self.client.get_balance_allowance(asset_type="COLLATERAL"))
        self._cash = float(Decimal(ba.balance) / USDC_DECIMALS)
        return self._cash

    def closed_only(self) -> bool:
        self.connect()
        return bool(call_with_retry(lambda: self.client.get_closed_only_mode()))

    # ---------------- submission ----------------
    def submit(self, req: OrderRequest, context: dict) -> SubmitResult:
        g = self.gate.check()
        if not g.allowed:
            return SubmitResult(False, None, "BLOCKED", "live gate closed: " + "; ".join(g.failures))
        if req.token_id in self.blocked_tokens:
            return SubmitResult(False, None, "BLOCKED", "token has an unreconciled order")
        if any(o["status"] == OrderStatus.UNKNOWN.value for o in self.store.list_orders(
                self.mode, statuses=[OrderStatus.UNKNOWN.value])):
            return SubmitResult(False, None, "BLOCKED", "an earlier order is UNKNOWN: reconcile first")
        tick = Decimal(str(context.get("tick_size") or "0.01"))
        price = Decimal(str(req.limit_price))
        if (price / tick) != (price / tick).to_integral_value():
            return SubmitResult(False, None, "REJECTED", f"price {price} not on tick grid {tick}")
        if req.size < float(context.get("min_order_size") or 0):
            return SubmitResult(False, None, "REJECTED", "size below exchange minimum")
        try:
            self.connect()
        except Exception as e:
            return SubmitResult(False, None, "FAILED", f"connect failed: {type(e).__name__}")

        # Immediate pre-trade validation against fresh exchange state.
        try:
            book = call_with_retry(lambda: self.client.get_order_book(asset_id=req.token_id))
            if Decimal(str(book.tick_size)) != tick:
                return SubmitResult(False, None, "REJECTED", "tick size changed; re-evaluate")
            if req.side == Side.BUY:
                bal = self.refresh_cash()
                need = req.size * req.limit_price * 1.05
                if bal < need:
                    return SubmitResult(False, None, "REJECTED", f"insufficient balance ${bal:.2f} < ${need:.2f}")
            else:
                ba = call_with_retry(lambda: self.client.get_balance_allowance(
                    asset_type="CONDITIONAL", asset_id=req.token_id))
                held = float(Decimal(ba.balance) / USDC_DECIMALS)
                if held + 1e-6 < req.size:
                    return SubmitResult(False, None, "REJECTED", f"only {held} shares held on exchange")
        except Exception as e:
            self.on_failure()
            return SubmitResult(False, None, "FAILED", f"pre-trade validation failed: {type(e).__name__}")

        try:
            signed = self._sign(req)
        except Exception as e:
            return SubmitResult(False, None, "REJECTED", f"signing/validation failed: {type(e).__name__}: {e}")

        oid = self.store.insert_order({
            "mode": self.mode, "token_id": req.token_id, "market_id": req.market_id,
            "event_id": req.event_id, "side": req.side.value, "order_type": req.order_type.value,
            "purpose": req.purpose, "strategy": req.strategy, "limit_price": req.limit_price,
            "size": req.size, "status": OrderStatus.PENDING_SUBMIT.value, "reason": req.reason,
            "expires_at": req.expires_at, "signed_order": _signed_summary(signed),
            "meta": {**req.meta, **{k: v for k, v in context.items() if isinstance(v, (str, int, float, bool))}},
        })
        e = _err_types()
        try:
            resp = self.client.post_order(signed)  # NOT retried automatically
        except e.RateLimitError as ex:
            self.store.update_order(oid, status=OrderStatus.FAILED.value, error=f"rate limited: {ex}")
            self.on_failure()
            return SubmitResult(False, oid, OrderStatus.FAILED.value, "rate limited (not accepted)")
        except e.RequestRejectedError as ex:
            self.store.update_order(oid, status=OrderStatus.REJECTED.value,
                                    error=f"{ex.status} {ex.code} restriction={ex.restriction}")
            if ex.restriction:
                self.halt(f"exchange trading restriction: {ex.restriction}")
            self.on_failure()
            return SubmitResult(False, oid, OrderStatus.REJECTED.value, f"rejected: {ex.code or ex.status}")
        except Exception as ex:  # timeout / transport / unknown: outcome AMBIGUOUS
            self.store.update_order(oid, status=OrderStatus.UNKNOWN.value,
                                    error=f"ambiguous submission: {type(ex).__name__}")
            self.blocked_tokens.add(req.token_id)
            self.alert("order_unknown", f"Order submission outcome unknown ({type(ex).__name__}); "
                       "new orders blocked until reconciled.", level="error")
            self.on_failure()
            return SubmitResult(False, oid, OrderStatus.UNKNOWN.value, "ambiguous; will reconcile")

        if getattr(resp, "ok", False):
            st = {"live": OrderStatus.OPEN, "delayed": OrderStatus.OPEN,
                  "matched": OrderStatus.OPEN}.get(str(resp.status), OrderStatus.OPEN)
            self.store.update_order(oid, exchange_order_id=str(resp.order_id), status=st.value,
                                    meta={**(self.store.get_order(oid) or {}).get("meta", {}),
                                          "post_status": str(resp.status),
                                          "trade_ids": list(resp.trade_ids)})
            self.on_success()
            return SubmitResult(True, oid, st.value, f"accepted ({resp.status})", str(resp.order_id))
        code = getattr(resp, "code", "unknown")
        msg = getattr(resp, "message", "")
        status = OrderStatus.CANCELED if code in ("fak_not_filled", "fok_not_filled", "unmatched") else OrderStatus.REJECTED
        self.store.update_order(oid, status=status.value, error=f"{code}: {msg}")
        if code not in ("fak_not_filled", "fok_not_filled", "unmatched"):
            self.on_failure()
        return SubmitResult(False, oid, status.value, f"{code}: {msg}")

    def _sign(self, req: OrderRequest) -> Any:
        size = Decimal(str(req.size)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        price = Decimal(str(req.limit_price))
        if req.order_type in (OrderType.FAK, OrderType.FOK):
            if req.side == Side.BUY:
                amount = (size * price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                return self.client.create_market_order(asset_id=req.token_id, side="BUY", amount=amount,
                                                       max_price=price, order_type=req.order_type.value)
            return self.client.create_market_order(asset_id=req.token_id, side="SELL", shares=size,
                                                   min_price=price, order_type=req.order_type.value)
        expiration = int(req.expires_at) if req.order_type == OrderType.GTD and req.expires_at else None
        return self.client.create_limit_order(asset_id=req.token_id, price=price, size=size,
                                              side=req.side.value, expiration=expiration)

    # ---------------- cancellation ----------------
    def cancel(self, order_id: str) -> CancelResult:
        o = self.store.get_order(order_id)
        if o is None:
            return CancelResult(order_id, False, "unknown local order")
        if OrderStatus(o["status"]) in TERMINAL_STATUSES:
            return CancelResult(order_id, True, f"already {o['status']}")
        if not o.get("exchange_order_id"):
            return CancelResult(order_id, False, "no exchange id yet (ambiguous); reconciliation required")
        try:
            self.connect()
            r = self.client.cancel_orders(order_ids=[o["exchange_order_id"]])
        except Exception as e:
            return CancelResult(order_id, False, f"cancel request failed: {type(e).__name__}")
        if o["exchange_order_id"] in [str(x) for x in r.canceled]:
            self.store.update_order(order_id, status=OrderStatus.CANCELED.value)
            return CancelResult(order_id, True, "cancel confirmed by exchange")
        reason = (r.not_canceled or {}).get(o["exchange_order_id"], "not confirmed")
        return CancelResult(order_id, False, f"cancel NOT confirmed: {reason}")

    # ---------------- reconciliation ----------------
    def step(self, now: float, books: BookLookup, fees: FeeLookup) -> list[Fill]:
        return self.reconcile_orders(now, fees)

    def reconcile_orders(self, now: float, fees: FeeLookup) -> list[Fill]:
        self.connect()
        fills: list[Fill] = []
        for o in self.store.list_orders(self.mode, statuses=NON_TERMINAL, limit=10_000):
            try:
                if o["status"] in (OrderStatus.UNKNOWN.value, OrderStatus.PENDING_SUBMIT.value):
                    self._resolve_unknown(o, now)
                    continue
                ex = call_with_retry(lambda: self.client.get_order(order_id=o["exchange_order_id"]))
                fills.extend(self._book_trades(o, fees))
                st = str(getattr(ex, "status", "")).upper()
                if st in ("CANCELED", "CANCELLED") or st.startswith("CANCELED_") or st == "INVALID":
                    o2 = self.store.get_order(o["id"])
                    self.store.update_order(o["id"], status=OrderStatus.FILLED.value
                                            if o2["filled_size"] >= o2["size"] - 1e-6 else OrderStatus.CANCELED.value)
                elif st == "MATCHED":
                    o2 = self.store.get_order(o["id"])
                    if o2["filled_size"] >= o2["size"] - 1e-6:
                        self.store.update_order(o["id"], status=OrderStatus.FILLED.value)
            except Exception as e:
                eobj = _err_types()
                if isinstance(e, eobj.RequestRejectedError) and getattr(e, "status", None) == 404:
                    fills.extend(self._book_trades(o, fees))
                    o2 = self.store.get_order(o["id"])
                    self.store.update_order(o["id"], status=OrderStatus.FILLED.value
                                            if o2["filled_size"] >= o2["size"] - 1e-6 else OrderStatus.CANCELED.value)
                else:
                    log.warning("reconcile order %s failed: %s", o["id"], type(e).__name__)
        return fills

    def _book_trades(self, o: dict, fees: FeeLookup) -> list[Fill]:
        """Book settled trades belonging to our order (idempotent by trade id)."""
        out: list[Fill] = []
        ex_id = o.get("exchange_order_id")
        if not ex_id:
            return out
        trades = call_with_retry(lambda: list(self.client.list_account_trades(asset_id=o["token_id"]).iter_items()))
        fee_sched = fees(o["token_id"])
        filled = o["filled_size"] or 0.0
        notional = (o["avg_fill_price"] or 0.0) * filled
        pending = 0.0
        for t in trades:
            mine_as_taker = str(t.taker_order_id) == ex_id
            maker_part = [m for m in t.maker_orders if str(m.order_id) == ex_id]
            if not mine_as_taker and not maker_part:
                continue
            tid = f"{t.id}:{ex_id}"
            if t.status == "FAILED":
                if not self.store.get(f"failed_trade:{tid}"):
                    self.store.set(f"failed_trade:{tid}", True)
                    self.alert("settlement_failed", f"A matched trade for order {ex_id[:10]}… failed to "
                               "settle on-chain; it is NOT booked.", level="error")
                continue
            if t.status not in SETTLED_TRADE_STATES:
                pending += float(t.size) if mine_as_taker else sum(float(m.matched_amount) for m in maker_part)
                continue
            if self.store.fill_exists(self.mode, tid):
                continue
            if mine_as_taker:
                size, price, is_taker = float(t.size), float(t.price), True
            else:
                size = sum(float(m.matched_amount) for m in maker_part)
                price, is_taker = float(maker_part[0].price), False
            fee = fee_for(size, price, fee_sched, is_taker=is_taker)  # estimate; cash is reconciled separately
            f = Fill(o["id"], o["token_id"], Side(o["side"]), price, size, fee,
                     t.matched_at.timestamp(), trade_id=tid, simulated=False)
            self.acct.apply_fill(f, {**(o.get("meta") or {}), "settlement_status": t.status})
            filled += size
            notional += size * price
            out.append(f)
        if out or pending:
            meta = {**(o.get("meta") or {}), "pending_settlement_size": pending}
            vals: dict[str, Any] = {"meta": meta}
            if out:
                vals.update(filled_size=filled, avg_fill_price=notional / filled if filled else None,
                            fees=(o["fees"] or 0) + sum(f.fee for f in out),
                            status=(OrderStatus.FILLED.value if filled >= o["size"] - 1e-6
                                    else OrderStatus.PARTIAL.value))
            self.store.update_order(o["id"], **vals)
        return out

    def _resolve_unknown(self, o: dict, now: float) -> None:
        """Find an ambiguously submitted order on the exchange; never resubmit blindly."""
        created = o["created_at"]
        cands = []
        for ex in call_with_retry(lambda: list(self.client.list_open_orders(asset_id=o["token_id"]).iter_items())):
            if self.store.find_order_by_exchange_id(str(ex.id)):
                continue
            ts = ex.created_at.timestamp()
            if (str(ex.side) == o["side"] and abs(float(ex.price) - o["limit_price"]) < 1e-9
                    and created - 10 <= ts <= created + 120):
                cands.append(str(ex.id))
        trades = call_with_retry(lambda: list(self.client.list_account_trades(asset_id=o["token_id"]).iter_items()))
        for t in trades:
            ts = t.matched_at.timestamp()
            if created - 10 <= ts <= created + 120 and str(t.side) == o["side"] and t.trader_side == "TAKER":
                if not self.store.find_order_by_exchange_id(str(t.taker_order_id)):
                    cands.append(str(t.taker_order_id))
        cands = sorted(set(cands))
        if len(cands) == 1:
            self.store.update_order(o["id"], exchange_order_id=cands[0], status=OrderStatus.OPEN.value,
                                    error="adopted after ambiguous submission")
            self.blocked_tokens.discard(o["token_id"])
            self.store.audit("reconcile", "adopt_unknown_order", {"order": o["id"], "exchange": cands[0]})
        elif len(cands) > 1:
            self.halt(f"cannot uniquely match ambiguous order {o['id'][:8]} ({len(cands)} candidates)")
            self.alert("reconcile_failed", "Ambiguous order could not be matched uniquely; new orders "
                       "halted. Manual review required.", level="critical")
        elif now - created > 180:
            self.store.update_order(o["id"], status=OrderStatus.FAILED.value,
                                    error="not found on exchange 3 min after ambiguous submission")
            self.blocked_tokens.discard(o["token_id"])
            self.store.audit("reconcile", "unknown_order_not_found", {"order": o["id"]})

    def reconcile_account(self, now: float, managed_positions: list[dict]) -> list[str]:
        """Full read-only reconciliation. Returns a list of problems (empty = OK)."""
        problems: list[str] = []
        self.connect()
        open_ex = call_with_retry(lambda: list(self.client.list_open_orders().iter_items()))
        ours = {o["exchange_order_id"] for o in self.store.list_orders(self.mode, limit=100_000)
                if o.get("exchange_order_id")}
        self.external_orders = [{"id": str(x.id)[:12] + "…", "side": str(x.side), "price": float(x.price),
                                 "size": float(x.original_size)} for x in open_ex if str(x.id) not in ours]
        for o in self.store.list_orders(self.mode, statuses=[OrderStatus.OPEN.value], limit=10_000):
            if o.get("order_type") in ("GTC", "GTD") and o["exchange_order_id"] not in {str(x.id) for x in open_ex}:
                pass  # resolved by reconcile_orders via get_order
        wallet = self.s.polymarket_wallet_address
        positions = call_with_retry(lambda: list(self.client.list_positions(user=wallet, page_size=500).iter_items()))
        ex_pos = {str(p.asset_id): float(p.current_size) for p in positions}
        managed = {p["token_id"]: p for p in managed_positions}
        # Positions the bot already booked as resolved but that are not redeemed on-chain yet.
        settled = {p["token_id"] for p in self.store.list_positions(self.mode, status="SETTLED")}
        unredeemed = sum(float(getattr(p, "current_value", 0) or 0) for p in positions
                         if str(p.asset_id) in settled and getattr(p, "redeemable", False))
        for tok, p in managed.items():
            have = ex_pos.get(tok, 0.0)
            if abs(have - p["shares"]) > max(0.05, 0.01 * p["shares"]):
                problems.append(f"position mismatch on {tok[:10]}…: exchange {have:.2f} vs bot {p['shares']:.2f}")
        self.external_positions = [{"asset": t[:12] + "…", "size": s} for t, s in ex_pos.items()
                                   if t not in managed and t not in settled and s > 0]
        self.refresh_cash()
        expected = self.acct.cash()
        have = (self._cash or 0.0) + unredeemed
        if self._cash is not None and abs(have - expected) > max(1.0, 0.01 * abs(expected)):
            problems.append(
                f"exchange cash ${self._cash:.2f} (+ ${unredeemed:.2f} awaiting redemption) differs from "
                f"bot accounting ${expected:.2f}. If you "
                "deposited/withdrew or traded manually, record it (`polybot record-flow`).")
        self.last_reconcile = now
        self.last_reconcile_ok = not problems
        return problems

    # ---------------- heartbeat (optional) ----------------
    def send_heartbeat(self) -> bool:
        """One heartbeat. API-KEY-WIDE: once started, if sends stop for ~10s the exchange
        *attempts* to cancel ALL open orders of this API key (including manual ones placed
        with the same key). Cancellation is not guaranteed and positions are never closed."""
        e = _err_types()
        self.connect()
        try:
            hb = self.client.send_order_heartbeat(heartbeat_id=self.heartbeat_id)
            self.heartbeat_id = str(getattr(hb, "heartbeat_id", "") or getattr(hb, "id", "") or "")
            return True
        except e.OrderHeartbeatMismatchError as ex:
            self.heartbeat_id = ex.heartbeat_id
            return False
        except Exception:
            return False


def _signed_summary(signed: Any) -> str:
    """Public, non-secret fields of a signed order (no private key material exists in it)."""
    import json
    keys = ("token_id", "side", "maker_amount", "taker_amount", "expiration", "salt", "order_type",
            "maker", "signer", "timestamp")
    return json.dumps({k: str(getattr(signed, k, "")) for k in keys})
