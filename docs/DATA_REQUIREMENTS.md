# Data feeds, subscriptions, wallet steps and permissions still needed

## Needed for paper trading that can actually make decisions

| Item | Why | Cost / notes | Status |
|---|---|---|---|
| **Network access to Polymarket** public APIs (gamma-api, clob, ws-subscriptions-clob) | Markets and order books | Free, no key. Must be permitted where the bot runs | Not verifiable from the build sandbox (blocked); verify on your machine |
| **The Odds API key** (`POLYBOT_ODDS_API_KEY`) | The only independent probability source implemented | Free tier ≈ 500 credits/month. Each poll costs (#sports × #regions) credits. Defaults (3 sports × 2 regions every 10 min) use about 26k/month, so a **paid plan** is needed for 24/7 operation, or poll less often / fewer sports. Check current pricing at the-odds-api.com | **Missing**: without it the bot never trades |

## Needed for live trading (only after [LIVE_TRADING.md](LIVE_TRADING.md))

| Item | Notes |
|---|---|
| Confirmation that **you may legally use Polymarket's trading service where you live** | The US, UK, France, Germany and others are blocked; some countries are close-only |
| A Polymarket account with completed onboarding (deposit + approvals done on polymarket.com) | Ideally a dedicated wallet for the bot |
| Signer private key **or** a CLOB-scoped session key | Required to sign orders; the API key alone cannot trade |
| Wallet (funder) address | From your profile |
| API key / secret / passphrase | You have these; optional (derivable) |
| USDC collateral in the wallet | No more than you are prepared to lose |

## Optional

| Item | Purpose |
|---|---|
| Telegram bot token + chat id, or a webhook (ntfy, Slack) | Push alerts |
| External uptime monitor | To learn about crashes or server outages |
| A VPS | For 24/7 operation. **Not purchased**; your decision |
| PostgreSQL | Only if you outgrow SQLite |

## Data the project does NOT have (and does not fake)

* **Historical Polymarket order books** (bids, asks, depth over time). Without
  them no precise historical backtest is possible. The bot **records its own
  point-in-time snapshots** (`data/research/`) so evidence builds up from the
  day it starts.
* **Historical bookmaker odds.** The Odds API sells historical snapshots on
  paid plans. They could seed a backtest but would still lack the matching
  Polymarket order books.
* **Live scores / in-play data.** No reliable low-latency provider is
  configured, so in-play strategies are disabled
  (`POLYBOT_LIVE_GAME_STRATEGY_ENABLED=false`). Adding one requires a
  contracted feed (e.g. a licensed sports-data vendor) and a separate,
  independently tested strategy. Live data delays are a major risk.

## Adding another market category later

Implement the `ProbabilityProvider` interface (`polybot/providers/base.py`)
with an auditable, legally accessible data source. Add resolution-rule checks
for that category. Publish a `StrategyCard` and test the category separately.
Don't add a category because it is popular. Add one only when it has reliable
data and a testable approach.
