"""Conservative pre-match sports value strategy.

Buys an outcome token only when an independent, de-vigged sportsbook
consensus says it is worth meaningfully more than the *all-in* cost of buying
it at executable prices (walking the ask side, plus fees), after an
uncertainty buffer. It never sells short and never uses Polymarket's own
price as evidence of fair value.
"""

from __future__ import annotations

import time

from polybot.config import Settings
from polybot.domain import FairEstimate, MarketInfo, OrderBook
from polybot.marketdata import BookStore
from polybot.pricing import fee_per_share, kelly_fraction_binary, quote_buy, round_to_tick
from polybot.strategy.base import Candidate, StrategyCard

CARD = StrategyCard(
    name="sports_value",
    information_used=(
        "Pre-match moneyline/1X2 odds from multiple sportsbooks via The Odds API; Polymarket "
        "order books (executable bids/asks and depth), fee schedule, tick size, minimum size, "
        "scheduled start time and rules text."),
    fair_value_method=(
        "Per-bookmaker margin removal (power method by default), weighted consensus "
        "(sharp books weighted higher). Uncertainty = sqrt(cross-book std^2 + (method spread/2)^2)."),
    why_edge_might_exist=(
        "Sharp sportsbook lines aggregate a lot of informed money; a thinner prediction market "
        "may lag them or be moved by less-informed flow. This is a HYPOTHESIS, not a "
        "demonstrated edge."),
    assumptions=[
        "Sportsbook consensus is a better probability estimate than the Polymarket price.",
        "Polymarket and the sportsbooks settle on the same basis (checked via rules keywords).",
        "Team/event matching is correct (strict matching; ambiguous cases skipped).",
        "Odds are recent enough (age limits) and books are not stale.",
    ],
    disabled_when=[
        "No odds API key / provider disabled or erroring.",
        "Fewer than POLYBOT_MIN_BOOKMAKERS fresh bookmakers, or they disagree.",
        "Order book stale, crossed, too wide, or too thin.",
        "Game starts within POLYBOT_MIN_MINUTES_TO_START (no live data feed).",
        "Any risk lock, kill switch, pause, degraded feed, clock skew.",
    ],
    how_tested=(
        "Unit-tested mechanics only (math, matching, sizing, risk gates). NOT yet tested for "
        "profitability: no historical Polymarket order-book + odds dataset was available. The "
        "bot records snapshots so an out-of-sample evaluation can be run later."),
    expected_costs=(
        "Taker fees (market-specific schedule), crossing the spread, walking the book "
        "(slippage), and adverse selection when the market is right and the books are wrong."),
    key_risks=[
        "The edge may not exist; sportsbook lines can be wrong or shaded.",
        "Correlated losses within a single game or matchday.",
        "Rules mismatches (e.g. extra time) that keyword checks miss.",
        "Late team news moving prices before odds refresh.",
        "Thin liquidity preventing exits before resolution.",
    ],
)


class SportsValueStrategy:
    card = CARD
    name = CARD.name

    def __init__(self, settings: Settings):
        self.s = settings

    def required_edge(self, fair: FairEstimate) -> tuple[float, float]:
        buffer = max(self.s.min_uncertainty, self.s.uncertainty_z * fair.uncertainty)
        return self.s.min_edge + buffer, buffer

    def evaluate(
        self,
        market: MarketInfo,
        fairs: dict[str, FairEstimate] | None,
        fair_reason: str,
        books: BookStore,
        budget_for: dict[str, float],
        bankroll: float,
        now: float | None = None,
        budget_notes: dict[str, list[str]] | None = None,
    ) -> list[Candidate]:
        now = now or time.time()
        out: list[Candidate] = []
        for tok in market.tokens:
            fair = (fairs or {}).get(tok.token_id)
            book = books.get(tok.token_id)
            c = Candidate(self.name, market, tok, fair, book, approved=False, reasons=[])
            out.append(c)
            if book is not None:  # show prices for every monitored outcome, estimate or not
                c.numbers.update(best_bid=book.best_bid, best_ask=book.best_ask,
                                 spread=None if book.spread is None else round(book.spread, 4),
                                 ask_depth_shares=round(sum(l.size for l in book.asks), 2),
                                 book_age_s=round(book.age(now), 1))
            if not self.s.strategy_sports_value_enabled:
                c.reasons.append("strategy disabled by configuration")
                continue
            if fair is None:
                c.reasons.append(f"no independent probability estimate: {fair_reason}")
                continue
            c.numbers.update(fair_prob=round(fair.prob, 4), uncertainty=round(fair.uncertainty, 4),
                             fair_source=fair.source, n_sources=fair.n_sources,
                             fair_age_s=round(fair.age(now)), fair_details=fair.details)
            if fair.age(now) > self.s.max_fair_age_s:
                c.reasons.append(f"probability estimate stale ({fair.age(now):.0f}s > {self.s.max_fair_age_s:.0f}s)")
                continue
            ok, why = books.quality(tok.token_id, now)
            if not ok:
                c.reasons.append(why)
                continue
            assert book is not None
            self._evaluate_book(c, market, fair, book, budget_for.get(tok.token_id, 0.0), bankroll,
                                (budget_notes or {}).get(tok.token_id))
        return out

    def _evaluate_book(self, c: Candidate, market: MarketInfo, fair: FairEstimate, book: OrderBook,
                       budget: float, bankroll: float, budget_notes: list[str] | None = None) -> None:
        s = self.s
        fee = market.fee
        tick = book.tick_size or market.tick_size or 0.01
        min_size = max(book.min_order_size or 0, market.min_order_size or 0)
        if book.best_ask is None:
            c.reasons.append("no asks: nothing to buy")
            return
        if book.spread is None or book.spread > s.max_spread + 1e-9:
            c.reasons.append(f"spread {book.spread} > max {s.max_spread}")
            return
        if not (s.min_order_price <= book.best_ask <= s.max_order_price):
            c.reasons.append(f"ask {book.best_ask} outside allowed price band "
                             f"[{s.min_order_price}, {s.max_order_price}]")
            return
        required, buffer = self.required_edge(fair)
        c.numbers.update(required_edge=round(required, 4), uncertainty_buffer=round(buffer, 4),
                         min_edge=s.min_edge)
        # Highest price at which the trade could still clear the required edge.
        cap_by_edge = fair.prob - required - fee_per_share(book.best_ask, fee)
        limit = min(book.best_ask + s.max_slippage, s.max_order_price, cap_by_edge)
        limit = round_to_tick(limit, tick, "down")
        top_edge = fair.prob - (book.best_ask + fee_per_share(book.best_ask, fee))
        c.numbers.update(edge_at_best_ask=round(top_edge, 4),
                         fee_per_share_at_ask=round(fee_per_share(book.best_ask, fee), 5))
        if limit < book.best_ask - 1e-9:
            c.reasons.append(
                f"edge at best ask {top_edge:+.3f} < required {required:.3f} "
                f"(min edge {s.min_edge:.3f} + uncertainty buffer {buffer:.3f})")
            return
        if budget <= 0:
            c.reasons.append("risk engine allows no new exposure: " + "; ".join(budget_notes or ["limit reached"]))
            return
        # Size: fractional Kelly on a *conservative* probability, capped by risk budget.
        q0 = quote_buy(book.asks, budget, limit, fee, s.max_depth_fraction)
        if q0 is None:
            c.reasons.append("no executable depth within limit price")
            return
        p_cons = max(0.0, fair.prob - buffer)
        k = kelly_fraction_binary(p_cons, q0.all_in_per_share)
        stake = s.kelly_fraction * k * bankroll
        final_budget = min(budget, stake)
        c.numbers.update(kelly_full=round(k, 4), kelly_stake=round(stake, 2), risk_budget=round(budget, 2))
        # Size so that even a complete fill AT THE LIMIT PRICE (worst case) stays within budget.
        worst_per_share = limit + fee_per_share(limit, fee)
        max_shares = int(final_budget / worst_per_share * 100) / 100
        q = quote_buy(book.asks, final_budget, limit, fee, s.max_depth_fraction, max_shares=max_shares)
        if q is None or q.shares < min_size:
            c.reasons.append(
                f"size {0 if q is None else q.shares:.2f} sh below exchange minimum {min_size} "
                f"(budget ${final_budget:.2f})")
            return
        edge = fair.prob - q.all_in_per_share
        c.numbers.update(
            limit_price=limit, shares=round(q.shares, 2), avg_price=round(q.avg_price, 4),
            fees=round(q.fees, 4), slippage=round(q.slippage, 4), total_cost=round(q.total_cost, 4),
            all_in_per_share=round(q.all_in_per_share, 4), edge=round(edge, 4),
            ev_per_share=round(edge, 4), ev_total=round(edge * q.shares, 4),
            ev_per_dollar=round(edge / q.all_in_per_share, 4),
        )
        if q.slippage > s.max_slippage + 1e-9:
            c.reasons.append(f"slippage {q.slippage:.3f} > max {s.max_slippage}")
            return
        if edge < required - 1e-9:
            c.reasons.append(f"edge after costs {edge:+.3f} < required {required:.3f}")
            return
        c.approved = True
        c.shares = q.shares
        c.limit_price = limit
        c.reasons.append(
            f"edge {edge:+.3f}/share >= required {required:.3f} "
            f"(fair {fair.prob:.3f} vs all-in {q.all_in_per_share:.3f}); "
            f"{q.shares:.2f} sh for ${q.total_cost:.2f}")
