"""Loop 1 features (section 5.3) and the time-correct training replay.

pair_aSR_live / pair_aSR_lower_ci come from Loop 2 *as it was at the event's time*: the replay runs
the streaming PSSA engine alongside Loop 1 and refreshes the lookup every 5,000 events, exactly as
the live services do. End-of-period statistics are never used.

    python -m loop1.features      # build data/generated/loop1_rows.pkl
"""

from __future__ import annotations

import argparse
import csv
import gzip
import math
import pickle
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from common.config import get_settings
from datagen.catalogue import Catalogue, load_catalogue
from loop1.candidates import Candidate, Ev, PatientState, StepResult, step
from loop2.stream_counts import PState, StreamingPSSA

NUMERIC = ("days_since_A_start", "B_is_marker_class", "pair_in_known_list", "pair_aSR_live", "pair_aSR_lower_ci",
           "different_prescriber", "n_active_drugs", "age_band_ordinal", "A_dose_increase_30d",
           "B_started_before_A_ever")
CATEGORICAL = ("A_atc_l3", "B_atc_l3", "sex")
ASR_FEATURES = ("pair_aSR_live", "pair_aSR_lower_ci")
KNOWN_FEATURE = "pair_in_known_list"
DEFAULT_ASR = (1.0, 0.0, None, 0)  # (aSR, lower, upper, n) when Loop 2 has no statistics yet
TRAIN_END = "2025-01-01"
VAL_END = "2025-07-01"

AsrLookup = Callable[[str, str], tuple[float, float, float | None, int]]


def age_ordinal(band: str) -> int:
    return 18 if band.endswith("+") else int(band.split("-")[0]) // 5


def asr_transform(x: float) -> float:
    """Model input for aSR-type features: log of the ratio, clipped (ratios are heavy-tailed)."""
    return math.log(min(max(x, 1e-3), 1000.0))


@dataclass
class FeatureSpace:
    numeric: tuple[str, ...]
    categories: dict[str, tuple[str, ...]]
    columns: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.columns:
            self.columns = list(self.numeric) + [f"{c}={v}" for c in CATEGORICAL if c in self.categories
                                                 for v in self.categories[c]]
        self._pos = {c: i for i, c in enumerate(self.columns)}

    @classmethod
    def build(cls, cat: Catalogue, drop: Sequence[str] = ()) -> FeatureSpace:
        l3 = tuple(sorted({d.atc_l3 for d in cat.drugs}))
        return cls(tuple(f for f in NUMERIC if f not in drop),
                   {"A_atc_l3": l3, "B_atc_l3": l3, "sex": ("F", "M", "U")})

    def base_feature(self, column: str) -> str:
        return column.split("=", 1)[0]

    def vector(self, f: Mapping[str, Any], out: np.ndarray | None = None) -> np.ndarray:
        x = np.zeros(len(self.columns), dtype=np.float32) if out is None else out
        for i, name in enumerate(self.numeric):
            v = float(f[name])
            x[i] = asr_transform(v) if name in ASR_FEATURES else v
        for c in CATEGORICAL:
            j = self._pos.get(f"{c}={f[c]}")
            if j is not None:
                x[j] = 1.0
        return x

    def matrix(self, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
        X = np.zeros((len(rows), len(self.columns)), dtype=np.float32)
        for i, r in enumerate(rows):
            self.vector(r, X[i])
        return X

    def to_json(self) -> dict[str, Any]:
        return {"numeric": list(self.numeric), "categories": {k: list(v) for k, v in self.categories.items()},
                "columns": self.columns, "asr_transform": "log(clip(x, 1e-3, 1000))"}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> FeatureSpace:
        return cls(tuple(d["numeric"]), {k: tuple(v) for k, v in d["categories"].items()}, list(d["columns"]))


def feature_row(cat: Catalogue, ev: Ev, res: StepResult, cand: Candidate, asr: AsrLookup) -> dict[str, Any]:
    """All features (plus display metadata) for one (A -> B) candidate pair."""
    a = cand.a
    a_asr, a_lo, a_hi, a_n = asr(a.atc_l4, ev.atc_l4)
    known = cat.known_pair(a.atc_l4, ev.atc_l4)
    b_drug = cat.by_id.get(ev.drug_id)
    return {
        "days_since_A_start": cand.days_since_a,
        "A_atc_l3": a.atc_l3,
        "B_atc_l3": ev.atc_l3,
        "B_is_marker_class": int(bool(b_drug and b_drug.is_marker)),
        "pair_in_known_list": int(known is not None),
        "pair_aSR_live": a_asr,
        "pair_aSR_lower_ci": a_lo,
        "different_prescriber": int(a.prescriber != ev.prescriber),
        "n_active_drugs": res.n_active_drugs,
        "age_band_ordinal": age_ordinal(ev.age_band),
        "sex": ev.sex,
        "A_dose_increase_30d": int(a.dose_up_day >= 0 and 0 <= ev.day - a.dose_up_day <= 30),
        "B_started_before_A_ever": int(res.b_class_first_prev is not None and res.b_class_first_prev < a.first_day),
        # metadata (not model inputs)
        "_A_drug_id": a.drug_id, "_A_atc_l4": a.atc_l4, "_A_dose": a.last_dose, "_aSR_hi": a_hi, "_aSR_n": a_n,
        "_known_pair_id": known.pair_id if known else None, "_B_dose": ev.dose_mg, "_age_band": ev.age_band,
    }


# ----------------------------------------------------------------------------- replay
@dataclass
class Row:
    event_id: str
    ts: str
    pharmacy_id: str
    patient: str
    a_drug: str
    b_drug: str
    a_l4: str
    b_l4: str
    label: int
    pair_id: str | None
    hidden_pair_id: str | None
    asof_events: int  # number of events Loop 2 had processed at the last refresh before this row
    features: dict[str, Any]


def read_labels(path: Path) -> dict[str, tuple[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {r["event_id"]: (r["cascade_index_drug"], r["pair_id"]) for r in csv.DictReader(f)}


def iter_raw_rows(path: Path) -> Iterable[dict[str, str]]:
    with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
        yield from csv.DictReader(f)


def replay_rows(rows: Iterable[Mapping[str, str]], cat: Catalogue, labels: Mapping[str, tuple[str, str]],
                hidden: Mapping[str, tuple[str, str]] | None = None, epoch: date = date(2023, 1, 1),
                refresh_every: int = 5000, on_event: Callable[[int, Ev], None] | None = None) -> list[Row]:
    """Replay events through Loop 2 (streaming PSSA) and Loop 1 candidates, in time order."""
    hidden = hidden or {}
    pssa = StreamingPSSA(cat.classes)
    pstates: dict[str, PState] = {}
    states: dict[str, PatientState] = {}
    lookup: dict[tuple[str, str], tuple[float, float, float, int]] = {}
    asof = 0
    out: list[Row] = []
    day_cache: dict[str, int] = {}

    def asr(a: str, b: str) -> tuple[float, float, float | None, int]:
        return lookup.get((a, b), DEFAULT_ASR)

    n = 0
    for raw in rows:
        ev = Ev.from_raw(raw, epoch, day_cache)
        st = states.get(ev.patient)
        if st is None:
            st = states[ev.patient] = PatientState()
        res = step(st, ev)
        if res.incident and res.candidates:
            lab = labels.get(ev.event_id)
            hid = hidden.get(ev.event_id)
            for cand in res.candidates:
                f = feature_row(cat, ev, res, cand, asr)
                out.append(Row(ev.event_id, raw["ts"], ev.pharmacy_id, ev.patient, cand.a.drug_id, ev.drug_id,
                               cand.a.atc_l4, ev.atc_l4, int(lab is not None and lab[0] == cand.a.drug_id),
                               lab[1] if lab is not None and lab[0] == cand.a.drug_id else None,
                               hid[1] if hid is not None and hid[0] == cand.a.drug_id else None, asof, f))
        ps = pstates.get(ev.patient)
        if ps is None:
            ps = pstates[ev.patient] = PState()
        pssa.process(ps, ev.atc_l4, ev.day)
        n += 1
        if on_event is not None:
            on_event(n, ev)
        if n % refresh_every == 0:
            lookup = pssa.snapshot().asr_lookup()
            asof = n
    return out


def split_of(ts: str) -> str:
    return "train" if ts < TRAIN_END else "val" if ts < VAL_END else "test"


def dataset_path() -> Path:
    return get_settings().generated_dir / "loop1_rows.pkl"


def build_dataset(events: Path | None = None, out: Path | None = None) -> list[Row]:
    s = get_settings()
    gen = s.generated_dir if events is None else events.parent
    events = events or gen / "events.csv.gz"
    cat = load_catalogue()
    t0 = time.time()
    rows = replay_rows(iter_raw_rows(events), cat, read_labels(gen / "labels.csv"),
                       read_labels(gen / "labels_hidden.csv"), s.epoch, s.recompute_every_events)
    out = out or (gen / "loop1_rows.pkl")
    with out.open("wb") as f:
        pickle.dump(rows, f, protocol=pickle.HIGHEST_PROTOCOL)
    pos = sum(r.label for r in rows)
    print(f"candidate rows: {len(rows):,} (positives {pos:,}) in {time.time() - t0:.0f} s -> {out}")
    return rows


def load_dataset(path: Path | None = None) -> list[Row]:
    with (path or dataset_path()).open("rb") as f:
        return pickle.load(f)  # our own artefact, never user input


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the time-correct Loop 1 candidate dataset")
    ap.add_argument("--events", type=Path, default=None)
    args = ap.parse_args()
    from loop1 import features  # pickle Row under its importable module name, not __main__

    features.build_dataset(args.events)


if __name__ == "__main__":
    main()
