"""Evidence report: says either 'preliminary evidence' or 'not enough evidence'.

The verdict rules are fixed in advance (not tuned to make results look good):

A. "Preliminary evidence of an edge" requires ALL of:
   - at least MIN_TEST_EVENTS independent events traded in the untouched test block,
   - the 95% event-clustered bootstrap CI of mean P&L per event in the test block
     lies entirely above zero,
   - the validation block's mean P&L per event is also positive,
   - the model's Brier score beats the Polymarket mid on the same tokens.
B. Otherwise: "Not enough evidence to claim an edge".
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from polybot.backtest.data import load_resolutions, load_snapshots
from polybot.backtest.engine import chronological_split, market_vs_model_calibration, simulate, walk_forward
from polybot.config import Settings

MIN_TEST_EVENTS = 100


def build_report(research_dir: Path, settings: Settings) -> tuple[str, dict]:
    snaps = load_snapshots(research_dir)
    res = load_resolutions(research_dir)
    data: dict = {"generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
                  "snapshots": len(snaps), "tokens_resolved": len(res),
                  "events": len({s.event_id for s in snaps}), "settings_hash": settings.risk_config_hash()}
    splits = chronological_split(snaps) if snaps else {"development": [], "validation": [], "test": []}
    results = {}
    for variant, use_exits in (("hold_to_resolution", False), ("configured_exits", True)):
        results[variant] = {k: simulate(k, v, res, settings, use_exits).summary(settings.bankroll_usd)
                            for k, v in splits.items()}
    data["results"] = results
    data["walk_forward_hold"] = walk_forward(snaps, res, settings, use_exits=False)
    data["calibration"] = market_vs_model_calibration(snaps, res) if snaps else {}
    verdict, why = verdict_for(data)
    data["verdict"], data["verdict_reasons"] = verdict, why
    return render(data), data


def verdict_for(data: dict) -> tuple[str, list[str]]:
    why = []
    test = data["results"]["hold_to_resolution"]["test"]
    val = data["results"]["hold_to_resolution"]["validation"]
    boot = test.get("event_bootstrap") or {}
    if test.get("events_traded", 0) < MIN_TEST_EVENTS:
        why.append(f"only {test.get('events_traded', 0)} events traded in the untouched test block "
                   f"(need >= {MIN_TEST_EVENTS})")
    ci = boot.get("ci_mean")
    if not ci or ci[0] <= 0:
        why.append("test-block 95% CI of mean P&L per event does not exclude zero" if ci else
                   "no confidence interval (too few events)")
    vb = (val.get("event_bootstrap") or {}).get("mean")
    if vb is None or vb <= 0:
        why.append("validation block mean P&L per event is not positive")
    cal = data.get("calibration") or {}
    mb = (cal.get("model") or {}).get("brier")
    kb = (cal.get("market_mid") or {}).get("brier")
    if mb is None or kb is None or mb >= kb:
        why.append("model is not shown to be better calibrated than the Polymarket mid")
    if why:
        return "B", why
    return "A", ["all pre-registered criteria met (still preliminary; past results do not guarantee future profit)"]


def render(d: dict) -> str:
    v = d["verdict"]
    head = ("**B. There is NOT enough evidence to claim an edge.**" if v == "B" else
            "**A. There is some PRELIMINARY evidence of an edge under the stated tests** (see limitations).")
    lines = [
        "# Evidence report", "", f"Generated: {d['generated_at']}  ", f"Settings hash: `{d['settings_hash']}`", "",
        "## Verdict", "", head, "", *[f"- {r}" for r in d["verdict_reasons"]], "",
        "## Data used", "",
        f"- Snapshots: {d['snapshots']}", f"- Distinct events: {d['events']}",
        f"- Tokens with recorded resolution: {d['tokens_resolved']}", "",
        "Data source: point-in-time snapshots recorded by this bot while running (fair estimate + "
        "order book at that moment). No third-party historical order-book dataset was used; none was "
        "available when this project was built.", "",
        "## Results (chronological blocks; thresholds NOT tuned on any block)", "",
    ]
    for variant, blocks in d["results"].items():
        lines += [f"### {variant}", "", "| block | events traded | trades | net P&L | win rate | profit factor | "
                  "fees | max DD | 95% CI mean P&L/event |", "|---|---|---|---|---|---|---|---|---|"]
        for name, r in blocks.items():
            ci = (r.get("event_bootstrap") or {}).get("ci_mean")
            lines.append(f"| {name} | {r['events_traded']} | {r['n_trades']} | {r['net_pnl']} | {r['win_rate']} | "
                         f"{r['profit_factor']} | {r['fees']} | {r['max_drawdown_usd']} | {ci} |")
        lines.append("")
    lines += ["Benchmark: not trading = $0.00 P&L with zero risk.", "",
              "## Walk-forward (hold to resolution)", ""]
    if d["walk_forward_hold"]:
        for f in d["walk_forward_hold"]:
            lines.append(f"- {f['name']}: events {f['events_traded']}, net P&L {f['net_pnl']}")
    else:
        lines.append("- Not enough events for walk-forward folds (need >= 50).")
    cal = d.get("calibration") or {}
    lines += ["", "## Calibration: independent model vs Polymarket mid (first pre-match snapshot per token)", "",
              f"- Model: {json.dumps({k: v for k, v in (cal.get('model') or {}).items() if k != 'table'})}",
              f"- Market mid: {json.dumps({k: v for k, v in (cal.get('market_mid') or {}).items() if k != 'table'})}",
              "", "## Limitations", "",
              "- Snapshots are taken at most every few minutes; fills between snapshots are not modelled.",
              "- Latency, queue position, market impact and settlement failures are not reproduced.",
              "- Fees use the market's published schedule at snapshot time.",
              "- Trades within one game are correlated; uncertainty is computed per event.",
              "- Good past or simulated performance does not guarantee future profit.", ""]
    return "\n".join(lines)
