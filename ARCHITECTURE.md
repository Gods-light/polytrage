# Architecture

## Module map

| Path | Owner | Responsibility |
|---|---|---|
| `src/polytrage/models.py` | shared contract | Frozen dataclasses every other module imports: `Market`, `Event`, `PricePoint`, `Series`, `AlignedRow`, `ArbWindow`, `BacktestParams`, `BacktestResult`. |
| `src/polytrage/data/` | data-eng | `gamma.py` (event fetch/search/parse), `clob.py` (price-history fetch/parse), `store.py` (JSON persistence used by the evolve loop). |
| `src/polytrage/engine/` | arb-eng | `align.py` (align multiple price series to common minutes), `arb.py` (detect long/short arbitrage windows). Pure functions, no I/O. |
| `src/polytrage/backtest/` | backtest-eng | `runner.py` (simulate one trade per window), `metrics.py` (aggregate results + markdown report). |
| `src/polytrage/optimize/` | opt-eng | `tuner.py` (grid search + walk-forward validation over `backtest.runner.run`). |
| `src/polytrage/cli.py` | cli-eng | argparse entrypoint: `scan` / `backtest` / `optimize` / `report`. |
| `scripts/evolve.py` | cicd-eng | Nightly self-tuning loop: discover events, backtest, walk-forward re-tune, commit `params.json` on improvement. |
| `.github/workflows/ci.yml`, `Makefile` | cicd-eng | `test` job (every push/PR) and `evolve` job (scheduled + manual dispatch). |

## Dataflow

```
  scripts/evolve.py                                    polytrage (cli.py)
  (CI job "evolve":                                     scan / backtest /
   cron 17 3 * * * + workflow_dispatch)                 optimize / report
             |                                                  |
             +---------------------+----------------------------+
                                    |
                                    v
                  +--------------------------------------+
                  |  data/                                |
                  |   gamma.py : fetch_event,              |
                  |              search_events   -> Event  |
                  |   clob.py  : fetch_history              |
                  |              (Mozilla UA)  -> list[PricePoint]
                  |   store.py : save_series /              |
                  |              load_series   -> JSON (evolve.py only)
                  +--------------------+-------------------+
                                       | Event, Series
                                       v
                  +--------------------------------------+
                  |  engine/                              |
                  |   align.py : align(series, order)     |
                  |              -> list[AlignedRow]      |
                  |   arb.py   : find_windows(rows, params)|
                  |              -> list[ArbWindow]       |
                  +--------------------+-------------------+
                                       | AlignedRow[], ArbWindow[]
                                       v
                  +--------------------------------------+
                  |  backtest/                            |
                  |   runner.py  : run(rows, params)       |
                  |                -> BacktestResult       |
                  |   metrics.py : summarize(results) -> dict
                  |                to_markdown(summary) -> str
                  +--------------------+-------------------+
                                       | BacktestResult
                                       v
                  +--------------------------------------+
                  |  optimize/tuner.py                    |
                  |   grid(...)         -> Iterator[BacktestParams]
                  |   tune(rows, grid)  -> (BacktestParams, BacktestResult)
                  |   walk_forward(...) -> dict (in- vs out-of-sample profit)
                  |   [wraps backtest.runner.run once per candidate params]
                  +--------------------+-------------------+
                                       |
                                       v
                       params.json  (the evolving artifact)
                                       |
                    evolve.py, only if out-of-sample net_profit improves:
                                       v
           results/history.jsonl + LEADERBOARD.md --> git commit (github-actions[bot])
```

Both entrypoints drive the same core pipeline (`data` → `engine.align` →
`engine.arb` → `backtest.runner` → `backtest.metrics`); `optimize/tuner.py`
doesn't sit in that line, it wraps `backtest.runner.run`, calling it once
per candidate `BacktestParams` from `grid()` and keeping whichever scores
best. `cli.py` is the human-driven entrypoint (prints tables/markdown to
stdout); `scripts/evolve.py` is the machine-driven one (mutates
`params.json` and commits). The `Makefile` wraps common invocations
(`make test`, `make backtest`, `make optimize`, `make evolve`,
`make install`).

## Design decisions

**Stdlib only.** `pyproject.toml` declares zero runtime dependencies;
`pytest` is the only dev dependency. `pip install -e ".[dev]"` is fast and
deterministic, and the whole pipeline (network fetch aside) runs in any
Python 3.12 environment with nothing to vendor or pin.

**Pure-parse vs fetch separation.** Both `gamma.py` and `clob.py` split
network I/O (`fetch_*`) from parsing (`parse_*`). The parse functions take
an already-fetched `dict` and return typed values with no I/O of their
own, so every module's tests run against `tests/fixtures/*.json` with zero
network calls — the parse functions are what the test suite actually
exercises.

**Mozilla UA requirement on CLOB.** `clob.polymarket.com/prices-history`
returns 403 for urllib's default User-Agent string. `clob.py` always sends
`User-Agent: Mozilla/5.0`; this is a hosting-side check on the client
string rather than a documented API requirement, so it's called out here
instead of being left to be rediscovered.

**Offline fixtures in CI.** `--fixtures` (default dir `tests/fixtures`)
lets `backtest`, `optimize`, and `evolve.py` run with no network access,
using the real England-vs-Argentina capture as ground truth. `ci.yml`'s
`test` job runs on every push/PR with no external dependency or flakiness
risk; the `evolve` job runs `evolve.py --fixtures` unconditionally and only
then attempts a live-data pass, which is allowed to fail without failing
the job.

**Inclusive thresholds.** `arb.py` treats `sum(YES) <= 1 - threshold` and
`sum(YES) >= 1 + threshold` as qualifying minutes (not strict `<` / `>`).
This matches the fixture's validated ground truth (`threshold=0.005`,
`max_gap_s=180` → range 0.9425–1.0212) and means a boundary price counts
as an opportunity instead of being silently excluded.

**Walk-forward to prevent overfit.** `optimize/tuner.py`'s `walk_forward`
tunes parameters on fold *i* and scores them on fold *i+1* — parameters
never get credit on the same data they were fit to. `evolve.py` only
commits a new `params.json` when the out-of-sample number improves, so the
nightly loop can't quietly overfit to a handful of recently closed events.

**`params.json` as the evolving artifact.** It's the one piece of mutable
state the whole system revolves around: `optimize` writes it directly,
`evolve.py` rewrites it automatically, `backtest`/`scan` can be pointed at
it, and its git history — together with `results/history.jsonl` and
`LEADERBOARD.md` — is a running record of how the strategy's parameters
have drifted over time and why each change was accepted.
