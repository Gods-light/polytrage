"""Tests for polytrage.engine: align() and arb.{find_windows,instant_edge,basket_cost}."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from polytrage.engine.align import align
from polytrage.engine.arb import basket_cost, find_windows, instant_edge
from polytrage.models import AlignedRow, BacktestParams, PricePoint

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# align()
# ---------------------------------------------------------------------------


def test_align_floors_dedupes_and_intersects():
    series = {
        "A": [
            PricePoint(t=0, p=0.50),
            PricePoint(t=30, p=0.55),  # same minute as t=0 -> last wins
            PricePoint(t=60, p=0.60),
            PricePoint(t=125, p=0.65),  # minute 120
        ],
        "B": [
            PricePoint(t=5, p=0.40),
            PricePoint(t=64, p=0.42),
            PricePoint(t=121, p=0.44),  # minute 120
            PricePoint(t=200, p=0.46),  # minute 180, absent from A
        ],
    }

    rows = align(series, order=["A", "B"])

    assert rows == [
        AlignedRow(t=0, prices=(0.55, 0.40)),
        AlignedRow(t=60, prices=(0.60, 0.42)),
        AlignedRow(t=120, prices=(0.65, 0.44)),
    ]


def test_align_orders_prices_per_order_arg():
    series = {
        "A": [PricePoint(t=0, p=0.1)],
        "B": [PricePoint(t=0, p=0.2)],
    }

    assert align(series, order=["A", "B"])[0].prices == (0.1, 0.2)
    assert align(series, order=["B", "A"])[0].prices == (0.2, 0.1)


# ---------------------------------------------------------------------------
# find_windows() -- hand-built rows
# ---------------------------------------------------------------------------


def _rows() -> list[AlignedRow]:
    # threshold=0.1 in these tests -> long qualifies at total<=0.9, short at total>=1.1
    data = [
        (0, (0.50, 0.50)),  # total 1.00 - neutral
        (60, (0.40, 0.45)),  # total 0.85 - long
        (120, (0.40, 0.40)),  # total 0.80 - long, min (peak)
        (180, (0.50, 0.50)),  # total 1.00 - neutral (gap row)
        (240, (0.42, 0.43)),  # total 0.85 - long
        (300, (0.60, 0.60)),  # total 1.20 - short
        (360, (0.65, 0.60)),  # total 1.25 - short, max (peak)
        (420, (0.50, 0.50)),  # total 1.00 - neutral
    ]
    return [AlignedRow(t=t, prices=p) for t, p in data]


def test_find_windows_merges_gaps_within_max_gap_s():
    rows = _rows()
    params = BacktestParams(threshold=0.1, max_gap_s=180, min_window_minutes=1)

    windows = find_windows(rows, params)
    long_windows = [w for w in windows if w.side == "long"]
    short_windows = [w for w in windows if w.side == "short"]

    assert len(long_windows) == 1
    lw = long_windows[0]
    assert (lw.start, lw.end, lw.minutes) == (60, 240, 3)
    assert lw.peak_sum == pytest.approx(0.80)
    assert lw.peak_t == 120
    # entry_sum is the FIRST qualifying row (t=60, total 0.85), not the peak (t=120, total 0.80).
    assert lw.entry_sum == pytest.approx(0.85)
    assert lw.entry_edge == pytest.approx(0.15)
    assert lw.entry_edge <= lw.edge

    assert len(short_windows) == 1
    sw = short_windows[0]
    assert (sw.start, sw.end, sw.minutes) == (300, 360, 2)
    assert sw.peak_sum == pytest.approx(1.25)
    assert sw.peak_t == 360
    # entry_sum is the FIRST qualifying row (t=300, total 1.20), not the peak (t=360, total 1.25).
    assert sw.entry_sum == pytest.approx(1.20)
    assert sw.entry_edge == pytest.approx(0.20)
    assert sw.entry_edge <= sw.edge


def test_find_windows_splits_on_gap_larger_than_max_gap_s():
    rows = _rows()
    params = BacktestParams(threshold=0.1, max_gap_s=60, min_window_minutes=1)

    long_windows = [w for w in find_windows(rows, params) if w.side == "long"]

    assert len(long_windows) == 2
    assert (long_windows[0].start, long_windows[0].end, long_windows[0].minutes) == (60, 120, 2)
    assert long_windows[0].peak_sum == pytest.approx(0.80)
    # first qualifying row of this group is still t=60 (total 0.85), same as the merged case above.
    assert long_windows[0].entry_sum == pytest.approx(0.85)
    assert (long_windows[1].start, long_windows[1].end, long_windows[1].minutes) == (240, 240, 1)
    assert long_windows[1].peak_sum == pytest.approx(0.85)
    # single-row window: the first (only) qualifying row IS the peak.
    assert long_windows[1].entry_sum == pytest.approx(0.85)
    assert long_windows[1].entry_edge == pytest.approx(long_windows[1].edge)


def test_find_windows_drops_windows_shorter_than_min_window_minutes():
    rows = _rows()
    params = BacktestParams(threshold=0.1, max_gap_s=180, min_window_minutes=3)

    windows = find_windows(rows, params)

    assert len(windows) == 1
    assert windows[0].side == "long"
    assert windows[0].minutes == 3


def test_find_windows_no_qualifying_rows_returns_empty():
    rows = [AlignedRow(t=0, prices=(0.5, 0.5))]
    params = BacktestParams(threshold=0.1)

    assert find_windows(rows, params) == []


# ---------------------------------------------------------------------------
# instant_edge() / basket_cost()
# ---------------------------------------------------------------------------


def test_basket_cost_long_is_sum_of_prices():
    assert basket_cost((0.4, 0.5), "long") == pytest.approx(0.9)


def test_basket_cost_short_is_n_minus_sum():
    assert basket_cost((0.4, 0.5), "short") == pytest.approx(1.1)
    assert basket_cost((0.3, 0.3, 0.3), "short") == pytest.approx(2.1)


def test_instant_edge_long_profitable():
    assert instant_edge((0.4, 0.5), "long") == pytest.approx(0.1)


def test_instant_edge_long_unprofitable_is_zero():
    assert instant_edge((0.6, 0.6), "long") == 0.0


def test_instant_edge_short_profitable():
    assert instant_edge((0.6, 0.6), "short") == pytest.approx(0.2)


def test_instant_edge_short_unprofitable_is_zero():
    assert instant_edge((0.4, 0.5), "short") == 0.0


def test_instant_edge_independent_of_leg_count():
    # The leg count cancels out of the short formula: only the sum matters.
    assert instant_edge((0.4, 0.4, 0.4), "short") == pytest.approx(0.2)
    assert instant_edge((0.3, 0.3, 0.3), "long") == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Real fixture: England vs Argentina WC semifinal
# ---------------------------------------------------------------------------


def _load_series(name: str) -> list[PricePoint]:
    raw = json.loads((FIXTURES / f"hist_{name}.json").read_text())
    return [PricePoint(t=point["t"], p=point["p"]) for point in raw]


def test_fixture_alignment_and_windows_match_reference():
    series = {name: _load_series(name) for name in ("ENG", "DRAW", "ARG")}
    rows = align(series, order=["ENG", "DRAW", "ARG"])

    totals = [row.total for row in rows]
    min_total = min(totals)
    max_total = max(totals)

    assert min_total == pytest.approx(0.9425, abs=1e-4)
    assert max_total == pytest.approx(1.0212, abs=1e-4)

    windows = find_windows(rows, BacktestParams())
    long_windows = [w for w in windows if w.side == "long"]

    peak_matches = [w for w in long_windows if w.peak_sum == pytest.approx(min_total, abs=1e-9)]
    assert len(peak_matches) == 1
    window = peak_matches[0]

    # entry_sum must be the total of the first qualifying row (t == window.start),
    # cross-checked independently against the full aligned-row list -- not the peak,
    # which would be lookahead bias for fill simulation.
    entry_row_total = next(row.total for row in rows if row.t == window.start)
    assert window.entry_sum == pytest.approx(entry_row_total, abs=1e-9)
    assert window.entry_sum == pytest.approx(0.9425, abs=1e-9)

    # In this particular window the extreme happens to occur on its first minute,
    # so entry and peak coincide here -- confirmed structurally elsewhere (the
    # hand-built gap-merge tests) that they can differ.
    assert window.entry_edge == pytest.approx(window.edge, abs=1e-9)

    # peak is by construction at least as extreme as entry -- must hold for every window.
    assert all(w.entry_edge <= w.edge + 1e-9 for w in windows)
