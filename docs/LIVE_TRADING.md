# ⚠️ Switching to LIVE trading (real money): separate, explicit process

**Do not start this until:**

* you have paper-traded for long enough to judge the bot's behaviour, and
* the evidence report has been reviewed.

At the time of writing there is **no evidence of a profitable edge**. You can
lose all the money you give the bot.

Nothing below places an order. The first real order can only happen after
**all** of the following are true:

1. `POLYBOT_MODE=live`
2. `POLYBOT_LIVE_TRADING_ENABLED=true`
3. Every checklist item is confirmed, *against the current risk settings*.
4. You typed `ENABLE LIVE TRADING` at the end of the checklist.
5. The geoblock check is recent (under 3 hours) and says you are not blocked,
   and the account is not in close-only mode.

Changing **any** risk setting afterwards (including from the dashboard)
automatically blocks live orders until you redo the checklist.

## Step 1: Eligibility (stop here if this fails)

* Polymarket's international exchange **blocks some countries entirely**. The
  docs list about 33, including the **United States, United Kingdom, France
  and Germany**. Some other countries are close-only.
* Check https://docs.polymarket.com (geoblocking pages) and the Terms of
  Service for your country.
* The checklist also calls the official `https://polymarket.com/api/geoblock`
  endpoint from the machine the bot runs on. This matters: a server in another
  country is not a way around the restriction. **Never use a VPN or misstate
  your location.**
* If you are blocked or close-only, keep using paper mode only.

## Step 2: A dedicated trading wallet (recommended)

* Create a **separate Polymarket account and wallet** used only by the bot.
  Fund it with no more than `POLYBOT_BANKROLL_USD`.
* Complete onboarding on polymarket.com: deposit, and the one-time trading
  approvals that the website asks for. **The bot never grants approvals or
  moves funds.** It refuses to start live mode if the approvals are incomplete.
* Separation limits the damage if the bot or its server is compromised. It
  does not remove risk.
* Do **not** trade by hand in the bot's wallet. Manual orders and positions
  are shown as "external" and are never touched, but they will trigger cash
  reconciliation alerts.

## Step 3: Credentials (entered locally, never in chat)

```bash
polybot setup-credentials --only POLYBOT_POLYMARKET_PRIVATE_KEY POLYBOT_POLYMARKET_WALLET_ADDRESS \
  POLYBOT_POLYMARKET_API_KEY POLYBOT_POLYMARKET_API_SECRET POLYBOT_POLYMARKET_API_PASSPHRASE
```

| Value | Where to find it | Needed? |
|---|---|---|
| Signer private key | Your wallet / Polymarket's key export (email accounts). Prefer a CLOB-scoped **session key** if your account supports it | **Yes.** Orders must be signed; the API key alone cannot trade |
| Wallet (funder) address | Your Polymarket profile / deposit address | Yes |
| API key / secret / passphrase | The ones you already have | Optional: derived from the signer if blank |

* **Never** enter a seed phrase. The tool refuses anything that looks like one.
* What these keys can do is explained in [SECURITY.md](SECURITY.md).

## Step 4: Edit `.env`

```
POLYBOT_MODE=live
POLYBOT_LIVE_TRADING_ENABLED=true
POLYBOT_BANKROLL_USD=<the amount you are prepared to lose>
# review every risk limit in .env now
```

## Step 5: Run the checklist

```bash
polybot live-checklist
```

The checklist:

* asks you to confirm eligibility (type YES),
* runs the geoblock check,
* connects with your credentials and shows only the first and last characters
  of the signer address,
* reads your balance and checks the close-only flag,
* runs a **read-only** reconciliation,
* shows every risk limit for confirmation,
* runs the offline kill-switch self-test,
* asks you to acknowledge that there is no profit guarantee,
* and finally asks you to type `ENABLE LIVE TRADING`.

It **places no orders**.

## Step 6: Start and watch closely

```bash
polybot run        # or: docker compose up -d
```

* The dashboard shows a red **LIVE (REAL MONEY)** badge, plus a "Live gate"
  section that lists anything still blocking.
* Keep the size small at first.
* Test the kill switch for real: `polybot kill`, then check that the dashboard
  shows the cancel confirmations, then `polybot clear-kill` and
  `polybot resume`.

## What live mode does differently

* **Entries:** FAK orders with a hard price cap, so the bot never pays more
  than its limit.
* **Exits:** FAK orders with a hard price floor.
* **Pre-trade checks:** immediately before each order it re-reads the book and
  tick size, checks your balance, and runs every risk limit again.
* **Ambiguous submissions:** if the post times out, the order is marked
  UNKNOWN and all new orders stop. The bot then searches the exchange's open
  orders and trades. A unique match is adopted. Several possible matches halt
  the bot and alert you. No match within 3 minutes marks the order failed. It
  **never resends automatically**.
* **Fills:** booked only from exchange trades that are MINED or CONFIRMED. A
  trade that fails to settle is reported, not booked.
* **Reconciliation:** every 15 minutes it compares positions and cash with the
  exchange. A mismatch halts new orders until you investigate and run
  `polybot clear-halt`.
* **Resolved markets:** recorded at the payout price. If the winnings are not
  redeemed automatically, redeem them on polymarket.com.

## Going back to paper

Set `POLYBOT_MODE=paper` and restart. Paper and live records are stored
separately.
