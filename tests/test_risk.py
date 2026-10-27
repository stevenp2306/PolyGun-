import pytest

from polybot.domain import Fill, OrderRequest, OrderType, Side
from polybot.portfolio import Accounting
from polybot.risk import RiskEngine, correlation_key
from tests.conftest import NOW, make_book, make_market, make_settings


def setup(store, **kw):
    s = make_settings(**kw)
    acct = Accounting(store, "paper")
    acct.ensure_initial_balance(1000.0)
    return s, acct, RiskEngine(s, store, "paper")


def buy(acct, token="M1-YES", size=20, price=0.5, fee=0.0, event="E-M1", key="epl:2027-01-15"):
    acct.apply_fill(Fill("o", token, Side.BUY, price, size, fee, NOW, trade_id=f"t-{token}-{size}-{price}"),
                    {"event_id": event, "market_id": event[2:], "correlation_key": key})


def snap(acct, books=None):
    books = books or {}
    return acct.snapshot(lambda t: books.get(t), lambda t: None, now=NOW)


def test_position_cap_is_min_of_usd_and_pct(store):
    s, acct, risk = setup(store, max_position_usd=5.0, max_position_pct=0.01)
    b, notes = risk.budget_for(make_market(), "M1-YES", snap(acct), {}, set())
    assert b == pytest.approx(5.0)
    s, acct, risk = setup(store, max_position_usd=50.0, max_position_pct=0.001, bankroll_usd=1000)
    assert risk.budget_for(make_market(), "M1-YES", snap(acct), {}, set())[0] == pytest.approx(1.0)


def test_no_doubling_down_and_no_opposite_side(store):
    s, acct, risk = setup(store)
    buy(acct)
    sn = snap(acct)
    assert risk.budget_for(make_market(), "M1-YES", sn, {}, set())[0] == 0
    assert risk.budget_for(make_market(), "M1-NO", sn, {}, set())[0] == 0


def test_event_total_and_correlated_caps(store):
    s, acct, risk = setup(store, max_event_exposure_pct=0.03, max_total_exposure_pct=0.04,
                          max_correlated_exposure_pct=0.035)
    buy(acct, token="X-YES", size=40, price=0.5, event="E-M1",
        key=correlation_key(make_market()))  # $20 in event E-M1, same league + matchday
    books = {"X-YES": make_book("X-YES", [(0.5, 1000)], [(0.51, 1000)])}  # marked at cost -> equity 1000
    m = make_market()  # event E-M1
    b, notes = risk.budget_for(m, "M1-YES", snap(acct, books), {}, set())
    assert b == pytest.approx(10.0)  # 3% of 1000 - 20
    m2 = make_market(mid="M2")
    b2, notes2 = risk.budget_for(m2, "M2-YES", snap(acct, books), {}, set())
    assert b2 == pytest.approx(15.0)  # correlated cap 35 - 20 binds before total 40 - 20


def test_open_orders_count_as_exposure_and_reserve(store):
    s, acct, risk = setup(store, min_cash_reserve_usd=990)
    b, notes = risk.budget_for(make_market(), "M1-YES", snap(acct), {}, set())
    assert b == pytest.approx(10.0)
    assert "cash_reserve" in notes[0]


def test_max_open_positions(store):
    s, acct, risk = setup(store, max_open_positions=1)
    buy(acct, token="Z-YES", event="E-Z")
    assert risk.budget_for(make_market(), "M1-YES", snap(acct), {}, set())[0] == 0


def test_daily_loss_lock_persists_and_needs_deliberate_reset(store):
    s, acct, risk = setup(store, daily_loss_limit_pct=0.03)
    buy(acct, size=100, price=0.5)  # cost 50
    books = {"M1-YES": make_book("M1-YES", [(0.5, 1000)], [(0.51, 1000)])}
    st = risk.update(snap(acct, books), NOW)
    assert st.can_open
    books = {"M1-YES": make_book("M1-YES", [(0.15, 1000)], [(0.2, 1000)])}  # mark falls -> -35
    st = risk.update(snap(acct, books), NOW + 60)
    assert "daily_loss" in st.triggered_now and not st.can_open
    # next UTC day: lock remains
    st = risk.update(snap(acct, books), NOW + 2 * 86400)
    assert not st.can_open and "daily_loss" in st.locks
    ok, msg = risk.resume("test")
    assert not ok and "reset" in msg
    risk.reset_locks("test", snap(acct, books))
    assert risk.update(snap(acct, books), NOW + 2 * 86400 + 5).can_open


def test_drawdown_lock_and_deposits_not_counted_as_profit(store):
    s, acct, risk = setup(store, max_drawdown_pct=0.10, daily_loss_limit_pct=0.5)
    st = risk.update(snap(acct), NOW)
    acct.record_external_flow("deposit", 500)
    st = risk.update(snap(acct), NOW + 1)
    assert st.hwm == pytest.approx(0.0)  # deposit is not trading profit
    buy(acct, size=400, price=0.5)  # 200 at risk
    books = {"M1-YES": make_book("M1-YES", [(0.05, 10000)], [(0.06, 10)])}
    st = risk.update(snap(acct, books), NOW + 2)
    assert "drawdown" in st.triggered_now


def test_kill_switch_blocks_everything_including_exits(store):
    s, acct, risk = setup(store)
    buy(acct)
    risk.kill("test", "x")
    sn = snap(acct)
    sell = OrderRequest("M1-YES", "M1", "E-M1", Side.SELL, 10, 0.4, OrderType.FAK, "exit", "s", "r")
    assert not risk.check_order(sell, sn, make_market())[0]
    buy_req = OrderRequest("M2-YES", "M2", "E-M2", Side.BUY, 10, 0.4, OrderType.FAK, "entry", "s", "r")
    assert not risk.check_order(buy_req, sn, make_market(mid="M2"), NOW)[0]


def test_loss_lock_still_allows_risk_reducing_exits(store):
    s, acct, risk = setup(store)
    buy(acct)
    store.set("paper:risk_locks", {"daily_loss": {}})
    sell = OrderRequest("M1-YES", "M1", "E-M1", Side.SELL, 10, 0.4, OrderType.FAK, "exit", "s", "r")
    assert risk.check_order(sell, snap(acct), make_market(), NOW)[0]
    oversell = OrderRequest("M1-YES", "M1", "E-M1", Side.SELL, 999, 0.4, OrderType.FAK, "exit", "s", "r")
    assert not risk.check_order(oversell, snap(acct), make_market(), NOW)[0]  # no shorting


def test_entry_order_checks(store, frozen_time):
    s, acct, risk = setup(store, max_order_price=0.9)
    sn = snap(acct)
    ok = OrderRequest("M1-YES", "M1", "E-M1", Side.BUY, 10, 0.5, OrderType.FAK, "entry", "s", "r")
    assert risk.check_order(ok, sn, make_market(), NOW)[0]
    too_high = OrderRequest("M1-YES", "M1", "E-M1", Side.BUY, 10, 0.95, OrderType.FAK, "entry", "s", "r")
    assert not risk.check_order(too_high, sn, make_market(), NOW)[0]
    big = OrderRequest("M1-YES", "M1", "E-M1", Side.BUY, 1000, 0.5, OrderType.FAK, "entry", "s", "r")
    assert not risk.check_order(big, sn, make_market(), NOW)[0]
    soon = make_market(start_in_h=0.1)
    assert not risk.check_order(ok, sn, soon, NOW)[0]


def test_circuit_breaker_and_degraded_data(store):
    s, acct, risk = setup(store, circuit_breaker_failures=2, circuit_breaker_cooldown_s=60)
    assert not risk.record_failure(NOW)
    assert risk.record_failure(NOW)
    assert not risk.entry_gate(NOW + 1)[0]
    assert risk.entry_gate(NOW + 61)[0]
    risk.degraded_reasons = ["market data feed stale"]
    ok, why = risk.entry_gate(NOW + 61)
    assert not ok and "stale" in why[0]


def test_pause_resume(store):
    s, acct, risk = setup(store)
    risk.pause("t")
    assert not risk.entry_gate(NOW)[0]
    assert risk.resume("t")[0]
    assert risk.entry_gate(NOW)[0]
