"""Aggregate BacktestResults into summary stats and a markdown report."""
from __future__ import annotations

from polytrage.models import BacktestResult

_EDGE_BUCKETS = ("<1c", "1-2c", "2-5c", ">=5c")


def _edge_bucket(edge: float) -> str:
    if edge < 0.01:
        return "<1c"
    if edge < 0.02:
        return "1-2c"
    if edge < 0.05:
        return "2-5c"
    return ">=5c"


def summarize(results: list[BacktestResult]) -> dict:
    """Aggregate backtest results (one per event) into totals and histograms."""
    totals = {
        "trades": sum(r.trades for r in results),
        "gross": sum(r.gross_edge for r in results),
        "net": sum(r.net_profit for r in results),
        "arb_minutes": sum(r.arb_minutes for r in results),
        "events": sum(r.events for r in results),
    }
    long_windows = 0
    short_windows = 0
    edge_buckets = {bucket: 0 for bucket in _EDGE_BUCKETS}
    for result in results:
        for window in result.windows:
            if window.side == "long":
                long_windows += 1
            elif window.side == "short":
                short_windows += 1
            edge_buckets[_edge_bucket(window.edge)] += 1

    return {
        "totals": totals,
        "profit_per_trade": totals["net"] / totals["trades"] if totals["trades"] else 0.0,
        "long_windows": long_windows,
        "short_windows": short_windows,
        "edge_buckets": edge_buckets,
    }


def to_markdown(summary: dict) -> str:
    """Render a summarize() dict as a compact markdown report."""
    totals = summary["totals"]
    lines = [
        "## Backtest summary",
        "",
        "| metric | value |",
        "|---|---|",
        f"| events | {totals['events']} |",
        f"| trades | {totals['trades']} |",
        f"| gross edge | ${totals['gross']:.4f} |",
        f"| net profit | ${totals['net']:.4f} |",
        f"| profit / trade | ${summary['profit_per_trade']:.4f} |",
        f"| arb minutes | {totals['arb_minutes']} |",
        f"| long windows | {summary['long_windows']} |",
        f"| short windows | {summary['short_windows']} |",
        "",
        "| edge bucket | windows |",
        "|---|---|",
    ]
    for bucket in _EDGE_BUCKETS:
        lines.append(f"| {bucket} | {summary['edge_buckets'][bucket]} |")
    return "\n".join(lines) + "\n"
