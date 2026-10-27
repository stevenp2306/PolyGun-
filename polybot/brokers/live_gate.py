"""The live-trading gate and setup checklist.

Live orders are possible ONLY when every one of these is true:

1. ``POLYBOT_MODE=live`` and ``POLYBOT_LIVE_TRADING_ENABLED=true`` (explicit local action).
2. The checklist file exists, every item is confirmed, and it was confirmed
   against the *current* risk configuration (hash match). Changing any risk
   setting invalidates the confirmation.
3. The most recent geoblock check says this location is not blocked, and the
   account is not in close-only mode.

Nothing in the checklist places an order.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polybot.config import Mode, Settings

CHECKLIST_ITEMS: dict[str, str] = {
    "jurisdiction_eligible": "I confirm I am legally allowed to use Polymarket's trading service from "
                             "my location and account; I will not use a VPN or misstate my location.",
    "geoblock_check_passed": "(automatic) Polymarket geoblock endpoint reports this server is not blocked.",
    "credentials_verified": "(automatic) Signer/wallet configured; API credentials accepted; secrets not printed.",
    "balance_and_permissions": "(automatic) Collateral balance readable, trading approvals complete, "
                               "account not in close-only mode.",
    "risk_limits_confirmed": "I reviewed the bankroll and every risk limit shown and accept them.",
    "kill_switch_tested": "(automatic) Kill switch blocks order submission and the cancel path works.",
    "reconciliation_tested": "(automatic) Read-only reconciliation against the exchange succeeded.",
    "no_profit_guarantee": "I understand there is no evidence yet of a profitable edge and I may lose "
                           "all money allocated to the bot.",
}

ENABLE_PHRASE = "ENABLE LIVE TRADING"


@dataclass
class GateResult:
    allowed: bool
    failures: list[str] = field(default_factory=list)


class LiveGate:
    def __init__(self, settings: Settings):
        self.s = settings
        self.geoblock: dict[str, Any] | None = None
        self.closed_only: bool | None = None

    def load(self) -> dict:
        p = Path(self.s.live_checklist_path)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, data: dict) -> None:
        p = Path(self.s.live_checklist_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        os.replace(tmp, p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass

    def check(self) -> GateResult:
        f: list[str] = []
        if self.s.mode != Mode.LIVE:
            f.append("POLYBOT_MODE is not 'live' (PAPER mode never sends real orders)")
        if not self.s.live_trading_enabled:
            f.append("POLYBOT_LIVE_TRADING_ENABLED is not true")
        data = self.load()
        items = data.get("items", {})
        for key in CHECKLIST_ITEMS:
            if not (items.get(key) or {}).get("confirmed"):
                f.append(f"checklist item not confirmed: {key}")
        if data and data.get("risk_config_hash") != self.s.risk_config_hash():
            f.append("risk configuration changed since the checklist was confirmed: re-run "
                     "`polybot live-checklist`")
        if not data.get("enabled_phrase_confirmed"):
            f.append(f"explicit enable step not completed (type '{ENABLE_PHRASE}' in the checklist)")
        if self.s.polymarket_private_key is None or not self.s.polymarket_wallet_address:
            f.append("signer key / wallet address not configured")
        if self.geoblock is None:
            f.append("geoblock status not checked yet (live orders wait for a successful check)")
        elif self.geoblock.get("blocked") is not False:
            f.append(f"geoblock: trading not permitted from this location "
                     f"({self.geoblock.get('country', '?')}); live trading stopped")
        elif time.time() - float(self.geoblock.get("checked_at", 0)) > 3 * 3600:
            f.append("geoblock check is more than 3h old")
        if self.closed_only:
            f.append("account is in close-only mode: no new positions allowed")
        return GateResult(not f, f)


async def fetch_geoblock(url: str, timeout: float = 10.0) -> dict[str, Any]:
    """Official endpoint: GET https://polymarket.com/api/geoblock -> {blocked, ip, country, region}."""
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(url)
        r.raise_for_status()
        data = r.json()
    # Never keep the IP address around longer than needed.
    return {"blocked": data.get("blocked"), "country": data.get("country"),
            "region": data.get("region"), "checked_at": time.time()}
