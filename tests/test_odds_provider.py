import logging

import httpx
import pytest

from polybot.providers.odds_api import OddsApiProvider
from tests.conftest import NOW, make_market, make_settings



def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def payload(start, books=None, upd=NOW):
    books = books if books is not None else [
        ("pinnacle", [2.00, 3.60, 4.00]), ("bet365", [1.95, 3.50, 4.00]),
        ("williamhill", [1.97, 3.50, 3.90]), ("unibet", [1.98, 3.55, 3.95])]
    return [{
        "id": "ev1", "sport_key": "soccer_epl", "commence_time": iso(start), "home_team": "Arsenal",
        "away_team": "Chelsea",
        "bookmakers": [{"key": k, "title": k, "last_update": iso(upd), "markets": [{
            "key": "h2h", "last_update": iso(upd),
            "outcomes": [{"name": "Arsenal", "price": o[0]}, {"name": "Draw", "price": o[1]},
                         {"name": "Chelsea", "price": o[2]}]}]} for k, o in books],
    }]


def provider(data, status=200, **kw):
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        return httpx.Response(status, json=data, headers={"x-requests-remaining": "450"})

    s = make_settings(odds_api_key="SECRETODDSKEY123", sports="soccer_epl", min_bookmakers=3, **kw)
    return OddsApiProvider(s, http=httpx.Client(transport=httpx.MockTransport(handler))), seen


def test_disabled_without_key_and_never_invents_data():
    p = OddsApiProvider(make_settings())
    assert not p.status().enabled
    est, why = p.estimate(make_market(), NOW)
    assert est is None and "not set" in why


def test_consensus_estimate_both_tokens(frozen_time):
    m = make_market()
    p, seen = provider(payload(m.game_start_time))
    p.refresh(NOW)
    assert p.status().quota_remaining == 450
    est, why = p.estimate(m, NOW)
    assert why == "ok"
    yes, no = est["M1-YES"], est["M1-NO"]
    assert yes.prob + no.prob == pytest.approx(1.0)
    assert 0.45 < yes.prob < 0.52
    assert yes.n_sources == 4 and yes.uncertainty > 0
    assert yes.details["books"] == ["bet365", "pinnacle", "unibet", "williamhill"]


def test_too_few_or_stale_bookmakers_rejected(frozen_time):
    m = make_market()
    p, _ = provider(payload(m.game_start_time, books=[("pinnacle", [2.0, 3.6, 4.0]), ("bet365", [1.95, 3.5, 4.0])]))
    p.refresh(NOW)
    assert p.estimate(m, NOW)[0] is None
    p2, _ = provider(payload(m.game_start_time, upd=NOW - 10_000))
    p2.refresh(NOW)
    est, why = p2.estimate(m, NOW)
    assert est is None and "fresh" in why


def test_disagreeing_books_rejected(frozen_time):
    m = make_market()
    books = [("a", [1.5, 4.5, 6.0]), ("b", [3.0, 3.4, 2.4]), ("c", [2.0, 3.6, 4.0])]
    p, _ = provider(payload(m.game_start_time, books=books), max_bookmaker_disagreement=0.03)
    p.refresh(NOW)
    est, why = p.estimate(m, NOW)
    assert est is None and "disagree" in why


def test_rules_mismatch_rejected(frozen_time):
    m = make_market(description="Resolves to the winner including extra time and penalties.")
    p, _ = provider(payload(m.game_start_time))
    p.refresh(NOW)
    assert p.estimate(m, NOW)[0] is None


def test_auth_failure_disables_and_error_never_contains_key(caplog, frozen_time):
    caplog.set_level(logging.DEBUG)
    p, _ = provider([], status=401)
    p.refresh(NOW)
    st = p.status()
    assert not st.enabled
    assert "SECRETODDSKEY123" not in (st.last_error or "")
    assert "SECRETODDSKEY123" not in caplog.text
