"""Loop 1 patient state, incident check and candidate generation (sections 5.1-5.2).

Pure logic shared by the live scorer and the offline training/evaluation replay, so both compute
exactly the same thing. Redis layout (`pstate:{patient_token}`):
    _f          first observed day
    d:<drug_id> JSON [atc_l4, atc_l3, first_day, last_day, prescriber, last_dose, prev_dose, dose_up_day, days_supply]
    c:<atc_l4>  last dispense day of the class (incident check)
    cf:<atc_l4> first-ever dispense day of the class
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

LOOKBACK_DAYS = 365
CANDIDATE_WINDOW_DAYS = 180
ACTIVE_GRACE_DAYS = 15


@dataclass(slots=True)
class DrugRec:
    drug_id: str
    atc_l4: str
    atc_l3: str
    first_day: int
    last_day: int
    prescriber: str
    last_dose: float
    prev_dose: float
    dose_up_day: int
    days_supply: int

    def dump(self) -> str:
        return json.dumps([self.atc_l4, self.atc_l3, self.first_day, self.last_day, self.prescriber,
                           self.last_dose, self.prev_dose, self.dose_up_day, self.days_supply],
                          separators=(",", ":"))

    @classmethod
    def load(cls, drug_id: str, raw: str) -> DrugRec:
        l4, l3, first, last, pr, dose, prev, up, ds = json.loads(raw)
        return cls(drug_id, l4, l3, int(first), int(last), pr, float(dose), float(prev), int(up), int(ds))


@dataclass
class PatientState:
    first_seen: int | None = None
    drugs: dict[str, DrugRec] = field(default_factory=dict)
    class_last: dict[str, int] = field(default_factory=dict)
    class_first: dict[str, int] = field(default_factory=dict)
    prior_cascade_count: int = 0
    indications: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_hash(cls, h: Mapping[str, str]) -> PatientState:
        st = cls()
        for k, v in h.items():
            if k == "_f":
                try:
                    st.first_seen = int(v)
                except (ValueError, TypeError):
                    pass
            elif k == "pc":
                try:
                    st.prior_cascade_count = int(v)
                except (ValueError, TypeError):
                    st.prior_cascade_count = 0
            elif k.startswith("ind:"):
                try:
                    st.indications[k[4:]] = int(v)
                except (ValueError, TypeError):
                    pass
            elif k.startswith("d:"):
                st.drugs[k[2:]] = DrugRec.load(k[2:], v)
            elif k.startswith("cf:"):
                st.class_first[k[3:]] = int(v)
            elif k.startswith("c:"):
                st.class_last[k[2:]] = int(v)
        return st


@dataclass(slots=True)
class Ev:
    """Minimal event view used by Loop 1 (built from a DispenseEvent or a raw generated row)."""

    event_id: str
    ts: datetime
    day: int
    pharmacy_id: str
    patient: str
    prescriber: str
    age_band: str
    sex: str
    drug_id: str
    atc_l4: str
    atc_l3: str
    days_supply: int
    dose_mg: float

    @classmethod
    def from_event(cls, e: Any, epoch: date) -> Ev:
        return cls(e.event_id, e.ts, (e.ts.date() - epoch).days, e.pharmacy_id, e.patient_token,
                   e.prescriber_token, e.age_band, e.sex, e.drug_id, e.atc_l4, e.atc_l3, int(e.days_supply),
                   float(e.dose_mg))

    @classmethod
    def from_raw(cls, r: Mapping[str, str], epoch: date, day_cache: dict[str, int] | None = None) -> Ev:
        ts = r["ts"]
        d10 = ts[:10]
        if day_cache is not None and d10 in day_cache:
            day = day_cache[d10]
        else:
            day = (date.fromisoformat(d10) - epoch).days
            if day_cache is not None:
                day_cache[d10] = day
        return cls(r["event_id"], datetime.fromisoformat(ts), day, r["pharmacy_id"], r["patient_id"],
                   r["prescriber_id"], r["age_band"], r["sex"], r["drug_id"], r["atc_l4"], r["atc_l3"],
                   int(r["days_supply"]), float(r["dose_mg"]))


@dataclass(slots=True)
class Candidate:
    a: DrugRec  # snapshot of drug A before this event
    days_since_a: int


@dataclass
class StepResult:
    incident: bool
    candidates: list[Candidate]
    n_active_drugs: int
    b_class_first_prev: int | None
    fields: dict[str, str]  # changed pstate fields (for Redis)
    prior_cascade_count: int = 0
    has_primary_indication_for_b: bool = False


def step(st: PatientState, ev: Ev, lookback: int = LOOKBACK_DAYS,
         window: int = CANDIDATE_WINDOW_DAYS) -> StepResult:
    """Incident check + candidates from the state *before* this event, then update the state."""
    fields: dict[str, str] = {}
    if st.first_seen is None:
        st.first_seen = ev.day
        fields["_f"] = str(ev.day)
    prev_class_day = st.class_last.get(ev.atc_l4)
    observed = ev.day - st.first_seen >= lookback
    incident = observed and (prev_class_day is None or ev.day - prev_class_day > lookback)
    candidates: list[Candidate] = []
    n_active = 0
    b_first_prev = st.class_first.get(ev.atc_l4)
    has_indication = ev.atc_l4 in st.indications
    if incident:
        for rec in st.drugs.values():
            if rec.atc_l4 == ev.atc_l4:
                continue
            if ev.day - rec.last_day <= rec.days_supply + ACTIVE_GRACE_DAYS:
                n_active += 1
            gap = ev.day - rec.first_day
            if 1 <= gap <= window:
                candidates.append(Candidate(DrugRec(**{s: getattr(rec, s) for s in DrugRec.__slots__}), gap))
        n_active += 1  # B itself

    rec = st.drugs.get(ev.drug_id)
    if rec is not None and ev.day - rec.last_day <= lookback:
        if ev.dose_mg > rec.last_dose:
            rec.dose_up_day = ev.day
        if ev.dose_mg != rec.last_dose:
            rec.prev_dose = rec.last_dose
        rec.last_dose = ev.dose_mg
        rec.last_day = ev.day
        rec.prescriber = ev.prescriber
        rec.days_supply = ev.days_supply
    else:
        rec = DrugRec(ev.drug_id, ev.atc_l4, ev.atc_l3, ev.day, ev.day, ev.prescriber, ev.dose_mg, ev.dose_mg,
                      -1, ev.days_supply)
        st.drugs[ev.drug_id] = rec
    fields[f"d:{ev.drug_id}"] = rec.dump()
    st.class_last[ev.atc_l4] = ev.day
    fields[f"c:{ev.atc_l4}"] = str(ev.day)
    if ev.atc_l4 not in st.class_first:
        st.class_first[ev.atc_l4] = ev.day
        fields[f"cf:{ev.atc_l4}"] = str(ev.day)
    return StepResult(incident, candidates, n_active, b_first_prev, fields,
                      st.prior_cascade_count, has_indication)
