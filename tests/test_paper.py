"""Unit tests for the paper-trading decision/settlement math."""
from polytrage.capture.paper import PaperBook, grade_fill_persistence


def _tick(ts: float, bids: list[float], sizes: list[float], short_baskets: float) -> dict:
    return {"type": "tick", "ts": ts, "short_baskets": short_baskets,
            "legs": [{"bb": b, "bbs": s, "ba": b + 0.01, "bas": 100.0, "mid": b + 0.005}
                     for b, s in zip(bids, sizes)]}


def test_fill_math_and_caps():
    book = PaperBook(cap_baskets=100, max_per_tick=30)
    f = book.on_tick(_tick(1.0, [0.36, 0.33, 0.33], [40, 60, 90], short_baskets=40))
    assert f is not None
    assert f.baskets == 30                       # throttled by max_per_tick
    assert abs(f.cost - (3 - 1.02) * 30) < 1e-9  # buy 3 NOs at 1-bid each
    assert abs(f.locked_edge - 0.02 * 30) < 1e-9
    # depth cap: smallest displayed size wins
    f2 = book.on_tick(_tick(2.0, [0.36, 0.33, 0.33], [5, 60, 90], short_baskets=40))
    assert f2 is not None and f2.baskets == 5
    # position cap
    book.fills[0].baskets = 99
    assert book.on_tick(_tick(3.0, [0.36, 0.33, 0.33], [40, 60, 90], 40)).baskets <= 1


def test_no_fill_when_no_arb_or_missing_bid():
    book = PaperBook()
    assert book.on_tick(_tick(1.0, [0.3, 0.3, 0.3], [10, 10, 10], short_baskets=0)) is None
    t = _tick(1.0, [0.36, 0.33, 0.33], [10, 10, 10], 5)
    t["legs"][1]["bb"] = None
    assert book.on_tick(t) is None


def test_settlement_live_winner_vs_windfall():
    book = PaperBook(cap_baskets=10, max_per_tick=10)
    book.on_tick(_tick(1.0, [0.36, 0.33, 0.33], [10, 10, 10], short_baskets=10))
    s = book.settle(winner_leg=0)
    # cost 10*(3-1.02)=19.8, payout 10*2=20 -> pnl 0.2 == locked edge
    assert abs(s["pnl"] - 0.2) < 1e-9
    assert abs(s["pnl"] - s["locked_edge_at_fill"]) < 1e-9
    w = book.settle(winner_leg=None)             # dead-tail winner: every NO pays
    assert abs(w["pnl"] - (30 - 19.8)) < 1e-9 and w["windfall"]


def test_grade_fill_persistence():
    fill_bids = [0.36, 0.33]
    solid = [_tick(t, [0.36, 0.33], [9, 9], 1) for t in (2.0, 3.0)]
    gone = [_tick(t, [0.35, 0.33], [9, 9], 0) for t in (2.0, 3.0)]
    assert grade_fill_persistence(fill_bids, solid, 5.0, 1.0) == "solid"
    assert grade_fill_persistence(fill_bids, gone, 5.0, 1.0) == "partial"
    assert grade_fill_persistence(fill_bids, [], 5.0, 1.0) == "unknown"
