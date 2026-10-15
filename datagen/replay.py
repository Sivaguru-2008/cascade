"""Time-ordered replay of generated raw events (section 4.6).

Reading and pacing live here; tokenising and publishing live in edge/pharmacy_edge.py, because only
the edge holds EDGE_SECRET. `python -m datagen.replay ...` is an alias for the edge CLI.
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from common.config import get_settings

RAW_COLUMNS = ("event_id", "ts", "pharmacy_id", "patient_id", "prescriber_id", "age_band", "sex", "drug_id",
               "atc_code", "atc_l4", "atc_l3", "qty", "days_supply", "dose_mg")


def default_events_path() -> Path:
    return get_settings().generated_dir / "events.csv.gz"


def iter_raw(path: Path | None = None, start: int = 0, stop: int | None = None) -> Iterator[dict[str, str]]:
    """Yield raw rows [start, stop) in file order (the generator writes them sorted by ts)."""
    path = path or default_events_path()
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f)):
            if i < start:
                continue
            if stop is not None and i >= stop:
                return
            yield row


@dataclass
class Pacer:
    """Maps simulated time to wall time. mode: max | days_per_sec | events_per_sec."""

    mode: str = "days_per_sec"
    rate: float = 1.0
    _t0_wall: float | None = None
    _t0_sim: datetime | None = None
    _n: int = 0

    @classmethod
    def parse(cls, spec: str) -> Pacer:
        spec = spec.strip()
        if spec == "max":
            return cls("max", 0.0)
        name, _, value = spec.partition("=")
        if name not in ("days_per_sec", "events_per_sec") or not value:
            raise ValueError("speed must be 'max', 'days_per_sec=<x>' or 'events_per_sec=<x>'")
        return cls(name, float(value))

    def reset(self) -> None:
        self._t0_wall = self._t0_sim = None
        self._n = 0

    def delay_for(self, ts: datetime) -> float:
        """Seconds to wait before emitting an event with simulated time ts."""
        now = time.monotonic()
        if self.mode == "max":
            return 0.0
        if self._t0_wall is None:
            self._t0_wall, self._t0_sim = now, ts
        if self.mode == "events_per_sec":
            target = self._t0_wall + self._n / self.rate
            self._n += 1
        else:
            sim_days = (ts - self._t0_sim).total_seconds() / 86400.0  # type: ignore[operator]
            target = self._t0_wall + sim_days / self.rate
        return max(0.0, target - now)

    async def wait(self, ts: datetime) -> None:
        delay = self.delay_for(ts)
        if delay > 0:
            await asyncio.sleep(delay)


DEMO_PLAN = "docs/demo_script_events.json"
H1_PAIR = ("C09XX", "A02BC")
SLOW = "events_per_sec=1"
CONTEXT_BEFORE, CONTEXT_AFTER = 8, 3


def demo_plan_path() -> Path:
    from common.config import ROOT

    return ROOT / DEMO_PLAN


def build_demo_plan(events: Path | None = None, out: Path | None = None) -> dict[str, Any]:
    """Choose the demo events with the trained model (offline, time-correct features) and write the plan.

    1. warm-up at max speed; 2. a slowed K1 amlodipine -> furosemide cascade event (scored >= threshold);
    3. a slowed coincidental A -> B event on a known class pair, label 0, scored below threshold;
    4. fast-forward through the Novacor ramp until Loop 2 flags H1 (the edge stops when it sees the flag).
    """
    import json

    from loop1.features import load_dataset
    from loop1.train import Loop1Model, model_dir_for

    s = get_settings()
    events = events or default_events_path()
    rows = load_dataset()
    model = Loop1Model.load(model_dir_for("full"))
    thr = s.threshold_override if s.threshold_override is not None else model.threshold
    known = model.cat.known_class_pairs
    window = [r for r in rows if "2025-03-01" <= r.ts < "2025-07-01"]
    scores = model.score([r.features for r in window])[0] if window else []
    scored = list(zip(window, scores, strict=True))
    k1 = [(r, sc) for r, sc in scored if r.pair_id == "K1" and sc >= thr and r.ts < "2025-06-20"]
    if not k1:
        raise SystemExit("no K1 cascade event scored above threshold in 2025-03..06; cannot build the demo plan")
    k1_row, k1_score = max(k1, key=lambda t: t[0].ts)
    coinc = [(r, sc) for r, sc in scored if r.label == 0 and (r.a_l4, r.b_l4) in known and sc < thr
             and r.ts > k1_row.ts and r.hidden_pair_id is None]
    same_ph = [t for t in coinc if t[0].pharmacy_id == k1_row.pharmacy_id]
    pick = (same_ph or coinc or [None])[0]
    wanted = {k1_row.event_id} | ({pick[0].event_id} if pick else set())
    index: dict[str, int] = {}
    n_total = 0
    for i, row in enumerate(iter_raw(events)):
        if row["event_id"] in wanted:
            index[row["event_id"]] = i
        n_total = i + 1
    i_k1 = index[k1_row.event_id]
    segments = [{"name": "warmup", "start": 0, "stop": max(0, i_k1 - CONTEXT_BEFORE), "speed": "max"},
                {"name": "k1_cascade", "start": max(0, i_k1 - CONTEXT_BEFORE), "stop": i_k1 + CONTEXT_AFTER + 1,
                 "speed": SLOW}]
    if pick:
        i_c = index[pick[0].event_id]
        segments += [{"name": "to_coincidental", "start": i_k1 + CONTEXT_AFTER + 1,
                      "stop": max(i_k1 + CONTEXT_AFTER + 1, i_c - CONTEXT_BEFORE), "speed": "max"},
                     {"name": "coincidental", "start": max(i_k1 + CONTEXT_AFTER + 1, i_c - CONTEXT_BEFORE),
                      "stop": i_c + CONTEXT_AFTER + 1, "speed": SLOW}]
        last = i_c + CONTEXT_AFTER + 1
    else:
        last = i_k1 + CONTEXT_AFTER + 1
    segments.append({"name": "novacor_fast_forward", "start": last, "stop": n_total, "speed": "max",
                     "stop_when_signal": list(H1_PAIR)})

    def desc(r: Any, sc: float) -> dict[str, Any]:
        return {"event_id": r.event_id, "ts": r.ts, "pharmacy_id": r.pharmacy_id, "index_drug_id": r.a_drug,
                "marker_drug_id": r.b_drug, "pair": [r.a_l4, r.b_l4], "label": r.label,
                "offline_score": round(float(sc), 4), "row_index": index.get(r.event_id)}

    plan = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "threshold": thr, "events_file": str(events),
            "demo_pharmacy": k1_row.pharmacy_id, "k1_cascade": desc(k1_row, k1_score),
            "coincidental": desc(*pick) if pick else None,
            "coincidental_note": ("label 0 on a known class pair, scored below threshold offline"
                                  + ("" if not pick or pick[0].pharmacy_id == k1_row.pharmacy_id
                                     else "; at a different pharmacy (none found at the demo pharmacy)"))
            if pick else "NOT FOUND: no label-0 known-pair event scored below threshold after the K1 event",
            "h1_pair": list(H1_PAIR), "segments": segments,
            "note": "offline_score uses time-correct offline features; the live score can differ slightly because "
                    "Loop 2 refreshes on a timer in the live system."}
    out = out or demo_plan_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return plan


async def _status(edge: Any, **fields: Any) -> None:
    import json

    fields["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    fields["published_total"] = edge.published
    fields["current_ts"] = edge.current_ts
    await edge.bus.set("demo:status", json.dumps(fields))


class _signal_check:  # noqa: N801 - used like a function
    """Called every 2,000 events: refreshes demo:status and, if a pair is given, returns True once Loop 2
    has flagged it (read from asr:{A}:{B})."""

    def __init__(self, edge: Any, stage: str, pair: list[str] | None) -> None:
        self.edge, self.stage, self.flagged = edge, stage, False
        self.key = f"asr:{pair[0]}:{pair[1]}" if pair else None

    async def __call__(self) -> bool:
        await _status(self.edge, stage=self.stage)
        if self.key is None:
            return False
        h = (await self.edge.bus.hgetall_many([self.key]))[0]
        self.flagged = h.get("signal") == "1"
        return self.flagged


async def run_demo_script(edge: Any, path: Path | None = None, warmup_only: bool = False) -> None:
    """Play docs/demo_script_events.json. Progress is kept in the bus (`demo:next_index`), so a pre-run
    warm-up (`--demo-script --warmup-only`) can be continued later by the dashboard's "Run demo script" button."""
    import itertools
    import json

    plan_file = demo_plan_path()
    if not plan_file.exists():
        await _status(edge, stage="error", error="docs/demo_script_events.json missing: run "
                                                 "`python -m datagen.replay --build-demo-plan`")
        return
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    pos = int(await edge.bus.get("demo:next_index") or 0)
    rows = iter_raw(path, pos)
    for seg in plan["segments"]:
        if seg["stop"] <= pos:
            continue
        await _status(edge, stage=seg["name"])
        chunk = itertools.islice(rows, seg["stop"] - pos)
        stop_pair = seg.get("stop_when_signal")
        check = _signal_check(edge, seg["name"], stop_pair)
        n = await edge.publish_rows(chunk, Pacer.parse(seg["speed"]), async_stop=check)
        flagged = check.flagged
        pos += n
        await edge.bus.set("demo:next_index", str(pos))
        if warmup_only and seg["name"] == "warmup":
            await _status(edge, stage="warmed_up")
            return
        if flagged:
            await _status(edge, stage="h1_flagged", h1_pair=stop_pair)
            return
    await _status(edge, stage="done")


def main() -> None:
    import sys

    if "--build-demo-plan" in sys.argv:
        import json

        plan = build_demo_plan()
        print(json.dumps({k: plan[k] for k in ("demo_pharmacy", "k1_cascade", "coincidental", "coincidental_note")},
                         indent=2))
        return
    from edge.pharmacy_edge import main as edge_main

    edge_main()


if __name__ == "__main__":
    main()
