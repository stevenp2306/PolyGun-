"""Research data: recording point-in-time snapshots and loading them for backtests.

Format (JSON lines), one record per (token, time):

    snapshots-*.jsonl : {"ts", "event_id", "market_id", "token_id", "label",
                         "game_start_time", "fair_prob", "fair_uncertainty",
                         "fair_fetched_at", "fair_source", "n_sources",
                         "bids": [[p, s], ...], "asks": [[p, s], ...],
                         "tick_size", "min_order_size", "fee": {rate, exponent, taker_only}}
    resolutions.jsonl : {"token_id", "payout", "resolved_at"}

Outcomes are stored SEPARATELY from snapshots and are only joined after a
simulated decision has been made, which prevents look-ahead leakage.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from polybot.domain import FairEstimate, FeeSchedule, Level, MarketInfo, OrderBook


class ResearchRecorder:
    def __init__(self, directory: Path, min_interval_s: float = 300.0):
        self.dir = Path(directory)
        self.min_interval_s = min_interval_s
        self._last: dict[str, float] = {}

    def record(self, market: MarketInfo, token_id: str, fair: FairEstimate | None, book: OrderBook | None,
               now: float | None = None) -> None:
        now = now or time.time()
        if fair is None or book is None or now - self._last.get(token_id, 0) < self.min_interval_s:
            return
        self._last[token_id] = now
        self.dir.mkdir(parents=True, exist_ok=True)
        tok = market.token(token_id)
        rec = {
            "ts": now, "event_id": market.event_id, "market_id": market.market_id, "token_id": token_id,
            "label": f"{market.event_title or market.question} :: {tok.label if tok else '?'}",
            "game_start_time": market.game_start_time, "fair_prob": fair.prob,
            "fair_uncertainty": fair.uncertainty, "fair_fetched_at": fair.fetched_at,
            "fair_source": fair.source, "n_sources": fair.n_sources,
            "bids": [[l.price, l.size] for l in book.bids[:15]],
            "asks": [[l.price, l.size] for l in book.asks[:15]],
            "tick_size": book.tick_size, "min_order_size": book.min_order_size,
            "fee": None if market.fee is None else vars(market.fee),
        }
        day = time.strftime("%Y%m%d", time.gmtime(now))
        with open(self.dir / f"snapshots-{day}.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")

    def record_resolution(self, token_id: str, payout: float, now: float | None = None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "resolutions.jsonl", "a") as f:
            f.write(json.dumps({"token_id": token_id, "payout": payout, "resolved_at": now or time.time()}) + "\n")


@dataclass
class Snapshot:
    ts: float
    event_id: str
    market_id: str
    token_id: str
    label: str
    game_start_time: float | None
    fair: FairEstimate
    book: OrderBook
    fee: FeeSchedule | None


def load_snapshots(directory: Path) -> list[Snapshot]:
    out: list[Snapshot] = []
    for p in sorted(Path(directory).glob("snapshots-*.jsonl")):
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            fee = FeeSchedule(**r["fee"]) if r.get("fee") else None
            out.append(Snapshot(
                ts=r["ts"], event_id=r.get("event_id") or r["market_id"], market_id=r["market_id"],
                token_id=r["token_id"], label=r.get("label", ""), game_start_time=r.get("game_start_time"),
                fair=FairEstimate(r["token_id"], r["fair_prob"], r["fair_uncertainty"],
                                  r.get("fair_source", "?"), r.get("n_sources", 0),
                                  r.get("fair_fetched_at", r["ts"]), r.get("fair_fetched_at", r["ts"])),
                book=OrderBook(r["token_id"], [Level(p, s) for p, s in r["bids"]],
                               [Level(p, s) for p, s in r["asks"]], received_at=r["ts"],
                               tick_size=r.get("tick_size", 0.01), min_order_size=r.get("min_order_size", 5)),
                fee=fee,
            ))
    out.sort(key=lambda s: s.ts)
    return out


def load_resolutions(directory: Path) -> dict[str, tuple[float, float]]:
    p = Path(directory) / "resolutions.jsonl"
    out: dict[str, tuple[float, float]] = {}
    if p.exists():
        for line in p.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                out[r["token_id"]] = (float(r["payout"]), float(r["resolved_at"]))
    return out


def iter_events(snaps: list[Snapshot]) -> Iterator[str]:
    seen = set()
    for s in snaps:
        if s.event_id not in seen:
            seen.add(s.event_id)
            yield s.event_id
