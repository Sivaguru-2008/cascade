"""Loop 2 population signal engine (section 6.2).

Consumes stream:dispense (group "signals"), maintains pair counts (Redis `pair:{A}:{B}` with fields
w30/w90/w180) and per-patient incident history (`pinc:{token}`), keeps daily incidence in NumPy
(snapshotted to disk every minute), and recomputes nSR / aSR / CI every 5,000 events or 10 s.
Each recompute writes:
  * Redis `asr:{A}:{B}` = {asr, lo, hi, n, signal, status}  -> read by Loop 1 as a live feature;
  * Postgres `signals` history rows + `signals_latest`      -> regulator view and history chart;
  * stream:signals summary (new signals, for the dashboard toast).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI

from common.bus import STREAM_DISPENSE, STREAM_SIGNALS, Bus, get_bus
from common.config import Settings, get_settings
from common.db import Database
from common.logging import get_logger
from common.schemas import DispenseEvent
from datagen.catalogue import Catalogue, load_catalogue, load_faers
from loop2.pssa import DISPLAY_WINDOW, classify, faers_supported_pairs
from loop2.stream_counts import PState, Snapshot, StreamingPSSA

log = get_logger("signals")
HISTORY_EVERY_SIM_DAYS = 7
SNAPSHOT_EVERY_S = 60.0


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _f(x: Any) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 6)


class SignalService:
    def __init__(self, bus: Bus, db: Database, cat: Catalogue, settings: Settings) -> None:
        self.bus = bus
        self.db = db
        self.cat = cat
        self.s = settings
        self.engine = StreamingPSSA(cat.classes, settings.windows)
        self.faers = faers_supported_pairs(load_faers())
        self.lock = asyncio.Lock()
        self.events_since = 0
        self.last_recompute = time.monotonic()
        self.last_snapshot = time.monotonic()
        self._written_asr: dict[tuple[str, str], tuple[Any, ...]] = {}
        self._written_latest: dict[tuple[str, str, int], tuple[Any, ...]] = {}
        self._history_day: dict[tuple[str, str, int], int] = {}
        self._signal: dict[tuple[str, str], bool] = {}
        self._signal_bh: dict[tuple[str, str], bool] = {}
        self._first_signal: dict[tuple[str, str], int] = {}
        self.n_recomputes = 0
        self.last_snapshot_obj: Snapshot | None = None

    @property
    def inc_path(self) -> Path:
        return self.s.state_dir / "loop2_incidence.npz"

    # ---------- restart ----------
    async def restore(self) -> None:
        keys = await self.bus.keys("pair:")
        if not keys:
            return
        hashes = await self.bus.hgetall_many(keys)
        rows = []
        for k, h in zip(keys, hashes, strict=True):
            _, a, b = k.split(":", 2)
            for W in self.engine.windows:
                rows.append((a, b, W, int(h.get(f"w{W}", "0"))))
        self.engine.load_counts(rows)
        if self.inc_path.exists():
            data = np.load(self.inc_path, allow_pickle=False)
            self.engine.load_incidence([str(c) for c in data["classes"]], data["inc"])
        for name, value in (self.db.fetchall("SELECT name, value FROM service_state WHERE name LIKE 'first_signal:%'")):
            a, b = name.split(":", 1)[1].split(">")
            self._first_signal[(a, b)] = int(value)
        log.info("restored Loop 2 state", extra={"pairs": len(keys)})

    def save_incidence(self) -> None:
        self.inc_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.inc_path.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, classes=np.array(self.engine.classes),
                            inc=self.engine.inc[:, :self.engine.max_day + 1])
        os.replace(tmp, self.inc_path)
        self.last_snapshot = time.monotonic()

    # ---------- per batch ----------
    async def process(self, events: list[DispenseEvent]) -> None:
        async with self.lock:
            tokens = list(dict.fromkeys(e.patient_token for e in events))
            hashes = await self.bus.hgetall_many([f"pinc:{t}" for t in tokens])
            states = {t: PState.from_hash(h) for t, h in zip(tokens, hashes, strict=True)}
            dirty: dict[str, dict[str, str]] = {t: {} for t in tokens}
            ops: list[tuple[str, str, int]] = []
            for e in events:
                day = (e.ts.date() - self.s.epoch).days
                upd = self.engine.process(states[e.patient_token], e.atc_l4, day)
                dirty[e.patient_token].update(upd.fields)
                ops += [(f"pair:{y}:{x}", f"w{W}", 1) for y, x, W in upd.increments]
            await self.bus.hset_many({f"pinc:{t}": f for t, f in dirty.items() if f})
            await self.bus.hincrby_many(ops)
            self.events_since += len(events)
            if self.events_since >= self.s.recompute_every_events:
                await self._recompute()
        if time.monotonic() - self.last_snapshot > SNAPSHOT_EVERY_S:
            self.save_incidence()

    async def tick(self) -> None:
        """Time-based trigger (every 10 s when events arrived)."""
        async with self.lock:
            if self.events_since > 0 and time.monotonic() - self.last_recompute >= self.s.recompute_every_seconds:
                await self._recompute()

    async def force_recompute(self) -> Snapshot:
        async with self.lock:
            return await self._recompute()

    async def _recompute(self) -> Snapshot:
        snap = self.engine.snapshot()
        self.last_snapshot_obj = snap
        self.events_since = 0
        self.last_recompute = time.monotonic()
        self.n_recomputes += 1
        sim_day = snap.sim_day
        now = _iso_now()
        s90 = snap.stats[DISPLAY_WINDOW]
        asr_writes: dict[str, dict[str, str]] = {}
        latest_rows: list[tuple[Any, ...]] = []
        history_rows: list[tuple[Any, ...]] = []
        new_signals: list[dict[str, Any]] = []
        nonzero = np.nonzero(sum(snap.stats[W].n for W in snap.windows) > 0)
        for i, j in zip(*nonzero, strict=True):
            if i == j:
                continue
            a, b = snap.classes[i], snap.classes[j]
            status = classify(a, b, self.cat.known_class_pairs, self.faers)
            sig, sig_bh = bool(snap.signal[i, j]), bool(snap.signal_bh[i, j])
            if sig and (a, b) not in self._first_signal:
                self._first_signal[(a, b)] = sim_day
                self.db.set_state(f"first_signal:{a}>{b}", str(sim_day), now)
            became = sig and not self._signal.get((a, b), False)
            became_bh = sig_bh and not self._signal_bh.get((a, b), False)
            self._signal_bh[(a, b)] = sig_bh
            if became or became_bh:
                new_signals.append({"a": a, "b": b, "status": status, "bh": sig_bh, "asr": _f(s90.asr[i, j]),
                                    "lo": _f(s90.lo[i, j]), "hi": _f(s90.hi[i, j])})
            flag_changed = self._signal.get((a, b), False) != sig
            self._signal[(a, b)] = sig
            n90 = int(s90.n[i, j])
            if n90 > 0:
                key = (n90, round(float(s90.asr[i, j]), 2), round(float(s90.lo[i, j]), 2), sig)
                if self._written_asr.get((a, b)) != key:
                    self._written_asr[(a, b)] = key
                    hi = _f(s90.hi[i, j])
                    asr_writes[f"asr:{a}:{b}"] = {
                        "asr": str(_f(s90.asr[i, j]) if np.isfinite(s90.asr[i, j]) else 1.0),
                        "lo": str(_f(s90.lo[i, j]) or 0.0), "hi": "" if hi is None else str(hi), "n": str(n90),
                        "signal": str(int(sig)), "status": status, "sim_day": str(sim_day)}
            for W in snap.windows:
                st = snap.stats[W]
                row = (a, b, W, sim_day, int(st.n_ab[i, j]), int(st.n_ba[i, j]), _f(st.nsr[i, j]), _f(st.asr[i, j]),
                       _f(st.lo[i, j]), _f(st.hi[i, j]), _f(st.p_value[i, j]), int(sig), int(sig_bh), status)
                cmp = (row[4], row[5], None if row[7] is None else round(row[7], 2),
                       None if row[8] is None else round(row[8], 2), row[11], row[12])
                if self._written_latest.get((a, b, W)) != cmp:
                    self._written_latest[(a, b, W)] = cmp
                    latest_rows.append(row + (self._first_signal.get((a, b)), now))
                has_data = int(st.n_ab[i, j] + st.n_ba[i, j]) > 0
                last_h = self._history_day.get((a, b, W))
                if has_data and (flag_changed or last_h is None or sim_day - last_h >= HISTORY_EVERY_SIM_DAYS):
                    self._history_day[(a, b, W)] = sim_day
                    history_rows.append((now,) + (row[3], a, b, W) + row[4:])
        await self.bus.hset_many(asr_writes)
        self.db.executemany(
            "INSERT INTO signals_latest (a_l4, b_l4, win, sim_day, n_ab, n_ba, nsr, asr, lo, hi, p_value, is_signal, "
            "is_signal_bh, status, first_signal_day, updated_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (a_l4, b_l4, win) DO UPDATE SET sim_day = excluded.sim_day, n_ab = excluded.n_ab, "
            "n_ba = excluded.n_ba, nsr = excluded.nsr, asr = excluded.asr, lo = excluded.lo, hi = excluded.hi, "
            "p_value = excluded.p_value, is_signal = excluded.is_signal, is_signal_bh = excluded.is_signal_bh, "
            "status = excluded.status, first_signal_day = excluded.first_signal_day, updated_ts = excluded.updated_ts",
            latest_rows)
        self.db.executemany(
            "INSERT INTO signals (snapshot_ts, sim_day, a_l4, b_l4, win, n_ab, n_ba, nsr, asr, lo, hi, p_value, "
            "is_signal, is_signal_bh, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", history_rows)
        sim_date = (self.s.epoch + timedelta(days=sim_day)).isoformat()
        self.db.set_state("loop2_sim_day", str(sim_day), now)
        await self.bus.set("loop2:sim_day", str(sim_day))
        n_sig = int(snap.signal.sum())
        await self.bus.publish(STREAM_SIGNALS, {"type": "signals", "sim_day": sim_day, "sim_date": sim_date,
                                                "n_signals": n_sig, "n_signals_bh": int(snap.signal_bh.sum()),
                                                "new_signals": new_signals, "changed": len(latest_rows)})
        for sgl in new_signals:
            log.info("new signal", extra={**sgl, "sim_date": sim_date})
        return snap

    async def consume_forever(self, consumer: str = "signals-1") -> None:
        await self.bus.ensure_group(STREAM_DISPENSE, "signals")
        while True:
            msgs = await self.bus.read_group(STREAM_DISPENSE, "signals", consumer, count=500, block_ms=500)
            if msgs:
                try:
                    await self.process([DispenseEvent.model_validate(m) for _, m in msgs])
                except Exception:
                    log.exception("batch failed; acknowledging to avoid a poison loop", extra={"n": len(msgs)})
                await self.bus.ack(STREAM_DISPENSE, "signals", [i for i, _ in msgs])
            await self.tick()


def build_signal_service(bus: Bus, db: Database, settings: Settings | None = None) -> SignalService:
    return SignalService(bus, db, load_catalogue(), settings or get_settings())


# ----------------------------------------------------------------------------- HTTP service
_state: dict[str, Any] = {}


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db = Database.connect()
    db.init_schema()
    bus = get_bus()
    svc = build_signal_service(bus, db)
    await svc.restore()
    _state["svc"] = svc
    task = asyncio.create_task(svc.consume_forever(os.environ.get("HOSTNAME", "signals-1")))
    yield
    task.cancel()
    svc.save_incidence()


app = FastAPI(title="CASCADE signals (Loop 2)", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    svc: SignalService | None = _state.get("svc")
    return {"status": "ok" if svc else "starting", "service": "signals",
            "events": svc.engine.n_events if svc else 0, "recomputes": svc.n_recomputes if svc else 0,
            "sim_day": svc.engine.last_day if svc else None}


def dump_snapshot_json(snap: Snapshot) -> str:
    """Debug helper: signals in a snapshot as JSON lines."""
    out = []
    for i, j in zip(*np.nonzero(snap.signal), strict=True):
        out.append({"a": snap.classes[i], "b": snap.classes[j], **snap.pair(i, j)})
    return "\n".join(json.dumps(o) for o in out)
