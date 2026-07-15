"""Tests for polytrage.backtest.

Group 1 (no engine import): pure arithmetic on hand-built ArbWindow /
BacktestResult objects — safe to run even before polytrage.engine exists.
Group 2 (needs polytrage.engine.arb, auto-skipped if absent): one
end-to-end test of runner.run() on tiny synthetic AlignedRow data.
"""
from __future__ import annotations

import pytest

from polytrage.backtest import metrics, runner
from polytrage.models import AlignedRow, ArbWindow, BacktestParams, BacktestResult


def _window(side: str, start: int, end: int, peak_sum: float, peak_t: int, minutes: int) -> ArbWindow:
    return ArbWindow(side=side, start=start, end=end, peak_sum=peak_sum, peak_t=peak_t, minutes=minutes)


def _result(windows: list[ArbWindow], trades: int, gross: float, net: float, arb_minutes: int) -> BacktestResult:
    return BacktestResult(
        params=BacktestParams(),
        windows=windows,
        trades=trades,
        gross_edge=gross,
        net_profit=net,
        arb_minutes=arb_minutes,
    )


# --- Group 1: pure unit tests of runner.simulate (no engine import) --------


def test_simulate_single_long_window():
    window = _window("long", start=60, end=180, peak_sum=0.90, peak_t=120, minutes=3)
    params = BacktestParams(fee=0.0, slippage=0.001)

    result = runner.simulate([window], params, n_legs=3)

    assert result.trades == 1
    assert result.windows == [window]
    assert result.gross_edge == pytest.approx(0.10, abs=1e-9)
    assert result.net_profit == pytest.approx(0.10 - 0.001 * 3, abs=1e-9)
    assert result.arb_minutes == 3
    assert result.events == 1


def test_simulate_multiple_windows_accumulate():
    long_w = _window("long", 60, 180, 0.90, 120, 3)
    short_w = _window("short", 300, 360, 1.12, 360, 2)
    params = BacktestParams(fee=0.0002, slippage=0.001)

    result = runner.simulate([long_w, short_w], params, n_legs=3)

    assert result.trades == 2
    assert result.gross_edge == pytest.approx(long_w.edge + short_w.edge, abs=1e-9)
    expected_net = (long_w.edge - params.fee - params.slippage * 3) + (
        short_w.edge - params.fee - params.slippage * 3
    )
    assert result.net_profit == pytest.approx(expected_net, abs=1e-9)
    assert result.arb_minutes == 5
    assert result.profit_per_trade == pytest.approx(expected_net / 2, abs=1e-9)


def test_simulate_empty_windows_is_all_zero():
    result = runner.simulate([], BacktestParams(), n_legs=3)

    assert result.trades == 0
    assert result.gross_edge == 0.0
    assert result.net_profit == 0.0
    assert result.arb_minutes == 0
    assert result.profit_per_trade == 0.0  # guards div-by-zero
    assert result.windows == []


def test_simulate_fee_and_slippage_reduce_net_below_gross():
    window = _window("long", 0, 60, 0.95, 0, 2)
    params = BacktestParams(fee=0.001, slippage=0.002)

    result = runner.simulate([window], params, n_legs=4)

    assert result.net_profit < result.gross_edge


# --- metrics.summarize / to_markdown (hand-built results, no engine) -------


def test_summarize_totals_and_buckets():
    w_tiny = _window("long", 0, 60, 0.995, 0, 1)  # edge 0.005 -> <1c
    w_small = _window("long", 60, 120, 0.985, 60, 1)  # edge 0.015 -> 1-2c
    w_mid = _window("short", 120, 180, 1.03, 180, 1)  # edge 0.03 -> 2-5c
    w_big = _window("short", 180, 240, 1.08, 240, 1)  # edge 0.08 -> >=5c

    r1 = _result([w_tiny, w_small], trades=2, gross=0.02, net=0.015, arb_minutes=2)
    r2 = _result([w_mid, w_big], trades=2, gross=0.11, net=0.10, arb_minutes=2)

    summary = metrics.summarize([r1, r2])

    assert summary["totals"] == pytest.approx(
        {"trades": 4, "gross": 0.13, "net": 0.115, "arb_minutes": 4, "events": 2}
    )
    assert summary["profit_per_trade"] == pytest.approx(0.115 / 4)
    assert summary["long_windows"] == 2
    assert summary["short_windows"] == 2
    assert summary["edge_buckets"] == {"<1c": 1, "1-2c": 1, "2-5c": 1, ">=5c": 1}


def test_summarize_empty_list_is_all_zero():
    summary = metrics.summarize([])

    assert summary["totals"] == {"trades": 0, "gross": 0.0, "net": 0.0, "arb_minutes": 0, "events": 0}
    assert summary["profit_per_trade"] == 0.0
    assert summary["long_windows"] == 0
    assert summary["short_windows"] == 0
    assert summary["edge_buckets"] == {"<1c": 0, "1-2c": 0, "2-5c": 0, ">=5c": 0}


def test_to_markdown_contains_key_figures():
    w = _window("long", 0, 60, 0.97, 0, 1)
    r = _result([w], trades=1, gross=0.03, net=0.025, arb_minutes=1)
    summary = metrics.summarize([r])

    report = metrics.to_markdown(summary)

    assert isinstance(report, str)
    assert "|" in report  # markdown table
    assert "trades" in report
    assert "1" in report
    for bucket in ("<1c", "1-2c", "2-5c", ">=5c"):
        assert bucket in report


# --- Group 2: integration test against the real engine ---------------------


def test_run_end_to_end_against_engine():
    pytest.importorskip("polytrage.engine.arb")

    params = BacktestParams(threshold=0.01, fee=0.0, slippage=0.001, max_gap_s=180, min_window_minutes=1)
    rows = [
        AlignedRow(t=0, prices=(0.40, 0.30, 0.30)),  # sum 1.00 neutral
        AlignedRow(t=60, prices=(0.32, 0.30, 0.30)),  # sum 0.92 long
        AlignedRow(t=120, prices=(0.30, 0.30, 0.30)),  # sum 0.90 long peak
        AlignedRow(t=180, prices=(0.33, 0.30, 0.30)),  # sum 0.93 long
        AlignedRow(t=240, prices=(0.40, 0.30, 0.30)),  # sum 1.00 neutral
        AlignedRow(t=300, prices=(0.40, 0.35, 0.35)),  # sum 1.10 short
        AlignedRow(t=360, prices=(0.40, 0.36, 0.36)),  # sum 1.12 short peak
        AlignedRow(t=420, prices=(0.40, 0.30, 0.30)),  # sum 1.00 neutral
    ]

    result = runner.run(rows, params)

    assert isinstance(result, BacktestResult)
    assert result.trades == len(result.windows) == 2
    assert result.events == 1

    long_windows = [w for w in result.windows if w.side == "long"]
    short_windows = [w for w in result.windows if w.side == "short"]
    assert len(long_windows) == 1
    assert len(short_windows) == 1

    lw = long_windows[0]
    assert (lw.start, lw.end, lw.minutes) == (60, 180, 3)
    assert lw.peak_sum == pytest.approx(0.90, abs=1e-9)
    assert lw.peak_t == 120

    sw = short_windows[0]
    assert (sw.start, sw.end, sw.minutes) == (300, 360, 2)
    assert sw.peak_sum == pytest.approx(1.12, abs=1e-9)
    assert sw.peak_t == 360

    n_legs = 3
    expected_gross = lw.edge + sw.edge
    expected_net = expected_gross - params.fee * 2 - params.slippage * n_legs * 2
    assert result.gross_edge == pytest.approx(expected_gross, abs=1e-9)
    assert result.net_profit == pytest.approx(expected_net, abs=1e-9)
    assert result.arb_minutes == 5
