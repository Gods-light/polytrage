"""Tests for polytrage.optimize.tuner.

grid() and _split_folds() have no dependency on polytrage.backtest and are
tested unconditionally. tune()/walk_forward() call backtest.runner.run
internally, so those tests guard with pytest.importorskip('polytrage.backtest')
-- they're skipped standalone (before backtest exists) and run fully once
backtest is built, per CONTRACTS.md.
"""
from __future__ import annotations

import dataclasses

import pytest

from polytrage.models import AlignedRow, BacktestParams
from polytrage.optimize import tuner
from polytrage.optimize.tuner import _split_folds


# ---------------------------------------------------------------------------
# grid() -- pure, no backtest dependency
# ---------------------------------------------------------------------------


def test_grid_cartesian_product_and_order():
    params = list(
        tuner.grid(
            thresholds=(0.001, 0.005),
            slippages=(0.0, 0.002),
            max_gaps=(60, 180),
            min_windows=(1, 2),
        )
    )
    assert len(params) == 2 * 2 * 2 * 2
    assert all(isinstance(p, BacktestParams) for p in params)
    assert (
        BacktestParams(threshold=0.005, slippage=0.002, max_gap_s=60, min_window_minutes=2)
        in params
    )
    # deterministic nesting: thresholds outermost, min_windows innermost
    assert params[0] == BacktestParams(
        threshold=0.001, slippage=0.0, max_gap_s=60, min_window_minutes=1
    )
    assert params[-1] == BacktestParams(
        threshold=0.005, slippage=0.002, max_gap_s=180, min_window_minutes=2
    )


def test_grid_uses_backtestparams_defaults_for_omitted_axes():
    params = list(tuner.grid(thresholds=(0.005,)))
    assert params == [BacktestParams(threshold=0.005)]
    assert params[0].fee == 0.0  # fee is never a search axis


# ---------------------------------------------------------------------------
# _split_folds() -- pure, no backtest dependency
# ---------------------------------------------------------------------------


def test_split_folds_distributes_remainder_to_earliest_folds():
    rows = [AlignedRow(t=i, prices=(0.5, 0.5)) for i in range(7)]
    chunks = _split_folds(rows, 3)
    assert [len(c) for c in chunks] == [3, 2, 2]
    assert [r.t for r in chunks[0]] == [0, 1, 2]
    assert [r.t for r in chunks[1]] == [3, 4]
    assert [r.t for r in chunks[2]] == [5, 6]


def test_split_folds_sorts_chronologically_first():
    rows = [
        AlignedRow(t=5, prices=(0.5, 0.5)),
        AlignedRow(t=1, prices=(0.5, 0.5)),
        AlignedRow(t=3, prices=(0.5, 0.5)),
    ]
    chunks = _split_folds(rows, 3)
    assert [c[0].t for c in chunks] == [1, 3, 5]


# ---------------------------------------------------------------------------
# tune() -- needs the real backtest runner
# ---------------------------------------------------------------------------


def test_tune_picks_the_threshold_with_max_net_profit():
    pytest.importorskip("polytrage.backtest")
    from polytrage.backtest import runner

    # By construction: sum=0.98 is a big long-side edge (2c), sum=0.999 is a
    # tiny one (0.1c). At threshold=0.005 only the first row qualifies as an
    # arb window; at threshold=0.0005 both do. The tiny second edge doesn't
    # cover its own slippage (2 legs * 0.001 = 0.002 > 0.001), so adding it
    # should make the looser threshold *worse*, not better.
    rows = [
        AlignedRow(t=0, prices=(0.49, 0.49)),
        AlignedRow(t=2000, prices=(0.4995, 0.4995)),
    ]
    params_grid = list(tuner.grid(thresholds=(0.0005, 0.005)))

    # Recompute the expected winner independently via the real runner, so
    # this test doesn't hardcode a net_profit value owned by another module.
    scored = [(p, runner.run(rows, p)) for p in params_grid]
    expected_params, expected_result = max(scored, key=lambda pair: pair[1].net_profit)

    best_params, best_result = tuner.tune(rows, params_grid)

    assert best_params == expected_params
    assert best_result.net_profit == expected_result.net_profit
    assert best_params.threshold == 0.005


def test_tune_tie_break_is_first_seen():
    pytest.importorskip("polytrage.backtest")

    # sum == 1.0 always -> never qualifies as an arb window at any positive
    # threshold, so every candidate scores an identical net_profit of 0.0.
    rows = [AlignedRow(t=i * 300, prices=(0.5, 0.5)) for i in range(3)]
    param_grid = list(tuner.grid(thresholds=(0.02, 0.01, 0.005)))

    best_params, best_result = tuner.tune(rows, param_grid)

    assert best_result.net_profit == 0.0
    assert best_result.trades == 0
    assert best_params == param_grid[0]
    assert best_params.threshold == 0.02


def test_tune_raises_on_empty_param_grid():
    pytest.importorskip("polytrage.backtest")
    rows = [AlignedRow(t=0, prices=(0.5, 0.5))]
    with pytest.raises(ValueError):
        tuner.tune(rows, [])


# ---------------------------------------------------------------------------
# walk_forward() -- needs the real backtest runner
# ---------------------------------------------------------------------------


def test_walk_forward_structure_and_fold_consistency():
    pytest.importorskip("polytrage.backtest")
    from polytrage.backtest import runner

    rows = [
        AlignedRow(t=0, prices=(0.49, 0.49)),         # fold 0: sum=0.98
        AlignedRow(t=3000, prices=(0.4995, 0.4995)),  # fold 1: sum=0.999
        AlignedRow(t=6000, prices=(0.51, 0.51)),      # fold 2: sum=1.02
    ]
    grid_list = list(tuner.grid(thresholds=(0.0005, 0.005)))
    fold_groups = [[rows[0]], [rows[1]], [rows[2]]]  # 3 rows / 3 folds, exact

    report = tuner.walk_forward(rows, grid_list, folds=3)

    assert set(report.keys()) == {"folds", "oos_total", "best_params"}
    assert len(report["folds"]) == 2  # folds-1 train/eval pairs

    last_expected_params = None
    for i, entry in enumerate(report["folds"]):
        assert entry["train_fold"] == i
        assert entry["eval_fold"] == i + 1

        expected_params, expected_train = tuner.tune(fold_groups[i], grid_list)
        expected_eval = runner.run(fold_groups[i + 1], expected_params)

        assert entry["params"] == dataclasses.asdict(expected_params)
        assert entry["in_sample_net"] == expected_train.net_profit
        assert entry["out_sample_net"] == expected_eval.net_profit

        last_expected_params = expected_params

    assert report["oos_total"] == sum(e["out_sample_net"] for e in report["folds"])
    assert report["best_params"] == dataclasses.asdict(last_expected_params)


def test_walk_forward_raises_on_too_few_folds():
    pytest.importorskip("polytrage.backtest")
    rows = [AlignedRow(t=0, prices=(0.5, 0.5))]
    grid_list = list(tuner.grid(thresholds=(0.005,)))
    with pytest.raises(ValueError):
        tuner.walk_forward(rows, grid_list, folds=1)


def test_walk_forward_raises_on_empty_param_grid():
    pytest.importorskip("polytrage.backtest")
    rows = [AlignedRow(t=i, prices=(0.5, 0.5)) for i in range(3)]
    with pytest.raises(ValueError):
        tuner.walk_forward(rows, [], folds=3)
