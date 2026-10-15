"""System throughput and scoring latency. Writes docs/eval/latency.json.

Replays the generated events at --speed max for --duration seconds (default 300) and reports sustained events/s
and latency p50/p95/p99 from the `latency` table (ms from message read to alert write / no-alert decision).

  --mode inprocess  (default when CASCADE_MODE=single) runs the scorer + Loop 2 in this process
                    (in-memory bus, temporary SQLite);
  --mode services   publishes to the running system's Redis via the edge (needs EDGE_SECRET) and reads the
                    configured Postgres. Run it on a freshly reset system: replaying from the start into a system
                    that already holds patient state would mix histories.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from common.bus import STREAM_DISPENSE, MemoryBus, get_bus
from common.config import ROOT, get_settings
from common.db import Database
from datagen.replay import Pacer, iter_raw
from edge.pharmacy_edge import Edge

OUT = ROOT / "docs" / "eval" / "latency.json"


async def _publish_for(edge: Edge, seconds: float) -> tuple[int, float]:
    t0 = time.monotonic()
    n = await edge.publish_rows(iter_raw(), Pacer.parse("max"), stop_check_every=1000,
                                should_stop=lambda: time.monotonic() - t0 >= seconds)
    return n, time.monotonic() - t0


def _summary(db: Database, since_wall: float, published: int, wall_s: float, mode: str) -> dict[str, Any]:
    rows = db.fetchall("SELECT ms, alerted, wall_ts FROM latency WHERE wall_ts >= ?", (since_wall,))
    ms = np.array([r[0] for r in rows], dtype=float)
    walls = np.array([r[2] for r in rows], dtype=float)
    span = float(walls.max() - walls.min()) if len(walls) > 1 else 0.0
    return {"mode": mode, "arch": platform.machine(), "cpu_count": os.cpu_count(), "events_published": published,
            "events_scored": len(rows), "publish_seconds": round(wall_s, 1), "scoring_span_seconds": round(span, 1),
            "sustained_events_per_s": round(len(rows) / span, 1) if span > 0 else None,
            "latency_ms": {"p50": round(float(np.percentile(ms, 50)), 3), "p95": round(float(np.percentile(ms, 95)), 3),
                           "p99": round(float(np.percentile(ms, 99)), 3), "max": round(float(ms.max()), 3)}
            if len(ms) else None,
            "alerts": int(sum(r[1] for r in rows)),
            "note": "latency includes queueing inside a consumer batch (scorer batches of up to 200 events)"}


async def inprocess(seconds: float) -> dict[str, Any]:
    from api.runtime import Runtime

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CASCADE_STATE_DIR"] = tmp
        from common.config import reload_settings

        s = reload_settings()
        bus = MemoryBus()
        db = Database.connect("sqlite", sqlite_path=Path(tmp) / "latency.sqlite3")
        rt = Runtime(s, db, bus)
        await rt.start(with_edge=False)
        since = time.time()
        published, wall = await _publish_for(Edge(bus, os.urandom(32)), seconds)
        await bus.wait_until_drained(STREAM_DISPENSE, timeout_s=600)
        await rt.stop()
        return _summary(db, since, published, wall, "inprocess (single-process, in-memory bus, SQLite)")


async def services(seconds: float) -> dict[str, Any]:
    from common.crypto import load_edge_secret

    bus = get_bus()
    db = Database.connect()
    since = time.time()
    published, wall = await _publish_for(Edge(bus, load_edge_secret()), seconds)
    await bus.wait_until_drained(STREAM_DISPENSE, timeout_s=600)
    await asyncio.sleep(2.0)  # the scorer flushes latency rows every second
    await bus.close()
    return _summary(db, since, published, wall, f"services (Redis bus, {db.dialect})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--duration", type=float, default=300.0)
    ap.add_argument("--mode", choices=["auto", "inprocess", "services"], default="auto")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    mode = args.mode if args.mode != "auto" else ("inprocess" if get_settings().mode == "single" else "services")
    res = asyncio.run(inprocess(args.duration) if mode == "inprocess" else services(args.duration))
    res["requested_duration_s"] = args.duration
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
