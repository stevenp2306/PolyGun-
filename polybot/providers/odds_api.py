"""Sportsbook consensus probabilities from The Odds API (https://the-odds-api.com).

Why this source: it is a commercial, documented API that redistributes
bookmaker prices under its own terms (no scraping). It has a small free tier
and paid tiers; continuous polling of several leagues needs a paid plan (see
docs/DATA_REQUIREMENTS.md). Without ``POLYBOT_ODDS_API_KEY`` the provider is
disabled and the sports strategy cannot trade.

Method:
1. For each bookmaker with a fresh ``h2h`` (moneyline / 1X2) market, remove the
   margin with the configured de-vig method.
2. Consensus probability = weighted mean across bookmakers (sharp books such
   as Pinnacle / exchanges weighted higher).
3. Uncertainty = sqrt(cross-book std^2 + de-vig-method spread^2). We do NOT
   divide by sqrt(n): bookmakers copy each other, so their errors are highly
   correlated and averaging does not shrink error the way independent samples
   would.

Limitations: odds are pre-match and refreshed every few minutes, not
real-time; bookmaker settlement rules can differ from Polymarket's; line moves
from team news reach sportsbooks and Polymarket at different times.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from typing import Any

import httpx

from polybot.config import Settings
from polybot.domain import FairEstimate, MarketInfo
from polybot.logging_setup import RedactingFilter
from polybot.providers.base import ProviderStatus
from polybot.providers.devig import devig
from polybot.providers.matching import (
    OddsEvent,
    load_aliases,
    map_tokens,
    match_event,
    resolution_compatible,
)

log = logging.getLogger(__name__)

# The Odds API authenticates with an ``apiKey`` query parameter, and httpx logs full request
# URLs at INFO level. Keep httpx quiet and redact anything it does log, regardless of how the
# application configured logging.
_httpx_log = logging.getLogger("httpx")
_httpx_log.setLevel(max(_httpx_log.level, logging.WARNING))
if not any(isinstance(f, RedactingFilter) for f in _httpx_log.filters):
    _httpx_log.addFilter(RedactingFilter())


def _iso(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


class OddsApiProvider:
    name = "the-odds-api"

    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.s = settings
        self.http = http
        self.events: dict[str, list[OddsEvent]] = {}
        self._status = ProviderStatus(name=self.name, enabled=settings.odds_api_key is not None)
        if not self._status.enabled:
            self._status.disabled_reason = (
                "POLYBOT_ODDS_API_KEY not set: no independent probabilities, sports strategy "
                "will not trade (no fake data is used)")
        self.aliases = load_aliases(settings.team_aliases_path)
        self._next_allowed: float = 0.0

    def status(self) -> ProviderStatus:
        self._status.events_cached = sum(len(v) for v in self.events.values())
        return self._status

    # ---------------- fetching ----------------
    def refresh(self, now: float | None = None) -> None:
        now = now or time.time()
        if not self._status.enabled or now < self._next_allowed:
            return
        assert self.s.odds_api_key is not None
        client = self.http or httpx.Client(timeout=20)
        try:
            for sport in self.s.sports_list:
                r = client.get(
                    f"{self.s.odds_api_base_url}/sports/{sport}/odds",
                    params={"apiKey": self.s.odds_api_key.get_secret_value(), "regions": self.s.odds_regions,
                            "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"},
                )
                if r.status_code == 429:
                    self._next_allowed = now + 300
                    self._status.last_error = "rate limited by odds provider (429); backing off 5 min"
                    return
                if r.status_code in (401, 403):
                    self._status.last_error = f"odds provider rejected credentials ({r.status_code})"
                    self._status.enabled = False
                    self._status.disabled_reason = self._status.last_error
                    return
                r.raise_for_status()
                rem = r.headers.get("x-requests-remaining")
                if rem is not None:
                    try:
                        self._status.quota_remaining = float(rem)
                    except ValueError:
                        pass
                self.events[sport] = self._parse(sport, r.json(), now)
            self._status.last_refresh = now
            self._status.last_error = None
        except httpx.HTTPError as e:
            # Never include the request URL (it carries the API key).
            self._status.last_error = f"odds fetch failed: {type(e).__name__}"
            log.warning(self._status.last_error)
        finally:
            if self.http is None:
                client.close()

    def _parse(self, sport: str, data: list[dict[str, Any]], now: float) -> list[OddsEvent]:
        out = []
        for e in data:
            try:
                out.append(OddsEvent(event_id=e["id"], sport_key=sport,
                                     commence_time=_iso(e["commence_time"]),
                                     home_team=e["home_team"], away_team=e["away_team"],
                                     raw={"bookmakers": e.get("bookmakers", []), "fetched_at": now}))
            except (KeyError, ValueError):
                continue
        return out

    # ---------------- estimation ----------------
    def consensus(self, ev: OddsEvent, now: float) -> tuple[dict[str, dict] | None, str]:
        """Per-outcome consensus {name: {prob, std, n, method_spread, as_of}}."""
        per_book: list[tuple[str, float, dict[str, float], dict[str, list[float]], float]] = []
        names: set[str] | None = None
        for bm in ev.raw.get("bookmakers", []):
            mk = next((m for m in bm.get("markets", []) if m.get("key") == "h2h"), None)
            if not mk:
                continue
            upd = _iso(mk.get("last_update") or bm.get("last_update"))
            if now - upd > self.s.odds_max_bookmaker_update_age_s:
                continue
            outs = mk.get("outcomes", [])
            try:
                odds = [float(o["price"]) for o in outs]
                labels = [o["name"] for o in outs]
            except (KeyError, ValueError, TypeError):
                continue
            if len(odds) < 2 or any(o <= 1.0 for o in odds):
                continue
            if names is None:
                names = set(labels)
            elif set(labels) != names:
                continue  # inconsistent outcome sets (e.g. 2-way vs 3-way)
            probs = dict(zip(labels, devig(odds, self.s.devig_method.value)))
            alt = {n: [] for n in labels}
            for m in ("multiplicative", "power", "shin"):
                for n, p in zip(labels, devig(odds, m)):
                    alt[n].append(p)
            w = self.s.sharp_weight if bm.get("key", "").lower() in self.s.sharp_bookmaker_set else 1.0
            per_book.append((bm.get("key", "?"), w, probs, alt, upd))
        if not per_book or names is None:
            return None, "no fresh bookmaker prices"
        if len(per_book) < self.s.min_bookmakers:
            return None, f"only {len(per_book)} fresh bookmakers (< {self.s.min_bookmakers})"
        out: dict[str, dict] = {}
        wsum = sum(b[1] for b in per_book)
        for n in names:
            mean = sum(b[1] * b[2][n] for b in per_book) / wsum
            var = sum(b[1] * (b[2][n] - mean) ** 2 for b in per_book) / wsum
            spread = max(max(b[3][n]) - min(b[3][n]) for b in per_book)
            out[n] = {"prob": mean, "std": math.sqrt(var), "n": len(per_book),
                      "method_spread": spread, "as_of": min(b[4] for b in per_book),
                      "books": sorted(b[0] for b in per_book)}
        return out, "ok"

    def estimate(self, market: MarketInfo, now: float | None = None) -> tuple[dict[str, FairEstimate] | None, str]:
        now = now or time.time()
        if not self._status.enabled:
            return None, self._status.disabled_reason or "provider disabled"
        all_events = [e for evs in self.events.values() for e in evs]
        if not all_events:
            return None, "no sportsbook data loaded yet"
        ev, why = match_event(market, all_events, self.s.match_start_tolerance_min * 60, self.aliases)
        if ev is None:
            return None, why
        if self.s.require_resolution_keywords:
            ok, why = resolution_compatible(market, ev.sport_key)
            if not ok:
                return None, why
        cons, why = self.consensus(ev, now)
        if cons is None:
            return None, why
        tm, why = map_tokens(market, ev, list(cons.keys()), self.aliases)
        if tm is None:
            return None, why
        out: dict[str, FairEstimate] = {}
        for token_id, (name, negate) in tm.mapping.items():
            c = cons[name]
            if c["std"] > self.s.max_bookmaker_disagreement:
                return None, f"bookmakers disagree on {name} (std {c['std']:.3f})"
            p = 1 - c["prob"] if negate else c["prob"]
            unc = math.sqrt(c["std"] ** 2 + (c["method_spread"] / 2) ** 2)
            out[token_id] = FairEstimate(
                token_id=token_id, prob=p, uncertainty=unc, source=self.name, n_sources=c["n"],
                data_as_of=c["as_of"], fetched_at=ev.raw.get("fetched_at", now),
                details={"sportsbook_event": f"{ev.home_team} vs {ev.away_team}",
                         "sport": ev.sport_key, "outcome": name, "complement": negate,
                         "devig": self.s.devig_method.value, "books": c["books"],
                         "cross_book_std": round(c["std"], 4),
                         "method_spread": round(c["method_spread"], 4),
                         "mapping": tm.explanation},
            )
        return out, "ok"
