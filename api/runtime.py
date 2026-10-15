"""Single-process runtime (CASCADE_MODE=single): every service in one asyncio loop.

This is the s390x fallback when Redis/Postgres or containers are unavailable: the bus is an in-memory
asyncio implementation and the database is SQLite by default. Services mode does not use this module.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from common.bus import Bus, get_bus
from common.config import Settings, get_settings
from common.db import Database
from common.logging import get_logger
from loop1.score_service import Scorer, build_scorer
from loop2.signal_service import SignalService, build_signal_service

log = get_logger("runtime")


class Runtime:
    def __init__(self, settings: Settings | None = None, db: Database | None = None, bus: Bus | None = None) -> None:
        self.s = settings or get_settings()
        self.db = db or Database.connect()
        self.bus = bus or get_bus()
        self.scorer: Scorer | None = None
        self.signals: SignalService | None = None
        self.tasks: list[asyncio.Task[Any]] = []

    async def start(self, with_edge: bool = True) -> None:
        self.db.init_schema()
        # separate connections: the scorer, Loop 2 and API readers never wait on each other's connection lock
        self.scorer = build_scorer(self.bus, self.db.clone(), self.s)
        self.signals = build_signal_service(self.bus, self.db.clone(), self.s)
        await self.signals.restore()
        self.tasks += [asyncio.create_task(self.scorer.consume_forever("scorer-single")),
                       asyncio.create_task(self.signals.consume_forever("signals-single"))]
        if with_edge and os.environ.get("EDGE_SECRET"):
            from edge.pharmacy_edge import control_loop

            self.tasks.append(asyncio.create_task(control_loop(self.bus)))
        log.info("single-process runtime started", extra={"edge": with_edge and bool(os.environ.get("EDGE_SECRET"))})

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.tasks = []
        if self.scorer is not None:
            self.scorer.flush_latency()
        if self.signals is not None:
            self.signals.save_incidence()
