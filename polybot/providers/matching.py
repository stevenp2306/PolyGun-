"""Matching Polymarket markets to sportsbook events and outcomes.

Matching is deliberately strict: a wrong match would produce a confidently
wrong "fair" probability, which is worse than no estimate at all. Anything
ambiguous returns ``None`` with a reason, and the strategy skips the market.
Add aliases (``POLYBOT_TEAM_ALIASES_PATH`` JSON file) to fix misses.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from polybot.domain import MarketInfo

_STOP = {"fc", "afc", "cf", "sc", "ac", "the", "club", "de", "football", "calcio", "ssc", "as"}

DEFAULT_ALIASES = {
    "man utd": "manchester united",
    "man united": "manchester united",
    "man city": "manchester city",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "brighton": "brighton and hove albion",
    "brighton hove albion": "brighton and hove albion",
    "newcastle": "newcastle united",
    "west ham": "west ham united",
    "nottm forest": "nottingham forest",
    "nott'm forest": "nottingham forest",
    "leeds": "leeds united",
    "inter": "inter milan",
    "internazionale": "inter milan",
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "bayern": "bayern munich",
    "bayern munchen": "bayern munich",
    "atletico": "atletico madrid",
    "atleti": "atletico madrid",
    "barca": "barcelona",
}


def normalize(name: str, aliases: dict[str, str] | None = None) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    al = {**DEFAULT_ALIASES, **(aliases or {})}
    if s in al:
        s = al[s]
    toks = [t for t in s.replace("'", "").split() if t not in _STOP]
    s2 = " ".join(toks)
    return al.get(s2, s2)


def load_aliases(path: Path | None) -> dict[str, str]:
    if path is None or not Path(path).is_file():
        return {}
    raw = json.loads(Path(path).read_text())
    return {normalize(k, {}): normalize(v, {}) for k, v in raw.items()}


def same_team(a: str, b: str, aliases: dict[str, str] | None = None) -> bool:
    na, nb = normalize(a, aliases), normalize(b, aliases)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    # Allow "Chiefs" vs "Kansas City Chiefs": the shorter name's tokens must all
    # appear in the longer one AND the shorter name must not be a generic word
    # shared by several teams (e.g. "manchester", "united", "city").
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    generic = {"united", "city", "manchester", "real", "athletic", "sporting", "new", "san",
               "los", "angeles", "york", "st", "saint", "inter", "milan", "town", "county", "rovers"}
    return short <= long_ and not short <= generic


def mentions(text: str, team: str, aliases: dict[str, str] | None = None) -> bool:
    nt = normalize(text, aliases)
    tn = normalize(team, aliases)
    if not tn:
        return False
    return re.search(rf"\b{re.escape(tn)}\b", nt) is not None


@dataclass
class OddsEvent:
    event_id: str
    sport_key: str
    commence_time: float
    home_team: str
    away_team: str
    # outcome name -> list of (bookmaker_key, decimal_odds_vector_index) handled by provider
    raw: dict


@dataclass
class TokenOutcomeMap:
    """token_id -> (provider outcome name, negate)."""

    mapping: dict[str, tuple[str, bool]]
    explanation: str


def split_title(title: str) -> tuple[str, str] | None:
    parts = re.split(r"\s+(?:vs\.?|v\.?|@|at)\s+", title, flags=re.I)
    if len(parts) != 2:
        return None
    return parts[0].strip(), parts[1].strip()


def match_event(market: MarketInfo, events: list[OddsEvent], tolerance_s: float,
                aliases: dict[str, str] | None = None) -> tuple[OddsEvent | None, str]:
    if market.game_start_time is None:
        return None, "market has no game start time"
    title = market.event_title or market.question
    teams = split_title(title or "")
    cands = []
    for ev in events:
        if abs(ev.commence_time - market.game_start_time) > tolerance_s:
            continue
        if teams:
            a, b = teams
            ok = ((same_team(a, ev.home_team, aliases) and same_team(b, ev.away_team, aliases)) or
                  (same_team(a, ev.away_team, aliases) and same_team(b, ev.home_team, aliases)))
        else:
            ok = mentions(market.question, ev.home_team, aliases) and mentions(market.question, ev.away_team, aliases)
        if ok:
            cands.append(ev)
    if not cands:
        return None, f"no sportsbook event matched '{title}' within {tolerance_s/60:.0f} min of start time"
    if len(cands) > 1:
        return None, f"ambiguous: {len(cands)} sportsbook events matched '{title}'"
    return cands[0], "matched"


def map_tokens(market: MarketInfo, ev: OddsEvent, outcome_names: list[str],
               aliases: dict[str, str] | None = None) -> tuple[TokenOutcomeMap | None, str]:
    """Map each market token to a sportsbook outcome (or its complement)."""
    labels = [t.label for t in market.tokens]
    # Case 1: outcome labels are the teams themselves (e.g. NFL "Chiefs" / "Bills").
    teams = [n for n in outcome_names if n.lower() != "draw"]
    mapping: dict[str, tuple[str, bool]] = {}
    for tok in market.tokens:
        hits = [n for n in teams if same_team(tok.label, n, aliases)]
        if len(hits) == 1:
            mapping[tok.token_id] = (hits[0], False)
    if len(mapping) == 2 and len({v[0] for v in mapping.values()}) == 2:
        if "Draw" in outcome_names or "draw" in [n.lower() for n in outcome_names]:
            return None, "team-labelled market but sportsbook has a draw outcome: rules differ"
        return TokenOutcomeMap(mapping, f"outcome labels {labels} are team names"), "ok"
    # Case 2: Yes/No market about one outcome.
    if sorted(l.lower() for l in labels) == ["no", "yes"]:
        yes = next(t for t in market.tokens if t.label.lower() == "yes")
        no = next(t for t in market.tokens if t.label.lower() == "no")
        q = market.question
        if re.search(r"\bdraw\b|\btie\b", q, re.I):
            draw_name = next((n for n in outcome_names if n.lower() == "draw"), None)
            if draw_name is None:
                return None, "draw market but sportsbook has no draw outcome"
            return TokenOutcomeMap({yes.token_id: (draw_name, False), no.token_id: (draw_name, True)},
                                   "Yes = draw"), "ok"
        hit = [n for n in teams if mentions(q, n, aliases)]
        if len(hit) > 1:
            # "Will A win against B?" -> the subject between "will" and "win/beat" decides.
            subj = re.search(r"\bwill\s+(.+?)\s+(?:win|beat)\b", q, re.I)
            hit = [n for n in hit if subj and mentions(subj.group(1), n, aliases)] if subj else []
        if len(hit) != 1:
            return None, f"could not identify which team the question refers to ({len(hit)} matches)"
        if not re.search(r"\bwin\b|\bbeat\b|\bvictory\b", q, re.I):
            return None, "question is not a plain 'win' question"
        return TokenOutcomeMap({yes.token_id: (hit[0], False), no.token_id: (hit[0], True)},
                               f"Yes = {hit[0]} wins"), "ok"
    return None, f"unrecognised outcome labels {labels}"


# Resolution-rule compatibility. Sportsbook soccer 1X2 (h2h) prices settle on
# regular time (90 minutes + stoppage); American-football moneylines include
# overtime. A Polymarket market whose rules text does not clearly state the same
# basis is rejected.
RESOLUTION_KEYWORDS = {
    "soccer": [r"90 minutes", r"regular time", r"regulation", r"stoppage time"],
    "americanfootball": [r"overtime", r"\bOT\b"],
    "basketball": [r"overtime", r"\bOT\b"],
    "icehockey": [r"overtime", r"shootout"],
    "baseball": [r"extra innings"],
}


def resolution_compatible(market: MarketInfo, sport_key: str) -> tuple[bool, str]:
    family = sport_key.split("_")[0]
    pats = RESOLUTION_KEYWORDS.get(family)
    if not pats:
        return False, f"no resolution-rule mapping for sport family '{family}'"
    text = market.description or ""
    if any(re.search(p, text, re.I) for p in pats):
        if family == "soccer" and re.search(r"extra time|penalt", text, re.I) and not re.search(
                r"(not|excluding|exclude|does not include)[^.]{0,60}(extra time|penalt)", text, re.I):
            return False, "rules mention extra time/penalties; sportsbook 1X2 settles on 90 minutes"
        return True, "rules text consistent with provider settlement basis"
    return False, "resolution rules text does not confirm the provider's settlement basis"
