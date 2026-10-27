import base64
import logging

import pytest
from fastapi.testclient import TestClient

from polybot.alerts import Alerter
from polybot.config import Settings
from polybot.dashboard.app import create_app
from polybot.engine import Engine
from polybot.logging_setup import RedactingFilter, redact, register_secrets
from tests.conftest import NOW, FakeProvider, FakeSource, make_market, make_settings

PK = "0x" + "ab" * 32


def test_redaction_of_registered_secrets_and_patterns():
    register_secrets(["my-super-secret-passphrase"])
    text = f"key={PK} passphrase: my-super-secret-passphrase url?apiKey=XYZ123&x=1 bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = redact(text)
    assert PK not in out and "my-super-secret-passphrase" not in out and "XYZ123" not in out
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in out


def test_logging_filter_redacts_messages_and_tracebacks(caplog):
    logger = logging.getLogger("polybot.test")
    handler = logging.Handler()
    records = []
    handler.emit = records.append
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    try:
        logger.error("signing with %s", PK)
        try:
            raise ValueError(f"bad key {PK}")
        except ValueError:
            logger.exception("failure")
    finally:
        logger.removeHandler(handler)
    assert all(PK not in r.getMessage() for r in records)


def test_settings_never_expose_secret_values():
    s = make_settings(polymarket_private_key=PK, dashboard_password="hunter2hunter2")
    pub = s.public_dict()
    assert pub["polymarket_private_key"] == "set" and pub["dashboard_password"] == "set"
    assert PK not in repr(s) and PK not in str(pub)
    assert PK in s.secret_values()  # available only for redaction registration


def test_placeholder_secrets_are_treated_as_unset():
    s = make_settings(polymarket_private_key="<your-private-key>", odds_api_key="", team_aliases_path="")
    assert s.polymarket_private_key is None and s.odds_api_key is None and s.team_aliases_path is None


def test_env_example_loads_and_has_no_secret_values(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path
    from polybot.providers.odds_api import OddsApiProvider
    src = Path(__file__).resolve().parents[1] / ".env.example"
    shutil.copy(src, tmp_path / ".env")
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.mode.value == "paper" and not s.live_trading_enabled
    assert s.secret_values() == []
    OddsApiProvider(s)  # blank optional paths must not crash


def test_alerts_redact_and_do_not_leak(store):
    s = make_settings(polymarket_private_key=PK)
    register_secrets(s.secret_values())
    a = Alerter(s, store)
    a.send("test", f"oops {PK}", dedupe=False)
    assert PK not in store.list_alerts()[0]["message"]


# ------------------------------------------------------------------ dashboard
def client(store, frozen_time, password="correct-horse-battery", **kw):
    s = make_settings(dashboard_password=password, **kw) if password else make_settings(**kw)
    src = FakeSource([make_market()])
    eng = Engine(s, store, src, src, [FakeProvider({})])
    eng.discovery.refresh(NOW)
    eng.tick(NOW)
    return TestClient(create_app(eng, s)), eng, s


def hdr(user="admin", pw="correct-horse-battery", action=False):
    h = {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
    if action:
        h["X-Polybot-Action"] = "1"
    return h


def test_dashboard_disabled_without_password(store, frozen_time):
    c, *_ = client(store, frozen_time, password=None)
    assert c.get("/api/overview").status_code == 503
    assert c.get("/healthz").status_code == 200


def test_dashboard_requires_auth(store, frozen_time):
    c, *_ = client(store, frozen_time)
    assert c.get("/api/overview").status_code == 401
    assert c.get("/api/overview", headers=hdr(pw="wrong")).status_code == 401
    r = c.get("/api/overview", headers=hdr())
    assert r.status_code == 200 and r.json()["mode"] == "PAPER" and r.json()["simulated"]
    assert r.headers["X-Frame-Options"] == "DENY"


def test_dashboard_pages_render(store, frozen_time):
    c, *_ = client(store, frozen_time)
    for path in ("/", "/static/app.js", "/api/positions", "/api/orders", "/api/opportunities",
                 "/api/performance", "/api/config", "/api/alerts", "/api/export.csv"):
        assert c.get(path, headers=hdr()).status_code == 200, path
    cfg = c.get("/api/config", headers=hdr()).json()
    assert "correct-horse-battery" not in str(cfg)


def test_controls_need_csrf_header_and_confirmation(store, frozen_time):
    c, eng, s = client(store, frozen_time)
    assert c.post("/api/control/kill", headers=hdr()).status_code == 400  # no custom header
    r = c.post("/api/control/kill", headers=hdr(action=True), json={})
    assert r.status_code == 200 and eng.risk.kill_active()
    assert not c.post("/api/control/resume", headers=hdr(action=True)).json()["ok"]
    assert not c.post("/api/control/clear-kill", headers=hdr(action=True), json={"confirm": "x"}).json()["ok"]
    assert c.post("/api/control/clear-kill", headers=hdr(action=True), json={"confirm": "CLEAR KILL"}).json()["ok"]
    assert eng.risk.paused()  # stays paused until a deliberate resume
    assert c.post("/api/control/resume", headers=hdr(action=True)).json()["ok"]
    store.set("paper:risk_locks", {"daily_loss": {}})
    assert not c.post("/api/control/resume", headers=hdr(action=True)).json()["ok"]
    assert not c.post("/api/control/reset-risk-lock", headers=hdr(action=True), json={"confirm": "no"}).json()["ok"]
    assert c.post("/api/control/reset-risk-lock", headers=hdr(action=True), json={"confirm": "RESET"}).json()["ok"]
    assert eng.risk.locks() == {}


def test_config_change_protected_workflow(store, frozen_time):
    c, eng, s = client(store, frozen_time)
    url, h = "/api/config", hdr(action=True)
    assert c.post(url, headers=h, json={"key": "min_edge", "value": "0.05", "password": "bad"}).status_code == 403
    assert c.post(url, headers=h, json={"key": "mode", "value": "live",
                                        "password": "correct-horse-battery"}).status_code == 400
    assert c.post(url, headers=h, json={"key": "min_edge", "value": "-1",
                                        "password": "correct-horse-battery"}).status_code == 400
    old_hash = s.risk_config_hash()
    r = c.post(url, headers=h, json={"key": "min_edge", "value": "0.05", "password": "correct-horse-battery"})
    assert r.status_code == 200 and s.min_edge == 0.05
    assert s.risk_config_hash() != old_hash  # would invalidate a live checklist
    assert any(a["action"] == "config_change" for a in store.list_audit())


def test_healthz_reports_stale_engine(store, frozen_time):
    c, eng, s = client(store, frozen_time)
    assert c.get("/healthz").status_code == 200
    frozen_time["now"] += 10_000
    assert c.get("/healthz").status_code == 503
