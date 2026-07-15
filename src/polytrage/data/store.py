"""Plain-JSON persistence for an Event and its price Series, used by the
evolve loop to cache fetched data between runs.
"""
from __future__ import annotations

import json
from pathlib import Path

from polytrage.models import Event, Market, PricePoint, Series


def save_series(dir: str | Path, event: Event, series: Series) -> None:
    """Write `event` and `series` to `<dir>/<event.slug>.json`."""
    path = Path(dir)
    path.mkdir(parents=True, exist_ok=True)
    payload = {"event": _event_to_dict(event), "series": _series_to_dict(series)}
    (path / f"{event.slug}.json").write_text(json.dumps(payload))


def load_series(dir: str | Path, slug: str) -> tuple[Event, Series]:
    """Read back the Event and Series previously saved for `slug`."""
    payload = json.loads((Path(dir) / f"{slug}.json").read_text())
    return _event_from_dict(payload["event"]), _series_from_dict(payload["series"])


def _event_to_dict(event: Event) -> dict:
    return {
        "id": event.id,
        "slug": event.slug,
        "title": event.title,
        "neg_risk": event.neg_risk,
        "closed": event.closed,
        "markets": [
            {
                "id": m.id,
                "question": m.question,
                "yes_token": m.yes_token,
                "no_token": m.no_token,
                "volume": m.volume,
            }
            for m in event.markets
        ],
    }


def _event_from_dict(d: dict) -> Event:
    markets = tuple(
        Market(
            id=m["id"],
            question=m["question"],
            yes_token=m["yes_token"],
            no_token=m["no_token"],
            volume=m["volume"],
        )
        for m in d["markets"]
    )
    return Event(
        id=d["id"],
        slug=d["slug"],
        title=d["title"],
        markets=markets,
        neg_risk=d["neg_risk"],
        closed=d["closed"],
    )


def _series_to_dict(series: Series) -> dict:
    return {token: [{"t": p.t, "p": p.p} for p in points] for token, points in series.items()}


def _series_from_dict(d: dict) -> Series:
    return {
        token: [PricePoint(t=p["t"], p=p["p"]) for p in points] for token, points in d.items()
    }
