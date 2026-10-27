"""Performance reporting and CSV export (paper and live kept strictly separate)."""

from __future__ import annotations

import csv
import io
import time
from collections import defaultdict

from polybot.backtest.stats import max_drawdown, trade_stats
from polybot.db import Store

PERIODS = {"1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "all": None}


def performance(store: Store, mode: str, period: str = "all") -> dict:
    span = PERIODS.get(period)
    since = time.time() - span if span else None
    closed = [p for p in store.list_positions(mode, status=None)
              if p["status"] in ("CLOSED", "SETTLED") and (since is None or (p.get("closed_at") or 0) >= since)]
    pnls = [p["realized_pnl"] for p in closed]
    by_strategy: dict[str, list[float]] = defaultdict(list)
    by_sport: dict[str, list[float]] = defaultdict(list)
    for p in closed:
        by_strategy[p.get("strategy") or "unknown"].append(p["realized_pnl"])
        by_sport[(p.get("correlation_key") or "unknown").split(":")[0]].append(p["realized_pnl"])
    fills = store.list_fills(mode, since=since, limit=1_000_000)
    eq = store.list_equity(mode, since=since)
    equity_series = [e["equity"] - e["net_flows"] for e in eq]
    mdd, _ = max_drawdown(equity_series) if equity_series else (0.0, 0.0)
    flows = sum(f["amount"] for f in store.list_cash_flows(mode))
    total_ret = None
    if eq and flows > 0:
        total_ret = (eq[-1]["equity"] - eq[-1]["net_flows"]) / flows if span is None else \
            ((eq[-1]["equity"] - eq[-1]["net_flows"]) - (eq[0]["equity"] - eq[0]["net_flows"])) / flows
    return {
        "mode": mode,
        "simulated": mode == "paper",
        "period": period,
        **trade_stats(pnls),
        "fees_paid": round(sum(f["fee"] for f in fills), 4),
        "n_fills": len(fills),
        "n_events": len({p.get("event_id") for p in closed}),
        "max_drawdown_usd": round(mdd, 4),
        "total_return": None if total_ret is None else round(total_ret, 5),
        "by_strategy": {k: trade_stats(v) for k, v in by_strategy.items()},
        "by_sport": {k: trade_stats(v) for k, v in by_sport.items()},
        "equity_curve": [{"ts": e["ts"], "trading_pnl": round(e["equity"] - e["net_flows"], 4)}
                         for e in eq[-500:]],
        "note": ("SIMULATED results from paper trading; they do not reproduce queue position, market "
                 "impact, missed fills or settlement problems." if mode == "paper" else
                 "Live results reconstructed from exchange trade records."),
    }


def export_csv(store: Store, mode: str | None = None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["record", "mode", "simulated", "time_utc", "order_id", "exchange_order_id", "trade_id",
                "token_id", "market_id", "event_id", "side", "type", "purpose", "status", "limit_price",
                "size", "filled_size", "fill_price", "fee", "reason", "error"])
    fmt = lambda ts: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) if ts else ""
    for o in reversed(store.list_orders(mode, limit=1_000_000)):
        w.writerow(["order", o["mode"], o["mode"] == "paper", fmt(o["created_at"]), o["id"],
                    o.get("exchange_order_id"), "", o["token_id"], o.get("market_id"), o.get("event_id"),
                    o["side"], o["order_type"], o["purpose"], o["status"], o["limit_price"], o["size"],
                    o["filled_size"], o.get("avg_fill_price"), o["fees"], o.get("reason"), o.get("error")])
    for f in reversed(store.list_fills(mode, limit=1_000_000)):
        w.writerow(["fill", f["mode"], f["mode"] == "paper", fmt(f["ts"]), f["order_id"], "", f["trade_id"],
                    f["token_id"], "", "", f["side"], "", "", f.get("settlement_status"), "", f["size"], "",
                    f["price"], f["fee"], "", ""])
    return buf.getvalue()
