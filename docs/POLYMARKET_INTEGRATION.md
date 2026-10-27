# Polymarket integration notes (checked 2026-10-27)

This records what the official sources said when the project was built, and
how the bot uses it. **Re-check before going live.** Polymarket changes its
APIs often: two Python SDKs were retired in 2026 alone.

## How this was checked, and its limits

The build environment's network blocked `docs.polymarket.com`,
`clob.polymarket.com` and `gamma-api.polymarket.com`. So:

* The integration is based on the **source code of the official unified SDK**
  (`github.com/Polymarket/py-sdk`, package `polymarket-client` **0.14.0**,
  released 2026-10-21, commit `7032ed0`). The SDK was read directly, and
  installed from PyPI.
* Statements about fees, rate limits and geoblocking come from search-engine
  excerpts of the official docs pages listed below. The pages themselves were
  not loaded. Treat those numbers as "verify before relying on them".
* The bot was **not run against live Polymarket endpoints**. In the smoke
  test, discovery failed with HTTP 403 from the sandbox proxy. The bot stayed
  `degraded` and refused to trade, which is the intended behaviour.

## SDK choice

| Package | Status (2026-10-27) | Used? |
|---|---|---|
| `py-clob-client` | Repository archived 2026-05-25; its README says it no longer works | No |
| `py-clob-client-v2` | Its README now says *deprecated, migrate to the unified SDK* | No |
| **`polymarket-client`** (import `polymarket`) | Official unified SDK, v0.14.0, Python ≥ 3.11 | **Yes, pinned `==0.14.0`** |

The version is pinned because 0.x minor releases may contain breaking changes.

## Public data vs authenticated trading

* **Public (no credentials):** markets, events, order books, prices and
  spreads, price history, public positions by address, and the WebSocket
  market channel (`wss://ws-subscriptions-clob.polymarket.com/ws/market`).
  The bot uses `PublicClient` and `AsyncPublicClient` for all of these.
* **Authenticated:** placing and cancelling orders, open orders, your own
  trades, balances and allowances, closed-only status, and heartbeats. These
  use `SecureClient`.

## Authentication and credentials

* **L1:** an EIP-712 signature from the *signer private key*. It creates or
  derives the CLOB API credentials.
* **L2:** HMAC request signing with the **API key + secret + passphrase**.
  Placing orders, cancelling and reading account data need it.
* **Every order is also EIP-712-signed by the signer key.** The API key,
  secret and passphrase alone cannot place orders. Live trading therefore
  needs:
  * the signer private key (or an authorised **session key**, see below),
  * the **wallet (funder) address** that holds the funds,
  * the API credentials. These are optional: if they are left blank, the SDK
    derives them from the signer.
* **Wallet types** are detected automatically from signer and wallet: `EOA`,
  `POLY_PROXY` (email/Magic accounts), `GNOSIS_SAFE` (browser-wallet
  accounts) and `DEPOSIT_WALLET` (the new default). A signer that does not
  derive the wallet is treated as a **session key** for a deposit wallet.
* **Session keys** (`authorize_session_key`) can be limited to the `CLOB`
  scope and expire (`valid_until`). Where your account supports them, they
  are the narrowest credential the bot can run with.

## Order types (what the bot uses)

| Type | Meaning | Bot usage |
|---|---|---|
| GTC | Resting limit order until cancelled | Supported; not used by default |
| GTD | Resting limit order with expiry (≥ 3 min in the future) | Supported |
| FOK | Market order: fill completely right now, or nothing | Supported |
| **FAK** | Market order: fill what is available right now, cancel the rest | **Default for entries and exits** |

* Entries are FAK buys with a hard `max_price`. Exits are FAK sells with a
  hard `min_price`. The SDK guarantees fills are never worse than the bound.
* The bot signs orders with `create_market_order` / `create_limit_order` and
  submits them with `post_order`. It does **not** use `place_*_order`, because
  that helper can automatically send token-approval transactions when an order
  is rejected for allowance.
* Post responses can be `live`, `matched` or `delayed`, or a rejection code
  (`not_enough_balance`, `fak_not_filled`, `market_not_ready`, ...).
* Trade settlement goes `MATCHED` → `MINED` → `CONFIRMED`, with
  `RETRYING`/`FAILED` possible. The bot books fills only at
  `MINED`/`CONFIRMED`, and alerts on `FAILED`.
* Tick size can only become finer, never coarser. The bot re-reads it just
  before every live order.
* Signed orders have no client-order-ID field. Duplicate prevention therefore
  works by persisting intent first and reconciling ambiguous submissions
  against open orders and trades. The bot never blindly resubmits.

## Fees

* Each market publishes a fee schedule: `rate`, `exponent`, `takerOnly` and
  `rebateRate`.
* The SDK budgets fees as **fee = shares × rate × (p·(1−p))^exponent**. The
  bot uses this formula. One docs excerpt writes the formula with an extra
  factor `p`. Since `p ≤ 1`, the SDK form is never smaller, so using it is
  conservative.
* Changelog excerpt: the sports taker fee rate rose **0.03 → 0.05 on
  2026-07-10**, and the maker rebate fell from 25% to 15%. The bot does not
  hard-code this; it reads each market's schedule.
* A market with **unknown fees is ineligible**.

## Rate limits

* Per-signer token buckets cover order placement and cancellation. Responses
  carry `Poly-RateLimit-*` headers.
* Changelog excerpts: `POST /order` burst 5000 per 10 s; sustained limit
  raised to 120000 per 10 min on 2026-06-01; up to 1000 order IDs per cancel
  request; enforcement warning mode from 2026-07-24.
* The bot sends only a handful of requests per minute.
* Reads retry with exponential backoff and jitter. **Order posts are never
  auto-retried.** Repeated failures open a circuit breaker.

## Order heartbeat (optional; off by default)

The first accepted heartbeat turns on timeout cancellation for **all open
orders of the API key**, across all processes, including orders you place by
hand with the same key. After about 10 s without a heartbeat, the server
*attempts* to cancel everything. Success is not guaranteed and there is no
"disarm" call. A heartbeat never closes positions. Enable
`POLYBOT_USE_ORDER_HEARTBEAT` only if you understand this.

## Geographic restrictions

* Official endpoint: `GET https://polymarket.com/api/geoblock` returns
  `{blocked, ip, country, region}`. Orders from blocked regions are rejected.
* The docs list about 33 fully blocked countries (excerpt includes the **US,
  UK, France, Germany**, Russia and Iran), some blocked regions, and a
  **close-only** tier in some countries (e.g. Singapore, Poland, Thailand,
  Taiwan).
* **Polymarket US** is a separate, CFTC-regulated venue. No public trading API
  for it was found, and this bot does not support it.
* The bot calls the geoblock endpoint during the live checklist and every
  15 minutes in live mode. **Blocked, unknown, or older than 3 hours ⇒ no live
  orders.** It also checks the account's closed-only flag.
* **Do not use a VPN or misstate your location.** Paper trading and research
  on public data keep working where they are permitted.

## Market versions

CLOB V2 went live on 2026-04-28. Markets can be v1 (CTF token IDs) or v2
(position IDs). This version of the bot trades **binary v1 markets addressed
by token ID** (the SDK's default listing). v2-only markets are skipped.

## Sources (accessed 2026-10-27)

* Official SDK: https://github.com/Polymarket/py-sdk (README, `src/polymarket/**`, CHANGELOG 0.12–0.14)
* PyPI: https://pypi.org/project/polymarket-client/ (0.14.0)
* Archived client notice: https://github.com/Polymarket/py-clob-client
* Deprecated v2 client notice: https://github.com/Polymarket/py-clob-client-v2
* Docs, SDK listing: https://docs.polymarket.com/api-reference/clients-sdks.md
* Docs, trading overview: https://docs.polymarket.com/trading/overview.md
* Docs, fees: https://docs.polymarket.com/trading/fees
* Docs, rate limits: https://docs.polymarket.com/api-reference/trading-rate-limits.md
* Docs, changelog: https://docs.polymarket.com/changelog
* Docs, geoblock: https://docs.polymarket.com/api-reference/geoblock.md and https://docs.polymarket.com/polymarket-learn/FAQ/geoblocking
* Odds data: https://the-odds-api.com (v4 API)
