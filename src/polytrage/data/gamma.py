"""Polymarket Gamma API client — event discovery and metadata.

Fetchers (`fetch_event`, `search_events`) do HTTP I/O; `parse_event` is pure
and is what tests exercise against `tests/fixtures/event.json`.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from polytrage.models import Event, Market

BASE_URL = "https://gamma-api.polymarket.com"

_USER_AGENT = "Mozilla/5.0"
_TIMEOUT = 30


def _get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gamma request failed for {url}: {exc}") from exc


def fetch_event(slug: str) -> Event:
    """Fetch one event by its slug from `GET /events?slug=...`."""
    url = f"{BASE_URL}/events?slug={urllib.parse.quote(slug)}"
    data = _get_json(url)
    if not data:
        raise RuntimeError(f"no event found for slug {slug!r} at {url}")
    return parse_event(data[0])


def search_events(q: str) -> list[Event]:
    """Search events by free-text query via `GET /public-search`."""
    url = f"{BASE_URL}/public-search?q={urllib.parse.quote(q)}&events_status=all"
    data = _get_json(url)
    return [parse_event(raw) for raw in data.get("events", [])]


def parse_event(raw: dict) -> Event:
    """Parse one raw Gamma event dict into an Event. Pure — no I/O.

    `raw["markets"][*]["clobTokenIds"]` is a JSON-encoded string list
    `[yes_token, no_token]` and must be decoded with json.loads.
    """
    markets = tuple(_parse_market(m) for m in raw["markets"])
    return Event(
        id=str(raw["id"]),
        slug=raw["slug"],
        title=raw["title"],
        markets=markets,
        neg_risk=bool(raw.get("negRisk", True)),
        closed=bool(raw.get("closed", False)),
    )


def _parse_market(raw: dict) -> Market:
    yes_token, no_token = json.loads(raw["clobTokenIds"])
    return Market(
        id=str(raw["id"]),
        question=raw["question"],
        yes_token=yes_token,
        no_token=no_token,
        volume=float(raw.get("volume", 0.0)),
    )
