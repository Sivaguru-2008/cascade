"""GET /stream: Server-Sent Events by role.

pharmacist: `event` (own pharmacy's dispenses, at most ~20/s, minimal fields) and `alert` (own pharmacy)
regulator : `signals` (Loop 2 recompute summaries, incl. newly flagged pairs)
both      : `hello` once, comment heartbeats every 15 s.
The dashboard reads it with fetch() so the API key travels in a header, never in the URL.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from api.auth import Principal, require_any
from common.bus import STREAM_ALERTS, STREAM_DISPENSE, STREAM_SIGNALS

router = APIRouter(tags=["stream"])
MAX_EVENTS_PER_S = 20
HEARTBEAT_S = 15.0


def _sse(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str, separators=(',', ':'))}\n\n".encode()


def _drug_name(drug_id: str) -> str:
    from datagen.catalogue import load_catalogue

    d = load_catalogue().by_id.get(drug_id)
    return d.display_name if d else drug_id


async def event_stream(request: Request, p: Principal) -> AsyncIterator[bytes]:
    bus = request.app.state.ctx.bus
    streams = [STREAM_DISPENSE, STREAM_ALERTS] if p.role == "pharmacist" else [STREAM_SIGNALS]
    cursors = {s: await bus.last_id(s) for s in streams}
    yield _sse("hello", {"role": p.label, "streams": streams})
    last_beat = time.monotonic()
    budget_t, sent = time.monotonic(), 0
    dropped = 0
    while not await request.is_disconnected():
        try:
            msgs = await bus.tail_many(cursors, count=500, block_ms=1000)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(1)
            continue
        out: list[bytes] = []
        for stream, msg_id, data in msgs:
            cursors[stream] = msg_id
            if stream == STREAM_DISPENSE:
                if data.get("pharmacy_id") != p.pharmacy_id:
                    continue
                now = time.monotonic()
                if now - budget_t >= 1.0:
                    budget_t, sent = now, 0
                if sent >= MAX_EVENTS_PER_S:
                    dropped += 1
                    continue
                sent += 1
                out.append(_sse("event", {"event_id": data["event_id"], "ts": data["ts"],
                                          "pharmacy_id": data["pharmacy_id"], "drug": _drug_name(data["drug_id"]),
                                          "atc_l4": data["atc_l4"]}))
            elif stream == STREAM_ALERTS:
                if data.get("pharmacy_id") != p.pharmacy_id:
                    continue
                data = dict(data)
                data.pop("patient_token", None)
                out.append(_sse("alert", data))
            elif stream == STREAM_SIGNALS:
                out.append(_sse("signals", data))
        if dropped and time.monotonic() - budget_t < 0.05:
            out.append(_sse("feed_sampled", {"dropped": dropped}))
            dropped = 0
        for chunk in out:
            yield chunk
        if time.monotonic() - last_beat > HEARTBEAT_S:
            last_beat = time.monotonic()
            yield b": heartbeat\n\n"


@router.get("/stream")
async def stream(request: Request, p: Principal = Depends(require_any)) -> StreamingResponse:
    return StreamingResponse(event_stream(request, p), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
