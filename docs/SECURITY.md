# Security and credentials

## What your credentials can do

| Credential | Power | If it leaks |
|---|---|---|
| **Signer private key** | Signs orders. For an EOA wallet it **controls the funds directly**: an attacker can transfer everything. For proxy, Safe and deposit wallets it is the owner key that controls the wallet. | Assume total loss of the wallet's funds and positions. Move remaining funds to a new wallet immediately |
| **CLOB-scoped session key** (if supported) | Trading on the order book for the deposit wallet, until it expires or is revoked. Scoped to CLOB | An attacker can place bad trades and drain value through trading, but cannot use other venues. Revoke it |
| **API key / secret / passphrase** | Authenticates order posting, cancels and account reads. **Cannot sign orders by itself** | Can cancel your orders and read account data. Delete and re-derive |
| Odds API key | Paid data quota | Quota theft. Rotate in the provider dashboard |
| Telegram token / webhook URL | Send messages as your bot | Spam / phishing. Revoke via @BotFather |
| Dashboard password | Pause, resume, kill switch, some settings changes | An attacker could pause or kill trading, or change limits within the allowed ranges. Note that any change blocks live orders until the checklist is re-run |

**Never** give anyone, and never enter into this bot, a **seed phrase**. The
bot does not ask for one, and `setup-credentials` refuses input that looks like
one.

## How the project handles secrets

* No secret is hard-coded.
* Secrets are typed with hidden input into `secrets/polybot.env`, mode 600,
  inside a 700 directory.
* `.env`, `secrets/`, the database, logs, backups and the checklist are all in
  `.gitignore`. `.dockerignore` keeps them out of image builds.
* In Docker, secrets are injected at runtime through `env_file`. The image
  contains none.
* Secrets are stored as `SecretStr`, so their representation is masked.
* The dashboard and `check-config` show only "set" / "not set".
* A logging filter redacts registered secret values, 64-hex-character keys,
  `apiKey=` query strings and Telegram tokens from every log line and
  traceback. The HTTP client's URL logging is silenced.
* Alerts pass through the same redactor and contain no wallet address.
* Signed orders stored in the database contain only public fields.
* Tests check that secrets do not appear in logs, alerts, the dashboard or the
  configuration views.

## Approvals and fund movement

* The bot contains **no withdrawal, transfer or approval features**.
* It deliberately uses `create_*_order` + `post_order` instead of the SDK's
  `place_*_order`, which can submit approval transactions automatically.
* Polymarket requires one-time token approvals so its exchange contracts can
  settle your trades: collateral spending and outcome-token transfers. Do these
  through Polymarket's own onboarding on polymarket.com. The bot only *checks*
  whether they are in place and refuses live mode if they are not.

## A separate trading wallet

Use a dedicated Polymarket account and wallet funded only with the bot's
bankroll. If your account type supports it, authorise a **CLOB-scoped session
key** for the bot instead of giving it the owner key. This limits what a
compromised server can reach. It does **not** eliminate risk: the bot's own
funds and positions remain exposed.

## Rotation after a suspected leak

1. Run `polybot kill`, then stop the bot.
2. **Private key or owner key:** create a new wallet. Move the funds and close
   or transfer positions through polymarket.com. Treat the old wallet as
   compromised forever.
3. **Session key:** revoke it (Polymarket UI, or the SDK's
   `revoke_session_key` run from a trusted machine), then authorise a new one.
4. **API credentials:** delete them (UI or SDK `delete_api_key`) and leave the
   API fields blank so new ones are derived. Or create new ones and run
   `setup-credentials` again.
5. Rotate the odds key, Telegram token, webhook URL and dashboard password.
6. Check `logs/` and the dashboard audit log for actions you did not take.
7. Redo `polybot live-checklist` before resuming.

## Server basics

See [DEPLOYMENT.md](DEPLOYMENT.md):

* SSH keys only, firewall, automatic security updates.
* Dashboard bound to localhost, reached through an SSH tunnel, VPN or TLS
  proxy.
* The container runs as non-root, with a read-only filesystem and all
  capabilities dropped.
