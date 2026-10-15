"""Pharmacy edge: tokenise raw dispenses and publish DispenseEvents to stream:dispense.

Raw patient and prescriber IDs never leave this process: each is replaced by
HMAC-SHA256(key=EDGE_SECRET, msg=id) before publishing.

    python -m edge.pharmacy_edge --speed days_per_sec=1
    python -m edge.pharmacy_edge --speed max --limit 50000
    python -m edge.pharmacy_edge --demo-script            # plays docs/demo_script_events.json
    python -m edge.pharmacy_edge --serve                  # /health + waits for commands (container default)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from common.bus import STREAM_CONTROL, STREAM_DISPENSE, Bus, get_bus
from common.crypto import hmac_token, load_edge_secret
from common.logging import get_logger
from common.schemas import DispenseEvent
from datagen.replay import Pacer, default_events_path, iter_raw

log = get_logger("edge")
BATCH = 500
TRIM_EVERY_BATCHES = 40


class Edge:
    def __init__(self, bus: Bus, secret: bytes) -> None:
        self.bus = bus
        self._secret = secret
        self._cache: dict[str, str] = {}
        self.published = 0
        self.current_ts: str | None = None

    def token(self, raw_id: str) -> str:
        tok = self._cache.get(raw_id)
        if tok is None:
            tok = hmac_token(self._secret, raw_id)
            if len(self._cache) < 500_000:
                self._cache[raw_id] = tok
        return tok

    def to_event(self, row: dict[str, str]) -> DispenseEvent:
        return DispenseEvent(
            event_id=row["event_id"], ts=datetime.fromisoformat(row["ts"]), pharmacy_id=row["pharmacy_id"],
            patient_token=self.token(row["patient_id"]), prescriber_token=self.token(row["prescriber_id"]),
            age_band=row["age_band"], sex=row["sex"], drug_id=row["drug_id"], atc_code=row["atc_code"],
            atc_l4=row["atc_l4"], atc_l3=row["atc_l3"], qty=int(row["qty"]), days_supply=int(row["days_supply"]),
            dose_mg=float(row["dose_mg"]),
        )

    async def publish_rows(self, rows: Iterable[dict[str, str]], pacer: Pacer,
                           should_stop: Callable[[], bool] | None = None,
                           stop_check_every: int = 2000,
                           async_stop: Callable[[], Any] | None = None) -> int:
        """Publish rows with pacing and back-pressure; returns the number published."""
        for group in ("scorer", "signals"):
            await self.bus.ensure_group(STREAM_DISPENSE, group)
        batch: list[dict[str, Any]] = []
        n = batches = 0
        for row in rows:
            ev = self.to_event(row)
            delay = pacer.delay_for(ev.ts)
            if delay > 0:
                if batch:
                    await self._flush(batch)
                    batch = []
                await asyncio.sleep(delay)
            batch.append(ev.model_dump(mode="json"))
            self.current_ts = row["ts"]
            n += 1
            if len(batch) >= BATCH or pacer.mode != "max":
                await self._flush(batch)
                batch = []
                batches += 1
                if batches % TRIM_EVERY_BATCHES == 0:
                    await self.bus.trim(STREAM_DISPENSE)
            if n % stop_check_every == 0:
                if should_stop is not None and should_stop():
                    break
                if async_stop is not None and await async_stop():
                    break
        if batch:
            await self._flush(batch)
        await self.bus.trim(STREAM_DISPENSE)
        return n

    async def _flush(self, batch: list[dict[str, Any]]) -> None:
        await self.bus.wait_for_capacity(STREAM_DISPENSE)
        await self.bus.publish_many(STREAM_DISPENSE, batch)
        self.published += len(batch)


async def replay(bus: Bus, speed: str, path: Path | None = None, start: int = 0,
                 limit: int | None = None) -> int:
    edge = Edge(bus, load_edge_secret())
    stop = None if limit is None else start + limit
    n = await edge.publish_rows(iter_raw(path, start, stop), Pacer.parse(speed))
    log.info("replay finished", extra={"published": n, "speed": speed})
    return n


async def control_loop(bus: Bus, path: Path | None = None) -> None:
    """Wait for commands from the API (stream:control): {"cmd": "demo"} or {"cmd": "replay", ...}."""
    from datagen.replay import run_demo_script

    await bus.ensure_group(STREAM_CONTROL, "edge")
    edge = Edge(bus, load_edge_secret())
    running: asyncio.Task[Any] | None = None
    while True:
        for msg_id, cmd in await bus.read_group(STREAM_CONTROL, "edge", "edge-1", count=10, block_ms=2000):
            await bus.ack(STREAM_CONTROL, "edge", [msg_id])
            if running is not None and not running.done():
                log.info("command ignored: a replay is already running", extra={"cmd": cmd.get("cmd")})
                continue
            if cmd.get("cmd") == "demo":
                running = asyncio.create_task(run_demo_script(edge, path))
            elif cmd.get("cmd") == "replay":
                running = asyncio.create_task(edge.publish_rows(
                    iter_raw(path, int(cmd.get("start", 0)), cmd.get("stop")),
                    Pacer.parse(str(cmd.get("speed", "days_per_sec=1")))))
            log.info("command accepted", extra={"cmd": cmd.get("cmd")})


def make_app() -> Any:
    from fastapi import FastAPI

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        bus = get_bus()
        task = asyncio.create_task(control_loop(bus, _events_path_from_env()))
        yield
        task.cancel()

    app = FastAPI(title="CASCADE edge", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "edge"}

    return app


def _events_path_from_env() -> Path:
    raw = os.environ.get("EDGE_EVENTS_PATH")
    return Path(raw) if raw else default_events_path()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="CASCADE pharmacy edge / replay")
    ap.add_argument("--speed", default="days_per_sec=1", help="max | days_per_sec=<x> | events_per_sec=<x>")
    ap.add_argument("--events", type=Path, default=None)
    ap.add_argument("--start", type=int, default=0, help="row offset to start from")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--demo-script", action="store_true")
    ap.add_argument("--warmup-only", action="store_true", help="with --demo-script: stop after the warm-up")
    ap.add_argument("--serve", action="store_true", help="health endpoint + wait for API commands")
    ap.add_argument("--port", type=int, default=int(os.environ.get("EDGE_PORT", "8004")))
    args = ap.parse_args(argv)
    if args.serve:
        import uvicorn

        uvicorn.run(make_app(), host=os.environ.get("BIND_HOST", "0.0.0.0"), port=args.port, log_level="warning")
        return

    async def _run() -> None:
        bus = get_bus()
        try:
            if args.demo_script:
                from datagen.replay import run_demo_script

                await run_demo_script(Edge(bus, load_edge_secret()), args.events, warmup_only=args.warmup_only)
            else:
                n = await replay(bus, args.speed, args.events, args.start, args.limit)
                print(json.dumps({"published": n}))
        finally:
            await bus.close()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
