# Setup guide (no programming experience needed)

This takes you from nothing to a **paper-trading** bot (simulated money, real
market data) running on your computer. Switching to real money is a separate
process: [LIVE_TRADING.md](LIVE_TRADING.md).

## 0. What you need

* A computer running macOS, Linux, or Windows with WSL2.
* **Python 3.11 or newer.** Check with `python3 --version`. If you don't have
  it, install it from python.org.
* An **odds data key** from https://the-odds-api.com. Without it the bot
  monitors markets but never trades, because it has no independent
  probabilities. There is a free tier for testing; running 24/7 needs a paid
  plan (see [DATA_REQUIREMENTS.md](DATA_REQUIREMENTS.md)).
* **Paper mode does NOT need your Polymarket keys.** Don't enter them until
  you decide to go live.

## 1. Download and install

Open a terminal (macOS: Terminal app; Windows: the Ubuntu/WSL app) and run
these lines one at a time:

```bash
git clone <repository-url> polybot
cd polybot
python3 -m venv .venv
source .venv/bin/activate        # you must run this line again in every new terminal
pip install -e ".[dev]"
```

## 2. Create your settings file

```bash
cp .env.example .env
```

`.env` holds **non-secret** settings: mode, limits and thresholds. Open it in
any text editor. Every line has an explanation above it. The defaults are
conservative examples. **They are not proven profitable and are not personal
financial advice.**

The settings you are most likely to change:

* `POLYBOT_PAPER_STARTING_BALANCE_USD`: the simulated money.
* `POLYBOT_BANKROLL_USD`: the most money the bot treats as its own.
* `POLYBOT_MAX_POSITION_USD` / `POLYBOT_MAX_POSITION_PCT`: the size of each bet.
* `POLYBOT_SPORTS`: which leagues to watch.

## 3. Enter secrets safely (never paste them into chat, email, or `.env`)

```bash
polybot setup-credentials --only POLYBOT_DASHBOARD_PASSWORD POLYBOT_ODDS_API_KEY
```

* You are asked for each value; **what you type is not shown**.
* The values are saved to `secrets/polybot.env`, which only your user account
  can read (permission 600).
* That folder is excluded from git, so it can never be committed.
* Pick a long, random dashboard password.
* Optional alerts: run `polybot setup-credentials --only
  POLYBOT_TELEGRAM_BOT_TOKEN POLYBOT_TELEGRAM_CHAT_ID`.
  * Create a Telegram bot by messaging **@BotFather**.
  * Send your new bot any message.
  * Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
    the `chat.id` number from the page.

To change a value later, run the same command again. Press Enter at a prompt to
keep its current value.

## 4. Check everything

```bash
polybot check-config
pytest              # optional: runs the automated tests (about 3 seconds)
```

`check-config` shows every setting (secrets only as "set" or "not set") and
lists anything that blocks trading.

## 5. Start it

```bash
polybot run
```

* Open **http://127.0.0.1:8080** and log in as `admin` with your dashboard
  password.
* The purple **PAPER (simulated)** badge means no real money is involved.
* Leave the terminal open. Ctrl+C stops the bot safely.

## 6. Reading the dashboard

| Tab | What to look for |
|---|---|
| Overview | Whether the bot *can* open positions right now, and why not. Feed and provider health. Account value. Risk locks |
| Opportunities | Every monitored outcome: estimated probability, bid/ask, fees, edge, and the exact reason for trade or skip |
| Positions | Shares, average cost, what you'd get by selling now, P&L after costs, why the bot entered, its exit plan |
| Orders | Every simulated (SIM) or real (REAL) order with its status |
| Performance | Paper and live results kept apart, by sport and by strategy |
| Controls | Pause / resume, kill switch, reset a risk lock, change a few settings (password required), export CSV |

**Expect lots of "skip".** That is normal. The bot trades only when every rule
passes.

## 7. Day-to-day

* `polybot status` gives a quick health check.
* `polybot pause` / `polybot resume` stop or allow new entries.
* `polybot kill` is the emergency stop. The dashboard's red button does the
  same thing.
* `polybot backup` saves a copy of the database.
* After a few weeks of paper trading, `polybot evidence-report` tells you
  honestly whether there is any evidence of an edge.

To keep it running when your laptop is closed, use a server:
[DEPLOYMENT.md](DEPLOYMENT.md).

## Troubleshooting

| Message | Meaning |
|---|---|
| "POLYBOT_ODDS_API_KEY not set" | No independent probabilities; nothing will be traded |
| "market discovery has not completed yet" / "feed stale" | No market data. Check your internet, or whether Polymarket is reachable from your location |
| "size … below exchange minimum" | Your position limit is too small for the exchange's 5-share minimum. Raise the bankroll or the position limit only if you're comfortable with that |
| "no sportsbook event matched" | Team names differ. Add aliases in a JSON file and set `POLYBOT_TEAM_ALIASES_PATH` |
| "resolution rules text does not confirm…" | The market's rules can't be confirmed to settle the same way as the sportsbook line, so it is skipped on purpose |
