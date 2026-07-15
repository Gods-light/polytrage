"""World Cup final depth-capture orchestrator (council milestone experiment).

Runs two concurrent captures:
1. world-cup-winner (live negRisk multi-outcome market) — starts immediately,
   validates the pipeline and measures depth on the live winner books.
2. The Spain vs Argentina final match event (3-way: ESP/draw/ARG) — polled
   for every 10 minutes until Polymarket lists it (semifinal listed ~3 days
   pre-match), then captured through the game until the event closes.

Data: <data-dir>/<slug>/<YYYYMMDD>.jsonl. Designed to run under systemd
(Restart=always); safe to restart at any time.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polytrage.capture.wss import DepthCapture  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0"}
FINAL_SLUGS = (
    "fifwc-esp-arg-2026-07-19", "fifwc-arg-esp-2026-07-19",
    "fifwc-spa-arg-2026-07-19", "fifwc-arg-spa-2026-07-19",
)
FINAL_QUERIES = ("spain argentina", "fifwc final")


def _get(url: str) -> list | dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def live_yes_tokens(event: dict, min_p: float = 0.001) -> tuple[list[str], float, int]:
    """(live tokens, sum of excluded dead-outcome prices, dead count).

    The dead tail matters: a TRUE long-arb basket buys EVERY outcome, so
    sums over live legs undercount cost by ~tail. Recorded as a meta row.
    """
    out: list[str] = []
    tail = 0.0
    dead = 0
    for m in event.get("markets", []):
        try:
            prices = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m["outcomePrices"]
            toks = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
        except (KeyError, ValueError, TypeError):
            continue
        p = float(prices[0])
        if min_p < p < 1 - min_p:
            out.append(toks[0])
        else:
            tail += p
            dead += 1
    return out, round(tail, 5), dead


def all_yes_tokens(event: dict) -> list[str]:
    out = []
    for m in event.get("markets", []):
        try:
            toks = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
            out.append(toks[0])
        except (KeyError, ValueError, TypeError):
            continue
    return out


def find_final_event() -> dict | None:
    for slug in FINAL_SLUGS:
        try:
            evs = _get(f"{GAMMA}/events?slug={slug}")
            if evs:
                return evs[0]
        except Exception:
            pass
    for q in FINAL_QUERIES:
        try:
            d = _get(f"{GAMMA}/public-search?q={urllib.parse.quote(q)}&events_status=active")
            if not isinstance(d, dict):
                continue
            for e in d.get("events", []):
                slug = e.get("slug", "")
                if slug.startswith("fifwc-") and "2026-07-19" in slug and not e.get("closed"):
                    return e
        except Exception:
            pass
    return None


async def capture_winner(data_dir: Path) -> None:
    while True:
        try:
            ev = _get(f"{GAMMA}/events?slug=world-cup-winner")[0]
        except Exception as exc:
            print(f"[winner] gamma fetch failed: {exc!r}; retry in 60s", flush=True)
            await asyncio.sleep(60)
            continue
        if ev.get("closed"):
            print("[winner] event closed — winner capture done", flush=True)
            return
        tokens, tail, dead = live_yes_tokens(ev)
        if not tokens:
            await asyncio.sleep(300)
            continue
        cap = DepthCapture("world-cup-winner", tokens, data_dir / "world-cup-winner",
                           meta={"live_legs": len(tokens), "dead_legs": dead, "dead_tail_sum": tail})
        # Re-resolve the live token set every 6h (outcomes die as games finish).
        await cap.run(stop_at=time.time() + 6 * 3600)


async def capture_final(data_dir: Path) -> None:
    ev = None
    while ev is None:
        ev = find_final_event()
        if ev is None:
            print("[final] match event not listed yet; recheck in 10 min", flush=True)
            await asyncio.sleep(600)
    slug = ev["slug"]
    tokens = all_yes_tokens(ev)
    print(f"[final] FOUND {slug} ({ev.get('title')}) — capturing {len(tokens)} outcome tokens", flush=True)
    cap = DepthCapture(slug, tokens, data_dir / slug)
    while True:
        await cap.run(stop_at=time.time() + 3600)
        try:
            fresh = _get(f"{GAMMA}/events?slug={slug}")[0]
            if fresh.get("closed"):
                print(f"[final] {slug} closed — capture complete", flush=True)
                return
        except Exception:
            pass


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/capture")
    args = ap.parse_args()
    data_dir = Path(args.data_dir)
    await asyncio.gather(capture_winner(data_dir), capture_final(data_dir))


if __name__ == "__main__":
    asyncio.run(main())
