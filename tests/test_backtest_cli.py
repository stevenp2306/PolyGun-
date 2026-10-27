import json
import random

import pytest

from polybot.backtest.data import ResearchRecorder, load_resolutions, load_snapshots
from polybot.backtest.engine import chronological_split, simulate
from polybot.backtest.report import build_report
from polybot.backtest.stats import calibration, cluster_bootstrap_ci, max_drawdown
from polybot.cli import main
from tests.conftest import NOW, make_book, make_fair, make_market, make_settings


def write_synthetic(dirpath, n_events=30, seed=1, edge=0.0):
    """SYNTHETIC data for mechanics tests only - not evidence of anything."""
    rng = random.Random(seed)
    rec = ResearchRecorder(dirpath, min_interval_s=0)
    for i in range(n_events):
        t0 = NOW + i * 86400
        m = make_market(mid=f"G{i}", now=t0)
        p = rng.uniform(0.3, 0.7)
        ask = round(min(0.95, max(0.05, p - edge)), 2)
        book = make_book(f"G{i}-YES", [(ask - 0.02, 200)], [(ask, 200)], now=t0)
        rec.record(m, f"G{i}-YES", make_fair(f"G{i}-YES", p, now=t0), book, now=t0)
        rec.record_resolution(f"G{i}-YES", 1.0 if rng.random() < p else 0.0, now=t0 + 2 * 86400)


def test_stats_helpers():
    assert max_drawdown([100, 120, 90, 130])[0] == 30
    ci = cluster_bootstrap_ci({"a": 1.0, "b": -1.0, "c": 0.5, "d": 0.2})
    assert ci["n_events"] == 4 and ci["ci_mean"][0] <= ci["mean"] <= ci["ci_mean"][1]
    cal = calibration([(0.9, 1), (0.1, 0), (0.8, 1)])
    assert cal["brier"] < 0.05


def test_record_load_roundtrip_and_no_outcome_in_snapshots(tmp_path):
    write_synthetic(tmp_path, 5)
    snaps = load_snapshots(tmp_path)
    assert len(snaps) == 5 and snaps[0].fair.prob > 0
    raw = (tmp_path / next(p.name for p in tmp_path.glob("snapshots-*"))).read_text()
    assert "payout" not in raw  # outcomes are stored separately
    assert len(load_resolutions(tmp_path)) == 5


def test_decisions_do_not_depend_on_outcomes(tmp_path):
    write_synthetic(tmp_path, 20, edge=0.1)
    snaps = load_snapshots(tmp_path)
    res = load_resolutions(tmp_path)
    s = make_settings(min_minutes_to_start=0)
    a = simulate("x", snaps, res, s, use_exits=False)
    flipped = {k: (1 - v[0], v[1]) for k, v in res.items()}
    b = simulate("x", snaps, flipped, s, use_exits=False)
    assert [t.token_id for t in a.trades] == [t.token_id for t in b.trades]  # same entries, different P&L
    assert a.trades and sum(t.pnl for t in a.trades) != sum(t.pnl for t in b.trades)


def test_chronological_split_preserves_order(tmp_path):
    write_synthetic(tmp_path, 20)
    sp = chronological_split(load_snapshots(tmp_path))
    last_dev = max(s.ts for s in sp["development"])
    assert all(s.ts > last_dev for s in sp["test"])


def test_report_with_no_data_says_not_enough_evidence(tmp_path):
    md, data = build_report(tmp_path / "empty", make_settings())
    assert data["verdict"] == "B" and "NOT enough evidence" in md


def test_report_with_small_synthetic_sample_is_still_B(tmp_path):
    write_synthetic(tmp_path, 40, edge=0.1)
    md, data = build_report(tmp_path, make_settings(min_minutes_to_start=0))
    assert data["verdict"] == "B"  # too few events for the pre-registered threshold


def test_cli_kill_pause_status_and_selftest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POLYBOT_DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    assert main(["pause"]) == 0
    assert main(["kill", "--reason", "test"]) == 0
    assert main(["status"]) == 0
    out = json.loads(capsys.readouterr().out.split("New entries paused.\n")[-1].split("loop (~5s).")[-1].split("\n", 1)[-1])
    assert out["kill_switch"]["active"] and out["paused"]["active"]
    assert main(["resume"]) == 1  # refused while kill switch is active
    assert main(["test-kill-switch"]) == 0
    assert main(["record-flow", "deposit", "25"]) == 0


def test_setup_credentials_writes_0600_and_refuses_seed_phrase(tmp_path, monkeypatch):
    import os
    answers = iter(["word " * 12, "0x" + "22" * 20])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    f = tmp_path / "secrets" / "x.env"
    assert main(["setup-credentials", "--file", str(f), "--only", "POLYBOT_POLYMARKET_PRIVATE_KEY",
                 "POLYBOT_POLYMARKET_WALLET_ADDRESS"]) == 0
    content = f.read_text()
    assert "PRIVATE_KEY" not in content and "WALLET_ADDRESS=0x22" in content
    assert oct(os.stat(f).st_mode & 0o777) == "0o600"


def test_check_config_runs_without_secrets(capsys):
    assert main(["check-config"]) == 0
    out = capsys.readouterr().out
    assert "No POLYBOT_ODDS_API_KEY" in out and "not set" in out
