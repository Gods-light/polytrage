# polytrage — module contracts (team build spec)

Every agent owns exactly the files listed for it. Never touch another module's
files. Shared types live in `src/polytrage/models.py` (already written — read
it first, import from it, do not modify it). Stdlib only, Python 3.12+, full
type hints. Each module ships its own pytest file; tests must pass offline
using `tests/fixtures/` (no network in tests).

## Fixtures (real data, England vs Argentina WC semifinal, Jul 12–15 2026)
- `tests/fixtures/event.json` — Gamma API response: list with one event dict;
  `markets[*].clobTokenIds` is a JSON-encoded string list `[yes, no]`;
  `outcomePrices` likewise. 3 markets (ENG win / draw / ARG win).
- `tests/fixtures/hist_{ENG,DRAW,ARG}.json` — CLOB `prices-history` `history`
  arrays: `[{"t": unix_sec, "p": price}, ...]`, ~5k one-minute points each.
- Ground truth on this data with threshold=0.005 inclusive, max_gap_s=180:
  sum(YES) ranges 0.9425–1.0212; best long edge 5.75c at t where sum=0.9425.

## data/ — owner: data-eng
Files: `src/polytrage/data/__init__.py`, `gamma.py`, `clob.py`, `store.py`,
`tests/test_data.py`.
- `gamma.py`: `fetch_event(slug) -> Event`, `search_events(q) -> list[Event]`,
  `parse_event(raw: dict) -> Event` (pure — testable from fixture).
  Base `https://gamma-api.polymarket.com`.
- `clob.py`: `fetch_history(token_id, start_ts, end_ts, fidelity=1) -> list[PricePoint]`,
  `parse_history(raw: dict) -> list[PricePoint]` (pure).
  Base `https://clob.polymarket.com/prices-history`.
  CRITICAL: the CLOB 403s python's default urllib User-Agent — always send
  `User-Agent: Mozilla/5.0`. Use urllib.request, timeout=30, raise RuntimeError
  with url on HTTP errors.
- `store.py`: `save_series(dir, event, series)`, `load_series(dir, slug) ->
  (Event, Series)` — plain JSON files under a data dir; used by evolve loop.
- Tests: parse functions against fixtures only (no live HTTP).

## engine/ — owner: arb-eng
Files: `src/polytrage/engine/__init__.py`, `align.py`, `arb.py`,
`tests/test_engine.py`.
- `align.py`: `align(series: Series, order: list[str]) -> list[AlignedRow]` —
  floor timestamps to the minute, keep minutes present in EVERY series.
- `arb.py`: `find_windows(rows, params: BacktestParams) -> list[ArbWindow]` —
  detect long (sum <= 1-threshold) and short (sum >= 1+threshold) windows,
  merging gaps <= max_gap_s, dropping windows shorter than min_window_minutes.
  Also `instant_edge(prices: Sequence[float], side) -> float` (gross edge of
  acting now) and `basket_cost(prices, side) -> float`.
- Tests: hand-built tiny rows with known windows + the real fixture (expect
  min sum 0.9425 ± 1e-4, and that a long window containing that minute exists).

## backtest/ — owner: backtest-eng
Files: `src/polytrage/backtest/__init__.py`, `runner.py`, `metrics.py`,
`tests/test_backtest.py`.
- `runner.py`: `run(rows, params) -> BacktestResult` — call
  `engine.arb.find_windows`, simulate one $1-basket trade per window at its
  peak: gross = window.edge, net = gross - fee*basket - slippage*n_legs.
  Fill BacktestResult fully.
- `metrics.py`: `summarize(results: list[BacktestResult]) -> dict` — totals,
  profit per trade, windows/day, edge histogram buckets; plus
  `to_markdown(summary) -> str` for reports.
- Import engine via `from polytrage.engine import arb` — the module will exist;
  code against the contract above even if writing in parallel.

## optimize/ — owner: opt-eng
Files: `src/polytrage/optimize/__init__.py`, `tuner.py`, `tests/test_optimize.py`.
- `tuner.py`: `grid(thresholds, slippages, ...) -> Iterator[BacktestParams]`;
  `tune(rows, param_grid) -> tuple[BacktestParams, BacktestResult]` — maximize
  net_profit; `walk_forward(rows, param_grid, folds=3) -> dict` — tune on fold
  i, evaluate on fold i+1, report in/out-of-sample profits (overfit check).
- Uses `backtest.runner.run`. Deterministic, no randomness.

## cli — owner: cli-eng
Files: `src/polytrage/cli.py`, `tests/test_cli.py`.
- argparse subcommands:
  `scan <slug>` — fetch live event + histories, print windows table;
  `backtest <slug|--fixtures>` — run backtest, print metrics markdown;
  `optimize <slug|--fixtures>` — walk-forward tune, print best params, write
  `params.json` (via `--out`);
  `report` — read `results/*.json`, print leaderboard.
- `--fixtures` mode loads tests/fixtures (path via `--fixtures-dir`, default
  `tests/fixtures`) so CI runs with no network. Keep I/O thin; all logic in
  the other modules. main(argv=None) -> int for testability.

## scripts + CI — owner: cicd-eng
Files: `scripts/evolve.py`, `.github/workflows/ci.yml`, `Makefile`,
`.gitignore`, `results/.gitkeep`.
- `evolve.py`: the self-evolution loop. Steps: (1) load current
  `params.json` (default BacktestParams if missing); (2) discover recently
  closed multi-outcome sports events via gamma (`data.gamma`), or use
  `--fixtures` offline mode; (3) fetch/backtest each; (4) walk-forward re-tune;
  (5) if out-of-sample net_profit improves on stored baseline, write new
  `params.json` + append `results/history.jsonl` + regenerate `LEADERBOARD.md`;
  exit 0 always, print a summary. Must run offline with `--fixtures`.
- `ci.yml`: job `test` (push/PR: `pip install -e .[dev]`, `pytest -q`);
  job `evolve` (schedule: cron "17 3 * * *" + workflow_dispatch; runs
  `python scripts/evolve.py --fixtures` then also live mode allowed to fail;
  commits changed params.json/LEADERBOARD.md/results with
  `github-actions[bot]` identity, `git pull --rebase` before push).
- Makefile: `test`, `backtest`, `optimize`, `evolve`, `install` targets.

## docs — owner: docs-eng
Files: `README.md`, `ARCHITECTURE.md`.
- README: what it is, install, CLI usage, the ENG-ARG case study numbers
  (sum range 0.9425–1.0212; 5.75c peak edge in-game at the 85' equalizer),
  evolve loop explanation. ARCHITECTURE: module map, dataflow diagram
  (ascii), design decisions (midpoint caveat, negRisk, UA gotcha, offline-CI).

## Conventions
- No third-party runtime deps. Tests: pytest, no network, < 10s total.
- Run `python3 -m pytest tests/<your file> -q` before declaring done.
- Money in floats is fine here (cent precision), round display to 4 decimals.
