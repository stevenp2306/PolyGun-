"""Local dashboard (FastAPI). Protected by HTTP Basic auth; binds to 127.0.0.1 by default.

Do NOT expose it to the internet directly: put it behind an SSH tunnel, a VPN
(e.g. Tailscale) or a TLS reverse proxy with its own authentication. See
docs/DEPLOYMENT.md.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from polybot.config import RUNTIME_EDITABLE, Settings
from polybot.reporting import export_csv, performance
from polybot.strategy.sports_value import CARD

STATIC = Path(__file__).parent / "static"
security = HTTPBasic(auto_error=False)


def create_app(engine: Any, settings: Settings) -> FastAPI:
    app = FastAPI(title="polybot", docs_url=None, redoc_url=None, openapi_url=None)
    store = engine.store
    failed: dict[str, list[float]] = {}

    def auth(request: Request, creds: HTTPBasicCredentials | None = Depends(security)) -> str:
        if settings.dashboard_password is None:
            raise HTTPException(503, "Dashboard disabled: set POLYBOT_DASHBOARD_PASSWORD (see README).")
        ip = request.client.host if request.client else "?"
        recent = [t for t in failed.get(ip, []) if time.time() - t < 300]
        if len(recent) >= 10:
            raise HTTPException(429, "too many failed logins; wait 5 minutes")
        ok = creds is not None and secrets.compare_digest(
            creds.username.encode(), settings.dashboard_username.encode()) and secrets.compare_digest(
            creds.password.encode(), settings.dashboard_password.get_secret_value().encode())
        if not ok:
            failed[ip] = recent + [time.time()]
            raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": "Basic"})
        return creds.username

    def csrf(x_polybot_action: str | None = Header(default=None)) -> None:
        # Custom header => browsers will not send it cross-site without a CORS preflight (which we never allow).
        if x_polybot_action != "1":
            raise HTTPException(400, "missing X-Polybot-Action header")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'"
        return resp

    @app.get("/healthz")
    def healthz():
        hb = store.get("engine_heartbeat") or {}
        age = time.time() - hb.get("ts", 0)
        ok = hb.get("state") in ("running", "degraded") and age < max(60, 6 * settings.loop_interval_s)
        return JSONResponse({"ok": ok, "state": hb.get("state"), "last_tick_age_s": round(age, 1)},
                            status_code=200 if ok else 503)

    @app.get("/")
    def index(user: str = Depends(auth)):
        return FileResponse(STATIC / "index.html")

    @app.get("/static/{name}")
    def static(name: str, user: str = Depends(auth)):
        p = (STATIC / name).resolve()
        if p.parent != STATIC.resolve() or not p.exists():
            raise HTTPException(404)
        return FileResponse(p)

    @app.get("/api/overview")
    def overview(user: str = Depends(auth)):
        e = engine
        snap = e.last_snapshot
        rs = e.last_risk
        now = time.time()
        today0 = now - (now % 86400)
        realized_today = sum(p["realized_pnl"] for p in store.list_positions(e.mode, status=None)
                             if (p.get("closed_at") or 0) >= today0)
        perf = performance(store, e.mode, "all")
        gate = e.gate.check() if e.mode == "live" else None
        return {
            "mode": e.mode.upper(), "simulated": e.mode == "paper", "state": e.state,
            "started_at": e.started_at, "last_tick": e.last_tick, "last_tick_error": e.last_tick_error,
            "cash": snap.cash if snap else None, "available_cash": snap.available_cash if snap else None,
            "equity": snap.equity if snap else None, "exposure": snap.exposure if snap else None,
            "reserved": snap.reserved if snap else None,
            "unrealized_conservative": snap.unrealized_conservative if snap else None,
            "realized_today": realized_today, "daily_pnl": rs.daily_pnl if rs else None,
            "drawdown_pct": rs.drawdown_pct if rs else None, "max_drawdown_usd": perf["max_drawdown_usd"],
            "total_return": perf["total_return"], "net_external_flows": snap.net_external_flows if snap else None,
            "can_open": rs.can_open if rs else False, "gate_reasons": e.risk.entry_gate()[1],
            "locks": e.risk.locks(), "kill": store.get(f"{e.mode}:kill_switch"),
            "kill_result": store.get(f"{e.mode}:kill_result"), "paused": e.risk.paused(),
            "halt": e.risk.halted(),
            "feed": {"status": e.health.status(now, settings.max_book_age_s), "ws_connected": e.health.ws_connected,
                     "ws_last_message": e.health.ws_last_message, "rest_last_success": e.health.rest_last_success,
                     "ws_reconnects": e.health.ws_reconnects, "last_error": e.health.last_error,
                     "clock_skew_s": e.health.clock_skew_s},
            "discovery": {"last_refresh": e.discovery.last_refresh, "markets_seen": len(e.discovery.markets),
                          "eligible": len(e.discovery.eligible)},
            "providers": e.provider_statuses(),
            "live_gate": None if gate is None else {"allowed": gate.allowed, "failures": gate.failures},
            "external_orders": getattr(e.broker, "external_orders", []),
            "external_positions": getattr(e.broker, "external_positions", []),
        }

    @app.get("/api/positions")
    def positions(user: str = Depends(auth)):
        snap = engine.last_snapshot
        out = []
        for pv in (snap.positions if snap else []):
            row = pv.row
            ex = engine.last_exit_views.get(pv.token_id, {})
            m = engine.market_for_token(pv.token_id)
            fairs, _ = engine.fairs.get(m.market_id, (None, "")) if m else (None, "")
            f = (fairs or {}).get(pv.token_id)
            out.append({
                "token_id": pv.token_id, "event": row.get("event_title") or row.get("question"),
                "outcome": row.get("outcome_label"), "shares": pv.shares, "avg_entry": pv.avg_cost,
                "cost_basis": pv.cost_basis, "best_bid": pv.best_bid, "exit_value": pv.liquidation_value,
                "exit_shares_absorbable": pv.liquidation_shares, "pnl_after_costs": pv.unrealized_conservative,
                "pnl_pct": pv.unrealized_pct, "fair_prob": f.prob if f else None,
                "edge_vs_bid": ex.get("numbers", {}).get("remaining_edge_vs_bid"),
                "entry_reason": row.get("entry_reason"), "exit_plan": row.get("exit_plan"),
                "exit_check": ex, "opened_at": row.get("opened_at"), "simulated": engine.mode == "paper",
            })
        return out

    @app.get("/api/orders")
    def orders(mode: str | None = None, user: str = Depends(auth)):
        rows = store.list_orders(mode or engine.mode, limit=300)
        for r in rows:
            r.pop("signed_order", None)
            r["simulated"] = r["mode"] == "paper"
            meta = r.get("meta") or {}
            r["meta"] = {k: meta[k] for k in ("outcome_label", "event_title", "post_status") if k in meta}
        return rows

    @app.get("/api/opportunities")
    def opportunities(user: str = Depends(auth)):
        return {"candidates": engine.last_candidates, "recent_decisions": store.list_decisions(limit=200)}

    @app.get("/api/performance")
    def perf(period: str = "all", mode: str | None = None, user: str = Depends(auth)):
        return {"paper": performance(store, "paper", period), "live": performance(store, "live", period)} \
            if mode is None else performance(store, mode, period)

    @app.get("/api/config")
    def config(user: str = Depends(auth)):
        return {"settings": settings.public_dict(), "risk_hash": settings.risk_config_hash(),
                "editable": sorted(RUNTIME_EDITABLE), "strategy_card": CARD.__dict__,
                "alert_channels": engine.alert.channels()}

    @app.get("/api/alerts")
    def alerts(user: str = Depends(auth)):
        return {"alerts": store.list_alerts(100), "audit": store.list_audit(100)}

    @app.post("/api/control/{action}")
    async def control(action: str, request: Request, user: str = Depends(auth), _: None = Depends(csrf)):
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        risk = engine.risk
        actor = f"dashboard:{user}"
        if action == "pause":
            risk.pause(actor)
            return {"ok": True, "message": "new entries paused"}
        if action == "resume":
            ok, msg = risk.resume(actor)
            return {"ok": ok, "message": msg}
        if action == "kill":
            import asyncio
            results = await asyncio.to_thread(engine.kill, actor, body.get("reason") or "dashboard kill switch")
            return {"ok": True, "message": "kill switch engaged", "cancel_results": results}
        if action == "clear-kill":
            if body.get("confirm") != "CLEAR KILL":
                return {"ok": False, "message": "type CLEAR KILL to confirm"}
            risk.clear_kill(actor)
            risk.pause(actor)
            return {"ok": True, "message": "kill switch cleared; entries remain PAUSED until you press Resume"}
        if action == "reset-risk-lock":
            if body.get("confirm") != "RESET":
                return {"ok": False, "message": "type RESET to confirm"}
            risk.reset_locks(actor, engine.last_snapshot)
            risk.pause(actor)
            return {"ok": True, "message": "risk locks reset; entries PAUSED - press Resume deliberately"}
        if action == "clear-halt":
            if body.get("confirm") != "CLEAR HALT":
                return {"ok": False, "message": "type CLEAR HALT to confirm"}
            risk.clear_halt(actor)
            return {"ok": True, "message": "halt cleared"}
        raise HTTPException(404, "unknown action")

    @app.post("/api/config")
    async def set_config(request: Request, user: str = Depends(auth), _: None = Depends(csrf)):
        body = await request.json()
        key, value, pw = body.get("key"), body.get("value"), body.get("password", "")
        assert settings.dashboard_password is not None
        if not secrets.compare_digest(str(pw).encode(), settings.dashboard_password.get_secret_value().encode()):
            raise HTTPException(403, "password re-entry required for configuration changes")
        if key not in RUNTIME_EDITABLE:
            raise HTTPException(400, f"{key} is not editable from the dashboard")
        caster = RUNTIME_EDITABLE[key]
        try:
            v = (str(value).lower() in ("1", "true", "yes", "on")) if caster is bool else caster(value)
            validated = Settings.model_validate({**settings.model_dump(), key: v})
        except Exception as e:
            raise HTTPException(400, f"invalid value: {e}")
        old = getattr(settings, key)
        object.__setattr__(settings, key, getattr(validated, key))
        overrides = store.get("config_overrides") or {}
        overrides[key] = getattr(validated, key)
        store.set("config_overrides", overrides)
        store.audit(f"dashboard:{user}", "config_change", {"key": key, "old": old, "new": getattr(validated, key)})
        note = " Live checklist must be re-confirmed (risk configuration changed)." if engine.mode == "live" else ""
        return {"ok": True, "message": f"{key}: {old} -> {getattr(validated, key)}.{note}"}

    @app.get("/api/export.csv")
    def export(mode: str | None = None, user: str = Depends(auth)):
        return PlainTextResponse(export_csv(store, mode), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=polybot-trades.csv"})

    return app
