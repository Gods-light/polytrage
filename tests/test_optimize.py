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
            max_gaps=(60, 180),
            min_windows=(1, 2),
        )
    )
    assert len(params) == 2 * 2 * 2  # thresholds x max_gaps x min_windows only
    assert all(isinstance(p, BacktestParams) for p in params)
    assert (
        BacktestParams(
            threshold=0.005,
            fee=tuner.FROZEN_FEE,
            slippage=tuner.FROZEN_SLIPPAGE,
            max_gap_s=60,
            min_window_minutes=2,
        )
        in params
    )
    # deterministic nesting: thresholds outermost, min_windows innermost
    assert params[0] == BacktestParams(
        threshold=0.001,
        fee=tuner.FROZEN_FEE,
        slippage=tuner.FROZEN_SLIPPAGE,
        max_gap_s=60,
        min_window_minutes=1,
    )
    assert params[-1] == BacktestParams(
        threshold=0.005,
        fee=tuner.FROZEN_FEE,
        slippage=tuner.FROZEN_SLIPPAGE,
        max_gap_s=180,
        min_window_minutes=2,
    )


def test_grid_uses_frozen_costs_and_backtestparams_defaults_for_other_axes():
    params = list(tuner.grid(thresholds=(0.005,)))
    assert params == [
        BacktestParams(
            threshold=0.005,
            fee=tuner.FROZEN_FEE,
            slippage=tuner.FROZEN_SLIPPAGE,
            max_gap_s=180,
            min_window_minutes=1,
        )
    ]


def test_grid_costs_are_frozen_across_every_combination():
    # Cost params must never vary across a grid -- an optimizer free to walk
    # slippage toward 0 always reports a rosier net_profit for the wrong
    # reason (Goodhart), regardless of how many detection-param combos exist.
    params = list(
        tuner.grid(thresholds=(0.001, 0.005, 0.01), max_gaps=(60, 180), min_windows=(1, 2, 3))
    )
    assert len(params) == 3 * 2 * 3
    assert {p.fee for p in params} == {tuner.FROZEN_FEE}
    assert {p.slippage for p in params} == {tuner.FROZEN_SLIPPAGE}


def test_grid_allows_a_deliberate_single_cost_override_not_a_search_axis():
    # fee/slippage can be overridden for a what-if scenario, but only as one
    # scalar applied to the whole grid -- never as a per-combination axis.
    params = list(tuner.grid(thresholds=(0.005, 0.01), fee=0.001, slippage=0.003))
    assert len(params) == 2
    assert all(p.fee == 0.001 and p.slippage == 0.003 for p in params)


def test_grid_fee_and_slippage_are_keyword_only():
    with pytest.raises(TypeError):
        list(tuner.grid((0.005,), (180,), (1,), 0.0, 0.002))  # type: ignore[misc]


def test_default_grid_is_the_documented_threshold_sweep():
    params = list(tuner.DEFAULT_GRID())
    assert [p.threshold for p in params] == [0.003, 0.005, 0.0075, 0.01, 0.015, 0.02]
    assert all(p.fee == tuner.FROZEN_FEE for p in params)
    assert all(p.slippage == tuner.FROZEN_SLIPPAGE for p in params)
    assert all(p.max_gap_s == 180 and p.min_window_minutes == 1 for p in params)


def test_default_grid_is_deterministic_and_fresh_each_call():
    first = list(tuner.DEFAULT_GRID())
    second = list(tuner.DEFAULT_GRID())
    assert first == second
    # each call returns an independent generator, not a shared/exhausted one
    gen = tuner.DEFAULT_GRID()
    assert next(gen).threshold == 0.003
    assert list(tuner.DEFAULT_GRID())[0].threshold == 0.003  # unaffected by the above


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
    # cover its own cost (2 legs * FROZEN_SLIPPAGE = 0.004 > its 0.001 edge),
    # so adding it should make the looser threshold *worse*, not better.
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
