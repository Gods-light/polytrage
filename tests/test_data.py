"""Tests for the data layer: pure parse functions and store round-trip.

No network — everything runs against tests/fixtures/.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from polytrage.data import store
from polytrage.data.clob import parse_history
from polytrage.data.gamma import parse_event
from polytrage.models import Event, PricePoint

FIXTURES = Path(__file__).parent / "fixtures"
OUTCOME_NAMES = ("ENG", "DRAW", "ARG")  # market order in event.json / hist_*.json


def _load_event() -> Event:
    raw = json.loads((FIXTURES / "event.json").read_text())[0]
    return parse_event(raw)


def _load_history(name: str) -> list[PricePoint]:
    raw = json.loads((FIXTURES / f"hist_{name}.json").read_text())
    return parse_history({"history": raw})


def test_parse_event_fields():
    event = _load_event()
    assert event.id == "694581"
    assert event.slug == "fifwc-eng-arg-2026-07-15"
    assert event.title == "England vs. Argentina"
    assert event.neg_risk is True
    assert event.closed is True
    assert len(event.markets) == 3


def test_parse_event_markets_order_and_tokens():
    event = _load_event()
    eng, draw, arg = event.markets

    assert eng.question == "Will England win on 2026-07-15?"
    assert draw.question == "Will England vs. Argentina end in a draw?"
    assert arg.question == "Will Argentina win on 2026-07-15?"

    # clobTokenIds is a JSON-encoded string "[yes, no]" — must be json.loads'd.
    assert eng.yes_token == (
        "62975583795498792086117213116943744194566684233762863056174017776352240969485"
    )
    assert eng.no_token == (
        "27980003456155592362417715508008664868431744696620193216867102238354645950149"
    )
    assert draw.yes_token == (
        "26480993337079434490856133942482101351393348205667335284619584625389515346213"
    )
    assert arg.yes_token == (
        "85633756210324151397822274959403676422451634259093065838612463900396751402106"
    )
    assert eng.volume == pytest.approx(17582672.06301029)


def test_parse_history_counts_and_order():
    for name, expected_len in zip(OUTCOME_NAMES, (4994, 4993, 4997)):
        points = _load_history(name)
        assert len(points) == expected_len
        ts = [p.t for p in points]
        assert ts == sorted(ts)
        assert all(0.0 <= p.p <= 1.0 for p in points)


def test_parse_history_known_points():
    eng = _load_history("ENG")
    assert eng[0] == PricePoint(t=1783850705, p=0.37)
    assert eng[-1] == PricePoint(t=1784150286, p=0.005)


def test_parse_history_missing_history_key_returns_empty():
    assert parse_history({}) == []


def test_ground_truth_sum_range():
    """Cross-checks parse_event + parse_history against the documented ground
    truth (CONTRACTS.md): aligning the 3 YES series by minute, sum(YES) over
    this fixture ranges 0.9425-1.0212.
    """
    event = _load_event()
    series = {
        market.yes_token: _load_history(name)
        for name, market in zip(OUTCOME_NAMES, event.markets)
    }
    by_minute = {
        token: {p.t - (p.t % 60): p.p for p in points} for token, points in series.items()
    }
    common = set.intersection(*(set(d) for d in by_minute.values()))
    sums = [sum(by_minute[m.yes_token][minute] for m in event.markets) for minute in common]

    assert min(sums) == pytest.approx(0.9425, abs=1e-4)
    assert max(sums) == pytest.approx(1.0212, abs=1e-3)


def test_store_round_trip(tmp_path):
    event = _load_event()
    series = {
        market.yes_token: _load_history(name)
        for name, market in zip(OUTCOME_NAMES, event.markets)
    }

    store.save_series(tmp_path, event, series)
    loaded_event, loaded_series = store.load_series(tmp_path, event.slug)

    assert loaded_event == event
    assert set(loaded_series.keys()) == set(series.keys())
    for token, points in series.items():
        assert loaded_series[token] == points


def test_load_series_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        store.load_series(tmp_path, "does-not-exist")
