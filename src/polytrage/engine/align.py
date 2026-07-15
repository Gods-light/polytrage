"""Align per-token price series onto a shared per-minute time grid."""
from __future__ import annotations

from polytrage.models import AlignedRow, Series


def align(series: Series, order: list[str]) -> list[AlignedRow]:
    """Floor every point's timestamp down to the minute (t // 60 * 60).

    If a token has multiple points in the same minute, the last one (series
    are sorted by t) wins. Only minutes present in every token's series are
    kept. Returns rows ascending by time, with `prices` ordered per `order`.
    """
    minute_prices: dict[str, dict[int, float]] = {}
    for token in order:
        prices_by_minute: dict[int, float] = {}
        for point in series[token]:
            minute = (point.t // 60) * 60
            prices_by_minute[minute] = point.p
        minute_prices[token] = prices_by_minute

    common_minutes = set(minute_prices[order[0]])
    for token in order[1:]:
        common_minutes &= minute_prices[token].keys()

    return [
        AlignedRow(
            t=minute,
            prices=tuple(minute_prices[token][minute] for token in order),
        )
        for minute in sorted(common_minutes)
    ]
