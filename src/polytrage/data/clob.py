"""Polymarket CLOB client — YES-token price history.

`fetch_history` does HTTP I/O; `parse_history` is pure and is what tests
exercise against `tests/fixtures/hist_*.json`.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from polytrage.models import PricePoint

BASE_URL = "https://clob.polymarket.com/prices-history"

# CRITICAL: the CLOB rejects urllib's default User-Agent with a 403.
_USER_AGENT = "Mozilla/5.0"
_TIMEOUT = 30


def fetch_history(
    token_id: str, start_ts: int, end_ts: int, fidelity: int = 1
) -> list[PricePoint]:
    """Fetch a YES token's price history for [start_ts, end_ts] (unix seconds)."""
    url = (
        f"{BASE_URL}?market={token_id}&startTs={start_ts}&endTs={end_ts}"
        f"&fidelity={fidelity}"
    )
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            raw = json.loads(response.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"CLOB request failed for {url}: {exc}") from exc
    return parse_history(raw)


def parse_history(raw: dict) -> list[PricePoint]:
    """Parse a `{"history": [{"t": ..., "p": ...}, ...]}` response. Pure — no I/O."""
    points = [PricePoint(t=int(pt["t"]), p=float(pt["p"])) for pt in raw.get("history", [])]
    return sorted(points, key=lambda pt: pt.t)
