# polytrage

Detection, backtesting, and self-tuning of multi-outcome arbitrage on
Polymarket.

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
detection parameters (threshold, slippage, gap-merging, minimum window
length). A nightly CI job re-runs the optimizer against newly-closed
events and commits new parameters only when they hold up on data the
tuner didn't see.

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

Running `polytrage backtest --fixtures` with default parameters
(`threshold=0.005`, `max_gap_s=180`) against the 4,993 minutes where all
three price series align:

| Metric | Value |
|---|---|
| sum(YES) range | $0.9425 – $1.0212 |
| Long windows (sum ≤ $0.995) | ~10 |
| Short windows (sum ≥ $1.005) | ~15 |
| Best edge | 5.75¢ per $1 basket |
| Best edge duration | 12 minutes |
| Best edge timing | starting right after Fernández's 85′ equalizer |
| Longest window | ~5.25 hours, pre-match on game day |

The best edge appears in the minutes after the 85′ equalizer: with the
score tied late and all three outcomes (England win / draw / Argentina
win) simultaneously live, the three independently-quoted YES prices
summed to $0.9425 — 5.75¢ below fair value. A $0.9425 basket (one YES
share of each outcome) was guaranteed to redeem for $1.00 once the match
finished, regardless of the eventual winner.

The longest-lived opportunity was a ~5.25-hour short window earlier on
game day, before kickoff, where the three YES prices held persistently at
or above $1.005 — the mirror trade, holding a NO basket, would have
redeemed for more than it cost.

These are backtested numbers against midpoint price history, not a claim
of realized trading profit — see Caveats.

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
