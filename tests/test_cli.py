"""Tests for polytrage.cli.

Guarded with pytest.importorskip inside individual tests (not at module
level, which would skip the whole file) so this file passes standalone even
before data/engine/backtest/optimize exist -- teammates build those in
parallel to CONTRACTS.md.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from polytrage import cli
from polytrage.models import ArbWindow, Event

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# --- parser shape (no other modules needed) ------------------------------

def test_parser_recognizes_all_subcommands():
    parser = cli._build_parser()
    assert parser.parse_args(["report"]).command == "report"
    assert parser.parse_args(["scan", "--fixtures"]).command == "scan"
    assert parser.parse_args(["backtest", "--fixtures"]).command == "backtest"
    assert parser.parse_args(["optimize", "--fixtures"]).command == "optimize"


def test_main_requires_a_subcommand():
    with pytest.raises(SystemExit):
        cli.main([])


@pytest.mark.parametrize("command", ["scan", "backtest", "optimize"])
def test_source_commands_require_slug_or_fixtures(command):
    with pytest.raises(SystemExit) as exc_info:
        cli.main([command])
    assert exc_info.value.code == 2


# --- pure formatting helpers (no other modules needed) --------------------

def test_format_windows_table_empty():
    event = Event(id="1", slug="x", title="Test Event", markets=())
    assert "no arbitrage windows found" in cli._format_windows_table(event, [])


def test_format_windows_table_with_window():
    event = Event(id="1", slug="x", title="Test Event", markets=())
    window = ArbWindow(side="long", start=0, end=60, peak_sum=0.95, peak_t=0, minutes=2)
    out = cli._format_windows_table(event, [window])
    assert "long" in out
    assert "0.9500" in out


def test_format_leaderboard_orders_by_net_profit_desc():
    out = cli._format_leaderboard([("event-b", 4.25, 2), ("event-a", 1.5, 3)])
    assert out.index("event-b") < out.index("event-a")


def test_extract_best_params_reads_best_params_dict():
    # tuner.walk_forward() returns best_params as a plain dict (asdict()),
    # not a BacktestParams instance -- see optimize/tuner.py.
    params_dict = {"threshold": 0.01, "fee": 0.0, "slippage": 0.001, "max_gap_s": 180, "min_window_minutes": 1}
    result = {"folds": [], "oos_total": 0.0, "best_params": params_dict}
    assert cli._extract_best_params(result) == params_dict
    with pytest.raises(KeyError):
        cli._extract_best_params({"folds": [], "oos_total": 0.0, "best_params": None})


# --- report (file I/O only, no other modules needed) -----------------------

def test_report_missing_results_dir(tmp_path, capsys):
    rc = cli.main(["report", "--results-dir", str(tmp_path / "nope")])
    assert rc == 0
    assert "no results" in capsys.readouterr().out.lower()


def test_report_prints_leaderboard(tmp_path, capsys):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    (results_dir / "a.json").write_text(json.dumps({"slug": "event-a", "net_profit": 1.5, "trades": 3}))
    (results_dir / "b.json").write_text(json.dumps({"slug": "event-b", "net_profit": 4.25, "trades": 2}))

    rc = cli.main(["report", "--results-dir", str(results_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.index("event-b") < out.index("event-a")


# --- fixture wiring (needs polytrage.data) --------------------------------

def test_load_fixtures_wires_series_to_markets():
    pytest.importorskip("polytrage.data.gamma")
    pytest.importorskip("polytrage.data.clob")
    event, series = cli._load_fixtures(FIXTURES_DIR)
    assert len(event.markets) == 3
    assert set(series.keys()) == {m.yes_token for m in event.markets}
    for points in series.values():
        assert len(points) > 1000


# --- full pipeline through fixtures (needs data + engine [+ backtest/optimize]) --

def test_scan_fixtures(capsys):
    pytest.importorskip("polytrage.data.gamma")
    pytest.importorskip("polytrage.data.clob")
    pytest.importorskip("polytrage.engine.align")
    pytest.importorskip("polytrage.engine.arb")
    rc = cli.main(["scan", "--fixtures", "--fixtures-dir", str(FIXTURES_DIR)])
    assert rc == 0
    assert capsys.readouterr().out.strip() != ""


def test_backtest_fixtures(capsys):
    pytest.importorskip("polytrage.data.gamma")
    pytest.importorskip("polytrage.data.clob")
    pytest.importorskip("polytrage.engine.align")
    pytest.importorskip("polytrage.engine.arb")
    pytest.importorskip("polytrage.backtest.runner")
    pytest.importorskip("polytrage.backtest.metrics")
    rc = cli.main(["backtest", "--fixtures", "--fixtures-dir", str(FIXTURES_DIR)])
    assert rc == 0
    assert "England vs. Argentina" in capsys.readouterr().out


def test_optimize_fixtures_writes_params(tmp_path, capsys):
    pytest.importorskip("polytrage.data.gamma")
    pytest.importorskip("polytrage.data.clob")
    pytest.importorskip("polytrage.engine.align")
    pytest.importorskip("polytrage.engine.arb")
    pytest.importorskip("polytrage.optimize.tuner")
    out_path = tmp_path / "params.json"
    rc = cli.main([
        "optimize", "--fixtures", "--fixtures-dir", str(FIXTURES_DIR),
        "--out", str(out_path),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "England vs. Argentina" in out

    data = json.loads(out_path.read_text())
    assert "threshold" in data
