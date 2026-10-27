# polybot: an evidence-first Polymarket bot

polybot watches Polymarket sports markets around the clock. It trades only
when an **independent** probability estimate beats the **executable** price
after fees, slippage, depth and an uncertainty buffer, and only when every
risk limit passes. It is built to sit out: in normal conditions most loops end
with "skip", and the dashboard shows the exact rule behind each decision.

> **Honest status (2026-10-27)**
> * **There is no evidence yet that this strategy is profitable.** The evidence
>   report says *"not enough evidence to claim an edge"* because no historical
>   dataset was available. See [docs/EVIDENCE_REPORT.md](docs/EVIDENCE_REPORT.md).
> * **PAPER mode is the default.** Live trading is disabled until you complete
>   a separate checklist ([docs/LIVE_TRADING.md](docs/LIVE_TRADING.md)).
> * The bot was not run against live Polymarket endpoints during development:
>   the build sandbox blocked them. The integration targets the official SDK
>   (`polymarket-client` 0.14.0) and is tested against mocked responses.
> * Without an odds-data key (`POLYBOT_ODDS_API_KEY`) the strategy has no
>   independent probabilities, so it monitors markets but never trades. It
>   does not invent numbers.

## What it does

| Area | What is implemented |
|---|---|
| Market discovery | Active sports moneyline markets via the official SDK; event / market / token IDs, rules text, start time, tick size, minimum size, fee schedule, liquidity. Stores the reasons each market was accepted or rejected |
| Market data | WebSocket order books plus REST snapshots as a fallback, staleness / crossed-book / dropped-message detection, clock-skew check |
| Fair value | Sportsbook consensus from The Odds API: per-bookmaker margin removal (power / Shin / multiplicative), sharp-weighted, with an uncertainty estimate. Strict team / event / rules matching |
| Strategy | `sports_value`: buy only if `fair − all-in cost ≥ min_edge + uncertainty buffer`. Every decision is logged with its numbers |
| Risk | Position, event, correlated and total exposure caps; cash reserve; daily-loss and drawdown locks (manual reset); kill switch; circuit breaker; data-quality gate |
| Exits | Take profit (net of fees, on cost basis), edge lost or reversed, optional trailing stop, stop-loss trigger, max hold, pre-start policy, hold to resolution |
| Paper trading | Real books, latency, depth haircut, partial fills, no fill just because a price is touched, cancels, expiry, settlement |
| Live adapter | Official SDK, gated by a checklist. Intent is persisted before posting, ambiguous results are reconciled (never blindly retried), and fills are booked from exchange trades only |
| Dashboard | Overview, positions, orders, opportunities, performance, controls (pause / resume / kill / reset), protected configuration changes, CSV export |
| Alerts | Log + dashboard always; Telegram and/or webhook optional; secrets redacted |
| Evidence | Point-in-time recorder, chronological split, walk-forward, event-clustered bootstrap, calibration against the market, fixed verdict rules |

## Start paper mode (local, about 10 minutes)

Requires Python 3.11+ (3.12/3.13 tested).

```bash
git clone <this repo> polybot && cd polybot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                 # non-secret settings; PAPER by default
polybot setup-credentials --only POLYBOT_DASHBOARD_PASSWORD POLYBOT_ODDS_API_KEY
polybot check-config                 # shows settings and anything missing
pytest                               # optional: run the 114 tests
polybot run                          # bot + dashboard
```

Then open http://127.0.0.1:8080 and log in as `admin` with the password you
set. Stop the bot with Ctrl+C: it cancels its own open entry orders and keeps
positions.

With Docker (24/7): see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

```bash
cp .env.example .env && mkdir -p data logs secrets && sudo chown -R 10001:10001 data logs
docker compose up -d --build && docker compose logs -f
```

## Documentation

| Document | Contents |
|---|---|
| [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md) | Step-by-step setup for non-programmers, including where to enter credentials |
| [.env.example](.env.example) | Every setting, explained |
| [docs/STRATEGY_AND_RISK.md](docs/STRATEGY_AND_RISK.md) | Plain-English strategy, cost maths, loss limits, exits |
| [docs/LIVE_TRADING.md](docs/LIVE_TRADING.md) | **Separate, explicit process** to switch to real money |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Always-on server, Docker, backups, monitoring, security |
| [docs/DATA_REQUIREMENTS.md](docs/DATA_REQUIREMENTS.md) | Feeds, subscriptions, wallet steps and permissions still needed |
| [docs/SECURITY.md](docs/SECURITY.md) | What your keys can do, rotation, separate wallet |
| [docs/POLYMARKET_INTEGRATION.md](docs/POLYMARKET_INTEGRATION.md) | API/SDK findings, **sources and the date checked** |
| [docs/EVIDENCE_REPORT.md](docs/EVIDENCE_REPORT.md) | What has and has not been tested for edge |
| [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md) | Automated test results |

## CLI

```
polybot run                 # engine + dashboard
polybot check-config        # settings (secrets as set/not set) + blocking issues
polybot status              # heartbeat, kill switch, locks, open orders
polybot pause | resume      # new entries (resume refused while a lock or kill is active)
polybot kill                # kill switch: no new orders; cancel the bot's own orders
polybot clear-kill | reset-risk-lock | clear-halt   # deliberate, typed confirmation
polybot record-flow deposit 100     # deposits/withdrawals are not P&L
polybot backup              # consistent SQLite backup
polybot evidence-report     # backtest recorded data -> reports/evidence-*.md
polybot setup-credentials   # hidden-input secrets file (chmod 600)
polybot live-checklist      # interactive live setup (never places an order)
polybot test-kill-switch    # offline kill-switch self-test
```

## Project layout

```
polybot/
  config.py          settings (env / .env / secrets file), risk-config hash
  discovery.py       market discovery + eligibility (official SDK)
  marketdata.py      order books, WebSocket feed, REST fallback, feed health, clock
  providers/         odds_api.py (The Odds API), devig.py, matching.py, base.py
  strategy/          base.py (StrategyCard), sports_value.py
  pricing.py         fees, book walking, EV, Kelly, liquidation value
  risk.py            limits, locks, kill switch, circuit breaker
  exits.py           profit taking / exit rules
  portfolio.py       accounting shared by paper and live
  brokers/           paper.py, live.py (official SDK), live_gate.py (checklist)
  engine.py          24/7 orchestration and reconciliation
  dashboard/         FastAPI app + static UI
  alerts.py          log / dashboard / Telegram / webhook
  reporting.py       performance, CSV export
  backtest/          recorder, point-in-time backtest, stats, evidence report
  db.py              SQLAlchemy Core schema (SQLite -> PostgreSQL)
  cli.py
tests/               114 tests, mocked APIs only
```

No profit is promised or implied. Prediction-market trading can lose all the
money you allocate to it.
