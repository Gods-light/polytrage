"""Paper trader over recorded capture ticks. Pure decision/settlement math —
the runner script does the file-tailing I/O.

Strategy (short side): when the tick shows executable short arb
(short_baskets > 0, i.e. sum of live-leg YES bids > 1 + handicap), simulate
buying NO on every live leg — equivalent to hitting each leg's YES bid, so
fills are priced at the recorded bids and capped by recorded bid sizes.

Basket economics (n live legs, dead tail excluded = conservative):
  cost/basket   = n - sum(bid_i)          (buy each NO at 1 - bid_i)
  min payout    = n - 1                   (winner among live legs)
  windfall      = n                       (winner in the dead tail)
  locked profit = sum(bid_i) - 1          per basket, the recorded edge

Fill realism: fills only up to displayed size at the touch; MAX_PER_TICK
throttles; fill-quality is graded afterwards by whether the hit bid level
persisted in subsequent ticks (a vanished bid = we were racing a cancel).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PaperFill:
    ts: float
    baskets: float
    bids: list[float]            # per-leg YES bid we hit
    sizes: list[float]           # displayed size at each bid
    cost: float                  # dollars paid (n - sum_bids) * baskets
    locked_edge: float           # (sum_bids - 1) * baskets


@dataclass
class PaperBook:
    cap_baskets: float = 500.0
    max_per_tick: float = 50.0
    fills: list[PaperFill] = field(default_factory=list)

    @property
    def position(self) -> float:
        return sum(f.baskets for f in self.fills)

    @property
    def cost(self) -> float:
        return sum(f.cost for f in self.fills)

    @property
    def locked_edge(self) -> float:
        return sum(f.locked_edge for f in self.fills)

    def on_tick(self, tick: dict) -> PaperFill | None:
        """Consider one recorded tick; return the fill made, if any."""
        if tick.get("type") != "tick" or tick.get("short_baskets", 0) <= 0:
            return None
        legs = tick.get("legs", [])
        if not legs or any(l.get("bb") is None for l in legs):
            return None
        room = self.cap_baskets - self.position
        if room <= 0:
            return None
        bids = [l["bb"] for l in legs]
        sizes = [l["bbs"] for l in legs]
        q = min(room, self.max_per_tick, tick["short_baskets"], min(sizes))
        if q <= 0:
            return None
        sum_bids = sum(bids)
        n = len(legs)
        fill = PaperFill(ts=tick["ts"], baskets=round(q, 2), bids=bids, sizes=sizes,
                         cost=round((n - sum_bids) * q, 4),
                         locked_edge=round((sum_bids - 1) * q, 4))
        self.fills.append(fill)
        return fill

    def settle(self, winner_leg: int | None) -> dict:
        """Final PnL. winner_leg = index of winning live leg, None = dead-tail
        winner (every NO pays)."""
        n_legs = len(self.fills[0].bids) if self.fills else 0
        payout_per_basket = float(n_legs if winner_leg is None else n_legs - 1)
        payout = payout_per_basket * self.position
        return {
            "baskets": round(self.position, 2),
            "cost": round(self.cost, 4),
            "payout": round(payout, 4),
            "pnl": round(payout - self.cost, 4),
            "locked_edge_at_fill": round(self.locked_edge, 4),
            "windfall": winner_leg is None,
        }


def grade_fill_persistence(fill_bids: list[float], later_ticks: list[dict],
                           horizon_s: float, fill_ts: float) -> str:
    """Would our fills have stood? Check whether each hit bid level still had
    a bid at-or-better in ticks up to horizon_s after the fill.
    Returns "solid" (all legs persisted), "partial", or "contested" (none)."""
    window = [t for t in later_ticks
              if t.get("type") == "tick" and fill_ts < t["ts"] <= fill_ts + horizon_s]
    if not window:
        return "unknown"
    persisted = 0
    for i, hit in enumerate(fill_bids):
        ok = all((t["legs"][i]["bb"] or 0) >= hit - 1e-9 for t in window if i < len(t.get("legs", [])))
        persisted += 1 if ok else 0
    if persisted == len(fill_bids):
        return "solid"
    return "partial" if persisted else "contested"
