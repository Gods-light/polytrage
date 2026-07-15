"""Parameter search over BacktestParams: grid generation, single-pass tuning
against `backtest.runner.run`, and walk-forward (in-sample / out-of-sample)
validation to check for overfitting. Deterministic -- no randomness.

`grid()` has no dependency on `polytrage.backtest` and works standalone.
`tune()` and `walk_forward()` import `backtest.runner` lazily, inside the
function body, so this module still imports cleanly before `backtest`
exists; only calling them requires it.
"""
from __future__ import annotations

from dataclasses import asdict
from itertools import product
from typing import Iterable, Iterator, Sequence

from polytrage.models import AlignedRow, BacktestParams, BacktestResult


def grid(
    thresholds: Sequence[float],
    slippages: Sequence[float] = (0.001,),
    max_gaps: Sequence[int] = (180,),
    min_windows: Sequence[int] = (1,),
) -> Iterator[BacktestParams]:
    """Cartesian product of the given axes as BacktestParams instances.

    Iteration order is deterministic: thresholds outermost, min_windows
    innermost. `fee` is not a search axis here and stays at the
    BacktestParams default (0.0) for every combination.
    """
    for threshold, slippage, max_gap, min_window in product(
        thresholds, slippages, max_gaps, min_windows
    ):
        yield BacktestParams(
            threshold=threshold,
            slippage=slippage,
            max_gap_s=max_gap,
            min_window_minutes=min_window,
        )


def tune(
    rows: list[AlignedRow],
    param_grid: Iterable[BacktestParams],
) -> tuple[BacktestParams, BacktestResult]:
    """Score every candidate in param_grid by running the real backtester and
    return the (params, result) with the highest net_profit.

    Ties keep the first-seen candidate: Python's max() returns the first
    maximal item for equal keys, which is exactly the tie-break we want.
    """
    param_list = list(param_grid)
    if not param_list:
        raise ValueError("param_grid must not be empty")

    from polytrage.backtest import runner

    scored = [(params, runner.run(rows, params)) for params in param_list]
    return max(scored, key=lambda pair: pair[1].net_profit)


def _split_folds(rows: Sequence[AlignedRow], folds: int) -> list[list[AlignedRow]]:
    """Split rows into `folds` contiguous, chronologically-ordered chunks of
    near-equal size. Any remainder is distributed one row each to the
    earliest folds."""
    ordered = sorted(rows, key=lambda r: r.t)
    base, remainder = divmod(len(ordered), folds)
    chunks: list[list[AlignedRow]] = []
    start = 0
    for i in range(folds):
        size = base + (1 if i < remainder else 0)
        chunks.append(ordered[start : start + size])
        start += size
    return chunks


def walk_forward(
    rows: list[AlignedRow],
    param_grid: Iterable[BacktestParams],
    folds: int = 3,
) -> dict:
    """Walk-forward overfit check.

    Splits rows into `folds` contiguous chronological chunks. For each
    consecutive pair (i, i+1), tunes param_grid on fold i (in-sample) and
    evaluates those tuned params on fold i+1 (out-of-sample).

    Returns:
        {
          "folds": [
              {"train_fold": i, "eval_fold": i+1,
               "params": <tuned BacktestParams as a plain dict>,
               "in_sample_net": float, "out_sample_net": float},
              ...  # one entry per consecutive pair, folds-1 total
          ],
          "oos_total": float,          # sum of out_sample_net across pairs
          "best_params": dict | None,  # params tuned on the LAST training
                                        # fold -- the go-forward choice, as
                                        # a plain (JSON-friendly) dict
        }
    """
    if folds < 2:
        raise ValueError("folds must be >= 2 to have a train/eval pair")

    grid_list = list(param_grid)
    if not grid_list:
        raise ValueError("param_grid must not be empty")

    from polytrage.backtest import runner

    fold_rows = _split_folds(rows, folds)

    reports: list[dict] = []
    best_params: BacktestParams | None = None
    for i in range(folds - 1):
        best_params, train_result = tune(fold_rows[i], grid_list)
        eval_result = runner.run(fold_rows[i + 1], best_params)
        reports.append(
            {
                "train_fold": i,
                "eval_fold": i + 1,
                "params": asdict(best_params),
                "in_sample_net": train_result.net_profit,
                "out_sample_net": eval_result.net_profit,
            }
        )

    return {
        "folds": reports,
        "oos_total": sum(r["out_sample_net"] for r in reports),
        "best_params": asdict(best_params) if best_params is not None else None,
    }
