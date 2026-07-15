"""Detect long/short arbitrage windows and price the basket at a moment."""
from __future__ import annotations

from typing import Sequence

from polytrage.models import AlignedRow, ArbWindow, BacktestParams


def basket_cost(prices: Sequence[float], side: str) -> float:
    """Dollar cost of buying one share of every leg for `side`.

    long: buy every YES -> sum(prices).
    short: buy every NO -> sum(1 - p) == len(prices) - sum(prices).
    """
    if side == "long":
        return sum(prices)
    if side == "short":
        return sum(1.0 - p for p in prices)
    raise ValueError(f"unknown side: {side!r}")


def instant_edge(prices: Sequence[float], side: str) -> float:
    """Gross edge in dollars per $1 basket if acting right now.

    long: pay sum(prices), collect $1 -> edge = 1 - sum.
    short: pay len(prices) - sum(prices), collect len(prices) - 1
           -> edge = sum - 1 (the leg count cancels out).
    Returns 0.0 when the trade would not be profitable.
    """
    total = sum(prices)
    if side == "long":
        edge = 1.0 - total
    elif side == "short":
        edge = total - 1.0
    else:
        raise ValueError(f"unknown side: {side!r}")
    return edge if edge > 0.0 else 0.0


def _qualifies(total: float, threshold: float, side: str) -> bool:
    if side == "long":
        return total <= 1.0 - threshold
    return total >= 1.0 + threshold


def _side_windows(rows: list[AlignedRow], params: BacktestParams, side: str) -> list[ArbWindow]:
    qualifying = [row for row in rows if _qualifies(row.total, params.threshold, side)]
    if not qualifying:
        return []

    groups: list[list[AlignedRow]] = [[qualifying[0]]]
    for row in qualifying[1:]:
        if row.t - groups[-1][-1].t <= params.max_gap_s:
            groups[-1].append(row)
        else:
            groups.append([row])

    peak = min if side == "long" else max
    windows = []
    for group in groups:
        peak_row = peak(group, key=lambda r: r.total)
        windows.append(
            ArbWindow(
                side=side,
                start=group[0].t,
                end=group[-1].t,
                peak_sum=peak_row.total,
                peak_t=peak_row.t,
                minutes=len(group),
                entry_sum=group[0].total,
            )
        )
    return [w for w in windows if w.minutes >= params.min_window_minutes]


def find_windows(rows: list[AlignedRow], params: BacktestParams) -> list[ArbWindow]:
    """Detect long and short arbitrage windows, tracked independently.

    A row qualifies long iff total <= 1 - threshold, short iff total >=
    1 + threshold (inclusive). Consecutive qualifying rows of the same side
    merge when separated by <= max_gap_s seconds; windows shorter than
    min_window_minutes (by qualifying-row count) are dropped.
    """
    return _side_windows(rows, params, "long") + _side_windows(rows, params, "short")
