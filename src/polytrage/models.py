"""Shared datatypes for polytrage. This module is the contract between all
components — data fetches produce these, the engine consumes and emits them,
the backtester and optimizer operate on them. Stdlib only.

All timestamps are unix seconds (UTC). All prices are dollars in [0, 1].
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Market:
    """One binary market (contract) inside an event."""
    id: str
    question: str
    yes_token: str          # CLOB token id of the YES outcome
    no_token: str           # CLOB token id of the NO outcome
    volume: float = 0.0


@dataclass(frozen=True)
class Event:
    """A mutually-exclusive multi-outcome event (negRisk on Polymarket).

    The arbitrage invariant: exactly one market resolves YES, so the fair
    sum of all YES prices is $1.00.
    """
    id: str
    slug: str
    title: str
    markets: tuple[Market, ...]
    neg_risk: bool = True
    closed: bool = False


@dataclass(frozen=True)
class PricePoint:
    t: int                  # unix seconds
    p: float                # price in dollars


# A price series per yes_token: {token_id: [PricePoint, ...]} sorted by t.
Series = dict[str, list[PricePoint]]


@dataclass(frozen=True)
class AlignedRow:
    """Prices of every outcome's YES token at one aligned minute."""
    t: int
    prices: tuple[float, ...]   # same order as Event.markets

    @property
    def total(self) -> float:
        return sum(self.prices)


@dataclass(frozen=True)
class ArbWindow:
    """A contiguous run of minutes where an arbitrage condition held.

    side: "long"  — sum(YES) < 1 - threshold: buy every YES, collect $1.
          "short" — sum(YES) > 1 + threshold: buy every NO for < $(n-1),
                    collect $(n-1).
    """
    side: str               # "long" | "short"
    start: int              # unix seconds, first minute in window
    end: int                # unix seconds, last minute in window
    peak_sum: float         # most extreme sum observed
    peak_t: int             # when the extreme occurred
    minutes: int            # number of qualifying data points

    @property
    def edge(self) -> float:
        """Best gross edge in dollars per $1 basket."""
        return abs(1.0 - self.peak_sum)


@dataclass
class BacktestParams:
    """Tunable strategy parameters. The optimizer searches over these."""
    threshold: float = 0.005        # min deviation from $1 to act on
    fee: float = 0.0                # taker fee per $ notional (Polymarket: 0)
    slippage: float = 0.001         # haircut per leg vs midpoint, dollars
    max_gap_s: int = 180            # merge windows separated by <= this
    min_window_minutes: int = 1     # ignore shorter windows


@dataclass
class BacktestResult:
    params: BacktestParams
    windows: list[ArbWindow] = field(default_factory=list)
    trades: int = 0
    gross_edge: float = 0.0         # sum of captured edge, $ per $1 basket
    net_profit: float = 0.0         # after fee + slippage
    arb_minutes: int = 0
    events: int = 1

    @property
    def profit_per_trade(self) -> float:
        return self.net_profit / self.trades if self.trades else 0.0
