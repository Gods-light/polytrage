"""Pure order-book state + executable-arbitrage math. No I/O, fully testable.

Long arb executes against ASKS (buy every YES): executable when the sum of
best asks < $1. Short arb executes against BIDS (sell every YES / negRisk
convert): executable when the sum of best bids > $1. `sweep_baskets` walks
all legs' ladders jointly to answer the council's question: how many $1
baskets could actually have been executed inside the window, at what edge.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Book:
    """One asset's ladder state. Prices/sizes as floats; size 0 deletes."""
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)

    def load_snapshot(self, bids: list[dict], asks: list[dict]) -> None:
        self.bids = {float(l["price"]): float(l["size"]) for l in bids if float(l["size"]) > 0}
        self.asks = {float(l["price"]): float(l["size"]) for l in asks if float(l["size"]) > 0}

    def apply_change(self, price: float, side: str, size: float) -> None:
        ladder = self.bids if side.upper() == "BUY" else self.asks
        if size <= 0:
            ladder.pop(price, None)
        else:
            ladder[price] = size

    @property
    def best_bid(self) -> tuple[float, float] | None:
        if not self.bids:
            return None
        p = max(self.bids)
        return p, self.bids[p]

    @property
    def best_ask(self) -> tuple[float, float] | None:
        if not self.asks:
            return None
        p = min(self.asks)
        return p, self.asks[p]

    @property
    def mid(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb and ba:
            return (bb[0] + ba[0]) / 2
        if ba:
            return ba[0]
        if bb:
            return bb[0]
        return None


def sweep_baskets(books: list[Book], side: str, max_baskets: float = 1e9) -> tuple[float, float]:
    """Jointly walk every leg's ladder and return (baskets, edge_dollars).

    side="long":  consume asks; profitable while sum of current level
                  prices < 1; edge per basket = 1 - sum.
    side="short": consume bids; profitable while sum > 1; edge = sum - 1.

    A "basket" is one unit of every leg. Returns total executable baskets
    and total gross edge in dollars, both 0.0 if the top of book is not
    profitable.
    """
    ladders: list[list[tuple[float, float]]] = []
    for b in books:
        src = b.asks if side == "long" else b.bids
        if not src:
            return 0.0, 0.0
        ladders.append(sorted(src.items(), key=lambda x: x[0], reverse=(side == "short")))

    idx = [0] * len(ladders)
    rem = [ladders[i][0][1] for i in range(len(ladders))]
    baskets = 0.0
    edge = 0.0
    while baskets < max_baskets:
        prices = [ladders[i][idx[i]][0] for i in range(len(ladders))]
        s = sum(prices)
        gap = (1.0 - s) if side == "long" else (s - 1.0)
        if gap <= 0:
            break
        q = min(min(rem), max_baskets - baskets)
        baskets += q
        edge += gap * q
        exhausted = False
        for i in range(len(ladders)):
            rem[i] -= q
            if rem[i] <= 1e-9:
                idx[i] += 1
                if idx[i] >= len(ladders[i]):
                    exhausted = True
                else:
                    rem[i] = ladders[i][idx[i]][1]
        if exhausted:
            break
    return baskets, edge


def tick_metrics(books: list[Book]) -> dict | None:
    """One measurement row across all legs; None until every leg has a book."""
    tops = []
    for b in books:
        bb, ba = b.best_bid, b.best_ask
        m = b.mid
        if m is None:
            return None
        tops.append({
            "bb": bb[0] if bb else None, "bbs": bb[1] if bb else 0.0,
            "ba": ba[0] if ba else None, "bas": ba[1] if ba else 0.0,
            "mid": m,
        })
    sum_mid = sum(t["mid"] for t in tops)
    sum_ask = sum(t["ba"] for t in tops) if all(t["ba"] is not None for t in tops) else None
    sum_bid = sum(t["bb"] for t in tops) if all(t["bb"] is not None for t in tops) else None
    long_b, long_e = sweep_baskets(books, "long") if sum_ask is not None else (0.0, 0.0)
    short_b, short_e = sweep_baskets(books, "short") if sum_bid is not None else (0.0, 0.0)
    return {
        "sum_mid": round(sum_mid, 5),
        "sum_ask": round(sum_ask, 5) if sum_ask is not None else None,
        "sum_bid": round(sum_bid, 5) if sum_bid is not None else None,
        "long_baskets": round(long_b, 2), "long_edge": round(long_e, 5),
        "short_baskets": round(short_b, 2), "short_edge": round(short_e, 5),
        "legs": tops,
    }
