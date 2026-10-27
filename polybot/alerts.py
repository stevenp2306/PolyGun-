"""Alerts: always logged + stored; optionally Telegram and/or a generic webhook.

Messages never include credentials (they pass through the redactor) and keep
account details to a minimum (no wallet address, no balances beyond P&L).
The bot cannot alert you about its own crash: use an external uptime monitor
on /healthz (see docs/DEPLOYMENT.md).
"""

from __future__ import annotations

import logging
import time

import httpx

from polybot.config import Settings
from polybot.db import Store
from polybot.logging_setup import redact

log = logging.getLogger("polybot.alerts")

LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR,
          "critical": logging.CRITICAL}


class Alerter:
    def __init__(self, settings: Settings, store: Store, http: httpx.Client | None = None):
        self.s = settings
        self.store = store
        self.http = http
        self._last: dict[str, float] = {}

    def channels(self) -> list[str]:
        ch = ["log", "dashboard"]
        if self.s.telegram_bot_token and self.s.telegram_chat_id:
            ch.append("telegram")
        if self.s.alert_webhook_url:
            ch.append("webhook")
        return ch

    def __call__(self, kind: str, message: str, level: str = "warning", dedupe: bool = True) -> None:
        self.send(kind, message, level, dedupe)

    def send(self, kind: str, message: str, level: str = "warning", dedupe: bool = True) -> None:
        now = time.time()
        msg = redact(f"[polybot {self.s.mode.value.upper()}] {message}")
        key = f"{kind}:{message[:80]}"
        if dedupe and now - self._last.get(key, 0) < self.s.alert_cooldown_s:
            return
        self._last[key] = now
        log.log(LEVELS.get(level, logging.WARNING), "ALERT %s: %s", kind, msg)
        delivered = {"log": True}
        if self.s.telegram_bot_token and self.s.telegram_chat_id:
            delivered["telegram"] = self._post(
                f"https://api.telegram.org/bot{self.s.telegram_bot_token.get_secret_value()}/sendMessage",
                {"chat_id": self.s.telegram_chat_id, "text": msg})
        if self.s.alert_webhook_url:
            delivered["webhook"] = self._post(self.s.alert_webhook_url.get_secret_value(),
                                              {"text": msg, "kind": kind, "level": level})
        try:
            self.store.add_alert(level, kind, msg, delivered)
        except Exception:  # never let alert storage break trading logic
            log.exception("could not store alert")

    def _post(self, url: str, payload: dict) -> bool:
        try:
            client = self.http or httpx.Client(timeout=10)
            try:
                r = client.post(url, json=payload)
                return r.status_code < 300
            finally:
                if self.http is None:
                    client.close()
        except Exception as e:
            log.warning("alert delivery failed: %s", type(e).__name__)  # no URL: it may embed a token
            return False
