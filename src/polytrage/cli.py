"""Command-line interface for polytrage.

Thin I/O layer only — all detection/backtest/optimization math lives in the
other modules. Submodules owned by other agents (data/engine/backtest/
optimize) are imported lazily inside each command function so that this
module (and its test file) import cleanly even before those packages exist.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from polytrage.models import AlignedRow, ArbWindow, BacktestParams, Event, Series

_DEFAULT_FIXTURES_DIR = "tests/fixtures"
_HIST_FILES = ("hist_ENG.json", "hist_DRAW.json", "hist_ARG.json")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polytrage", description="Polymarket multi-outcome arbitrage toolkit"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="fetch an event + histories and print current arb windows")
    _add_source_args(p_scan)
    p_scan.set_defaults(func=_cmd_scan, _parser=p_scan)

    p_backtest = sub.add_parser("backtest", help="run a backtest and print a metrics report")
    _add_source_args(p_backtest)
    p_backtest.set_defaults(func=_cmd_backtest, _parser=p_backtest)

    p_optimize = sub.add_parser("optimize", help="walk-forward tune params and print/save the best")
    _add_source_args(p_optimize)
    p_optimize.add_argument("--folds", type=int, default=3, help="walk-forward folds (default: 3)")
    p_optimize.add_argument("--out", default=None, help="write chosen params as JSON to this path")
    p_optimize.set_defaults(func=_cmd_optimize, _parser=p_optimize)

    p_report = sub.add_parser("report", help="read results/*.json and print a leaderboard")
    p_report.add_argument("--results-dir", default="results", help="results directory (default: results)")
    p_report.set_defaults(func=_cmd_report, _parser=p_report)

    return parser


def _add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("slug", nargs="?", help="Polymarket event slug (omit when using --fixtures)")
    parser.add_argument("--fixtures", action="store_true", help="load tests/fixtures instead of the network")
    parser.add_argument(
        "--fixtures-dir", default=_DEFAULT_FIXTURES_DIR,
        help=f"fixtures directory (default: {_DEFAULT_FIXTURES_DIR})",
    )
    parser.add_argument(
        "--hours", type=float, default=48.0,
        help="live lookback window in hours (default: 48, ignored with --fixtures)",
    )


# --- data loading ------------------------------------------------------

def _require_slug_or_fixtures(args: argparse.Namespace) -> None:
    if not args.fixtures and not args.slug:
        args._parser.error("a slug is required unless --fixtures is set")


def _load_source(args: argparse.Namespace) -> tuple[Event, Series]:
    if args.fixtures:
        return _load_fixtures(Path(args.fixtures_dir))
    return _load_live(args.slug, args.hours)


def _load_fixtures(fixtures_dir: Path) -> tuple[Event, Series]:
    """Load event + histories from a fixtures dir instead of the network.

    event.json is a Gamma API response: a list with one event dict. The
    hist_{ENG,DRAW,ARG}.json files are pre-ordered to match event.json's
    market order (England win, draw, Argentina win), so they're zipped to
    markets by position rather than by parsing the question text.
    """
    from polytrage.data import clob, gamma

    event_list = json.loads((fixtures_dir / "event.json").read_text())
    event = gamma.parse_event(event_list[0])

    series: Series = {}
    for market, filename in zip(event.markets, _HIST_FILES):
        history = json.loads((fixtures_dir / filename).read_text())
        series[market.yes_token] = clob.parse_history({"history": history})
    return event, series


def _load_live(slug: str, hours: float) -> tuple[Event, Series]:
    from polytrage.data import clob, gamma

    event = gamma.fetch_event(slug)
    end_ts = int(time.time())
    start_ts = end_ts - int(hours * 3600)
    series: Series = {
        market.yes_token: clob.fetch_history(market.yes_token, start_ts, end_ts, fidelity=1)
        for market in event.markets
    }
    return event, series


def _align_rows(event: Event, series: Series) -> list[AlignedRow]:
    from polytrage.engine import align

    order = [market.yes_token for market in event.markets]
    return align.align(series, order)


# --- commands ------------------------------------------------------

def _cmd_scan(args: argparse.Namespace) -> int:
    _require_slug_or_fixtures(args)
    from polytrage.engine import arb

    event, series = _load_source(args)
    rows = _align_rows(event, series)
    windows = arb.find_windows(rows, BacktestParams())
    print(_format_windows_table(event, windows))
    return 0


def _cmd_backtest(args: argparse.Namespace) -> int:
    _require_slug_or_fixtures(args)
    from polytrage.backtest import metrics, runner

    event, series = _load_source(args)
    rows = _align_rows(event, series)
    result = runner.run(rows, BacktestParams())
    summary = metrics.summarize([result])
    print(f"# {event.title}\n")
    print(metrics.to_markdown(summary))
    return 0


def _cmd_optimize(args: argparse.Namespace) -> int:
    _require_slug_or_fixtures(args)
    from polytrage.optimize import tuner

    event, series = _load_source(args)
    rows = _align_rows(event, series)

    # Materialized to a list: walk_forward evaluates the grid once per fold,
    # and a generator would be exhausted after the first pass.
    param_grid = list(tuner.grid(
        thresholds=[0.0025, 0.005, 0.0075, 0.01],
        slippages=[0.0, 0.0005, 0.001, 0.002],
    ))
    result = tuner.walk_forward(rows, param_grid, folds=args.folds)
    best_params = _extract_best_params(result)

    print(f"# {event.title}\n")
    print(_format_optimize_result(best_params, result))

    if args.out:
        Path(args.out).write_text(json.dumps(best_params, indent=2) + "\n")
        print(f"wrote {args.out}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    results_dir = Path(args.results_dir)
    files = sorted(results_dir.glob("*.json")) if results_dir.is_dir() else []
    if not files:
        print(f"no results found in {results_dir}")
        return 0

    rows = []
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        label = data.get("slug") or data.get("event") or data.get("title") or path.stem
        rows.append((str(label), float(data.get("net_profit", 0.0)), int(data.get("trades", 0))))
    rows.sort(key=lambda row: row[1], reverse=True)

    print(_format_leaderboard(rows))
    return 0


# --- formatting ------------------------------------------------------

def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _format_windows_table(event: Event, windows: list[ArbWindow]) -> str:
    if not windows:
        return f"{event.title}: no arbitrage windows found."

    header = f"{'side':<6} {'start':<17} {'end':<17} {'minutes':>7} {'peak_sum':>9} {'edge':>8}"
    lines = [event.title, header, "-" * len(header)]
    for window in sorted(windows, key=lambda w: w.start):
        lines.append(
            f"{window.side:<6} {_fmt_ts(window.start):<17} {_fmt_ts(window.end):<17} "
            f"{window.minutes:>7} {window.peak_sum:>9.4f} {window.edge:>8.4f}"
        )
    return "\n".join(lines)


def _extract_best_params(result: dict) -> dict:
    """Pull the go-forward params out of a walk_forward() result.

    `best_params` is already a plain JSON-ready dict (tuner.walk_forward
    builds it via dataclasses.asdict), not a BacktestParams instance.
    """
    best = result.get("best_params")
    if not isinstance(best, dict):
        raise KeyError(f"walk_forward() result has no usable 'best_params' dict; got: {best!r}")
    return best


def _format_optimize_result(best_params: dict, result: dict) -> str:
    lines = ["Best parameters (walk-forward):"]
    for field_name, value in best_params.items():
        lines.append(f"  {field_name}: {value}")
    lines.append("")
    lines.append(f"Out-of-sample net profit (sum across folds): {result.get('oos_total', 0.0):.4f}")
    lines.append("Per-fold detail:")
    for fold in result.get("folds", []):
        lines.append(
            f"  fold {fold['train_fold']}->{fold['eval_fold']}: "
            f"in-sample ${fold['in_sample_net']:.4f}, out-of-sample ${fold['out_sample_net']:.4f}"
        )
    return "\n".join(lines)


def _format_leaderboard(rows: list[tuple[str, float, int]]) -> str:
    header = f"{'event':<30} {'net_profit':>12} {'trades':>8}"
    lines = ["Leaderboard", header, "-" * len(header)]
    for label, net_profit, trades in rows:
        lines.append(f"{label:<30} {net_profit:>12.4f} {trades:>8}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    sys.exit(main())
