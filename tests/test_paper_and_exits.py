import pytest

from polybot.brokers.paper import PaperBroker
from polybot.config import PreStartAction
from polybot.domain import OrderRequest, OrderType, Side
from polybot.exits import evaluate_exit
from polybot.portfolio import Accounting
from polybot.pricing import fee_for
from tests.conftest import FEE, NOW, make_book, make_fair, make_market, make_settings

CTX = {"tick_size": 0.01, "min_order_size": 5, "event_id": "E-M1", "market_id": "M1",
       "event_title": "Arsenal vs. Chelsea", "outcome_label": "Yes"}


def broker(store, **kw):
    s = make_settings(**kw)
    acct = Accounting(store, "paper")
    acct.ensure_initial_balance(1000.0)
    return PaperBroker(s, store, acct), acct, s


def req(side=Side.BUY, size=20, price=0.52, otype=OrderType.FAK, purpose="entry", expires=None):
    return OrderRequest("M1-YES", "M1", "E-M1", side, size, price, otype, purpose, "test", "r", expires_at=expires)


def step(b, book, now=NOW):
    return b.step(now, lambda t: book, lambda t: FEE)


def test_fak_fills_against_book_with_fees_and_haircut(store):
    b, acct, s = broker(store, paper_depth_haircut=0.5)
    r = b.submit(req(size=20), CTX)
    assert r.ok
    book = make_book("M1-YES", [(0.48, 100)], [(0.50, 10), (0.51, 30), (0.60, 100)])
    fills = step(b, book)
    assert [round(f.size, 2) for f in fills] == [5, 15]  # half of displayed size, up to limit 0.52
    o = store.get_order(r.order_id)
    assert o["status"] == "FILLED" and o["filled_size"] == pytest.approx(20)
    expected_cost = 5 * 0.50 + 15 * 0.51 + fee_for(5, 0.50, FEE) + fee_for(15, 0.51, FEE)
    assert acct.cash() == pytest.approx(1000 - expected_cost)
    pos = store.get_position("paper", "M1-YES")
    assert pos["shares"] == pytest.approx(20) and pos["cost_basis"] == pytest.approx(expected_cost)


def test_fak_partial_fill_kills_remainder(store):
    b, acct, s = broker(store)
    r = b.submit(req(size=50), CTX)
    step(b, make_book("M1-YES", [(0.48, 100)], [(0.50, 10), (0.70, 100)]))
    o = store.get_order(r.order_id)
    assert o["status"] == "CANCELED" and o["filled_size"] == pytest.approx(10)
    assert "partial" in o["error"]


def test_fok_all_or_nothing(store):
    b, acct, s = broker(store)
    r = b.submit(req(size=50, otype=OrderType.FOK), CTX)
    step(b, make_book("M1-YES", [(0.48, 100)], [(0.50, 10)]))
    assert store.get_order(r.order_id)["filled_size"] == 0
    assert acct.cash() == pytest.approx(1000)


def test_latency_delays_execution(store):
    b, acct, s = broker(store, paper_latency_ms=2000)
    r = b.submit(req(), CTX)
    import time
    t0 = time.time()
    assert step(b, make_book("M1-YES", [], [(0.5, 100)]), now=t0) == []
    assert step(b, make_book("M1-YES", [], [(0.5, 100)]), now=t0 + 3)


def test_passive_order_not_filled_on_touch_only_on_trade_through(store):
    b, acct, s = broker(store, paper_passive_fill_fraction=0.5)
    r = b.submit(req(size=20, price=0.45, otype=OrderType.GTC), CTX)
    step(b, make_book("M1-YES", [(0.44, 100)], [(0.47, 100)]))  # not marketable
    step(b, make_book("M1-YES", [(0.44, 100)], [(0.45, 100)]))  # touches our price: no fill
    assert store.get_order(r.order_id)["filled_size"] == 0
    step(b, make_book("M1-YES", [(0.40, 100)], [(0.44, 10)]))  # trades through by a tick
    o = store.get_order(r.order_id)
    assert o["status"] == "PARTIAL" and o["filled_size"] == pytest.approx(5)
    assert o["fees"] == 0  # maker on a taker-only schedule


def test_cancel_and_expiry(store):
    b, acct, s = broker(store)
    r = b.submit(req(price=0.40, otype=OrderType.GTC), CTX)
    assert b.cancel(r.order_id).confirmed
    assert store.get_order(r.order_id)["status"] == "CANCELED"
    r2 = b.submit(req(price=0.40, otype=OrderType.GTD, expires=NOW + 10), CTX)
    step(b, make_book("M1-YES", [(0.38, 1)], [(0.45, 1)]), now=NOW + 11)
    assert store.get_order(r2.order_id)["status"] == "EXPIRED"


def test_rejects_off_tick_and_below_minimum(store):
    b, acct, s = broker(store)
    assert not b.submit(req(price=0.505), CTX).ok
    assert not b.submit(req(size=2), CTX).ok


def test_round_trip_realized_pnl_and_settlement(store):
    b, acct, s = broker(store)
    b.submit(req(size=20, price=0.50), CTX)
    step(b, make_book("M1-YES", [(0.48, 100)], [(0.50, 100)]))
    cost = 20 * 0.5 + fee_for(20, 0.5, FEE)
    b.submit(req(side=Side.SELL, size=10, price=0.60, purpose="exit"), CTX)
    step(b, make_book("M1-YES", [(0.60, 100)], [(0.62, 100)]))
    pos = store.get_position("paper", "M1-YES")
    sell_net = 10 * 0.6 - fee_for(10, 0.6, FEE)
    assert pos["realized_pnl"] == pytest.approx(sell_net - cost / 2)
    proceeds = acct.settle("M1-YES", 1.0, NOW)
    assert proceeds == pytest.approx(10)
    pos = store.get_position("paper", "M1-YES")
    assert pos["status"] == "SETTLED" and pos["shares"] == 0
    assert pos["realized_pnl"] == pytest.approx(sell_net + 10 - cost)
    assert acct.cash() == pytest.approx(1000 - cost + sell_net + 10)
    assert acct.settle("M1-YES", 1.0, NOW) is None  # idempotent


# ------------------------------------------------------------------ exits
def pos(shares=20, cost=10.25, **kw):
    return {"shares": shares, "cost_basis": cost, "opened_at": NOW - 3600, "peak_pnl_pct": None, **kw}


def test_take_profit_basis_is_net_exit_value_vs_cost_basis():
    s = make_settings(take_profit_pct=0.06, exit_on_edge_loss=False)
    m = make_market()
    book = make_book("M1-YES", [(0.56, 100)], [(0.57, 100)])
    d = evaluate_exit(s, pos(), m, book, None, NOW)
    net = 20 * 0.56 - fee_for(20, 0.56, FEE)
    assert d.numbers["net_exit_value"] == pytest.approx(net, abs=1e-4)
    assert d.numbers["pnl_pct"] == pytest.approx((net - 10.25) / 10.25, abs=1e-4)
    assert d.action == "EXIT" and "take profit" in d.reasons[0]
    book2 = make_book("M1-YES", [(0.53, 100)], [(0.54, 100)])
    assert evaluate_exit(s, pos(), m, book2, None, NOW).action == "HOLD"


def test_edge_gone_exit_and_hold_when_edge_remains():
    s = make_settings(take_profit_pct=0.5, exit_on_edge_loss=True)
    m = make_market()
    book = make_book("M1-YES", [(0.60, 100)], [(0.61, 100)])
    # exit value after fees = 0.60 - 0.012 = 0.588 >= fair 0.58 -> edge gone
    assert evaluate_exit(s, pos(), m, book, make_fair("M1-YES", 0.58), NOW).action == "EXIT"
    d = evaluate_exit(s, pos(), m, book, make_fair("M1-YES", 0.75), NOW)
    assert d.action == "HOLD" and "holding" in d.reasons[0]


def test_stale_estimate_is_not_a_reason_to_hold():
    s = make_settings(take_profit_pct=0.5)
    book = make_book("M1-YES", [(0.52, 100)], [(0.53, 100)])
    d = evaluate_exit(s, pos(), make_market(), book, make_fair("M1-YES", 0.9, age=99_999), NOW)
    assert d.action == "HOLD" and "stale" in d.reasons[0]


def test_trailing_and_stop_loss_and_time_rules():
    m = make_market()
    book = make_book("M1-YES", [(0.52, 100)], [(0.53, 100)])
    s = make_settings(take_profit_pct=0.5, exit_on_edge_loss=False, trailing_enabled=True,
                      trailing_activation_pct=0.04, trailing_distance_pct=0.03)
    d = evaluate_exit(s, pos(peak_pnl_pct=0.08), m, book, None, NOW)
    assert d.action == "EXIT" and "trailing" in d.reasons[0]
    s2 = make_settings(take_profit_pct=0.5, exit_on_edge_loss=False)
    low = make_book("M1-YES", [(0.30, 100)], [(0.31, 100)])
    assert evaluate_exit(s2, pos(), m, low, None, NOW).action == "HOLD"  # stop-loss disabled by default
    s3 = make_settings(take_profit_pct=0.5, exit_on_edge_loss=False, stop_loss_pct=0.2)
    d3 = evaluate_exit(s3, pos(), m, low, None, NOW)
    assert d3.action == "EXIT" and "NOT guaranteed" in d3.reasons[0]
    s4 = make_settings(take_profit_pct=0.5, exit_on_edge_loss=False, max_holding_hours=0.5)
    assert evaluate_exit(s4, pos(), m, book, None, NOW).action == "EXIT"
    s5 = make_settings(take_profit_pct=0.5, exit_on_edge_loss=False, pre_start_action=PreStartAction.EXIT)
    assert evaluate_exit(s5, pos(), make_market(start_in_h=0.001), book, None, NOW).action == "EXIT"


def test_no_bids_means_hold_and_floor_respected():
    s = make_settings(stop_loss_pct=0.1, min_exit_price=0.05)
    d = evaluate_exit(s, pos(), make_market(), make_book("M1-YES", [], [(0.5, 10)]), None, NOW)
    assert d.action == "HOLD" and "not liquidating" in d.reasons[0]
    d2 = evaluate_exit(s, pos(), make_market(), make_book("M1-YES", [(0.02, 1000)], [(0.03, 10)]), None, NOW)
    assert d2.action == "EXIT" and d2.min_price >= 0.05  # order will simply not fill below the floor
