import pytest

from polybot.providers.devig import devig, multiplicative, overround, power, shin
from polybot.providers.matching import (
    OddsEvent,
    map_tokens,
    match_event,
    normalize,
    resolution_compatible,
    same_team,
)
from tests.conftest import NOW, make_market

ODDS = [1.50, 4.20, 6.50]  # home / draw / away, ~5% overround


@pytest.mark.parametrize("method", ["multiplicative", "power", "shin"])
def test_devig_sums_to_one(method):
    p = devig(ODDS, method)
    assert sum(p) == pytest.approx(1.0, abs=1e-9)
    assert all(0 < x < 1 for x in p)


def test_power_and_shin_shrink_longshots_vs_multiplicative():
    m, pw, sh = multiplicative(ODDS), power(ODDS), shin(ODDS)
    assert pw[2] < m[2] and sh[2] < m[2]  # longshot gets less probability
    assert pw[0] > m[0] and sh[0] > m[0]  # favourite gets more
    assert overround(ODDS) == pytest.approx(1 / 1.5 + 1 / 4.2 + 1 / 6.5 - 1)


def test_devig_rejects_invalid_odds():
    with pytest.raises(ValueError):
        devig([1.0, 2.0])


def test_team_normalisation_and_strict_matching():
    assert normalize("Arsenal FC") == "arsenal"
    assert same_team("Man Utd", "Manchester United")
    assert same_team("Chiefs", "Kansas City Chiefs")
    assert not same_team("Manchester", "Manchester City")  # generic token alone is ambiguous
    assert not same_team("Manchester United", "Manchester City")


def ev(home="Arsenal", away="Chelsea", start=NOW + 24 * 3600):
    return OddsEvent("ev1", "soccer_epl", start, home, away, {})


def test_match_event_requires_both_teams_and_start_time():
    m = make_market()
    assert match_event(m, [ev()], 1800)[0] is not None
    assert match_event(m, [ev(start=NOW + 30 * 3600)], 1800)[0] is None  # kickoff differs
    assert match_event(m, [ev(away="Liverpool")], 1800)[0] is None
    got, why = match_event(m, [ev(), ev()], 1800)
    assert got is None and "ambiguous" in why


def test_map_tokens_yes_no_question():
    m = make_market()
    tm, _ = map_tokens(m, ev(), ["Arsenal", "Chelsea", "Draw"])
    assert tm.mapping["M1-YES"] == ("Arsenal", False)
    assert tm.mapping["M1-NO"] == ("Arsenal", True)


def test_map_tokens_team_labels_and_draw_question():
    from polybot.domain import OutcomeToken
    nfl = make_market(tokens=[OutcomeToken("A", "Chiefs"), OutcomeToken("B", "Bills")],
                      question="Chiefs vs. Bills", event_title="Chiefs vs. Bills")
    tm, _ = map_tokens(nfl, ev("Kansas City Chiefs", "Buffalo Bills"), ["Kansas City Chiefs", "Buffalo Bills"])
    assert tm.mapping == {"A": ("Kansas City Chiefs", False), "B": ("Buffalo Bills", False)}
    draw = make_market(question="Will Arsenal vs. Chelsea end in a draw?")
    tm2, _ = map_tokens(draw, ev(), ["Arsenal", "Chelsea", "Draw"])
    assert tm2.mapping["M1-YES"] == ("Draw", False)


def test_map_tokens_rejects_ambiguous_question():
    m = make_market(question="Will Arsenal or Chelsea score first?")
    tm, why = map_tokens(m, ev(), ["Arsenal", "Chelsea", "Draw"])
    assert tm is None


def test_resolution_rule_compatibility():
    ok, _ = resolution_compatible(make_market(), "soccer_epl")
    assert ok
    bad = make_market(description="Resolves to the team that advances, including extra time and penalties.")
    assert not resolution_compatible(bad, "soccer_epl")[0]
    vague = make_market(description="Resolves Yes if Arsenal wins.")
    assert not resolution_compatible(vague, "soccer_epl")[0]
    nfl = make_market(description="Includes overtime.")
    assert resolution_compatible(nfl, "americanfootball_nfl")[0]
