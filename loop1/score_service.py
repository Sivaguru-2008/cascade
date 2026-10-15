"""Loop 1 real-time scorer (section 5.6).

Consumes stream:dispense (consumer group "scorer") in small batches: one pipelined read of patient
state, the pure candidate/feature logic from loop1.candidates/features, one pipelined read of Loop 2's
live `asr:{A}:{B}` keys, model scoring, then alert writes (Postgres `alerts` + stream:alerts).
latency_ms = time from message read to alert write, or to the no-alert decision.

    uvicorn loop1.score_service:app --port 8001     # also exposes POST /score and GET /health
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any

import numpy as np
from fastapi import FastAPI

from common.bus import STREAM_ALERTS, STREAM_DISPENSE, Bus, get_bus
from common.config import Settings, get_settings
from common.crypto import Cipher, cipher_from_settings
from common.db import Database
from common.logging import get_logger
from common.schemas import Alert, DispenseEvent, ScoreDecision
from datagen.catalogue import UNKNOWN_REACTION, Catalogue, load_catalogue, load_faers
from loop1.candidates import Ev, PatientState, StepResult, step
from loop1.features import DEFAULT_ASR, feature_row
from loop1.train import Loop1Model, model_dir_for
from loop2.pssa import classify, faers_supported_pairs

log = get_logger("scorer")
ASR_CACHE_TTL_S = 2.0
LATENCY_FLUSH_S = 1.0


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def known_source_label(cat: Catalogue, pair_id: str | None) -> str | None:
    k = cat.known_by_id.get(pair_id or "")
    if k is None:
        return None
    return k.source_short if k.verified else f"{k.source_short} (source not yet verified)"


class Scorer:
    def __init__(self, model: Loop1Model, cat: Catalogue, bus: Bus, db: Database, cipher: Cipher,
                 settings: Settings) -> None:
        self.model = model
        self.cat = cat
        self.bus = bus
        self.db = db
        self.cipher = cipher
        self.s = settings
        self.threshold = settings.threshold_override if settings.threshold_override is not None else model.threshold
        self.faers = faers_supported_pairs(load_faers())
        self.lock = asyncio.Lock()
        self._asr: dict[tuple[str, str], tuple[float, tuple[float, float, float | None, int]]] = {}
        self._latency: list[tuple[str, float, int, float]] = []
        self._last_flush = time.monotonic()
        self.n_events = 0
        self.n_alerts = 0

    async def _asr_many(self, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], tuple[float, float, Any, int]]:
        now = time.monotonic()
        out: dict[tuple[str, str], tuple[float, float, Any, int]] = {}
        missing = []
        for p in pairs:
            hit = self._asr.get(p)
            if hit is not None and hit[0] > now:
                out[p] = hit[1]
            else:
                missing.append(p)
        if missing:
            hashes = await self.bus.hgetall_many([f"asr:{a}:{b}" for a, b in missing])
            for p, h in zip(missing, hashes, strict=True):
                val = DEFAULT_ASR
                if h and int(h.get("n", "0")) > 0:
                    hi = h.get("hi")
                    val = (float(h["asr"]), float(h["lo"]), float(hi) if hi not in (None, "") else None,
                           int(h["n"]))
                self._asr[p] = (now + ASR_CACHE_TTL_S, val)
                out[p] = val
        return out

    async def process(self, events: list[DispenseEvent], t_read: float) -> list[ScoreDecision]:
        async with self.lock:
            return await self._process(events, t_read)

    async def _process(self, events: list[DispenseEvent], t_read: float) -> list[ScoreDecision]:
        evs = [Ev.from_event(e, self.s.epoch) for e in events]
        tokens = list(dict.fromkeys(ev.patient for ev in evs))
        hashes = await self.bus.hgetall_many([f"pstate:{t}" for t in tokens])
        states = {t: PatientState.from_hash(h) for t, h in zip(tokens, hashes, strict=True)}
        dirty: dict[str, dict[str, str]] = {t: {} for t in tokens}
        stepped: list[tuple[Ev, DispenseEvent, StepResult]] = []
        needed: set[tuple[str, str]] = set()
        for ev, e in zip(evs, events, strict=True):
            res = step(states[ev.patient], ev, self.s.lookback_days, self.s.candidate_window_days)
            dirty[ev.patient].update(res.fields)
            stepped.append((ev, e, res))
            if res.incident:
                needed |= {(c.a.atc_l4, ev.atc_l4) for c in res.candidates}
        asr = await self._asr_many(needed) if needed else {}

        def lookup(a: str, b: str) -> tuple[float, float, Any, int]:
            return asr.get((a, b), DEFAULT_ASR)

        decisions: list[ScoreDecision] = []
        for ev, e, res in stepped:
            d = ScoreDecision(event_id=ev.event_id, incident=res.incident, scored=False,
                              n_candidates=len(res.candidates), threshold=self.threshold)
            if res.incident and res.candidates:
                feats = [feature_row(self.cat, ev, res, c, lookup) for c in res.candidates]
                scores, _, _, X = self.model.score(feats)
                k = int(np.argmax(scores))
                d.scored, d.score = True, round(float(scores[k]), 6)
                if scores[k] >= self.threshold:
                    alert = await self._alert(e, ev, res, feats[k], X[k], float(scores[k]))
                    d.alerted, d.alert = True, alert
            d.latency_ms = round((time.perf_counter() - t_read) * 1000.0, 3)
            self._latency.append((ev.event_id, d.latency_ms, int(d.alerted), time.time()))
            decisions.append(d)
        await self.bus.hset_many({f"pstate:{t}": f for t, f in dirty.items() if f})
        self.n_events += len(events)
        if time.monotonic() - self._last_flush > LATENCY_FLUSH_S or len(self._latency) > 5000:
            await self.aflush_latency()
        return decisions

    async def _alert(self, e: DispenseEvent, ev: Ev, res: StepResult, f: Mapping[str, Any], x: np.ndarray,
                     score: float) -> Alert:
        seq = await self.bus.incr("seq:alert")
        alert = make_alert(self.cat, self.faers, f"AL-{seq:06d}", ev.event_id, e.ts, ev.pharmacy_id, ev.patient,
                           ev.drug_id, ev.atc_l4, f, score, self.model.reasons(f, x, ev.drug_id))
        payload = alert.model_dump(mode="json")
        ts = now_iso()
        await asyncio.to_thread(
            self.db.execute,
            "INSERT INTO alerts (alert_id, event_id, ts, pharmacy_id, score, status, index_atc_l4, marker_atc_l4, "
            "pair_status, enc_blob, created_ts, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (alert.alert_id, alert.event_id, payload["ts"], alert.pharmacy_id, alert.score, alert.status,
             alert.index_atc_l4, alert.marker_atc_l4, alert.pair_status,
             self.cipher.encrypt_json(payload, aad=alert.alert_id), ts, ts))
        await self.bus.publish(STREAM_ALERTS, payload)
        self.n_alerts += 1
        return alert

    async def aflush_latency(self) -> None:
        """Latency rows are written in a worker thread so a slow disk never blocks the event loop."""
        rows, self._latency = self._latency, []
        self._last_flush = time.monotonic()
        if rows:
            await asyncio.to_thread(
                self.db.executemany, "INSERT INTO latency (event_id, ms, alerted, wall_ts) VALUES (?, ?, ?, ?)", rows)

    def flush_latency(self) -> None:
        rows, self._latency = self._latency, []
        self._last_flush = time.monotonic()
        if rows:
            self.db.executemany("INSERT INTO latency (event_id, ms, alerted, wall_ts) VALUES (?, ?, ?, ?)", rows)

    async def consume_forever(self, consumer: str = "scorer-1") -> None:
        await self.bus.ensure_group(STREAM_DISPENSE, "scorer")
        while True:
            msgs = await self.bus.read_group(STREAM_DISPENSE, "scorer", consumer, count=200, block_ms=500)
            if msgs:
                t_read = time.perf_counter()
                try:
                    events = [DispenseEvent.model_validate(m) for _, m in msgs]
                    await self.process(events, t_read)
                except Exception:
                    log.exception("batch failed; acknowledging to avoid a poison loop", extra={"n": len(msgs)})
                await self.bus.ack(STREAM_DISPENSE, "scorer", [i for i, _ in msgs])
            elif self._latency:
                await self.aflush_latency()


def make_alert(cat: Catalogue, faers: set[tuple[str, str]], alert_id: str, event_id: str, ts: datetime,
               pharmacy_id: str, patient: str, b_drug_id: str, b_l4: str, f: Mapping[str, Any], score: float,
               reasons: list[str]) -> Alert:
    """The Alert payload (section 5.5) from a scored feature row; shared by the live scorer and eval."""
    a = cat.by_id.get(f["_A_drug_id"])
    b = cat.by_id.get(b_drug_id)
    known = cat.known_by_id.get(f.get("_known_pair_id") or "")
    n = int(f["_aSR_n"])
    return Alert(
        alert_id=alert_id, event_id=event_id, ts=ts, pharmacy_id=pharmacy_id, patient_token=patient,
        index_drug=a.display_name if a else f["_A_drug_id"], marker_drug=b.display_name if b else b_drug_id,
        index_atc_l4=f["_A_atc_l4"], marker_atc_l4=b_l4, days_between=int(f["days_since_A_start"]),
        score=round(score, 4), reasons=reasons,
        pair_aSR=round(float(f["pair_aSR_live"]), 3) if n else None,
        pair_aSR_ci=[round(float(f["pair_aSR_lower_ci"]), 3), _r(f.get("_aSR_hi"))] if n else [None, None],
        known_source=known_source_label(cat, known.pair_id if known else None),
        index_drug_id=f["_A_drug_id"], marker_drug_id=b_drug_id, index_dose_mg=float(f["_A_dose"]),
        marker_dose_mg=float(f["_B_dose"]), suspected_reaction=known.reaction if known else UNKNOWN_REACTION,
        age_band=f["_age_band"], sex=f["sex"], n_active_drugs=int(f["n_active_drugs"]),
        different_prescriber=bool(f["different_prescriber"]),
        pair_status=classify(f["_A_atc_l4"], b_l4, cat.known_class_pairs, faers),  # type: ignore[arg-type]
    )


def _r(x: Any) -> float | None:
    return None if x is None else round(float(x), 3)


def build_scorer(bus: Bus, db: Database, settings: Settings | None = None) -> Scorer:
    s = settings or get_settings()
    variant = "no_known" if s.no_known_prior else "full"
    cat = load_catalogue()
    model = Loop1Model.load(model_dir_for(variant, s.model_dir), cat)
    log.info("model loaded", extra={"variant": variant, "backend": model.backend, "threshold": model.threshold})
    return Scorer(model, cat, bus, db, cipher_from_settings(), s)


# ----------------------------------------------------------------------------- HTTP service
_state: dict[str, Any] = {}


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db = Database.connect()
    db.init_schema()
    bus = get_bus()
    scorer = build_scorer(bus, db)
    _state["scorer"] = scorer
    task = asyncio.create_task(scorer.consume_forever(os.environ.get("HOSTNAME", "scorer-1")))
    yield
    task.cancel()
    scorer.flush_latency()


app = FastAPI(title="CASCADE scorer (Loop 1)", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    sc: Scorer | None = _state.get("scorer")
    return {"status": "ok" if sc else "starting", "service": "scorer",
            "model": sc.model.variant if sc else None, "backend": sc.model.backend if sc else None,
            "threshold": sc.threshold if sc else None, "events": sc.n_events if sc else 0,
            "alerts": sc.n_alerts if sc else 0}


@app.post("/score", response_model=ScoreDecision)
async def score(event: DispenseEvent) -> ScoreDecision:
    sc: Scorer = _state["scorer"]
    t_read = time.perf_counter()
    return (await sc.process([event], t_read))[0]
