"""End-to-end paper-mode engine tests with fake market data and a fake probability provider."""

import pytest

from polybot.domain import OrderRequest, OrderType, Side
from polybot.engine import Engine
from tests.conftest import NOW, FakeProvider, FakeSource, make_book, make_market, make_settings


def build(store, frozen_time, prob=0.62, market=None, **kw):
    s = make_settings(**kw)
    m = market or make_market()
    src = FakeSource([m])
    eng = Engine(s, store, src, src, [FakeProvider({"M1-YES": prob, "M1-NO": 1 - prob})])
    eng.startup()
    eng.discovery.refresh(frozen_time["now"])
    return eng, m


def books(eng, now, yes_bid=0.48, yes_ask=0.50):
    eng.books.put(make_book("M1-YES", [(yes_bid, 500)], [(yes_ask, 500)], now=now))
    eng.books.put(make_book("M1-NO", [(1 - yes_ask, 500)], [(1 - yes_bid, 500)], now=now))
    eng.health.rest_last_success = now


def advance(ft, secs):
    ft["now"] += secs
    return ft["now"]


def orders(store, **kw):
    return store.list_orders("paper", **kw)


def test_paper_loop_places_fills_and_tracks_position(store, frozen_time):
    eng, m = build(store, frozen_time)
    books(eng, NOW)
    eng.tick(NOW)
    os_ = orders(store)
    assert len(os_) == 1 and os_[0]["side"] == "BUY" and os_[0]["token_id"] == "M1-YES"
    assert os_[0]["exchange_order_id"].startswith("SIM-")
    t = advance(frozen_time, 5)
    books(eng, t)
    eng.tick(t)
    pos = store.list_positions("paper")
    assert len(pos) == 1 and pos[0]["shares"] > 0
    assert pos[0]["entry_reason"] and pos[0]["exit_plan"]
    dec = store.list_decisions()
    assert any(d["decision"] == "TRADE" for d in dec)
    assert any(d["decision"] == "SKIP" for d in dec)  # the NO side was evaluated and rejected
    # no adding to the position on later loops
    t = advance(frozen_time, 5)
    books(eng, t)
    eng.tick(t)
    assert len(orders(store)) == 1


def test_restart_does_not_duplicate_orders(store, frozen_time):
    eng, m = build(store, frozen_time, paper_latency_ms=60_000)
    books(eng, NOW)
    eng.tick(NOW)
    assert len(orders(store)) == 1  # pending (latency) and not yet filled
    eng2, _ = build(store, frozen_time, paper_latency_ms=60_000)  # simulated restart, same database
    books(eng2, NOW + 1)
    eng2.tick(NOW + 1)
    assert len(orders(store)) == 1


def test_no_entries_on_stale_data(store, frozen_time):
    eng, m = build(store, frozen_time)
    books(eng, NOW - 10_000)  # stale books
    eng.tick(NOW)
    assert orders(store) == []
    assert any("stale" in " ".join(c["reasons"]) for c in eng.last_candidates)


def test_no_entries_without_independent_probability(store, frozen_time):
    s = make_settings()
    src = FakeSource([make_market()])
    eng = Engine(s, store, src, src, [FakeProvider({}, reason="no sportsbook match")])
    eng.discovery.refresh(NOW)
    books(eng, NOW)
    eng.tick(NOW)
    assert orders(store) == []
    assert "no sportsbook match" in eng.last_candidates[0]["reasons"][0]


def test_kill_switch_cancels_bot_orders_and_blocks_new_ones(store, frozen_time):
    eng, m = build(store, frozen_time, paper_latency_ms=60_000)
    books(eng, NOW)
    eng.tick(NOW)
    results = eng.kill("test")
    assert results and all(r["confirmed"] for r in results)
    assert orders(store)[0]["status"] == "CANCELED"
    eng.tick(NOW + 1)
    assert len(orders(store)) == 1
    assert store.list_alerts()[0]["kind"] in ("kill_switch",) or any(a["kind"] == "kill_switch" for a in store.list_alerts())


def test_risk_lock_cancels_entries_and_alerts(store, frozen_time):
    eng, m = build(store, frozen_time, daily_loss_limit_pct=0.001, max_drawdown_pct=0.9)
    books(eng, NOW)
    eng.tick(NOW)
    t = advance(frozen_time, 5)
    books(eng, t)
    eng.tick(t)  # filled
    t = advance(frozen_time, 5)
    books(eng, t, yes_bid=0.05, yes_ask=0.06)  # crash -> conservative mark loss
    eng.tick(t)
    assert "daily_loss" in eng.risk.locks()
    assert any(a["kind"] == "risk_limit" for a in store.list_alerts())
    assert not eng.risk.entry_gate(t)[0]


def test_settlement_of_resolved_market(store, frozen_time):
    eng, m = build(store, frozen_time)
    books(eng, NOW)
    eng.tick(NOW)
    t = advance(frozen_time, 5)
    books(eng, t)
    eng.tick(t)
    shares = store.list_positions("paper")[0]["shares"]
    cash_before = eng.acct.cash()
    m.closed, m.resolution_status, m.outcome_prices = True, "resolved", [1.0, 0.0]
    eng.tick(t + 5)
    assert store.list_positions("paper") == []
    assert eng.acct.cash() == pytest.approx(cash_before + shares)


def test_entry_orders_cancelled_close_to_start(store, frozen_time):
    eng, m = build(store, frozen_time, market=make_market(start_in_h=0.1), min_minutes_to_start=0)
    eng.broker.submit(OrderRequest("M1-YES", "M1", "E-M1", Side.BUY, 10, 0.30, OrderType.GTC, "entry", "t", "r"),
                      {"tick_size": 0.01, "min_order_size": 5})
    books(eng, NOW)
    eng.tick(NOW)
    assert all(o["status"] == "CANCELED" for o in orders(store) if o["order_type"] == "GTC")


def test_paper_mode_never_constructs_live_broker(store, frozen_time):
    eng, _ = build(store, frozen_time)
    from polybot.brokers.paper import PaperBroker
    assert isinstance(eng.broker, PaperBroker) and eng.broker.simulated


def test_take_profit_exit_flow(store, frozen_time):
    eng, m = build(store, frozen_time, take_profit_pct=0.05, exit_on_edge_loss=False)
    books(eng, NOW)
    eng.tick(NOW)
    t = advance(frozen_time, 5)
    books(eng, t)
    eng.tick(t)
    t = advance(frozen_time, 5)
    books(eng, t, yes_bid=0.60, yes_ask=0.61)
    eng.tick(t)  # exit order submitted
    t = advance(frozen_time, 5)
    books(eng, t, yes_bid=0.60, yes_ask=0.61)
    eng.tick(t)  # exit filled
    closed = store.list_positions("paper", status=None)
    assert closed[0]["status"] == "CLOSED" and closed[0]["realized_pnl"] > 0
