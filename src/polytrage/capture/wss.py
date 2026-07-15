"""Async WSS client for Polymarket's CLOB market channel.

Maintains per-asset Books from `book` snapshots + `price_change` deltas and
appends measurement rows to a JSONL file:
  {"type":"tick", ...}       every tick_interval seconds once books are live
  {"type":"window", ...}     on executable-arb open/close transitions
  {"type":"snapshot_meta"}   whenever a full book snapshot arrives

Requires the `websockets` package (optional dependency, capture extra).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import websockets

from .books import Book, tick_metrics

WSS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _as_events(raw: str | bytes) -> list[dict]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if raw in ("PONG", "PING"):
        return []
    data = json.loads(raw)
    return data if isinstance(data, list) else [data]


class DepthCapture:
    def __init__(self, slug: str, asset_ids: list[str], out_dir: Path,
                 tick_interval: float = 1.0, label: str = "",
                 meta: dict | None = None):
        self.slug = slug
        self.assets = list(asset_ids)
        self.books: dict[str, Book] = {a: Book() for a in self.assets}
        self.out_dir = out_dir
        self.tick_interval = tick_interval
        self.label = label
        self.meta = meta or {}
        self._arb_open = {"long": False, "short": False}
        out_dir.mkdir(parents=True, exist_ok=True)

    def _out(self) -> Path:
        return self.out_dir / f"{time.strftime('%Y%m%d', time.gmtime())}.jsonl"

    def _write(self, row: dict) -> None:
        row["ts"] = round(time.time(), 3)
        with self._out().open("a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def _handle(self, ev: dict) -> None:
        et = ev.get("event_type")
        aid = ev.get("asset_id")
        if aid not in self.books:
            return
        if et == "book":
            self.books[aid].load_snapshot(
                ev.get("bids", ev.get("buys", [])), ev.get("asks", ev.get("sells", [])))
            self._write({"type": "snapshot_meta", "asset": aid,
                         "bids": len(self.books[aid].bids), "asks": len(self.books[aid].asks)})
        elif et == "price_change":
            changes = ev.get("changes") or [ev]
            for ch in changes:
                try:
                    self.books[aid].apply_change(
                        float(ch["price"]), ch["side"], float(ch["size"]))
                except (KeyError, ValueError, TypeError):
                    pass

    def _tick(self) -> None:
        m = tick_metrics([self.books[a] for a in self.assets],
                         long_handicap=float(self.meta.get("dead_tail_sum", 0.0)))
        if m is None:
            return
        m["type"] = "tick"
        self._write(m)
        for side in ("long", "short"):
            now_open = m[f"{side}_baskets"] > 0
            if now_open != self._arb_open[side]:
                self._arb_open[side] = now_open
                self._write({"type": "window", "side": side,
                             "state": "open" if now_open else "close",
                             "sum_ask": m["sum_ask"], "sum_bid": m["sum_bid"],
                             "baskets": m[f"{side}_baskets"], "edge": m[f"{side}_edge"]})
                print(f"[{self.slug}] {side} arb {'OPEN' if now_open else 'closed'} "
                      f"baskets={m[f'{side}_baskets']} edge=${m[f'{side}_edge']:.4f}", flush=True)

    async def run(self, stop_at: float | None = None) -> None:
        if self.meta:
            self._write({"type": "meta", **self.meta})
        backoff = 1.0
        while stop_at is None or time.time() < stop_at:
            try:
                async with websockets.connect(WSS_URL, ping_interval=None,
                                              max_size=2 ** 24) as ws:
                    await ws.send(json.dumps({"type": "market", "assets_ids": self.assets}))
                    print(f"[{self.slug}] connected, subscribed {len(self.assets)} assets", flush=True)
                    backoff = 1.0
                    last_tick = 0.0
                    last_ping = time.time()
                    while stop_at is None or time.time() < stop_at:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            for ev in _as_events(raw):
                                self._handle(ev)
                        except asyncio.TimeoutError:
                            pass
                        now = time.time()
                        if now - last_ping >= 10:
                            await ws.send("PING")
                            last_ping = now
                        if now - last_tick >= self.tick_interval:
                            self._tick()
                            last_tick = now
            except Exception as exc:  # noqa: BLE001 — capture must survive anything
                self._write({"type": "error", "err": repr(exc)[:300]})
                print(f"[{self.slug}] reconnect after error: {exc!r} (backoff {backoff}s)", flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
