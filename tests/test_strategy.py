import pytest

from polybot.marketdata import BookStore
from polybot.strategy.sports_value import SportsValueStrategy
from tests.conftest import NOW, make_book, make_fair, make_market, make_settings


def run(fair_prob=0.60, bids=((0.48, 500),), asks=((0.50, 500),), budget=50.0, fair_age=0, book_age=0,
        unc=0.01, **settings_kw):
    s = make_settings(**settings_kw)
    m = make_market()
    books = BookStore(s.max_book_age_s)
    books.put(make_book("M1-YES", bids, asks, now=NOW - book_age))
    books.put(make_book("M1-NO", [(0.48, 500)], [(0.52, 500)], now=NOW - book_age))
    fairs = {"M1-YES": make_fair("M1-YES", fair_prob, unc=unc, age=fair_age)} if fair_prob is not None else None
    cands = SportsValueStrategy(s).evaluate(m, fairs, "no estimate", books,
                                            {"M1-YES": budget, "M1-NO": budget}, 1000.0, NOW)
    return cands[0], s


def test_approves_when_edge_exceeds_costs_and_buffer():
    c, s = run()
    assert c.approved, c.reasons
    n = c.numbers
    # all-in includes the taker fee: 0.50 + 0.05*0.25 = 0.5125
    assert n["all_in_per_share"] == pytest.approx(0.5125, abs=1e-4)
    assert n["edge"] == pytest.approx(0.60 - 0.5125, abs=1e-4)
    assert n["required_edge"] == pytest.approx(s.min_edge + max(s.min_uncertainty, s.uncertainty_z * 0.01))
    assert c.limit_price >= 0.50 and c.shares >= 5


def test_rejects_without_independent_estimate():
    c, _ = run(fair_prob=None)
    assert not c.approved
    assert "no independent probability estimate" in c.reasons[0]


def test_market_price_is_never_used_as_fair_value():
    # The bid/ask imply ~0.49; with no external estimate the strategy must not trade either side.
    c, _ = run(fair_prob=None, bids=((0.10, 500),), asks=((0.11, 500),))
    assert not c.approved


def test_uses_executable_ask_not_midpoint():
    # mid = 0.45 would look like a 10-point edge vs 0.55, but the ask is 0.52 with fee -> edge too small
    c, _ = run(fair_prob=0.55, bids=((0.38, 500),), asks=((0.52, 500),), max_spread=0.2)
    assert not c.approved
    assert "required" in " ".join(c.reasons)


def test_stale_estimate_and_stale_book_rejected():
    c, _ = run(fair_age=10_000)
    assert not c.approved and "stale" in c.reasons[0]
    c2, _ = run(book_age=10_000)
    assert not c2.approved and "stale" in c2.reasons[0]


def test_wide_spread_and_price_band():
    c, _ = run(bids=((0.30, 500),), asks=((0.50, 500),))
    assert not c.approved and "spread" in c.reasons[0]
    c2, _ = run(fair_prob=0.99, bids=((0.93, 500),), asks=((0.95, 500),))
    assert not c2.approved and "price band" in c2.reasons[0]


def test_uncertainty_buffer_blocks_marginal_trade():
    c, _ = run(fair_prob=0.56, unc=0.05)  # edge ~0.0475 < 0.03 + 0.05
    assert not c.approved


def test_size_below_exchange_minimum_rejected():
    c, _ = run(budget=1.0)
    assert not c.approved and "below exchange minimum" in c.reasons[-1]


def test_thin_depth_limits_size_and_slippage_cap():
    c, _ = run(asks=((0.50, 12), (0.58, 500)), max_slippage=0.01)
    assert c.approved
    assert c.shares <= 12 * 0.5 + 1e-9  # max_depth_fraction=0.5 of the only level inside the limit
    c2, _ = run(asks=((0.50, 6), (0.58, 500)), max_slippage=0.01)
    assert not c2.approved  # 3 usable shares < 5-share exchange minimum
