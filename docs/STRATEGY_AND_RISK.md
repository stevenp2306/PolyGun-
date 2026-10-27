# Strategy and risk, in plain English

## The idea

Sportsbooks such as Pinnacle and the betting exchanges take very large
amounts of money from informed bettors, and their prices move quickly when
news arrives. If a Polymarket price for the same game sits clearly below what
those books imply, *after* every cost, buying it may have positive expected
value.

That is a **hypothesis**. It has not been demonstrated (see
`EVIDENCE_REPORT.md`). The bot is built to test it cheaply and honestly, and to
sit out whenever the evidence for a specific trade is weak.

## Step by step

1. **Find markets.** Active sports *moneyline* markets with an order book, a
   start time, rules text, a known fee schedule, and enough liquidity. Markets
   that start within 30 minutes are skipped: there is no reliable live-score
   feed, so the bot does not trade in-play.
2. **Get an independent probability.** The bot pulls pre-match odds from
   several bookmakers (The Odds API) and removes each bookmaker's margin.
   *Example:* decimal odds 2.00 / 3.60 / 4.00 imply 50% / 27.8% / 25%. That
   adds up to 102.8%; the extra 2.8% is the margin. The **power** method shrinks
   the longshots a little more than the favourite until the total is 100%.
   * It then averages across bookmakers (sharp books count 3×).
   * **Uncertainty** = how much the bookmakers disagree, combined with how much
     the three margin-removal methods disagree.
   * It requires ≥ 3 fresh bookmakers that agree within 4 points.
   * It requires the Polymarket rules text to confirm the same settlement basis:
     90 minutes for soccer, overtime included for NFL.
   * It requires an unambiguous team and start-time match (within 30 min).

   If any of this fails, there is **no estimate** and therefore **no trade**.
   Polymarket's own price is never used as evidence that Polymarket is
   mispriced.
3. **Price the trade at executable prices.** The bot walks the *ask* side of
   the book. It assumes it can take at most 50% of the size shown at each level,
   and adds the taker fee at every level it fills:
   `fee per share = rate × (p × (1 − p))^exponent`. For a sports market with
   rate 0.05 at p = 0.50 that is 1.25 cents per share.
4. **Require a real margin.**
   `edge = estimated probability − all-in cost per share`, and it trades only
   if `edge ≥ min_edge (3 pts) + max(1 pt, 1 × uncertainty)`.

## Dashboard example

| | |
|---|---|
| Market | Team A wins |
| Estimated probability | 60.0% ± 1.2% (5 bookmakers, power de-vig) |
| Executable buy price | 50.0¢ best ask; average fill 50.3¢ for 9.6 shares |
| Fee | 1.25¢/share · slippage 0.3¢/share · uncertainty buffer 1.2¢ |
| All-in cost | 51.55¢/share |
| Edge after costs | 60.0 − 51.55 = **8.45 pts** vs required 3 + 1.2 = 4.2 pts |
| EV after costs | 8.45¢ × 9.6 sh = **$0.81** (16% per $ risked), **if** the estimate is right |
| Decision | TRADE |
| Reason | "edge +0.085/share ≥ required 0.042 (fair 0.600 vs all-in 0.516); 9.60 sh for $4.95" |

A rejection shows the exact rule that failed, for example "spread 0.06 > max
0.04", "only 2 fresh bookmakers (< 3)" or "game starts in 12 min".

## Position size

The bot takes the smallest of these:

1. A fractional Kelly stake: 10% of Kelly, computed on the *conservative*
   probability (estimate − buffer).
2. `min($5, 1% of bankroll)`.
3. What is left under the per-event (3%), correlated (6%, same league and same
   day) and total (20%) exposure caps.
4. Available cash minus the reserve.
5. Half of the visible depth up to the limit price.

The order size is chosen so that **even a complete fill at the limit price**
fits the budget. It never adds to an existing position, never doubles down,
and never holds both sides of a market. There is no leverage or borrowing.

> With small bankrolls, Polymarket's minimum order (usually 5 shares) can be
> larger than 1% of the bankroll. The bot then skips everything, which is the
> correct, safe outcome.

## How the loss limits are calculated

* **Equity** = cash + what all positions would fetch if sold **now** into the
  visible bids, after fees. Shares the bids can't absorb count as **$0**. This
  is deliberately pessimistic.
* **Open buy orders** count as exposure, because they could still fill.
* **Deposits and withdrawals** are recorded as cash flows (`polybot record-flow`)
  and removed: `trading P&L = equity − net deposits`.
* **Daily loss** = trading P&L now − trading P&L at 00:00 UTC. It includes
  realized P&L, conservative unrealized P&L and all fees. A lock triggers at
  −3% × bankroll.
* **Drawdown** = (peak trading P&L − now) / (net deposits + peak). A lock
  triggers at 10%.
* **When a lock triggers**, the bot:
  * stops opening positions immediately,
  * cancels its own entry orders,
  * sends an alert,
  * stays locked across days and restarts until you run
    `polybot reset-risk-lock` (or the dashboard equivalent, typing RESET).
    After a reset, entries stay *paused* until you press Resume.
* **While locked**, the bot keeps monitoring and may still *exit* positions by
  its normal rules (risk-reducing). It never dumps positions at any price.

**Actual losses can exceed these limits.** Prices can gap between loops. Books
can be empty when you need to sell. Outages, settlement failures or a market
resolving against you can all cost more than the limit. In the worst case you
lose everything the open positions cost.

## Exits

The profit basis is **net exit value vs. cost basis**:

```
net_exit_value = proceeds of selling ALL shares into current bids (walking depth) − taker fees
cost_basis     = everything paid for the shares, including entry fees
pnl_pct        = (net_exit_value − cost_basis) / cost_basis
```

"Take profit at 6%" means selling everything right now would return 6% more
than the total invested. It is not 6% on the token price.

| Rule | Default | Note |
|---|---|---|
| Take profit | 6% | Experiment with 5–8%; unvalidated |
| Edge gone | on | Exit when a fresh estimate ≤ what selling pays now (and in profit), or the estimate falls well below it |
| Trailing | off | Activates at +4%, exits after a 3-pt giveback |
| Stop-loss | off | Only a trigger: **no guaranteed price; may not fill** |
| Max holding | off | |
| Pre-start | hold | `exit_if_profitable` or `exit` are available |
| Hold to resolution | on | Only if no rule fires; a stale estimate is **not** a reason to hold |

Exits are FAK sells with a hard minimum price (never below 2¢). With no bids,
the position is reported as at risk and not sold.

Test each exit rule **separately and out of sample**. Do not pick a rule
because it did best on the data used to invent it. The evidence report runs
"hold to resolution" and "configured exits" as separate variants for this
reason.

## Why it could lose money

1. **The edge may not exist.** Polymarket prices may already be as good as the
   sportsbooks, or better.
2. **Adverse selection.** When the bot's estimate and the market disagree, the
   market is sometimes the one that is right (late team news, injuries).
3. **Costs.** Fees and crossing the spread eat small edges.
4. **Correlation.** Several positions on one matchday can all lose together.
5. **Model or matching errors.** For example, a rules difference the keyword
   check misses.
6. **Liquidity.** It may be impossible to exit before resolution.
7. **Operations.** Outages, stale data, bugs, or a settlement that fails.

## Momentum and LLMs

* No momentum strategy is implemented. The `POLYBOT_MOMENTUM_ENABLED` flag
  exists only to keep any future work behind a separate, independently
  evaluated switch.
* No language model produces probabilities. Every number comes from auditable
  data and fixed rules.
