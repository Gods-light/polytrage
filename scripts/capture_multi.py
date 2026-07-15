"""Multi-event depth capture — build the council's 20-30 event evidence base
without waiting for the WC final.

Captures every configured negRisk event concurrently (one WSS connection
each). Live-outcome token sets are re-resolved every 6h (outcomes die as
events progress). Measurement note: legs are the outcomes priced inside
(min_p, 1-min_p); long-tail dead outcomes are excluded, so recorded sums
omit a small tail (< ~1c on typical events) — analysis must treat
executable-arb readings on wide events as approximate below that scale.

Data: <data-dir>/<slug>/<YYYYMMDD>.jsonl, same schema as capture_final.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polytrage.capture.wss import DepthCapture  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0"}

DEFAULT_SLUGS = (
    "2026-the-open-championship-winner",   # golf major, LIVE Jul 16-19 — in-play
    "elon-musk-of-tweets-july-10-july-17", # bucket market resolving Jul 17
    "world-cup-golden-boot-winner",        # reprices on WC final goals
    "fed-decision-in-july-181",            # macro regime, FOMC Jul 28-29
    "next-prime-minister-of-ethiopia",     # news-shock regime, high volume
    "presidential-election-winner-2028",   # 31 live outcomes — sweep stress test
)


def _get(url: str) -> list | dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def live_yes_tokens(event: dict, min_p: float = 0.005) -> tuple[list[str], float, int]:
    """(live tokens, sum of excluded dead-outcome prices, dead count).

    The dead tail matters: a TRUE long-arb basket must buy EVERY outcome,
    so recorded sums over live legs undercount cost by ~tail. Written as a
    meta row so analysis can correct; short-arb readings are conservative.
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


async def capture_event(slug: str, data_dir: Path) -> None:
    while True:
        try:
            evs = _get(f"{GAMMA}/events?slug={slug}")
            ev = evs[0] if isinstance(evs, list) and evs else None
        except Exception as exc:
            print(f"[{slug}] gamma fetch failed: {exc!r}; retry in 120s", flush=True)
            await asyncio.sleep(120)
            continue
        if ev is None or ev.get("closed"):
            print(f"[{slug}] closed/missing — capture done", flush=True)
            return
        tokens, tail, dead = live_yes_tokens(ev)
        if len(tokens) < 2:
            print(f"[{slug}] <2 live outcomes; recheck in 30 min", flush=True)
            await asyncio.sleep(1800)
            continue
        print(f"[{slug}] capturing {len(tokens)} live outcomes (dead tail {tail} over {dead})", flush=True)
        cap = DepthCapture(slug, tokens, data_dir / slug,
                           meta={"live_legs": len(tokens), "dead_legs": dead, "dead_tail_sum": tail})
        await cap.run(stop_at=time.time() + 6 * 3600)  # re-resolve token set


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/capture")
    ap.add_argument("--slugs", nargs="*", default=list(DEFAULT_SLUGS))
    args = ap.parse_args()
    data_dir = Path(args.data_dir)
    await asyncio.gather(*(capture_event(s, data_dir) for s in args.slugs))


if __name__ == "__main__":
    asyncio.run(main())
