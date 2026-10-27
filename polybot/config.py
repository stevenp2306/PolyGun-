"""Configuration.

All settings come from environment variables (prefix ``POLYBOT_``) or a local
``.env`` file, plus an optional secrets file (default ``secrets/polybot.env``)
created by ``polybot setup-credentials``. Every setting is documented in
``.env.example``.

IMPORTANT: every numeric threshold below is an *unvalidated research
assumption*. None of them has been shown to be profitable.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRETS_FILE = "secrets/polybot.env"


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class DevigMethod(str, Enum):
    MULTIPLICATIVE = "multiplicative"
    POWER = "power"
    SHIN = "shin"


class PreStartAction(str, Enum):
    HOLD = "hold"
    EXIT_IF_PROFITABLE = "exit_if_profitable"
    EXIT = "exit"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POLYBOT_",
        env_file=(".env", DEFAULT_SECRETS_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------- general ----------------
    mode: Mode = Mode.PAPER
    database_url: str = "sqlite:///data/polybot.db"
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    loop_interval_s: float = 5.0

    # ---------------- capital ----------------
    bankroll_usd: float = Field(500.0, gt=0)
    paper_starting_balance_usd: float = Field(500.0, gt=0)

    # ---------------- risk limits (examples, not advice) ----------------
    max_position_usd: float = Field(5.0, gt=0)
    max_position_pct: float = Field(0.01, gt=0, le=1)
    max_event_exposure_pct: float = Field(0.03, gt=0, le=1)
    max_total_exposure_pct: float = Field(0.20, gt=0, le=1)
    max_correlated_exposure_pct: float = Field(0.06, gt=0, le=1)
    daily_loss_limit_pct: float = Field(0.03, gt=0, le=1)
    max_drawdown_pct: float = Field(0.10, gt=0, le=1)
    max_open_positions: int = Field(10, ge=1)
    min_cash_reserve_usd: float = Field(20.0, ge=0)
    max_spread: float = Field(0.04, gt=0, lt=1)
    max_slippage: float = Field(0.01, ge=0, lt=1)
    max_book_age_s: float = Field(30.0, gt=0)
    max_fair_age_s: float = Field(900.0, gt=0)
    max_order_price: float = Field(0.90, gt=0, lt=1)
    min_order_price: float = Field(0.05, gt=0, lt=1)
    min_minutes_to_start: float = Field(30.0, ge=0)
    max_hours_to_resolution: float = Field(168.0, gt=0)
    max_depth_fraction: float = Field(0.5, gt=0, le=1)
    kelly_fraction: float = Field(0.10, gt=0, le=1)
    max_clock_skew_s: float = Field(2.0, gt=0)
    circuit_breaker_failures: int = Field(5, ge=1)
    circuit_breaker_cooldown_s: float = Field(300.0, gt=0)

    # ---------------- sports value strategy ----------------
    strategy_sports_value_enabled: bool = True
    min_edge: float = Field(0.03, ge=0, lt=1)
    uncertainty_z: float = Field(1.0, ge=0)
    min_uncertainty: float = Field(0.01, ge=0)
    min_bookmakers: int = Field(3, ge=1)
    max_bookmaker_disagreement: float = Field(0.04, gt=0)
    devig_method: DevigMethod = DevigMethod.POWER
    sharp_bookmakers: str = "pinnacle,betfair_ex_eu,matchbook"
    sharp_weight: float = Field(3.0, ge=1)
    odds_regions: str = "eu,uk"
    sports: str = "soccer_epl,soccer_uefa_champs_league,americanfootball_nfl"
    odds_poll_interval_s: float = Field(600.0, ge=60)
    odds_max_bookmaker_update_age_s: float = Field(1800.0, gt=0)
    match_start_tolerance_min: float = Field(30.0, ge=0)
    require_resolution_keywords: bool = True
    live_game_strategy_enabled: bool = False  # no reliable live-score feed configured
    momentum_enabled: bool = False  # research flag only; no momentum trading implemented

    # ---------------- exits ----------------
    take_profit_pct: float = Field(0.06, ge=0)
    exit_on_edge_loss: bool = True
    edge_exit_margin: float = Field(0.0, ge=0)
    trailing_enabled: bool = False
    trailing_activation_pct: float = Field(0.04, ge=0)
    trailing_distance_pct: float = Field(0.03, ge=0)
    max_holding_hours: float = Field(0.0, ge=0)  # 0 = no limit
    hold_to_resolution: bool = True
    stop_loss_pct: float = Field(0.0, ge=0)  # 0 = disabled
    min_exit_price: float = Field(0.02, ge=0, lt=1)
    pre_start_action: PreStartAction = PreStartAction.HOLD
    cancel_entries_before_start_min: float = Field(10.0, ge=0)

    # ---------------- paper trading ----------------
    paper_latency_ms: float = Field(750.0, ge=0)
    paper_depth_haircut: float = Field(0.5, gt=0, le=1)
    paper_slippage_ticks: int = Field(0, ge=0)
    paper_passive_fill_fraction: float = Field(0.5, ge=0, le=1)

    # ---------------- market data ----------------
    discovery_interval_s: float = Field(300.0, ge=30)
    book_poll_interval_s: float = Field(10.0, ge=1)
    use_websocket: bool = True
    min_market_liquidity_usd: float = Field(500.0, ge=0)
    max_markets_monitored: int = Field(200, ge=1)

    # ---------------- live trading (disabled by default) ----------------
    live_trading_enabled: bool = False
    live_checklist_path: Path = Path("data/live_checklist.json")
    polymarket_private_key: SecretStr | None = None
    polymarket_wallet_address: str | None = None
    polymarket_api_key: SecretStr | None = None
    polymarket_api_secret: SecretStr | None = None
    polymarket_api_passphrase: SecretStr | None = None
    use_order_heartbeat: bool = False
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    clob_url: str = "https://clob.polymarket.com"

    # ---------------- external data ----------------
    odds_api_key: SecretStr | None = None
    odds_api_base_url: str = "https://api.the-odds-api.com/v4"
    team_aliases_path: Path | None = None

    # ---------------- dashboard ----------------
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    dashboard_username: str = "admin"
    dashboard_password: SecretStr | None = None

    # ---------------- alerts ----------------
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    alert_webhook_url: SecretStr | None = None
    alert_cooldown_s: float = Field(600.0, ge=0)

    @field_validator(
        "polymarket_private_key",
        "polymarket_api_key",
        "polymarket_api_secret",
        "polymarket_api_passphrase",
        "odds_api_key",
        "dashboard_password",
        "telegram_bot_token",
        "alert_webhook_url",
        mode="before",
    )
    @classmethod
    def _blank_secret_is_none(cls, v: Any) -> Any:
        if isinstance(v, str) and (not v.strip() or v.strip().startswith("<")):
            return None  # empty or still a placeholder like <your-key>
        return v

    @field_validator("polymarket_wallet_address", "telegram_chat_id", "team_aliases_path", mode="before")
    @classmethod
    def _blank_is_none(cls, v: Any) -> Any:
        if isinstance(v, str) and (not v.strip() or v.strip().startswith("<")):
            return None
        return v

    @model_validator(mode="after")
    def _check_ranges(self) -> "Settings":
        if self.min_order_price >= self.max_order_price:
            raise ValueError("min_order_price must be below max_order_price")
        return self

    # ---------------- helpers ----------------
    @property
    def sports_list(self) -> list[str]:
        return [s.strip() for s in self.sports.split(",") if s.strip()]

    @property
    def sharp_bookmaker_set(self) -> set[str]:
        return {s.strip().lower() for s in self.sharp_bookmakers.split(",") if s.strip()}

    def secret_values(self) -> list[str]:
        """All configured secret values (used only to redact them from logs)."""
        out = []
        for name, value in self:
            if isinstance(value, SecretStr):
                raw = value.get_secret_value()
                if raw and len(raw) >= 6:
                    out.append(raw)
        return out

    def risk_config(self) -> dict[str, Any]:
        """The settings that the live checklist confirmation is bound to."""
        keys = [k for k in RISK_KEYS]
        return {k: getattr(self, k) for k in keys}

    def risk_config_hash(self) -> str:
        blob = json.dumps(
            {k: (v.value if isinstance(v, Enum) else v) for k, v in self.risk_config().items()},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def public_dict(self) -> dict[str, Any]:
        """Settings safe to show on the dashboard (secrets only as set/unset)."""
        out: dict[str, Any] = {}
        for name, value in self:
            if isinstance(value, SecretStr) or name in SECRET_FIELDS:
                out[name] = "set" if value else "not set"
            elif isinstance(value, Enum):
                out[name] = value.value
            elif isinstance(value, Path):
                out[name] = str(value)
            else:
                out[name] = value
        return out


SECRET_FIELDS = {
    "polymarket_private_key",
    "polymarket_api_key",
    "polymarket_api_secret",
    "polymarket_api_passphrase",
    "odds_api_key",
    "dashboard_password",
    "telegram_bot_token",
    "alert_webhook_url",
}

RISK_KEYS = [
    "bankroll_usd",
    "max_position_usd",
    "max_position_pct",
    "max_event_exposure_pct",
    "max_total_exposure_pct",
    "max_correlated_exposure_pct",
    "daily_loss_limit_pct",
    "max_drawdown_pct",
    "max_open_positions",
    "min_cash_reserve_usd",
    "max_spread",
    "max_slippage",
    "max_book_age_s",
    "max_fair_age_s",
    "max_order_price",
    "min_order_price",
    "min_minutes_to_start",
    "max_hours_to_resolution",
    "max_depth_fraction",
    "kelly_fraction",
    "min_edge",
    "uncertainty_z",
    "min_uncertainty",
    "min_bookmakers",
    "take_profit_pct",
    "stop_loss_pct",
    "use_order_heartbeat",
]

# Settings the dashboard may change at runtime (protected workflow). Anything
# else requires editing .env and restarting. Changing any of these alters the
# risk-config hash and therefore invalidates the live checklist.
RUNTIME_EDITABLE = {
    "max_position_usd": float,
    "max_position_pct": float,
    "max_event_exposure_pct": float,
    "max_total_exposure_pct": float,
    "daily_loss_limit_pct": float,
    "max_drawdown_pct": float,
    "max_open_positions": int,
    "min_edge": float,
    "take_profit_pct": float,
    "trailing_enabled": bool,
    "exit_on_edge_loss": bool,
}


def load_settings(**overrides: Any) -> Settings:
    return Settings(**overrides)
