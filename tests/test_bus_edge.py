import asyncio
import csv
import gzip
import json
import re

from common.bus import STREAM_DISPENSE, MemoryBus
from datagen import generate
from datagen.replay import Pacer, iter_raw
from edge.pharmacy_edge import Edge

SECRET = bytes(range(32))


def test_edge_publishes_only_tokens(tmp_path):
    generate.main(["--patients", "80", "--seed", "5", "--out", str(tmp_path)])
    path = tmp_path / "events.csv.gz"
    with gzip.open(path, "rt") as f:
        raw = list(csv.DictReader(f))[:400]
    raw_ids = {r["patient_id"] for r in raw} | {r["prescriber_id"] for r in raw}

    async def run():
        bus = MemoryBus()
        edge = Edge(bus, SECRET)
        n = await edge.publish_rows(iter_raw(path, 0, 400), Pacer.parse("max"))
        assert n == 400
        got = []
        while True:
            msgs = await bus.read_group(STREAM_DISPENSE, "scorer", "t", count=1000, block_ms=10)
            if not msgs:
                break
            got += msgs
        return got

    msgs = asyncio.run(run())
    assert len(msgs) == 400
    blob = json.dumps([m for _, m in msgs])
    assert not any(rid in blob for rid in raw_ids), "raw IDs leaked onto the bus"
    for _, m in msgs:
        assert re.fullmatch(r"[0-9a-f]{64}", m["patient_token"])
        assert re.fullmatch(r"[0-9a-f]{64}", m["prescriber_token"])
        assert "patient_id" not in m and "is_cascade" not in m
    assert [m["event_id"] for _, m in msgs] == [r["event_id"] for r in raw]


def test_memory_bus_groups_are_independent_and_backpressure():
    async def run():
        bus = MemoryBus()
        bus.max_lag = 5
        for g in ("scorer", "signals"):
            await bus.ensure_group(STREAM_DISPENSE, g)
        await bus.publish_many(STREAM_DISPENSE, [{"i": i} for i in range(10)])
        assert await bus.lag(STREAM_DISPENSE) == 10
        a = await bus.read_group(STREAM_DISPENSE, "scorer", "c", count=100, block_ms=10)
        assert len(a) == 10
        assert await bus.lag(STREAM_DISPENSE) == 10  # signals has not read yet
        b = await bus.read_group(STREAM_DISPENSE, "signals", "c", count=4, block_ms=10)
        assert [m["i"] for _, m in b] == [0, 1, 2, 3]
        assert await bus.lag(STREAM_DISPENSE) == 6
        waiter = asyncio.create_task(bus.wait_for_capacity(STREAM_DISPENSE))
        await asyncio.sleep(0.1)
        assert not waiter.done()
        await bus.read_group(STREAM_DISPENSE, "signals", "c", count=100, block_ms=10)
        await asyncio.wait_for(waiter, 1)

    asyncio.run(run())


def test_pacer_modes():
    assert Pacer.parse("max").mode == "max"
    p = Pacer.parse("days_per_sec=2")
    assert (p.mode, p.rate) == ("days_per_sec", 2.0)
