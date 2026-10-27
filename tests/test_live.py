"""Live adapter tests against a FAKE SDK client. No network, no real orders, no real funds."""

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from polymarket import errors as E

from polybot.brokers.live import LiveBroker, LiveTradingBlocked, call_with_retry
from polybot.brokers.live_gate import CHECKLIST_ITEMS, LiveGate
from polybot.domain import OrderRequest, OrderType, Side
from polybot.portfolio import Accounting
from tests.conftest import FEE, make_settings

FAKE_KEY = "0x" + "11" * 32


class Pager:
    def __init__(self, items):
        self.items = items

    def iter_items(self):
        return iter(self.items)


class FakeClient:
    def __init__(self):
        self.posted = []
        self.post_behaviour = "ok"
        self.open_orders = []
        self.trades = []
        self.orders = {}
        self.positions = []
        self.balance = 500_000_000  # $500
        self.cancel_ok = True
        self.signer = "0xABCDEF0000000000000000000000000000001234"

    # reads
    def get_order_book(self, asset_id):
        return NS(tick_size="0.01")

    def get_balance_allowance(self, asset_type, asset_id=None):
        return NS(balance=self.balance if asset_type == "COLLATERAL" else 100_000_000)

    def get_closed_only_mode(self):
        return False

    def list_open_orders(self, asset_id=None):
        return Pager([o for o in self.open_orders if asset_id in (None, o.asset_id)])

    def list_account_trades(self, asset_id=None):
        return Pager(self.trades)

    def list_positions(self, user, page_size=500):
        return Pager(self.positions)

    def get_order(self, order_id):
        return self.orders[order_id]

    # signing / posting
    def create_market_order(self, **kw):
        return NS(kind="market", token_id=kw["asset_id"], **kw)

    def create_limit_order(self, **kw):
        return NS(kind="limit", token_id=kw["asset_id"], **kw)

    def place_limit_order(self, **kw):  # must never be used (may auto-approve)
        raise AssertionError("place_limit_order must not be called")

    place_market_order = place_limit_order

    def post_order(self, signed):
        self.posted.append(signed)
        if self.post_behaviour == "timeout":
            raise E.TransportError("read timeout")
        if self.post_behaviour == "ratelimit":
            raise E.RateLimitError("slow down", retry_after=1)
        if self.post_behaviour == "reject":
            return NS(ok=False, code="not_enough_balance", message="no money")
        oid = f"0xORDER{len(self.posted)}"
        self.orders[oid] = NS(status="LIVE")
        return NS(ok=True, order_id=oid, status="matched", trade_ids=("T1",))

    def cancel_orders(self, order_ids):
        if self.cancel_ok:
            return NS(canceled=tuple(order_ids), not_canceled={})
        return NS(canceled=(), not_canceled={order_ids[0]: "order not found"})

    def send_order_heartbeat(self, heartbeat_id=""):
        if heartbeat_id == "":
            raise E.OrderHeartbeatMismatchError("hb-2")
        return NS(heartbeat_id="hb-3")


def live_settings(tmp_path, **kw):
    base = dict(mode="live", live_trading_enabled=True, polymarket_private_key=FAKE_KEY,
                polymarket_wallet_address="0x" + "22" * 20, live_checklist_path=str(tmp_path / "chk.json"))
    return make_settings(**{**base, **kw})


def confirm_checklist(s, gate=None):
    gate = gate or LiveGate(s)
    gate.save({"items": {k: {"confirmed": True} for k in CHECKLIST_ITEMS},
               "risk_config_hash": s.risk_config_hash(), "enabled_phrase_confirmed": True})
    gate.geoblock = {"blocked": False, "country": "XX", "checked_at": time.time()}
    return gate


def make_broker(tmp_path, store, confirm=True, **kw):
    s = live_settings(tmp_path, **kw)
    gate = LiveGate(s)
    if confirm:
        confirm_checklist(s, gate)
    acct = Accounting(store, "live")
    acct.ensure_initial_balance(500.0)
    client = FakeClient()
    alerts, halts = [], []
    b = LiveBroker(s, store, acct, gate, client_factory=lambda: client,
                   alert=lambda kind, msg, **k: alerts.append((kind, msg)), halt=halts.append)
    return b, client, alerts, halts, s


CTX = {"tick_size": 0.01, "min_order_size": 5, "event_id": "E1", "market_id": "M1"}


def buy_req(size=10, price=0.5):
    return OrderRequest("TOK", "M1", "E1", Side.BUY, size, price, OrderType.FAK, "entry", "test", "r")


def test_live_broker_cannot_exist_in_paper_mode(store):
    with pytest.raises(LiveTradingBlocked):
        LiveBroker(make_settings(), store, Accounting(store, "live"), LiveGate(make_settings()))


def test_incomplete_checklist_blocks_without_network(tmp_path, store):
    b, client, *_ = make_broker(tmp_path, store, confirm=False)
    r = b.submit(buy_req(), CTX)
    assert not r.ok and r.status == "BLOCKED"
    assert client.posted == []


def test_changed_risk_config_invalidates_checklist(tmp_path, store):
    s = live_settings(tmp_path)
    gate = confirm_checklist(s)
    assert gate.check().allowed
    s2 = live_settings(tmp_path, max_position_usd=999)
    g2 = LiveGate(s2)
    g2.geoblock = gate.geoblock
    assert not g2.check().allowed
    assert any("risk configuration changed" in f for f in g2.check().failures)


def test_disabled_flag_or_geoblock_blocks(tmp_path):
    s = live_settings(tmp_path, live_trading_enabled=False)
    assert not confirm_checklist(s).check().allowed
    s2 = live_settings(tmp_path)
    g = confirm_checklist(s2)
    g.geoblock = {"blocked": True, "country": "US", "checked_at": time.time()}
    assert any("geoblock" in f for f in g.check().failures)
    g.geoblock = None
    assert not g.check().allowed


def test_successful_submit_uses_create_and_post_with_price_cap(tmp_path, store):
    b, client, *_ = make_broker(tmp_path, store)
    r = b.submit(buy_req(size=10, price=0.5), CTX)
    assert r.ok and r.exchange_order_id == "0xORDER1"
    signed = client.posted[0]
    assert signed.kind == "market" and str(signed.max_price) == "0.5" and signed.order_type == "FAK"
    row = store.get_order(r.order_id)
    assert row["status"] == "OPEN" and FAKE_KEY not in (row["signed_order"] or "")


def test_ambiguous_timeout_marks_unknown_and_never_resubmits(tmp_path, store):
    b, client, alerts, halts, s = make_broker(tmp_path, store)
    client.post_behaviour = "timeout"
    r = b.submit(buy_req(), CTX)
    assert r.status == "UNKNOWN" and len(client.posted) == 1
    client.post_behaviour = "ok"
    r2 = b.submit(buy_req(), CTX)  # same or any token: blocked until reconciled
    assert not r2.ok and r2.status == "BLOCKED" and len(client.posted) == 1
    assert alerts and alerts[0][0] == "order_unknown"


def test_reconcile_adopts_unique_match(tmp_path, store):
    b, client, alerts, halts, s = make_broker(tmp_path, store)
    client.post_behaviour = "timeout"
    r = b.submit(buy_req(price=0.5), CTX)
    created = store.get_order(r.order_id)["created_at"]
    client.open_orders = [NS(id="0xFOUND", asset_id="TOK", side="BUY", price=0.5, original_size=10,
                             created_at=datetime.fromtimestamp(created + 1, tz=timezone.utc))]
    client.orders["0xFOUND"] = NS(status="LIVE")
    b.reconcile_orders(created + 5, lambda t: FEE)
    o = store.get_order(r.order_id)
    assert o["exchange_order_id"] == "0xFOUND" and o["status"] == "OPEN"
    assert "TOK" not in b.blocked_tokens and not halts


def test_reconcile_ambiguous_match_halts(tmp_path, store):
    b, client, alerts, halts, s = make_broker(tmp_path, store)
    client.post_behaviour = "timeout"
    r = b.submit(buy_req(price=0.5), CTX)
    created = store.get_order(r.order_id)["created_at"]
    mk = lambda i: NS(id=f"0xC{i}", asset_id="TOK", side="BUY", price=0.5, original_size=10,
                      created_at=datetime.fromtimestamp(created + i, tz=timezone.utc))
    client.open_orders = [mk(1), mk(2)]
    b.reconcile_orders(created + 5, lambda t: FEE)
    assert halts and "cannot uniquely match" in halts[0]


def test_unknown_order_not_found_becomes_failed_after_timeout(tmp_path, store):
    b, client, *_ = make_broker(tmp_path, store)
    client.post_behaviour = "timeout"
    r = b.submit(buy_req(), CTX)
    created = store.get_order(r.order_id)["created_at"]
    b.reconcile_orders(created + 30, lambda t: FEE)
    assert store.get_order(r.order_id)["status"] == "UNKNOWN"
    b.reconcile_orders(created + 200, lambda t: FEE)
    assert store.get_order(r.order_id)["status"] == "FAILED"


def test_rate_limited_post_is_not_retried(tmp_path, store):
    b, client, *_ = make_broker(tmp_path, store)
    client.post_behaviour = "ratelimit"
    r = b.submit(buy_req(), CTX)
    assert r.status == "FAILED" and len(client.posted) == 1


def test_exchange_rejection_recorded(tmp_path, store):
    b, client, *_ = make_broker(tmp_path, store)
    client.post_behaviour = "reject"
    r = b.submit(buy_req(), CTX)
    assert not r.ok and r.status == "REJECTED" and "not_enough_balance" in r.message


def test_insufficient_balance_checked_before_signing(tmp_path, store):
    b, client, *_ = make_broker(tmp_path, store)
    client.balance = 1_000_000  # $1
    r = b.submit(buy_req(size=10, price=0.5), CTX)
    assert not r.ok and "insufficient balance" in r.message and client.posted == []


def test_read_retry_with_backoff_on_rate_limit():
    calls, sleeps = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise E.RateLimitError("429", retry_after=0.5)
        return "ok"

    assert call_with_retry(fn, sleep=sleeps.append) == "ok"
    assert len(calls) == 3 and all(s >= 0.5 for s in sleeps)


def test_settled_trades_booked_once_and_failed_trades_not_booked(tmp_path, store):
    b, client, alerts, halts, s = make_broker(tmp_path, store)
    r = b.submit(buy_req(size=10, price=0.5), CTX)
    t = datetime.fromtimestamp(time.time(), tz=timezone.utc)
    client.trades = [
        NS(id="T1", taker_order_id="0xORDER1", maker_orders=(), status="CONFIRMED", size="6", price="0.5",
           side="BUY", trader_side="TAKER", matched_at=t),
        NS(id="T2", taker_order_id="0xORDER1", maker_orders=(), status="FAILED", size="4", price="0.5",
           side="BUY", trader_side="TAKER", matched_at=t),
    ]
    client.orders["0xORDER1"] = NS(status="MATCHED")
    b.reconcile_orders(time.time(), lambda tok: FEE)
    b.reconcile_orders(time.time(), lambda tok: FEE)  # idempotent
    pos = store.get_position("live", "TOK")
    assert pos["shares"] == pytest.approx(6)
    assert any(k == "settlement_failed" for k, _ in alerts)
    assert store.get_order(r.order_id)["status"] == "PARTIAL"


def test_cancel_only_own_orders_and_report_unconfirmed(tmp_path, store):
    b, client, *_ = make_broker(tmp_path, store)
    r = b.submit(OrderRequest("TOK", "M1", "E1", Side.BUY, 10, 0.4, OrderType.GTC, "entry", "t", "r"), CTX)
    client.cancel_ok = False
    res = b.cancel(r.order_id)
    assert not res.confirmed and "NOT confirmed" in res.message
    client.cancel_ok = True
    assert b.cancel(r.order_id).confirmed
    assert not b.cancel("not-a-bot-order").confirmed


def test_account_reconciliation_detects_mismatch_and_external_items(tmp_path, store):
    b, client, *_ = make_broker(tmp_path, store)
    client.positions = [NS(asset_id="TOK", current_size=3.0), NS(asset_id="MANUAL", current_size=7.0)]
    client.open_orders = [NS(id="0xMANUALORDER", asset_id="X", side="SELL", price=0.7, original_size=5,
                             created_at=datetime.now(tz=timezone.utc))]
    problems = b.reconcile_account(time.time(), [{"token_id": "TOK", "shares": 10.0}])
    assert any("position mismatch" in p for p in problems)
    assert b.external_positions and b.external_orders  # shown, never touched
    client.balance = 400_000_000
    problems = b.reconcile_account(time.time(), [])
    assert any("differs from bot accounting" in p for p in problems)


def test_heartbeat_recovers_from_id_mismatch(tmp_path, store):
    b, client, *_ = make_broker(tmp_path, store)
    assert b.send_heartbeat() is False and b.heartbeat_id == "hb-2"
    assert b.send_heartbeat() is True and b.heartbeat_id == "hb-3"


def test_unredeemed_winnings_do_not_trigger_cash_mismatch(tmp_path, store):
    b, client, *_ = make_broker(tmp_path, store)
    b.acct.apply_fill(__import__("polybot.domain", fromlist=["Fill"]).Fill(
        "o", "WIN", Side.BUY, 0.5, 20, 0.0, time.time(), trade_id="tw"), {})
    b.acct.settle("WIN", 1.0)  # bot books +$20 payout; exchange cash unchanged until redeemed
    client.balance = int((500 - 10) * 1_000_000)
    client.positions = [NS(asset_id="WIN", current_size=20.0, current_value=20.0, redeemable=True)]
    assert b.reconcile_account(time.time(), []) == []
    assert b.external_positions == []
