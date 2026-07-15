"""Paper-trade a capture stream (council caveat: validates STANDING
dislocations, not goal-second races — legging/cancel risk is only partially
observable, which is why fills get persistence grades).

Tails data/capture/<slug>/*.jsonl live, feeds ticks to PaperBook, journals
fills to data/paper/<slug>-<side>/trades.jsonl and a rolling state.json
(position, cost, locked edge, fill-quality histogram, mark-to-market).
Settlement is manual/analysis-time (event resolution), but state.json's
locked_edge already is the guaranteed-if-among-live-legs profit.

Usage: paper_trade.py --slug world-cup-golden-boot-winner --side short \
         [--cap 500] [--max-per-tick 50] [--grade-horizon 30]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polytrage.capture.paper import PaperBook, grade_fill_persistence  # noqa: E402


def follow_ticks(cap_dir: Path):
    """Yield parsed rows from the newest capture file, tail -f style,
    rolling over UTC-midnight file changes."""
    pos = 0
    current: Path | None = None
    while True:
        files = sorted(cap_dir.glob("*.jsonl"))
        if not files:
            time.sleep(5)
            continue
        newest = files[-1]
        if newest != current:
            current, pos = newest, 0
        with newest.open() as f:
            f.seek(pos)
            chunk = f.read()
            pos = f.tell()
        for line in chunk.splitlines():
            try:
                yield json.loads(line)
            except ValueError:
                pass
        time.sleep(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--side", choices=["short"], default="short")
    ap.add_argument("--cap", type=float, default=500.0)
    ap.add_argument("--max-per-tick", type=float, default=50.0)
    ap.add_argument("--grade-horizon", type=float, default=30.0)
    ap.add_argument("--data-dir", default="data/capture")
    ap.add_argument("--out-dir", default="data/paper")
    args = ap.parse_args()

    cap_dir = Path(args.data_dir) / args.slug
    out_dir = Path(args.out_dir) / f"{args.slug}-{args.side}"
    out_dir.mkdir(parents=True, exist_ok=True)
    trades_path = out_dir / "trades.jsonl"
    state_path = out_dir / "state.json"

    book = PaperBook(cap_baskets=args.cap, max_per_tick=args.max_per_tick)
    # Rehydrate an existing paper session so systemd restarts don't double-fill.
    if trades_path.exists():
        from polytrage.capture.paper import PaperFill
        for line in trades_path.open():
            try:
                d = json.loads(line)
                book.fills.append(PaperFill(**{k: d[k] for k in
                                  ("ts", "baskets", "bids", "sizes", "cost", "locked_edge")}))
            except (ValueError, KeyError):
                pass
        print(f"[paper] rehydrated {len(book.fills)} fills, position {book.position}", flush=True)

    recent: deque[dict] = deque(maxlen=600)
    ungraded: list[dict] = []
    last_state = 0.0

    print(f"[paper] {args.slug} {args.side}: cap {args.cap} baskets, "
          f"max {args.max_per_tick}/tick, grading horizon {args.grade_horizon}s", flush=True)

    for row in follow_ticks(cap_dir):
        if row.get("type") == "tick":
            recent.append(row)
            # grade fills whose horizon has elapsed
            for g in list(ungraded):
                if row["ts"] >= g["ts"] + args.grade_horizon:
                    grade = grade_fill_persistence(g["bids"], list(recent),
                                                   args.grade_horizon, g["ts"])
                    g["grade"] = grade
                    with trades_path.open("a") as f:
                        f.write(json.dumps({**g, "type": "grade"},
                                           separators=(",", ":")) + "\n")
                    print(f"[paper] fill@{g['ts']:.0f} graded {grade}", flush=True)
                    ungraded.remove(g)
        fill = book.on_tick(row)
        if fill:
            d = {"type": "fill", "ts": fill.ts, "baskets": fill.baskets,
                 "bids": fill.bids, "sizes": fill.sizes, "cost": fill.cost,
                 "locked_edge": fill.locked_edge}
            with trades_path.open("a") as f:
                f.write(json.dumps(d, separators=(",", ":")) + "\n")
            ungraded.append(dict(d))
            print(f"[paper] FILL {fill.baskets} baskets, cost ${fill.cost:.2f}, "
                  f"locked edge ${fill.locked_edge:.4f} (position {book.position})", flush=True)
        now = time.time()
        if now - last_state >= 60:
            state = {"ts": round(now, 1), "slug": args.slug, "side": args.side,
                     "position_baskets": round(book.position, 2),
                     "cost": round(book.cost, 4),
                     "locked_edge": round(book.locked_edge, 4),
                     "cap": args.cap, "fills": len(book.fills)}
            state_path.write_text(json.dumps(state, indent=1))
            last_state = now
    return 0


if __name__ == "__main__":
    sys.exit(main())
