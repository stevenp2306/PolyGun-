import pytest
from polymarket import Market

from polybot.discovery import Discovery, check_eligibility, market_from_sdk
from polybot.domain import Level
from polybot.marketdata import BookStore, FeedHealth
from tests.conftest import NOW, FakeSource, make_book, make_market, make_settings


def sdk_market(**over):
    payload = dict(
        id="123", question="Will Arsenal win on 2027-01-16?", slug="arsenal", conditionId="0x" + "ab" * 32,
        description="Resolves Yes if Arsenal wins in regular time (90 minutes).", category="Sports",
        active=True, closed=False, archived=False, acceptingOrders=True, enableOrderBook=True, negRisk=True,
        endDate="2027-01-16T20:00:00Z", outcomes='["Yes", "No"]', outcomePrices='["0.6", "0.4"]',
        clobTokenIds='["111", "222"]', liquidityNum=25000, volumeNum=90000, bestBid="0.59", bestAsk="0.61",
        orderMinSize="5", orderPriceMinTickSize="0.01", feesEnabled=True,
        feeSchedule={"exponent": 1, "rate": "0.05", "takerOnly": True, "rebateRate": "0.15"},
        sportsMarketType="moneyline", gameId="G1", gameStartTime="2027-01-16T17:30:00Z",
        events=[{"id": "EV1", "slug": "ars-che", "title": "Arsenal vs. Chelsea"}],
        tags=[{"id": "1", "slug": "epl", "label": "EPL"}],
    )
    payload.update(over)
    return Market.parse_response(payload)


def test_market_from_real_sdk_model():
    m = market_from_sdk(sdk_market())
    assert m.market_id == "123" and m.event_id == "EV1" and m.event_title == "Arsenal vs. Chelsea"
    assert [t.token_id for t in m.tokens] == ["111", "222"] and m.tokens[0].label == "Yes"
    assert m.fee.rate == pytest.approx(0.05) and m.fee.taker_only
    assert m.tick_size == 0.01 and m.min_order_size == 5
    assert m.game_start_time is not None and m.sports_market_type == "moneyline"
    assert m.outcome_prices == [0.6, 0.4]


def test_eligibility_reasons():
    s = make_settings(min_market_liquidity_usd=100)
    assert check_eligibility(make_market(), s, NOW).eligible
    cases = {
        "market closed": make_market(closed=True),
        "not accepting orders": make_market(accepting_orders=False),
        "unsupported market type": make_market(sports_market_type="totals"),
        "pre-match strategy only": make_market(start_in_h=0.2),
        "fee schedule unknown": make_market(fee=None),
        "liquidity": make_market(liquidity=10),
        "no resolution criteria": make_market(description=""),
        "resolution more than": make_market(start_in_h=400),
    }
    for needle, m in cases.items():
        el = check_eligibility(m, s, NOW)
        assert not el.eligible and any(needle in r for r in el.reasons), (needle, el.reasons)


def test_discovery_stores_reasons(store):
    s = make_settings()
    d = Discovery(FakeSource([make_market(), make_market(mid="M2", closed=True)]), s, store)
    d.refresh(NOW)
    assert list(d.eligible) == ["M1"]
    rows = {r["market_id"]: r for r in store.list_markets()}
    assert rows["M2"]["eligible"] is False and "market closed" in rows["M2"]["reasons"]


def test_book_staleness_crossed_and_suspect():
    bs = BookStore(max_age_s=30)
    bs.put(make_book("A", [(0.4, 10)], [(0.5, 10)], now=NOW))
    assert bs.fresh("A", NOW + 10) is not None
    assert bs.fresh("A", NOW + 31) is None and "stale" in bs.quality("A", NOW + 31)[1]
    bs.put(make_book("B", [(0.6, 10)], [(0.5, 10)], now=NOW))
    assert bs.fresh("B", NOW) is None and "crossed" in bs.quality("B", NOW)[1]
    bs.suspect.add("A")
    assert bs.fresh("A", NOW) is None
    bs.put(make_book("A", [(0.4, 10)], [(0.5, 10)], now=NOW))  # REST snapshot clears suspicion
    assert bs.fresh("A", NOW) is not None
    assert bs.quality("ZZZ", NOW) == (False, "no order book received")


def test_price_change_deltas_and_out_of_order_snapshots():
    bs = BookStore(30)
    b = make_book("A", [(0.4, 10)], [(0.5, 10)], now=NOW)
    b.exchange_ts = 100
    bs.put(b)
    bs.apply_price_change("A", "SELL", 0.49, 5, NOW + 1, 101)
    assert bs.get("A").best_ask == 0.49
    bs.apply_price_change("A", "SELL", 0.49, 0, NOW + 2, 102)  # level removed
    assert bs.get("A").best_ask == 0.5
    old = make_book("A", [(0.1, 1)], [(0.9, 1)], now=NOW + 3)
    old.exchange_ts = 50
    bs.put(old)
    assert bs.get("A").best_bid == 0.4  # older message ignored


def test_feed_health_status():
    h = FeedHealth()
    assert h.status(NOW, 30) == "stale"
    h.rest_last_success = NOW - 5
    assert h.status(NOW, 30) == "ok"
    h.rest_consecutive_failures = 3
    assert h.status(NOW, 30) == "degraded"
