# polytrage

Detection, backtesting, and self-tuning of multi-outcome arbitrage on
Polymarket.

## Status

This is a signal-research and backtesting instrument, not a trading bot
— it does not place live orders. That's a council decision, not a
boilerplate disclaimer: a four-voice review on 2026-07-16 found real
methodology risk (lookahead bias in the backtest fill, a cost parameter
the optimizer could Goodhart toward zero, a single-event sample) and set
a 20-30 event bar before any tuning claim gets trusted beyond that. See
[`docs/COUNCIL-2026-07-16.md`](docs/COUNCIL-2026-07-16.md) for the full
verdict, what got fixed in code, and what's still open.

## What it does

Polymarket runs many events as a set of separate binary (YES/NO) markets
over mutually-exclusive outcomes — e.g. "England wins" / "Draw" / "Argentina
wins" for a single match, each with its own order book (a `negRisk` event
on Polymarket). Exactly one of those markets resolves YES, so the fair
value of `sum(YES prices)` across all outcomes is exactly $1.00.

The outcomes are quoted independently and don't always agree:

- **Long**: when `sum(YES) <= $1 - threshold`, buying one YES share of
  every outcome costs less than $1 and is guaranteed to pay out $1 on
  resolution — a locked profit of `$1 - sum(YES)` per basket.
- **Short**: when `sum(YES) >= $1 + threshold`, the mirror trade — buy one
  NO share of every outcome — costs less than `$(n-1)` and is guaranteed to
  pay out `$(n-1)` (exactly one outcome's NO loses, all others win). On
  Polymarket this side is executed via the event's `negRisk` conversion
  rather than as `n` separate limit orders (see Caveats).

polytrage fetches event and price-history data from Polymarket's public
APIs, finds these windows historically, backtests a one-basket-per-window
strategy against them, and runs a walk-forward optimizer to tune the
detection parameters (threshold, gap-merging, minimum window length).
Trading costs (fee, slippage) are frozen pessimistic constants, not
something the optimizer is allowed to search — see Status. A nightly CI
job re-runs the optimizer against newly-closed events and commits new
parameters only when they hold up on data the tuner didn't see.

## Install

Requires Python 3.12+. No third-party runtime dependencies.

```bash
pip install -e ".[dev]"
```

This installs the `polytrage` console script; `pytest` is the only dev
dependency.

## CLI usage

```bash
# Fetch a live/closed event + its price histories, print detected windows
polytrage scan fifwc-eng-arg-2026-07-15

# Backtest against the checked-in fixture — no network, this is what CI runs
polytrage backtest --fixtures

# Backtest a specific event by slug (fetches live from Polymarket)
polytrage backtest fifwc-eng-arg-2026-07-15

# Walk-forward tune parameters against the fixture and write params.json
polytrage optimize --fixtures --out params.json

# Same, against a live slug
polytrage optimize fifwc-eng-arg-2026-07-15 --out params.json

# Point at a different fixtures directory
polytrage backtest --fixtures --fixtures-dir tests/fixtures

# Print the leaderboard built from results/*.json
polytrage report
```

`--fixtures` is the offline mode: it loads `tests/fixtures` (or whatever
`--fixtures-dir` points at) instead of calling the Gamma/CLOB APIs, so
`backtest` and `optimize` produce identical output with no network access —
this is what CI runs.

## Case study: England vs. Argentina, WC semifinal (Jul 15 2026)

The fixture data (`tests/fixtures/event.json` +
`hist_{ENG,DRAW,ARG}.json`) is a real capture of Polymarket's three-way
market for this match, from event creation (Jul 12, 10:05 UTC) through
close (Jul 15, 21:21 UTC — final score England 1–2 Argentina: Gordon 55′,
Fernández 85′, Martínez 90+′).

`polytrage backtest` fills each detected window at its **entry** price —
the edge at the first qualifying minute, i.e. what a strategy watching
the market in real time could actually have acted on — not its peak.
Filling at the peak is lookahead bias: the peak isn't knowable until
after the window has already closed. The peak is still reported
alongside it, labeled as a theoretical ceiling, so the size of that gap
stays visible instead of being quietly assumed away (council finding a,
`docs/COUNCIL-2026-07-16.md`). Costs are frozen at $0/leg fee and
$0.002/leg slippage — pessimistic constants, not values the optimizer is
allowed to search down (finding b).

This is `make backtest`'s literal current output, over the 4,993 minutes
where all three price series align (`threshold=0.005`, `max_gap_s=180s`):

| Metric | Value |
|---|---|
| sum(YES) range | $0.9425 – $1.0212 |
| Trades (windows) | 26 (11 long, 15 short) |
| Gross edge — executed (entry-fill) | $0.2450 |
| Gross edge — theoretical max (peak-fill) | $0.2913 |
| Net profit (after frozen fee + slippage) | $0.0890 |
| Profit / trade | $0.0034 |
| Arb minutes | 1,393 |

The single best trade is unaffected by the entry-vs-peak distinction: a
5.75¢-per-basket long window running 20:46–20:57 UTC, right after
Fernández's 85′ equalizer. With the score tied late and all three
outcomes (England win / draw / Argentina win) simultaneously live, the
three independently-quoted YES prices opened that window already at
their most extreme — $0.9425 — and never moved further, so entry edge
and peak edge are identical (5.75¢ both). A $0.9425 basket (one YES
share of each outcome) was guaranteed to redeem for $1.00 once the match
finished, regardless of the eventual winner.

Not every window is that clean. The longest-lived opportunity — a short
window from 12:45 to 17:59 UTC on game day, before kickoff, where the
three YES prices held persistently at or above $1.005 — shows exactly
the gap the entry-fill fix exists to close: 0.63¢ captured at entry
versus 1.12¢ at that window's eventual peak. Reporting the peak number
there would have overstated what an entry-time strategy could actually
have captured.

These are backtested numbers against midpoint price history from a
single event, not a claim of realized trading profit — see Caveats and
Status.

## The evolve loop

`scripts/evolve.py` runs nightly under CI (cron `17 3 * * *`, plus manual
`workflow_dispatch`):

1. Load the current `params.json` (or `BacktestParams` defaults if none
   exists).
2. Discover recently-closed multi-outcome sports events via the Gamma API
   (or use `--fixtures` for an offline dry run).
3. Fetch and backtest each one.
4. Walk-forward re-tune: tune on fold *i*, evaluate on fold *i+1*.
5. If the re-tuned parameters' out-of-sample `net_profit` beats the stored
   baseline, write the new `params.json`, append a row to
   `results/history.jsonl`, and regenerate `LEADERBOARD.md`. Otherwise,
   leave everything as-is.

The job always exits 0 and prints a summary — it never fails CI, and it
never commits a parameter change that only looked good on the data it was
tuned on. `params.json` is the one artifact this loop mutates over time;
its git history is the record of how the strategy's parameters evolved.
`make evolve` runs the same script locally.

## Caveats

- **Midpoint prices, not executable bid/ask.** `clob.py` pulls
  Polymarket's `prices-history` series — a single price per minute, not
  order-book depth. Backtested edges assume you can transact the full
  basket size at that price; `BacktestParams.slippage` applies a flat
  per-leg haircut to approximate the real spread, but it isn't a
  simulation of actual book depth. Live edges will generally run smaller
  than backtested ones.
- **The short side depends on negRisk conversion.** Buying a NO share of
  every outcome isn't modeled as `n` independent limit orders — it's
  priced as Polymarket's `negRisk` conversion, which mints and redeems a
  full NO basket as a unit for `$(n-1)`. A real execution goes through
  that contract mechanism, not a plain order per leg.
