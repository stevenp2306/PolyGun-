import pytest

from polybot.domain import FeeSchedule, Level
from polybot.pricing import (
    expected_value_per_share,
    fee_for,
    fee_per_share,
    kelly_fraction_binary,
    liquidation_value,
    quote_buy,
    round_to_tick,
    walk_book,
)

FEE = FeeSchedule(rate=0.05, exponent=1, taker_only=True)


def test_fee_formula_matches_sdk_shape():
    # fee/share = rate * (p(1-p))^exponent ; at p=0.5: 0.05*0.25 = 0.0125
    assert fee_per_share(0.5, FEE) == pytest.approx(0.0125)
    assert fee_for(100, 0.5, FEE) == pytest.approx(1.25)
    # symmetric and smaller near the edges
    assert fee_per_share(0.1, FEE) == pytest.approx(fee_per_share(0.9, FEE))
    assert fee_per_share(0.95, FEE) < fee_per_share(0.5, FEE)


def test_maker_pays_no_fee_when_taker_only_and_none_schedule_is_zero():
    assert fee_per_share(0.5, FEE, is_taker=False) == 0
    assert fee_per_share(0.5, None) == 0
    assert fee_per_share(0.5, FeeSchedule(0.02, 1, taker_only=False), is_taker=False) == pytest.approx(0.005)


def test_walk_book_respects_limit_size_and_depth():
    asks = [Level(0.50, 10), Level(0.51, 10), Level(0.55, 100)]
    w = walk_book(asks, "BUY", shares=25, limit_price=0.52)
    assert w.shares == pytest.approx(20)  # 0.55 is beyond the limit
    assert w.avg_price == pytest.approx(0.505)
    assert not w.fully_filled
    w2 = walk_book(asks, "BUY", shares=25, limit_price=0.60, depth_fraction=0.5)
    assert w2.shares == pytest.approx(25)
    assert [f.size for f in w2.fills] == [5, 5, 15]


def test_walk_book_budget_includes_fees():
    asks = [Level(0.50, 1000)]
    w = walk_book(asks, "BUY", budget=10.0, fee=FEE)
    assert w.total_cost <= 10.0 + 1e-9
    assert w.shares == pytest.approx(10 / (0.5 + 0.0125), abs=0.01)


def test_quote_reports_slippage_vs_best_ask():
    q = quote_buy([Level(0.40, 10), Level(0.42, 100)], budget=20, limit_price=0.45, fee=None, depth_fraction=1)
    assert q.best_ask == 0.40
    assert q.slippage > 0
    assert q.all_in_per_share == pytest.approx(q.avg_price)


def test_liquidation_values_unabsorbed_shares_at_zero():
    lv = liquidation_value([Level(0.6, 10)], shares=30, fee=FEE)
    assert lv.shares == 10
    assert lv.net_proceeds == pytest.approx(10 * 0.6 - fee_for(10, 0.6, FEE))


def test_ev_and_kelly():
    assert expected_value_per_share(0.6, 0.5) == pytest.approx(0.1)
    assert kelly_fraction_binary(0.6, 0.5) == pytest.approx(0.2)
    assert kelly_fraction_binary(0.4, 0.5) == 0
    assert kelly_fraction_binary(0.9, 1.0) == 0


def test_round_to_tick():
    assert round_to_tick(0.4567, 0.01, "down") == pytest.approx(0.45)
    assert round_to_tick(0.4511, 0.01, "up") == pytest.approx(0.46)
    assert round_to_tick(0.45, 0.01, "down") == pytest.approx(0.45)
    assert round_to_tick(0.4567, 0.001, "down") == pytest.approx(0.456)
