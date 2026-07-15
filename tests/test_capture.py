"""Unit tests for the pure book/executable-arb math in capture.books."""
from polytrage.capture.books import Book, sweep_baskets, tick_metrics


def _book(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> Book:
    b = Book()
    b.load_snapshot(
        [{"price": p, "size": s} for p, s in bids],
        [{"price": p, "size": s} for p, s in asks],
    )
    return b


def test_book_snapshot_and_best_levels():
    b = _book([(0.40, 100), (0.39, 50)], [(0.42, 80), (0.45, 200)])
    assert b.best_bid == (0.40, 100)
    assert b.best_ask == (0.42, 80)
    assert b.mid == (0.40 + 0.42) / 2


def test_apply_change_updates_and_deletes():
    b = _book([(0.40, 100)], [(0.42, 80)])
    b.apply_change(0.41, "BUY", 30)
    assert b.best_bid == (0.41, 30)
    b.apply_change(0.41, "BUY", 0)
    assert b.best_bid == (0.40, 100)
    b.apply_change(0.42, "SELL", 0)
    assert b.best_ask is None


def test_sweep_long_single_level():
    # 3 legs, best asks sum to 0.97 -> 3c edge, capped by smallest size (50)
    books = [
        _book([], [(0.35, 100)]),
        _book([], [(0.32, 50)]),
        _book([], [(0.30, 200)]),
    ]
    baskets, edge = sweep_baskets(books, "long")
    assert baskets == 50
    assert abs(edge - 50 * 0.03) < 1e-9


def test_sweep_long_walks_deeper_levels_until_sum_hits_one():
    # First 10 baskets at sum 0.97 (3c), then leg2's next level makes sum 0.99
    # (1c) for 20 more, then 1.01 -> stop.
    books = [
        _book([], [(0.35, 1000)]),
        _book([], [(0.32, 10), (0.34, 20), (0.36, 500)]),
        _book([], [(0.30, 1000)]),
    ]
    baskets, edge = sweep_baskets(books, "long")
    assert baskets == 30
    assert abs(edge - (10 * 0.03 + 20 * 0.01)) < 1e-9


def test_sweep_short_uses_bids_above_one():
    # Best bids sum 1.02 -> 2c short edge for min-size 40, next level sum 0.998 -> stop
    books = [
        _book([(0.36, 40), (0.33, 100)], []),
        _book([(0.33, 60)], []),
        _book([(0.33, 90)], []),
    ]
    baskets, edge = sweep_baskets(books, "short")
    assert baskets == 40
    assert abs(edge - 40 * 0.02) < 1e-9


def test_sweep_not_profitable_returns_zero():
    books = [_book([], [(0.50, 100)]), _book([], [(0.51, 100)])]
    assert sweep_baskets(books, "long") == (0.0, 0.0)


def test_tick_metrics_none_until_all_books_present():
    assert tick_metrics([_book([(0.4, 1)], [(0.42, 1)]), Book()]) is None


def test_tick_metrics_sums_and_executables():
    books = [
        _book([(0.36, 40)], [(0.37, 100)]),
        _book([(0.33, 60)], [(0.34, 50)]),
        _book([(0.33, 90)], [(0.28, 200)]),
    ]
    m = tick_metrics(books)
    assert m is not None
    assert abs(m["sum_ask"] - 0.99) < 1e-9      # long arb executable
    assert abs(m["sum_bid"] - 1.02) < 1e-9      # short arb executable
    assert m["long_baskets"] == 50              # capped by leg2 ask size
    assert m["short_baskets"] == 40             # capped by leg1 bid size
    assert abs(m["long_edge"] - 50 * 0.01) < 1e-6
    assert abs(m["short_edge"] - 40 * 0.02) < 1e-6
