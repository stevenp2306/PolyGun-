"""Persistence (SQLAlchemy Core).

SQLite by default; set ``POLYBOT_DATABASE_URL=postgresql+psycopg://...`` and
install the ``postgres`` extra to move to PostgreSQL. The schema uses only
portable column types.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

orders = Table(
    "orders", metadata,
    Column("id", String(64), primary_key=True),
    Column("mode", String(8), nullable=False, index=True),
    Column("exchange_order_id", String(128), index=True),
    Column("token_id", String(128), nullable=False, index=True),
    Column("market_id", String(64)),
    Column("event_id", String(64)),
    Column("side", String(4), nullable=False),
    Column("order_type", String(4), nullable=False),
    Column("purpose", String(8), nullable=False),
    Column("strategy", String(64)),
    Column("limit_price", Float, nullable=False),
    Column("size", Float, nullable=False),
    Column("filled_size", Float, nullable=False, default=0.0),
    Column("avg_fill_price", Float),
    Column("fees", Float, nullable=False, default=0.0),
    Column("status", String(16), nullable=False, index=True),
    Column("reason", Text),
    Column("error", Text),
    Column("expires_at", Float),
    Column("activate_at", Float),  # paper: earliest time it can interact with the book
    Column("signed_order", Text),  # live: serialized signed order (never contains the key)
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("meta", JSON),
)

fills = Table(
    "fills", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("mode", String(8), nullable=False, index=True),
    Column("order_id", String(64), index=True),
    Column("trade_id", String(128)),
    Column("token_id", String(128), nullable=False, index=True),
    Column("side", String(4), nullable=False),
    Column("price", Float, nullable=False),
    Column("size", Float, nullable=False),
    Column("fee", Float, nullable=False),
    Column("ts", Float, nullable=False),
    Column("settlement_status", String(32)),
)

positions = Table(
    "positions", metadata,
    Column("mode", String(8), primary_key=True),
    Column("token_id", String(128), primary_key=True),
    Column("market_id", String(64)),
    Column("event_id", String(64)),
    Column("event_title", Text),
    Column("outcome_label", String(128)),
    Column("question", Text),
    Column("correlation_key", String(128)),
    Column("shares", Float, nullable=False),
    Column("cost_basis", Float, nullable=False),  # incl. entry fees, of the shares still held
    Column("realized_pnl", Float, nullable=False, default=0.0),
    Column("fees_paid", Float, nullable=False, default=0.0),
    Column("opened_at", Float),
    Column("closed_at", Float),
    Column("status", String(16), nullable=False),  # OPEN / CLOSED / SETTLED
    Column("strategy", String(64)),
    Column("entry_reason", Text),
    Column("exit_plan", Text),
    Column("peak_pnl_pct", Float),
    Column("game_start_time", Float),
    Column("end_date", Float),
    Column("updated_at", Float),
)

cash_flows = Table(
    "cash_flows", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("mode", String(8), nullable=False),
    Column("kind", String(16), nullable=False),  # deposit / withdrawal / initial / settlement
    Column("amount", Float, nullable=False),
    Column("ts", Float, nullable=False),
    Column("note", Text),
)

decisions = Table(
    "decisions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False, index=True),
    Column("mode", String(8)),
    Column("strategy", String(64)),
    Column("market_id", String(64)),
    Column("event_id", String(64)),
    Column("token_id", String(128), index=True),
    Column("label", Text),
    Column("decision", String(8), nullable=False),  # TRADE / SKIP / EXIT / HOLD
    Column("reasons", JSON),
    Column("numbers", JSON),
)

alerts = Table(
    "alerts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False, index=True),
    Column("level", String(8), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("message", Text, nullable=False),
    Column("delivered", JSON),
)

audit = Table(
    "audit", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False, index=True),
    Column("actor", String(32), nullable=False),
    Column("action", String(64), nullable=False),
    Column("details", JSON),
)

kv = Table(
    "kv", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", JSON),
    Column("updated_at", Float),
)

equity_snapshots = Table(
    "equity_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("mode", String(8), nullable=False, index=True),
    Column("ts", Float, nullable=False, index=True),
    Column("cash", Float),
    Column("equity", Float),
    Column("exposure", Float),
    Column("net_flows", Float),
)

markets = Table(
    "markets", metadata,
    Column("market_id", String(64), primary_key=True),
    Column("token_ids", JSON),
    Column("question", Text),
    Column("event_title", Text),
    Column("category", String(64)),
    Column("eligible", Boolean),
    Column("reasons", JSON),
    Column("snapshot", JSON),
    Column("updated_at", Float),
)


class Store:
    def __init__(self, url: str):
        if url.startswith("sqlite:///"):
            path = Path(url.removeprefix("sqlite:///"))
            if str(path) != ":memory:":
                path.parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if url in ("sqlite://", "sqlite:///:memory:"):
            from sqlalchemy.pool import StaticPool
            kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, **kwargs)
        if url.startswith("sqlite:///") and "memory" not in url:
            with self.engine.begin() as c:
                c.exec_driver_sql("PRAGMA journal_mode=WAL")
        metadata.create_all(self.engine)

    # ---------------- key/value state ----------------
    def get(self, key: str, default: Any = None) -> Any:
        with self.engine.connect() as c:
            row = c.execute(select(kv.c.value).where(kv.c.key == key)).first()
        return row[0] if row else default

    def set(self, key: str, value: Any) -> None:
        now = time.time()
        with self.engine.begin() as c:
            if c.execute(select(kv.c.key).where(kv.c.key == key)).first():
                c.execute(update(kv).where(kv.c.key == key).values(value=value, updated_at=now))
            else:
                c.execute(insert(kv).values(key=key, value=value, updated_at=now))

    def delete_key(self, key: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(kv).where(kv.c.key == key))

    # ---------------- orders ----------------
    def insert_order(self, row: dict[str, Any]) -> str:
        row = dict(row)
        row.setdefault("id", uuid.uuid4().hex)
        now = time.time()
        row.setdefault("created_at", now)
        row.setdefault("updated_at", now)
        row.setdefault("filled_size", 0.0)
        row.setdefault("fees", 0.0)
        with self.engine.begin() as c:
            c.execute(insert(orders).values(**row))
        return row["id"]

    def update_order(self, order_id: str, **values: Any) -> None:
        values["updated_at"] = time.time()
        with self.engine.begin() as c:
            c.execute(update(orders).where(orders.c.id == order_id).values(**values))

    def get_order(self, order_id: str) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(select(orders).where(orders.c.id == order_id)).mappings().first()
        return dict(r) if r else None

    def find_order_by_exchange_id(self, exchange_id: str) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(select(orders).where(orders.c.exchange_order_id == exchange_id)).mappings().first()
        return dict(r) if r else None

    def list_orders(self, mode: str | None = None, statuses: list[str] | None = None,
                    limit: int = 500) -> list[dict]:
        q = select(orders).order_by(orders.c.created_at.desc()).limit(limit)
        if mode:
            q = q.where(orders.c.mode == mode)
        if statuses:
            q = q.where(orders.c.status.in_(statuses))
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]

    # ---------------- fills ----------------
    def insert_fill(self, row: dict[str, Any]) -> None:
        with self.engine.begin() as c:
            c.execute(insert(fills).values(**row))

    def fill_exists(self, mode: str, trade_id: str) -> bool:
        with self.engine.connect() as c:
            return c.execute(select(fills.c.id).where(fills.c.mode == mode, fills.c.trade_id == trade_id)).first() is not None

    def list_fills(self, mode: str | None = None, since: float | None = None, limit: int = 5000) -> list[dict]:
        q = select(fills).order_by(fills.c.ts.desc()).limit(limit)
        if mode:
            q = q.where(fills.c.mode == mode)
        if since:
            q = q.where(fills.c.ts >= since)
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]

    # ---------------- positions ----------------
    def upsert_position(self, row: dict[str, Any]) -> None:
        row = dict(row)
        row["updated_at"] = time.time()
        with self.engine.begin() as c:
            exists = c.execute(select(positions.c.token_id).where(
                positions.c.mode == row["mode"], positions.c.token_id == row["token_id"])).first()
            if exists:
                c.execute(update(positions).where(
                    positions.c.mode == row["mode"], positions.c.token_id == row["token_id"]).values(**row))
            else:
                c.execute(insert(positions).values(**row))

    def list_positions(self, mode: str, status: str | None = "OPEN") -> list[dict]:
        q = select(positions).where(positions.c.mode == mode)
        if status:
            q = q.where(positions.c.status == status)
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]

    def get_position(self, mode: str, token_id: str) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(select(positions).where(
                positions.c.mode == mode, positions.c.token_id == token_id)).mappings().first()
        return dict(r) if r else None

    # ---------------- cash flows ----------------
    def add_cash_flow(self, mode: str, kind: str, amount: float, note: str = "", ts: float | None = None) -> None:
        with self.engine.begin() as c:
            c.execute(insert(cash_flows).values(mode=mode, kind=kind, amount=amount,
                                                ts=ts or time.time(), note=note))

    def list_cash_flows(self, mode: str) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(
                select(cash_flows).where(cash_flows.c.mode == mode).order_by(cash_flows.c.ts)).mappings()]

    # ---------------- decisions / alerts / audit ----------------
    def add_decision(self, row: dict[str, Any]) -> None:
        with self.engine.begin() as c:
            c.execute(insert(decisions).values(**row))

    def list_decisions(self, limit: int = 300, since: float | None = None) -> list[dict]:
        q = select(decisions).order_by(decisions.c.ts.desc()).limit(limit)
        if since:
            q = q.where(decisions.c.ts >= since)
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]

    def prune_decisions(self, older_than: float) -> None:
        with self.engine.begin() as c:
            c.execute(delete(decisions).where(decisions.c.ts < older_than,
                                              decisions.c.decision == "SKIP"))

    def add_alert(self, level: str, kind: str, message: str, delivered: dict | None = None) -> None:
        with self.engine.begin() as c:
            c.execute(insert(alerts).values(ts=time.time(), level=level, kind=kind,
                                            message=message, delivered=delivered or {}))

    def list_alerts(self, limit: int = 100) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(
                select(alerts).order_by(alerts.c.ts.desc()).limit(limit)).mappings()]

    def audit(self, actor: str, action: str, details: dict | None = None) -> None:
        with self.engine.begin() as c:
            c.execute(insert(audit).values(ts=time.time(), actor=actor, action=action,
                                           details=json.loads(json.dumps(details or {}, default=str))))

    def list_audit(self, limit: int = 100) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(
                select(audit).order_by(audit.c.ts.desc()).limit(limit)).mappings()]

    # ---------------- equity ----------------
    def add_equity_snapshot(self, mode: str, cash: float, equity: float, exposure: float,
                            net_flows: float, ts: float | None = None) -> None:
        with self.engine.begin() as c:
            c.execute(insert(equity_snapshots).values(mode=mode, ts=ts or time.time(), cash=cash,
                                                      equity=equity, exposure=exposure, net_flows=net_flows))

    def list_equity(self, mode: str, since: float | None = None, limit: int = 20000) -> list[dict]:
        q = select(equity_snapshots).where(equity_snapshots.c.mode == mode).order_by(
            equity_snapshots.c.ts).limit(limit)
        if since:
            q = q.where(equity_snapshots.c.ts >= since)
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]

    # ---------------- markets ----------------
    def upsert_market(self, market_id: str, **values: Any) -> None:
        values["updated_at"] = time.time()
        values = json.loads(json.dumps(values, default=str))
        with self.engine.begin() as c:
            if c.execute(select(markets.c.market_id).where(markets.c.market_id == market_id)).first():
                c.execute(update(markets).where(markets.c.market_id == market_id).values(**values))
            else:
                c.execute(insert(markets).values(market_id=market_id, **values))

    def list_markets(self, eligible_only: bool = False, limit: int = 1000) -> list[dict]:
        q = select(markets).order_by(markets.c.updated_at.desc()).limit(limit)
        if eligible_only:
            q = q.where(markets.c.eligible.is_(True))
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]
