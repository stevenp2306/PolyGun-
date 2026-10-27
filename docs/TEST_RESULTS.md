# Automated test results

Run on 2026-10-27, Python 3.13.17, `polymarket-client` 0.14.0:

```
$ pytest
114 passed in ~3s
```

All tests use mocked data or fake clients. **No test touches a real exchange
or real funds.** Reproduce with `pip install -e ".[dev]" && pytest -v`.

| File | Tests | Covers (requirement) |
|---|---|---|
| `test_pricing.py` | 8 | Fee formula, maker/taker, book walking with limit/depth/budget, slippage, EV, Kelly, liquidation value, tick rounding |
| `test_devig_matching.py` | 11 | Margin removal (3 methods), longshot shrinkage, strict team/event matching, token mapping, resolution-rule compatibility |
| `test_odds_provider.py` | 6 | Disabled without key (no fake data), consensus, too few / stale books, disagreement, rules mismatch, **API key never in errors or logs** |
| `test_strategy.py` | 9 | Executable price vs midpoint, no trade without an independent estimate, stale data, spread/price band, uncertainty buffer, minimum size, depth limits |
| `test_risk.py` | 12 | Position / event / correlated / total caps, no doubling down, open orders as exposure, cash reserve, max positions, **daily-loss lock persists until deliberate reset**, drawdown lock, deposits ≠ profit, **kill switch blocks exits too**, locks still allow risk-reducing exits, circuit breaker, degraded-data gate |
| `test_paper_and_exits.py` | 13 | Paper fills with fees + haircut, partial fill/kill, FOK, latency, passive orders need a trade-through, cancel, expiry, tick/min-size rejects, round-trip P&L + settlement; take-profit basis, edge-gone, stale-estimate rule, trailing, stop-loss (off by default), max hold, pre-start, no-bid hold, exit floor |
| `test_live.py` | 18 | **Live broker cannot exist in PAPER mode**, **incomplete checklist blocks with no network call**, risk-config change invalidates checklist, geoblock/flag gating, create+post (never `place_*`), **ambiguous timeout → UNKNOWN, never resubmitted**, reconciliation adopts a unique match / halts on ambiguity / fails after timeout, rate-limited post not retried, rejections, pre-trade balance check, read retries with backoff, settled-only booking + failed-settlement alert, own-orders-only cancel with confirmation status, position/cash mismatch detection, unredeemed winnings, heartbeat ID recovery |
| `test_engine.py` | 10 | End-to-end paper loop, **restart without duplicate orders**, stale data, no estimate → no trade, kill switch cancels and blocks, risk lock cancels entries and alerts, settlement, pre-start cancels, PAPER never builds live broker, take-profit flow |
| `test_discovery_marketdata.py` | 6 | Parsing the official SDK `Market` model, eligibility reasons, stored selection reasons, stale/crossed/suspect books, WebSocket deltas and out-of-order messages, feed health |
| `test_security_dashboard.py` | 12 | **Secret redaction** (registered values, key patterns, tracebacks), settings never expose secrets, placeholders ignored, `.env.example` contains no secrets, alerts redacted; dashboard: disabled without password, auth, all pages render, CSRF header + typed confirmations, protected config changes, health endpoint |
| `test_backtest_cli.py` | 9 | Stats helpers, recorder round-trip with outcomes stored separately, **decisions independent of outcomes (no look-ahead)**, chronological split, verdict B with no / little data, CLI kill/pause/status/self-test, credentials file mode 600 + seed-phrase refusal, check-config |

## Manual checks performed

* `polybot run` smoke test in the sandbox: Polymarket hosts were blocked.
  * The engine stayed up in `degraded` state, raised alerts, and refused
    entries.
  * `/healthz` and the dashboard API worked.
  * SIGTERM produced a clean shutdown.
* Dashboard driven in headless Chromium with a demo engine on fake data:
  * all tabs rendered,
  * no JavaScript errors.
* `docker compose config` validated the Compose file.

## Not verified here

* **Docker image build:** no Docker daemon in the sandbox.
* **Live Polymarket connectivity:** network blocked.
* **Real-account live trading:** intentionally not attempted.
