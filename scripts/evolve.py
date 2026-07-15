#!/usr/bin/env python3
"""Self-evolution loop.

Loads the current strategy params, backtests + walk-forward re-tunes them
against recently-closed multi-outcome sports events (or offline fixtures),
and adopts the re-tuned params only if they beat the stored out-of-sample
baseline in results/baseline.json. Designed to run nightly from CI: never
raises (network or parsing problems are logged and skipped), always exits 0.

Offline (no network, used by CI and local `make evolve`):
    python scripts/evolve.py --fixtures

Live (discovers real closed events via the Gamma API; best-effort):
    python scripts/evolve.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

try:
    import polytrage  # noqa: F401
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polytrage.data import clob, gamma, store
from polytrage.engine import align as align_mod
from polytrage.backtest import metrics, runner
from polytrage.optimize import tuner
from polytrage.models import (
    AlignedRow,
    BacktestParams,
    BacktestResult,
    Event,
    Market,
    PricePoint,
    Series,
)

# Modest grid per CONTRACTS.md; fee/max_gap_s/min_window_minutes stay at
# BacktestParams defaults (which match the fixtures' ground-truth calibration).
THRESHOLDS = [0.003, 0.005, 0.0075, 0.01, 0.015, 0.02]
SLIPPAGES = [0.0005, 0.001, 0.002]
FOLDS = 3

SPORTS_QUERIES = ["World Cup", "NBA", "NFL", "Premier League", "Champions League", "UFC"]
MAX_LIVE_EVENTS = 5
# Event carries no start/end dates (see models.py), so there's no exact window
# to target for a discovered live event -- fall back to a generous lookback.
LOOKBACK_S = 30 * 24 * 3600

LEADERBOARD_TOP_N = 20


def fresh_grid() -> Iterator[BacktestParams]:
    """A new grid iterator each call -- tuner.grid()'s Iterator is exhausted
    after one use, and we need one per event (tune) plus one per event
    (walk_forward)."""
    return tuner.grid(THRESHOLDS, SLIPPAGES)


# ---------------------------------------------------------------- fixtures --

def load_fixture_event(fixtures_dir: Path) -> tuple[Event, Series]:
    """Parse tests/fixtures/{event,hist_ENG,hist_DRAW,hist_ARG}.json directly,
    the same way the CLI does in --fixtures mode.

    Markets in event.json are already ordered ENG win / draw / ARG win,
    matching hist_ENG/DRAW/ARG.json positionally. clobTokenIds is a
    JSON-encoded string list [yes_token, no_token].
    """
    with (fixtures_dir / "event.json").open() as f:
        raw_events = json.load(f)
    raw = raw_events[0]

    markets = []
    for m in raw["markets"]:
        yes_token, no_token = json.loads(m["clobTokenIds"])
        markets.append(
            Market(
                id=str(m["id"]),
                question=m["question"],
                yes_token=yes_token,
                no_token=no_token,
                volume=float(m.get("volume", 0.0)),
            )
        )
    event = Event(
        id=str(raw["id"]),
        slug=raw["slug"],
        title=raw["title"],
        markets=tuple(markets),
        neg_risk=bool(raw.get("negRisk", True)),
        closed=bool(raw.get("closed", False)),
    )

    hist_files = ["hist_ENG.json", "hist_DRAW.json", "hist_ARG.json"]
    series: Series = {}
    for market, hist_name in zip(event.markets, hist_files):
        with (fixtures_dir / hist_name).open() as f:
            raw_hist = json.load(f)
        series[market.yes_token] = [PricePoint(t=int(p["t"]), p=float(p["p"])) for p in raw_hist]

    return event, series


# -------------------------------------------------------------- live mode --

def discover_live_events(data_dir: Path, max_events: int = MAX_LIVE_EVENTS) -> list[tuple[Event, Series]]:
    """Best-effort: search Gamma for recently closed multi-outcome sports
    events, then fetch/cache their price histories. Any failure for a single
    query or event is logged and skipped; this never raises.
    """
    slugs: dict[str, None] = {}
    for q in SPORTS_QUERIES:
        try:
            events = gamma.search_events(q)
        except Exception as exc:
            print(f"[evolve] search_events({q!r}) failed: {exc}")
            continue
        for ev in events:
            if ev.closed and len(ev.markets) >= 3:
                slugs.setdefault(ev.slug, None)
        if len(slugs) >= max_events:
            break

    out: list[tuple[Event, Series]] = []
    for slug in list(slugs)[:max_events]:
        try:
            event = gamma.fetch_event(slug)
        except Exception as exc:
            print(f"[evolve] fetch_event({slug!r}) failed: {exc}")
            continue
        if not (event.closed and len(event.markets) >= 3):
            continue
        try:
            out.append(load_or_fetch_series(data_dir, event))
        except Exception as exc:
            print(f"[evolve] skipping event {slug!r}: {exc}")
    return out


def load_or_fetch_series(data_dir: Path, event: Event) -> tuple[Event, Series]:
    """Try the on-disk cache first (data.store), else fetch fresh histories
    from the CLOB and cache them for next time."""
    try:
        return store.load_series(data_dir, event.slug)
    except Exception:
        pass  # not cached, or cache unreadable -- fetch fresh below

    end_ts = int(time.time())
    start_ts = end_ts - LOOKBACK_S
    series: Series = {
        market.yes_token: clob.fetch_history(market.yes_token, start_ts, end_ts, fidelity=1)
        for market in event.markets
    }

    try:
        store.save_series(data_dir, event, series)
    except Exception as exc:
        print(f"[evolve] warning: failed to cache series for {event.slug!r}: {exc}")

    return event, series


# -------------------------------------------------------------- evaluation --

def build_rows(event: Event, series: Series) -> list[AlignedRow]:
    order = [m.yes_token for m in event.markets]
    return align_mod.align(series, order)


def evaluate_event(
    event: Event, rows: list[AlignedRow], current_params: BacktestParams
) -> dict[str, Any] | None:
    """Backtest current params, full-data tune, and walk-forward validate one
    event. Returns None (after logging) if any step fails; never raises.
    """
    try:
        current_result = runner.run(rows, current_params)
    except Exception as exc:
        print(f"[evolve] backtest failed for {event.slug!r}: {exc}")
        return None

    try:
        tuned_params, tuned_result = tuner.tune(rows, fresh_grid())
    except Exception as exc:
        print(f"[evolve] tune failed for {event.slug!r}: {exc}")
        return None

    try:
        wf = tuner.walk_forward(rows, fresh_grid(), folds=FOLDS)
    except Exception as exc:
        print(f"[evolve] walk_forward failed for {event.slug!r}: {exc}")
        return None

    oos_net_profit = extract_oos_net_profit(wf)
    if oos_net_profit is None:
        print(
            f"[evolve] warning: couldn't read an out-of-sample profit from "
            f"walk_forward's result for {event.slug!r} (keys={list(wf)!r}); skipping event"
        )
        return None

    return {
        "event": event,
        "current_result": current_result,
        "tuned_params": tuned_params,
        "tuned_result": tuned_result,
        "walk_forward": wf,
        "oos_net_profit": oos_net_profit,
    }


def extract_oos_net_profit(wf: dict[str, Any]) -> float | None:
    """Pull the out-of-sample net-profit total out of tuner.walk_forward()'s
    return dict: {"folds": [...], "oos_total": float, "best_params": ...} --
    oos_total is the sum of each consecutive fold-pair's out-of-sample net
    profit. Returns None (instead of raising) if the shape doesn't match, so
    a future change to tuner.py degrades to "skip this event" rather than
    crashing the loop.
    """
    val = wf.get("oos_total")
    return float(val) if isinstance(val, (int, float)) else None


def choose_global_params(evaluations: list[dict[str, Any]]) -> BacktestParams:
    """Adopt the tuned params from whichever event's full-data tune performed
    best in-sample. With --fixtures there's exactly one event, so this is
    just that event's tuned params."""
    best = max(evaluations, key=lambda e: e["tuned_result"].net_profit)
    return best["tuned_params"]


# -------------------------------------------------------- results & history --

def load_params(path: Path) -> BacktestParams:
    if not path.exists():
        return BacktestParams()
    try:
        with path.open() as f:
            data = json.load(f)
        return BacktestParams(**data)
    except Exception as exc:
        print(f"[evolve] warning: couldn't load {path} ({exc}); using defaults")
        return BacktestParams()


def load_baseline_oos(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        with path.open() as f:
            data = json.load(f)
    except Exception as exc:
        print(f"[evolve] warning: couldn't read baseline {path} ({exc}); treating as no baseline")
        return None
    val = data.get("oos_total") if isinstance(data, dict) else None
    return float(val) if isinstance(val, (int, float)) else None


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def append_history(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True))
        f.write("\n")


def regenerate_leaderboard(history_path: Path, out_path: Path, top_n: int = LEADERBOARD_TOP_N) -> None:
    entries: list[dict[str, Any]] = []
    if history_path.exists():
        with history_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    entries.sort(key=lambda e: e.get("oos_total", float("-inf")), reverse=True)
    entries = entries[:top_n]

    lines = [
        "# Leaderboard",
        "",
        "Best out-of-sample parameter sets found by `scripts/evolve.py`, best first.",
        "",
        "| Rank | Timestamp | OOS Net Profit | Threshold | Fee | Slippage | Max Gap (s) | Min Window (min) | Improved |",
        "|---:|---|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for i, e in enumerate(entries, start=1):
        params = e.get("params") or {}
        lines.append(
            "| {rank} | {ts} | {oos:.4f} | {threshold} | {fee} | {slippage} | {max_gap_s} | {min_window_minutes} | {improved} |".format(
                rank=i,
                ts=e.get("ts", ""),
                oos=e.get("oos_total", 0.0),
                threshold=params.get("threshold", ""),
                fee=params.get("fee", ""),
                slippage=params.get("slippage", ""),
                max_gap_s=params.get("max_gap_s", ""),
                min_window_minutes=params.get("min_window_minutes", ""),
                improved=e.get("improved", ""),
            )
        )

    out_path.write_text("\n".join(lines) + "\n")


# ------------------------------------------------------------------- main --

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Self-evolution loop for polytrage strategy params.")
    parser.add_argument(
        "--fixtures", action="store_true", help="offline mode: parse tests/fixtures instead of hitting the network"
    )
    parser.add_argument(
        "--fixtures-dir", default="tests/fixtures", help="fixtures directory (default: tests/fixtures)"
    )
    parser.add_argument("--data-dir", default="data", help="cached event/series data directory (default: data)")
    parser.add_argument("--results-dir", default="results", help="results output directory (default: results)")
    parser.add_argument("--params-file", default="params.json", help="strategy params file (default: params.json)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except Exception:
        print("[evolve] unexpected error; exiting 0 anyway")
        traceback.print_exc()
        return 0


def _main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_dir = Path(args.data_dir)
    results_dir = Path(args.results_dir)
    params_path = Path(args.params_file)

    current_params = load_params(params_path)
    print(f"[evolve] current params: {asdict(current_params)}")

    if args.fixtures:
        event, series = load_fixture_event(Path(args.fixtures_dir))
        datasets = [(event, series)]
        print(f"[evolve] fixtures mode: loaded event {event.slug!r} ({len(event.markets)} markets)")
    else:
        datasets = discover_live_events(data_dir)
        print(f"[evolve] live mode: discovered {len(datasets)} closed multi-outcome event(s)")

    if not datasets:
        print("[evolve] no events to evaluate; nothing to do")
        return 0

    evaluations: list[dict[str, Any]] = []
    current_results: list[BacktestResult] = []
    for event, series in datasets:
        try:
            rows = build_rows(event, series)
        except Exception as exc:
            print(f"[evolve] align failed for {event.slug!r}: {exc}")
            continue
        if not rows:
            print(f"[evolve] no aligned rows for {event.slug!r}; skipping")
            continue
        result = evaluate_event(event, rows, current_params)
        if result is None:
            continue
        evaluations.append(result)
        current_results.append(result["current_result"])

    if not evaluations:
        print("[evolve] no event could be evaluated; nothing to do")
        return 0

    if current_results:
        print("[evolve] current-params backtest summary:")
        print(metrics.to_markdown(metrics.summarize(current_results)))

    oos_total = sum(e["oos_net_profit"] for e in evaluations)
    global_params = choose_global_params(evaluations)

    baseline_path = results_dir / "baseline.json"
    baseline_oos = load_baseline_oos(baseline_path)
    had_baseline = baseline_oos is not None
    improved_over_prior = had_baseline and oos_total > baseline_oos
    should_adopt = (not had_baseline) or improved_over_prior

    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    baseline_display = f"{baseline_oos:.4f}" if had_baseline else "none"
    print(f"[evolve] out-of-sample net profit this run: {oos_total:.4f} (baseline: {baseline_display})")

    if should_adopt:
        write_json(params_path, asdict(global_params))
        write_json(baseline_path, {"ts": ts, "oos_total": oos_total, "params": asdict(global_params)})
        append_history(
            results_dir / "history.jsonl",
            {"ts": ts, "params": asdict(global_params), "oos_total": oos_total, "improved": improved_over_prior},
        )
        regenerate_leaderboard(results_dir / "history.jsonl", Path("LEADERBOARD.md"))
        verb = "improved on" if improved_over_prior else "established (no prior baseline)"
        print(
            f"[evolve] {verb} baseline -> adopted new params {asdict(global_params)}, "
            f"updated {baseline_path}, results/history.jsonl, LEADERBOARD.md"
        )
    else:
        print(f"[evolve] no improvement over baseline ({oos_total:.4f} <= {baseline_oos:.4f}); params.json unchanged")

    print("[evolve] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
