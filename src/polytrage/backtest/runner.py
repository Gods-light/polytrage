"""Backtest runner: turn engine-detected ArbWindows into simulated trades.

One $1 basket is traded per window, executed at the window's peak edge.
`run` is the end-to-end entry point (rows -> engine -> trades); `simulate`
is the pure trade-arithmetic step, kept separate so it can be unit tested
without depending on the engine module.
"""
from __future__ import annotations

from collections.abc import Sequence

from polytrage.models import AlignedRow, ArbWindow, BacktestParams, BacktestResult


def simulate(windows: Sequence[ArbWindow], params: BacktestParams, n_legs: int) -> BacktestResult:
    """Simulate one $1-basket trade per window, executed at its peak.

    gross edge = window.edge (per $1 basket)
    net        = gross - params.fee*1.0 - params.slippage*n_legs
    """
    result = BacktestResult(params=params, windows=list(windows))
    for window in windows:
        gross = window.edge
        net = gross - params.fee * 1.0 - params.slippage * n_legs
        result.trades += 1
        result.gross_edge += gross
        result.net_profit += net
        result.arb_minutes += window.minutes
    return result


def run(rows: Sequence[AlignedRow], params: BacktestParams) -> BacktestResult:
    """Find arbitrage windows in `rows` and simulate trading them.

    Local import: polytrage.engine is built in parallel and may not exist
    yet at backtest-development time.
    """
    from polytrage.engine import arb

    n_legs = len(rows[0].prices) if rows else 0
    windows = arb.find_windows(rows, params)
    return simulate(windows, params, n_legs)
