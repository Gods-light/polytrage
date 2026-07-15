"""Parameter search over BacktestParams: grid generation, single-pass tuning
against `backtest.runner.run`, and walk-forward (in-sample / out-of-sample)
validation to check for overfitting. Deterministic -- no randomness.

Cost parameters (fee, slippage) are frozen constants, not search axes: an
optimizer free to walk slippage toward 0 will always report a rosier
net_profit by lowering its own assumed trading cost rather than by finding a
genuinely better detection threshold (a Goodhart failure mode -- caught in
council review). grid()/DEFAULT_GRID() only search detection params
(threshold, max_gap_s, min_window_minutes); every candidate they emit
carries the same FROZEN_FEE/FROZEN_SLIPPAGE. DEFAULT_GRID is the single
canonical grid -- cli and evolve both import it so they can't silently
diverge on what "the grid" means.

`grid()`/`DEFAULT_GRID()` have no dependency on `polytrage.backtest` and
work standalone. `tune()` and `walk_forward()` import `backtest.runner`
lazily, inside the function body, so this module still imports cleanly
before `backtest` exists; only calling them requires it.
"""
from __future__ import annotations

from dataclasses import asdict
from itertools import product
from typing import Iterable, Iterator, Sequence

from polytrage.models import AlignedRow, BacktestParams, BacktestResult

FROZEN_FEE: float = 0.0        # venue fact today (Polymarket taker fee is 0)
FROZEN_SLIPPAGE: float = 0.002  # deliberately pessimistic per-leg haircut vs midpoint

_DEFAULT_THRESHOLDS: tuple[float, ...] = (0.003, 0.005, 0.0075, 0.01, 0.015, 0.02)


def grid(
    thresholds: Sequence[float],
    max_gaps: Sequence[int] = (180,),
    min_windows: Sequence[int] = (1,),
    *,
    fee: float = FROZEN_FEE,
    slippage: float = FROZEN_SLIPPAGE,
) -> Iterator[BacktestParams]:
    """Cartesian product over detection params only (thresholds, max_gaps,
    min_windows) as BacktestParams instances.

    fee/slippage are NOT search axes -- they're keyword-only scalars applied
    identically to every combination (default: the frozen constants above),
    so no grid ever "improves" net_profit by walking costs toward zero.
    Override them only for a deliberate what-if scenario, never as part of a
    search sweep. Iteration order is deterministic: thresholds outermost,
    min_windows innermost.
    """
    for threshold, max_gap, min_window in product(thresholds, max_gaps, min_windows):
        yield BacktestParams(
            threshold=threshold,
            fee=fee,
            slippage=slippage,
            max_gap_s=max_gap,
            min_window_minutes=min_window,
        )


def DEFAULT_GRID() -> Iterator[BacktestParams]:
    """The single canonical search grid: the documented threshold sweep,
    every other axis left at grid()'s own defaults (frozen costs included).
    cli and evolve both call this instead of building their own grid() so
    they can never disagree on what params were searched. Fresh generator
    on every call.
    """
    return grid(_DEFAULT_THRESHOLDS)


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
