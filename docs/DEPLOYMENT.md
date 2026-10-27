# Running 24/7

The bot only trades while its process runs and can reach the internet. It
**cannot trade during internet, server, Polymarket, odds-provider, or account
outages**. When data goes stale it stops opening positions, and it cannot
manage exits while it is down.

## A. Local development

See [SETUP_GUIDE.md](SETUP_GUIDE.md): `polybot run` in a virtualenv. Your
laptop must stay awake.

## B. Always-on server (small VPS)

**Nothing here was deployed or paid for.** Choosing and paying for a server is
your decision.

A 1 vCPU / 1–2 GB RAM VPS is enough: the bot uses roughly 150–300 MB of RAM.
Pick a provider and **region where using Polymarket is permitted for you**.
The geoblock check runs from the server, and a server abroad does not change
your own eligibility.

### Deployment checklist

1. **OS updates.**
   `sudo apt update && sudo apt full-upgrade -y && sudo apt install -y unattended-upgrades fail2ban`,
   then `sudo dpkg-reconfigure -plow unattended-upgrades`.
2. **Non-root user + SSH keys only.** Create a user. In `/etc/ssh/sshd_config`
   set `PasswordAuthentication no` and `PermitRootLogin no`.
3. **Firewall.** `sudo ufw default deny incoming && sudo ufw allow OpenSSH && sudo ufw enable`.
   Do **not** open port 8080.
4. **Docker.** Install Docker Engine + the compose plugin (docs.docker.com).
5. **Code and settings.**
   ```bash
   git clone <repo> polybot && cd polybot
   cp .env.example .env && nano .env
   mkdir -p data logs secrets && chmod 700 secrets
   sudo chown -R 10001:10001 data logs      # the container runs as uid 10001
   ```
6. **Secrets.** Run `polybot setup-credentials` on the server (from a
   virtualenv), or create `secrets/polybot.env` with `chmod 600`. Secrets are
   injected at runtime and are **never baked into the image**.
7. **Start.** `docker compose up -d --build`, then `docker compose ps` should
   show `healthy` after about a minute.
8. **Dashboard access.** It is published on the server's `127.0.0.1:8080`
   only. Use one of:
   * **SSH tunnel (simplest):** `ssh -L 8080:127.0.0.1:8080 you@server`, then
     open http://127.0.0.1:8080 on your computer.
   * **Private VPN** (e.g. Tailscale/WireGuard), with the port reachable only
     on the VPN.
   * **TLS reverse proxy** with its own authentication: see
     `deploy/Caddyfile.example`. Never expose plain HTTP.
9. **External uptime monitoring** (see below).
10. **Backups** (see below).
11. **Restart test.** `docker compose restart`, then check the dashboard: same
    positions, no duplicate orders, a "Bot started" alert.

### Restart, crash and shutdown behaviour

* `restart: unless-stopped` restarts the container after crashes and reboots.
* A Docker `HEALTHCHECK` calls `/healthz`. It returns 503 if the loop has not
  completed recently.
* On SIGTERM (`docker compose stop`, reboot) the bot:
  * stops new orders,
  * cancels its own *entry* orders,
  * leaves positions untouched,
  * exits within 30 s.
* On start it reloads state from the database. In live mode it reconciles with
  the exchange before trusting local state. A UNKNOWN order blocks new orders
  until it is resolved, so a restart cannot create duplicates.
* Ordinary failures don't stop the process: network errors, rate limits,
  provider errors and exceptions inside one loop are logged, alerted (with
  deduplication), retried with backoff, and the bot continues in `degraded`
  state.

### Logs

* Application logs go to `logs/polybot.log`, rotated at 10 MB × 10 files.
* Container stdout is also rotated (json-file, 10 MB × 5).
* Secrets are redacted before writing.
* View them with `docker compose logs -f --tail=200`.

### Backups and restore

The SQLite database (`data/polybot.db`) holds all orders, fills, positions and
decisions.

```bash
# online, consistent backup (safe while running)
docker compose exec polybot polybot backup --keep 30      # -> data/backups/polybot-YYYYmmdd-HHMMSS.db
# nightly via cron on the host (crontab -e):
17 3 * * * cd /home/you/polybot && docker compose exec -T polybot polybot backup --keep 30 >/dev/null 2>&1
```

* Copy `data/backups/` off the server (e.g. `rsync`/`restic` to storage you
  control). Backups contain trading history, so keep them private.
* **Restore:**
  ```bash
  docker compose stop
  cp data/backups/polybot-<timestamp>.db data/polybot.db
  docker compose start
  ```
  After a restore in live mode, open the dashboard and let reconciliation run.
  Investigate any halt it raises before clearing it: the exchange is the source
  of truth.
* **PostgreSQL:** set `POLYBOT_DATABASE_URL=postgresql+psycopg://...`, install
  the `postgres` extra (`pip install ".[postgres]"`, or add it to the
  Dockerfile), and back up with `pg_dump`. The schema is created
  automatically.

### Monitoring and alerts

The bot sends alerts for:

* trades placed and filled,
* failed orders,
* risk limits hit,
* stale data or a disconnected feed,
* restarts,
* reconciliation failures and halts,
* the kill switch,
* the geoblock check.

Delivery is to the log and dashboard always, plus Telegram and/or a webhook
(e.g. ntfy) if configured. Identical alerts repeat at most every 10 minutes.

**A crashed bot cannot report its own crash**, and neither can a dead server.
Set up an **independent** uptime monitor (e.g. Uptime Kuma on another machine,
or a hosted monitor) that checks `/healthz` every minute and alerts you on a
non-200 response or a timeout. `/healthz` returns only
`{ok, state, last_tick_age_s}`, with no account data. Ways to reach it:

* expose it through your TLS proxy (the example Caddyfile serves only
  `/healthz` without auth), or
* use the monitor's SSH / agent check, or
* a host cron:
  `curl -fsS http://127.0.0.1:8080/healthz || <send alert via your phone-notification service>`.
  This one only works while the host is up, so combine it with an external
  ping of the host.

### Updating

```bash
git pull && docker compose up -d --build
```

Re-run `polybot check-config`. In live mode, any change to a risk setting
requires `polybot live-checklist` again.
