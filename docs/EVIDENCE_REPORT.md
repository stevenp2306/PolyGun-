# Evidence report

**Verdict: B. There is NOT enough evidence to claim an edge.**

Generated 2026-10-27 by `polybot evidence-report`. The full machine output is
reproduced below. Re-run it after weeks of paper trading; the verdict rules
are fixed in `polybot/backtest/report.py` and were not tuned to produce a
particular answer.

## What has been tested, and what has not

| Question | Tested? | How / why not |
|---|---|---|
| Is the arithmetic right (fees, book walking, EV, Kelly, P&L, settlement)? | **Yes** | Unit tests with hand-computed values |
| Do risk limits, locks, kill switch and gates behave as specified? | **Yes** | Unit and end-to-end tests (mocked data) |
| Does paper trading fill conservatively (latency, depth haircut, no fill on a touch)? | **Yes** | Tests |
| Does the live adapter avoid duplicates and reconcile ambiguous orders? | **Yes, against a fake SDK client** | Not run against the real exchange |
| Does the bot parse real Polymarket market objects? | **Partly** | Tested with the official SDK's own `Market` model on a realistic payload; live endpoints were unreachable from the build sandbox |
| Does the backtest avoid look-ahead? | **Yes** | Test: flipping all outcomes leaves every entry decision unchanged |
| **Is the sports-value strategy profitable after costs?** | **NO** | No historical Polymarket order books + matching bookmaker odds were available. No result exists, and none was fabricated |
| Is the sportsbook consensus better calibrated than Polymarket's own price? | **NO** | Same reason; the report computes this once recorded data exists |
| Do the take-profit / trailing / exit thresholds help? | **NO** | They are unvalidated starting assumptions; the report evaluates "hold to resolution" and "configured exits" separately |
| Live execution quality (slippage, fill rates, settlement) | **NO** | Requires live trading, which is disabled |

## How evidence will accumulate

* While running (paper or live), the bot writes **point-in-time snapshots** to
  `data/research/`: each estimate together with the order book at that moment.
* Resolutions are written to a separate file, so outcomes cannot leak into
  decisions.
* `polybot evidence-report` then:
  * splits the events chronologically into development / validation /
    untouched test blocks,
  * runs walk-forward folds,
  * computes **event-clustered** bootstrap confidence intervals (trades on the
    same game are correlated),
  * compares calibration with the Polymarket mid,
  * benchmarks against not trading.
* Verdict **A** requires **all** of:
  * ≥ 100 events traded in the test block,
  * a 95% CI for mean P&L per event entirely above zero,
  * a positive validation block,
  * a model better calibrated than the market.

Even then the evidence would be *preliminary*. Paper results do not reproduce
queue position, market impact, missed fills or settlement problems, and past
or simulated performance does not guarantee future profit.

---

## Generated report (2026-10-27)


Generated: 2026-10-27 19:05 UTC  
Settings hash: `6f9601b7376e58ec`

### Verdict

**B. There is NOT enough evidence to claim an edge.**

- only 0 events traded in the untouched test block (need >= 100)
- no confidence interval (too few events)
- validation block mean P&L per event is not positive
- model is not shown to be better calibrated than the Polymarket mid

### Data used

- Snapshots: 0
- Distinct events: 0
- Tokens with recorded resolution: 0

Data source: point-in-time snapshots recorded by this bot while running (fair estimate + order book at that moment). No third-party historical order-book dataset was used; none was available when this project was built.

### Results (chronological blocks; thresholds NOT tuned on any block)

### hold_to_resolution

| block | events traded | trades | net P&L | win rate | profit factor | fees | max DD | 95% CI mean P&L/event |
|---|---|---|---|---|---|---|---|---|
| development | 0 | 0 | 0 | None | None | 0 | 0.0 | None |
| validation | 0 | 0 | 0 | None | None | 0 | 0.0 | None |
| test | 0 | 0 | 0 | None | None | 0 | 0.0 | None |

### configured_exits

| block | events traded | trades | net P&L | win rate | profit factor | fees | max DD | 95% CI mean P&L/event |
|---|---|---|---|---|---|---|---|---|
| development | 0 | 0 | 0 | None | None | 0 | 0.0 | None |
| validation | 0 | 0 | 0 | None | None | 0 | 0.0 | None |
| test | 0 | 0 | 0 | None | None | 0 | 0.0 | None |

Benchmark: not trading = $0.00 P&L with zero risk.

### Walk-forward (hold to resolution)

- Not enough events for walk-forward folds (need >= 50).

### Calibration: independent model vs Polymarket mid (first pre-match snapshot per token)

- Model: {}
- Market mid: {}

### Limitations

- Snapshots are taken at most every few minutes; fills between snapshots are not modelled.
- Latency, queue position, market impact and settlement failures are not reproduced.
- Fees use the market's published schedule at snapshot time.
- Trades within one game are correlated; uncertainty is computed per event.
- Good past or simulated performance does not guarantee future profit.
