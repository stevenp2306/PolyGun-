"""Command-line interface: ``polybot <command>`` (or ``python -m polybot <command>``)."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

from polybot.config import DEFAULT_SECRETS_FILE, RUNTIME_EDITABLE, Mode, Settings
from polybot.db import Store
from polybot.logging_setup import setup_logging


def _settings(apply_overrides: bool = True) -> tuple[Settings, Store]:
    s = Settings()
    store = Store(s.database_url)
    if apply_overrides:
        for k, v in (store.get("config_overrides") or {}).items():
            if k in RUNTIME_EDITABLE:
                object.__setattr__(s, k, Settings.model_validate({**s.model_dump(), k: v}).__getattribute__(k))
    return s, store


# ---------------------------------------------------------------- run
def cmd_run(args) -> int:
    s, store = _settings()
    setup_logging(s.log_dir, s.log_level, s.secret_values())
    import logging

    import uvicorn

    from polybot.alerts import Alerter
    from polybot.dashboard.app import create_app
    from polybot.discovery import SdkMarketSource
    from polybot.engine import Engine
    from polybot.marketdata import SdkBookSource
    from polybot.providers.odds_api import OddsApiProvider

    log = logging.getLogger("polybot")
    if s.mode == Mode.LIVE:
        log.warning("MODE=LIVE. Real orders are possible only if the live gate passes.")
    else:
        log.info("MODE=PAPER. No real orders will be sent.")
    engine = Engine(s, store, SdkMarketSource(), SdkBookSource(), [OddsApiProvider(s)], alerter=Alerter(s, store))
    if s.dashboard_password is None:
        log.warning("POLYBOT_DASHBOARD_PASSWORD not set: dashboard pages are disabled (only /healthz works).")
    if s.dashboard_host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("Dashboard bound to %s: make sure it is behind TLS + firewall/VPN.", s.dashboard_host)
    server = uvicorn.Server(uvicorn.Config(create_app(engine, s), host=s.dashboard_host, port=s.dashboard_port,
                                           log_config=None, access_log=False))
    server.install_signal_handlers = lambda: None  # we handle signals ourselves

    async def main() -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover (Windows)
                pass
        server_task = asyncio.create_task(server.serve())
        runner = asyncio.create_task(engine.run())
        done, _ = await asyncio.wait({runner, asyncio.create_task(stop.wait())},
                                     return_when=asyncio.FIRST_COMPLETED)
        if runner in done and runner.exception():
            log.error("engine stopped unexpectedly: %r", runner.exception())
        log.info("shutdown requested: stopping new orders, cancelling bot entry orders")
        await asyncio.to_thread(engine.shutdown)
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            server_task.cancel()
        runner.cancel()
        try:
            await runner
        except (asyncio.CancelledError, Exception):
            pass
        log.info("stopped cleanly")

    asyncio.run(main())
    return 0


# ---------------------------------------------------------------- credentials
SECRET_PROMPTS = [
    ("POLYBOT_POLYMARKET_PRIVATE_KEY", "Signer private key (0x...) - prefer a dedicated trading wallet or a "
                                       "CLOB-scoped session key; NEVER a seed phrase", True),
    ("POLYBOT_POLYMARKET_WALLET_ADDRESS", "Polymarket wallet (funder) address shown on your profile (0x...)", False),
    ("POLYBOT_POLYMARKET_API_KEY", "CLOB API key (optional - leave blank to derive from the signer)", True),
    ("POLYBOT_POLYMARKET_API_SECRET", "CLOB API secret (optional)", True),
    ("POLYBOT_POLYMARKET_API_PASSPHRASE", "CLOB API passphrase (optional)", True),
    ("POLYBOT_ODDS_API_KEY", "The Odds API key (needed for the sports strategy)", True),
    ("POLYBOT_DASHBOARD_PASSWORD", "Dashboard password (choose a long random one)", True),
    ("POLYBOT_TELEGRAM_BOT_TOKEN", "Telegram bot token for alerts (optional)", True),
    ("POLYBOT_TELEGRAM_CHAT_ID", "Telegram chat id (optional)", False),
    ("POLYBOT_ALERT_WEBHOOK_URL", "Generic alert webhook URL, e.g. ntfy/Slack (optional)", True),
]


def cmd_setup_credentials(args) -> int:
    path = Path(args.file or DEFAULT_SECRETS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()
    print(f"Secrets will be written to {path} (permissions 600). Input is hidden. Press Enter to keep the "
          "current value / skip. Nothing is printed or logged.\n")
    if args.only:
        prompts = [p for p in SECRET_PROMPTS if p[0] in args.only]
    else:
        prompts = SECRET_PROMPTS
    for key, label, hidden in prompts:
        cur = "set" if existing.get(key) else "not set"
        val = (getpass.getpass if hidden else input)(f"{label} [{cur}]: ").strip()
        if val:
            if "PRIVATE_KEY" in key and len(val.split()) >= 12:
                print("  That looks like a seed phrase. Refusing to store it. Never share a seed phrase.")
                continue
            existing[key] = val
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# polybot secrets - never commit, never share. Created by `polybot setup-credentials`.\n")
        for k, v in existing.items():
            f.write(f"{k}={v}\n")
    os.chmod(path, 0o600)
    print(f"\nSaved. Keys configured: {', '.join(k for k, v in existing.items() if v) or 'none'}")
    return 0


# ---------------------------------------------------------------- status / checks
def cmd_check_config(args) -> int:
    s, store = _settings()
    pub = s.public_dict()
    print("polybot configuration (secrets shown only as set/not set)\n")
    for k in ("mode", "bankroll_usd", "paper_starting_balance_usd", "database_url", "dashboard_host",
              "dashboard_port", "dashboard_password", "odds_api_key", "polymarket_private_key",
              "polymarket_wallet_address", "live_trading_enabled", "telegram_bot_token", "alert_webhook_url"):
        print(f"  {k:32} {pub[k]}")
    print("\nRisk limits (unvalidated example values):")
    from polybot.risk import RiskEngine
    for k, v in RiskEngine(s, store, s.mode.value).describe().items():
        print(f"  {k:32} {v}")
    print(f"\nRisk config hash: {s.risk_config_hash()}")
    issues = []
    if s.odds_api_key is None:
        issues.append("No POLYBOT_ODDS_API_KEY: the sports strategy has no independent probabilities and "
                      "will not trade (it still monitors markets).")
    if s.dashboard_password is None:
        issues.append("No POLYBOT_DASHBOARD_PASSWORD: dashboard disabled.")
    if s.mode == Mode.LIVE:
        from polybot.brokers.live_gate import LiveGate
        g = LiveGate(s).check()
        issues += [f"live gate: {f}" for f in g.failures if "geoblock status not checked" not in f]
    print("\nIssues:" if issues else "\nNo blocking issues found.")
    for i in issues:
        print(f"  - {i}")
    return 0


def cmd_status(args) -> int:
    s, store = _settings()
    mode = s.mode.value
    hb = store.get("engine_heartbeat") or {}
    print(json.dumps({
        "heartbeat": hb, "heartbeat_age_s": round(time.time() - hb.get("ts", 0), 1) if hb else None,
        "kill_switch": store.get(f"{mode}:kill_switch"), "paused": store.get(f"{mode}:paused"),
        "risk_locks": store.get(f"{mode}:risk_locks"), "halt": store.get(f"{mode}:halt"),
        "open_positions": len(store.list_positions(mode)),
        "open_orders": len(store.list_orders(mode, statuses=["OPEN", "PARTIAL", "PENDING_SUBMIT", "UNKNOWN"])),
    }, indent=2, default=str))
    return 0


def _risk(s: Settings, store: Store):
    from polybot.risk import RiskEngine
    return RiskEngine(s, store, s.mode.value)


def cmd_kill(args) -> int:
    s, store = _settings()
    _risk(s, store).kill("cli", args.reason)
    print("Kill switch set. The running bot stops new orders and cancels its own open orders within one "
          "loop (~5s). Check the dashboard or `polybot status` for cancel confirmations.")
    return 0


def cmd_pause(args) -> int:
    s, store = _settings()
    _risk(s, store).pause("cli")
    print("New entries paused.")
    return 0


def cmd_resume(args) -> int:
    s, store = _settings()
    ok, msg = _risk(s, store).resume("cli")
    print(msg)
    return 0 if ok else 1


def cmd_reset_risk_lock(args) -> int:
    s, store = _settings()
    r = _risk(s, store)
    print(f"Active locks: {r.locks() or 'none'}")
    if input("Type RESET to clear risk locks (entries stay paused until you run `polybot resume`): ") != "RESET":
        print("Cancelled.")
        return 1
    r.reset_locks("cli")
    r.pause("cli")
    store.delete_key(f"{s.mode.value}:day_start")
    print("Locks cleared; daily baseline restarts on the next loop; entries PAUSED.")
    return 0


def cmd_clear_kill(args) -> int:
    s, store = _settings()
    if input("Type CLEAR KILL to clear the kill switch: ") != "CLEAR KILL":
        return 1
    r = _risk(s, store)
    r.clear_kill("cli")
    r.pause("cli")
    print("Kill switch cleared; entries paused until `polybot resume`.")
    return 0


def cmd_clear_halt(args) -> int:
    s, store = _settings()
    r = _risk(s, store)
    print(f"Halt: {r.halted()}")
    if input("Type CLEAR HALT after you have checked the cause: ") != "CLEAR HALT":
        return 1
    r.clear_halt("cli")
    print("Halt cleared.")
    return 0


def cmd_record_flow(args) -> int:
    s, store = _settings()
    from polybot.portfolio import Accounting
    Accounting(store, args.mode or s.mode.value).record_external_flow(args.kind, args.amount, args.note or "")
    print(f"Recorded {args.kind} of ${abs(args.amount):.2f} for {args.mode or s.mode.value} (not counted as P&L).")
    return 0


def cmd_backup(args) -> int:
    s, _ = _settings(apply_overrides=False)
    if not s.database_url.startswith("sqlite:///"):
        print("For PostgreSQL use pg_dump (see docs/DEPLOYMENT.md).")
        return 1
    src = s.database_url.removeprefix("sqlite:///")
    out_dir = Path(args.dir or (s.data_dir / "backups"))
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"polybot-{time.strftime('%Y%m%d-%H%M%S')}.db"
    with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
        a.backup(b)  # consistent online backup
    os.chmod(dst, 0o600)
    keep = sorted(out_dir.glob("polybot-*.db"))[:-args.keep] if args.keep else []
    for old in keep:
        old.unlink()
    print(f"Backup written: {dst}")
    return 0


def cmd_evidence_report(args) -> int:
    s, _ = _settings()
    from polybot.backtest.report import build_report
    md, data = build_report(Path(args.data or (s.data_dir / "research")), s)
    out = Path(args.out or f"reports/evidence-{time.strftime('%Y%m%d')}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(md)
    print(f"\nWritten to {out}")
    return 0


# ---------------------------------------------------------------- live checklist
def cmd_test_kill_switch(args) -> int:
    """Offline test: the kill switch blocks every submission path without touching the exchange."""
    s, _ = _settings()
    from polybot.brokers.paper import PaperBroker
    from polybot.domain import OrderRequest, OrderType, Side
    from polybot.portfolio import Accounting
    from polybot.risk import RiskEngine

    tmp = Store("sqlite://")
    t_s = Settings.model_validate({**s.model_dump(), "mode": "paper"})
    acct = Accounting(tmp, "paper")
    acct.ensure_initial_balance(100)
    broker = PaperBroker(t_s, tmp, acct)
    risk = RiskEngine(t_s, tmp, "paper")
    req = OrderRequest("TEST", "M", "E", Side.BUY, 5, 0.5, OrderType.GTC, "entry", "test", "kill-switch test")
    res = broker.submit(req, {"tick_size": 0.01, "min_order_size": 1})
    risk.kill("test", "kill switch self-test")
    from polybot.engine import Engine

    class _Nil:
        def fetch_sports_markets(self, limit): return []
        def fetch_market(self, mid): return None
        def fetch_books(self, toks): return []

    eng = Engine(t_s, tmp, _Nil(), _Nil(), [], broker=broker)
    results = eng._process_kill(force=True)
    snap = acct.snapshot(lambda t: None, lambda t: None)
    ok_block, why = risk.check_order(req, snap, None)
    passed = (res.ok and results and all(r["confirmed"] for r in results) and not ok_block
              and tmp.get_order(res.order_id)["status"] == "CANCELED")
    print("kill switch self-test:", "PASSED" if passed else "FAILED", "-", why)
    return 0 if passed else 1


def cmd_live_checklist(args) -> int:
    from polybot.brokers.live_gate import CHECKLIST_ITEMS, ENABLE_PHRASE, LiveGate, fetch_geoblock

    s, store = _settings()
    gate = LiveGate(s)
    data = gate.load() or {"items": {}}
    items = data.setdefault("items", {})
    now = time.time()
    print("LIVE TRADING CHECKLIST - nothing here places an order.\n")

    def mark(key: str, ok: bool, note: str = "") -> None:
        items[key] = {"confirmed": bool(ok), "ts": now, "note": note}
        print(f"  [{'x' if ok else ' '}] {key}: {note}")

    def ask(key: str) -> None:
        print(f"\n{CHECKLIST_ITEMS[key]}")
        mark(key, input("Type YES to confirm: ").strip() == "YES", "user confirmation")

    # 1. jurisdiction
    ask("jurisdiction_eligible")
    # 2. geoblock
    try:
        geo = asyncio.run(fetch_geoblock(s.geoblock_url))
        mark("geoblock_check_passed", geo.get("blocked") is False,
             f"blocked={geo.get('blocked')} country={geo.get('country')} region={geo.get('region')}")
    except Exception as e:
        mark("geoblock_check_passed", False, f"check failed: {type(e).__name__}")
    # 3/4/6. credentials, balances, reconciliation (read-only)
    if s.mode != Mode.LIVE:
        print("\nPOLYBOT_MODE is not 'live'; account checks skipped. Set POLYBOT_MODE=live and re-run.")
        for k in ("credentials_verified", "balance_and_permissions", "reconciliation_tested"):
            mark(k, False, "skipped (mode != live)")
    else:
        from polybot.brokers.live import LiveBroker
        from polybot.portfolio import Accounting

        acct = Accounting(store, "live")
        broker = LiveBroker(s, store, acct, gate)
        try:
            broker.connect()
            signer = getattr(broker.client, "signer", "?")
            mark("credentials_verified", True, f"signer {str(signer)[:6]}…{str(signer)[-4:]}, "
                                               f"wallet type {getattr(broker.client, 'wallet_type', '?')}")
            cash = broker.refresh_cash()
            co = broker.closed_only()
            mark("balance_and_permissions", cash > 0 and not co,
                 f"collateral ${cash:.2f}, approvals complete, closed_only={co}")
            if not acct.store.list_cash_flows("live") and cash > 0:
                acct.store.add_cash_flow("live", "initial", cash, "exchange balance at live setup")
            problems = broker.reconcile_account(time.time(), store.list_positions("live"))
            mark("reconciliation_tested", not problems, "; ".join(problems) or "ok")
        except Exception as e:
            mark("credentials_verified", items.get("credentials_verified", {}).get("confirmed", False),
                 f"error: {type(e).__name__}: {e}")
            mark("balance_and_permissions", False, "not verified")
            mark("reconciliation_tested", False, "not verified")
    # 5. risk limits
    print("\nRisk configuration to confirm:")
    from polybot.risk import RiskEngine
    for k, v in RiskEngine(s, store, "live").describe().items():
        print(f"  {k:30} {v}")
    print(f"  max loss you are exposing: up to the full bankroll ${s.bankroll_usd:.2f} in adverse cases")
    ask("risk_limits_confirmed")
    # kill switch
    mark("kill_switch_tested", cmd_test_kill_switch(args) == 0, "offline self-test")
    ask("no_profit_guarantee")
    data["risk_config_hash"] = s.risk_config_hash()
    all_ok = all((items.get(k) or {}).get("confirmed") for k in CHECKLIST_ITEMS)
    data["enabled_phrase_confirmed"] = False
    if all_ok:
        print(f"\nAll checks passed. To ALLOW live orders, type exactly: {ENABLE_PHRASE}")
        data["enabled_phrase_confirmed"] = input("> ").strip() == ENABLE_PHRASE
    else:
        print("\nNot all items passed; live trading stays disabled.")
    data["completed_at"] = now
    gate.save(data)
    print("\nSaved checklist. Live orders additionally require POLYBOT_LIVE_TRADING_ENABLED=true and "
          "POLYBOT_MODE=live, and they stop automatically if any risk setting changes.")
    return 0 if data["enabled_phrase_confirmed"] else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="polybot", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the bot + dashboard (PAPER by default)").set_defaults(fn=cmd_run)
    sp = sub.add_parser("setup-credentials", help="enter secrets locally (hidden input, file mode 600)")
    sp.add_argument("--file")
    sp.add_argument("--only", nargs="*", help="only prompt for these variable names")
    sp.set_defaults(fn=cmd_setup_credentials)
    sub.add_parser("check-config", help="show configuration and blocking issues").set_defaults(fn=cmd_check_config)
    sub.add_parser("status", help="show heartbeat, locks, kill switch").set_defaults(fn=cmd_status)
    sp = sub.add_parser("kill", help="engage the kill switch")
    sp.add_argument("--reason", default="cli kill switch")
    sp.set_defaults(fn=cmd_kill)
    sub.add_parser("pause", help="pause new entries").set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="resume entries (refused while a lock/kill is active)").set_defaults(fn=cmd_resume)
    sub.add_parser("reset-risk-lock", help="deliberately clear daily-loss/drawdown locks").set_defaults(fn=cmd_reset_risk_lock)
    sub.add_parser("clear-kill", help="clear the kill switch").set_defaults(fn=cmd_clear_kill)
    sub.add_parser("clear-halt", help="clear a reconciliation/exchange halt").set_defaults(fn=cmd_clear_halt)
    sp = sub.add_parser("record-flow", help="record a deposit/withdrawal (not P&L)")
    sp.add_argument("kind", choices=["deposit", "withdrawal"])
    sp.add_argument("amount", type=float)
    sp.add_argument("--note")
    sp.add_argument("--mode", choices=["paper", "live"])
    sp.set_defaults(fn=cmd_record_flow)
    sp = sub.add_parser("backup", help="consistent SQLite backup")
    sp.add_argument("--dir")
    sp.add_argument("--keep", type=int, default=30)
    sp.set_defaults(fn=cmd_backup)
    sp = sub.add_parser("evidence-report", help="backtest recorded data and write the evidence report")
    sp.add_argument("--data")
    sp.add_argument("--out")
    sp.set_defaults(fn=cmd_evidence_report)
    sub.add_parser("live-checklist", help="interactive live-trading setup checklist (places no orders)"
                   ).set_defaults(fn=cmd_live_checklist)
    sub.add_parser("test-kill-switch", help="offline kill-switch self-test").set_defaults(fn=cmd_test_kill_switch)
    args = p.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
