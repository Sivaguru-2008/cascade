"""Pure-Python synthetic dispensing generator (sections 4.3 and 4.4).

    python -m datagen.generate --patients 20000 --seed 7
    python -m datagen.generate --patients 20000 --seed 7 --no-inject --out data/generated/null

Writes to --out (default data/generated/):
    events.csv.gz       raw dispenses (raw patient_id / prescriber_id; the edge tokenises them)
    labels.csv          known-cascade labels K1-K9 (training/evaluation only, never on the bus)
    labels_hidden.csv   hidden Novacor -> PPI labels (H1, never used for training)
    generation_report.json

All data is synthetic. Rates are chosen by the team, not estimated from real data.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
import time
import uuid
from array import array
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from common.config import get_settings
from datagen import inject
from datagen.catalogue import Catalogue, load_catalogue

START = date(2023, 1, 1)
NOVACOR_LAUNCH = date(2025, 7, 1)
NOVACOR_MAX_SHARE = 0.15
NOVACOR_RAMP_DAYS = 182
H1 = dict(pair_id="H1", index="Novacor", marker="pantoprazole", lag_min=7, lag_max=45, p=0.06)
N_PHARMACIES = 50
N_PRESCRIBERS = 300
HOME_PHARMACY_P = 0.90
DISCONTINUE_PER_YEAR = 0.10
CHRONIC_DAYS_SUPPLY = 30
MIN_KNOWN_EVENTS = 150  # at the reference population of 20,000
MIN_HIDDEN_EVENTS = 80
ANTIHYPERTENSIVE_L4 = {"C08CA", "C09AA", "C09CA", "C03AA", "C03BA", "C07AB"}

AGE_GROUPS = ("<40", "40-59", "60-74", "75+")

# condition -> (prevalence by age group, first-line drugs, max drugs, sex restriction)
CONDITIONS: dict[str, tuple[tuple[float, float, float, float], tuple[str, ...], int, str | None]] = {
    "hypertension": ((0.10, 0.30, 0.50, 0.60), ("amlodipine", "nifedipine", "enalapril", "ramipril",
                     "lisinopril", "perindopril", "losartan", "telmisartan", "valsartan",
                     "olmesartan medoxomil", "hydrochlorothiazide", "chlortalidone", "indapamide",
                     "metoprolol", "atenolol", "bisoprolol"), 2, None),
    "diabetes": ((0.03, 0.12, 0.22, 0.22), ("metformin", "glimepiride", "gliclazide", "sitagliptin",
                 "vildagliptin", "empagliflozin", "dapagliflozin"), 2, None),
    "dyslipidaemia": ((0.05, 0.15, 0.30, 0.30), ("atorvastatin", "rosuvastatin", "simvastatin"), 1, None),
    "ischaemic_heart": ((0.005, 0.03, 0.10, 0.15), ("acetylsalicylic acid", "clopidogrel",
                        "isosorbide mononitrate"), 2, None),
    "atrial_fibrillation": ((0.002, 0.01, 0.05, 0.10), ("apixaban", "rivaroxaban", "warfarin", "digoxin"),
                            1, None),
    "heart_failure": ((0.001, 0.005, 0.03, 0.07), ("furosemide", "torasemide", "spironolactone",
                      "carvedilol"), 2, None),
    "gerd": ((0.08, 0.10, 0.12, 0.12), ("pantoprazole", "omeprazole", "rabeprazole", "esomeprazole",
             "lansoprazole", "famotidine"), 1, None),
    "osteoarthritis": ((0.02, 0.10, 0.20, 0.25), ("paracetamol", "diclofenac", "ibuprofen", "naproxen",
                       "etoricoxib", "tramadol"), 1, None),
    "neuropathic_pain": ((0.01, 0.03, 0.05, 0.05), ("pregabalin", "gabapentin", "duloxetine",
                         "amitriptyline"), 1, None),
    "depression": ((0.06, 0.06, 0.05, 0.05), ("sertraline", "escitalopram", "fluoxetine", "mirtazapine",
                   "duloxetine"), 1, None),
    "anxiety": ((0.04, 0.04, 0.03, 0.03), ("escitalopram", "alprazolam", "clonazepam", "lorazepam"), 1, None),
    "psychosis": ((0.01, 0.01, 0.01, 0.01), ("risperidone", "olanzapine", "quetiapine", "aripiprazole",
                  "haloperidol"), 1, None),
    "dementia": ((0.0, 0.0, 0.02, 0.08), ("donepezil", "rivastigmine", "memantine"), 1, None),
    "bph": ((0.0, 0.05, 0.20, 0.30), ("tamsulosin", "alfuzosin", "silodosin", "finasteride"), 1, "M"),
    "parkinson": ((0.0, 0.002, 0.01, 0.02), ("levodopa and decarboxylase inhibitor", "pramipexole",
                  "ropinirole"), 1, None),
    "hypothyroid": ((0.03, 0.05, 0.06, 0.06), ("levothyroxine sodium",), 1, None),
    "asthma_copd": ((0.06, 0.06, 0.08, 0.10), ("salbutamol", "budesonide", "tiotropium bromide",
                    "montelukast"), 2, None),
    "epilepsy": ((0.01, 0.01, 0.01, 0.01), ("levetiracetam", "valproic acid", "carbamazepine"), 1, None),
    "osteoporosis": ((0.0, 0.02, 0.08, 0.12), ("alendronic acid", "colecalciferol", "calcium carbonate"),
                     2, None),
    "gout": ((0.005, 0.02, 0.04, 0.05), ("allopurinol", "febuxostat", "colchicine"), 1, None),
    "insomnia": ((0.03, 0.04, 0.06, 0.08), ("zolpidem", "zopiclone"), 1, None),
    "allergic_rhinitis": ((0.10, 0.07, 0.04, 0.03), ("cetirizine", "levocetirizine", "loratadine",
                          "fexofenadine"), 1, None),
    "overactive_bladder": ((0.005, 0.02, 0.05, 0.08), ("oxybutynin", "solifenacin", "tolterodine",
                           "mirabegron"), 1, None),
    "vertigo": ((0.005, 0.01, 0.03, 0.04), ("betahistine", "cinnarizine"), 1, None),
    "constipation": ((0.02, 0.03, 0.05, 0.08), ("lactulose", "macrogol", "senna glycosides", "bisacodyl"),
                     1, None),
}
N_CONDITIONS_P = {"<40": (0.60, 0.28, 0.09, 0.03), "40-59": (0.40, 0.33, 0.17, 0.10),
                  "60-74": (0.20, 0.32, 0.28, 0.20), "75+": (0.15, 0.30, 0.30, 0.25)}
PREVALENT_P = 0.60

# Background starts per patient-year by class (all within the 0.5-3% band of section 4.3).
ACUTE_PREFIX_RATES = (("J01", 0.03), ("M01A", 0.03), ("R05D", 0.03), ("N02", 0.02), ("R06A", 0.02),
                      ("A06A", 0.02), ("A02BC", 0.02), ("H02AB", 0.02), ("A03FA", 0.015),
                      ("A04AA", 0.015), ("A03BB", 0.015), ("A03AA", 0.015), ("N05CF", 0.01))
CHRONIC_BACKGROUND_RATE = 0.005

# Index drugs used for top-up starts, with a demographic eligibility rule (see DECISIONS.md).
ACUTE_TOPUP = {"metoclopramide", "haloperidol", "diclofenac"}


def acute_rate(l4: str) -> float | None:
    for prefix, rate in ACUTE_PREFIX_RATES:
        if l4.startswith(prefix):
            return rate
    return None


@dataclass
class Patient:
    idx: int
    age: int
    sex: str
    home_pharmacy: int
    prescribers: list[int]
    conditions: list[str] = field(default_factory=list)

    @property
    def age_band(self) -> str:
        return "90+" if self.age >= 90 else f"{self.age // 5 * 5}-{self.age // 5 * 5 + 4}"

    @property
    def age_group(self) -> int:
        return 0 if self.age < 40 else 1 if self.age < 60 else 2 if self.age < 75 else 3


@dataclass
class Episode:
    patient: int
    drug_id: str
    atc_l4: str
    start: int  # true start day (negative = before the simulation, i.e. prevalent use)
    prescriber: int
    days: list[int]
    origin: str  # condition | prevalent | background | topup | novacor | inject
    dose_mult: float = 1.0
    dose_up_at: int = -1  # dispense index from which the dose doubles (-1 = never)
    days_supply: int = CHRONIC_DAYS_SUPPLY
    label: tuple[str, str] | None = None  # (pair_id, index drug_id) for injected episodes


class Generator:
    def __init__(self, cat: Catalogue, n_patients: int, seed: int, years: int = 3) -> None:
        self.cat = cat
        self.n = n_patients
        self.end_day = (date(START.year + years, 1, 1) - START).days - 1
        self.launch_day = (NOVACOR_LAUNCH - START).days
        ss = np.random.SeedSequence(seed)
        streams = ss.spawn(8)
        self.rng_pop, self.rng_cond, self.rng_bg, self.rng_top, self.rng_nova, self.rng_inj, \
            self.rng_exp, self.rng_ids = (np.random.default_rng(s) for s in streams)
        self.patients: list[Patient] = []
        self.eps: dict[int, list[Episode]] = {}
        self.stats: dict[str, int] = {}

    # ---------- population ----------
    def make_population(self) -> None:
        r = self.rng_pop
        for i in range(self.n):
            if r.random() < 0.40:
                age = int(min(94, 60 + r.gamma(2.0, 6.0)))
            else:
                age = int(r.integers(18, 60))
            sex_draw = r.random()
            sex = "M" if sex_draw < 0.497 else "F" if sex_draw < 0.994 else "U"
            k = int(r.integers(1, 5))
            prescribers = sorted({int(x) for x in r.choice(N_PRESCRIBERS, size=k, replace=False)})
            self.patients.append(Patient(i, age, sex, int(r.integers(N_PHARMACIES)), prescribers))
            self.eps[i] = []

    def _prescriber(self, p: Patient, r: np.random.Generator) -> int:
        if len(p.prescribers) == 1 or r.random() < 0.6:
            return p.prescribers[0]
        return p.prescribers[int(r.integers(1, len(p.prescribers)))]

    # ---------- episodes ----------
    def _is_acute(self, drug_id: str, origin: str) -> bool:
        d = self.cat.by_id[drug_id]
        if origin == "topup":
            return d.generic_name in ACUTE_TOPUP
        if origin in ("condition", "prevalent"):
            return False
        return acute_rate(d.atc_l4) is not None

    def _dispense_days(self, first: int, acute: bool, days_supply: int, r: np.random.Generator) -> list[int]:
        days: list[int] = []
        d = first
        if acute:
            n = 1 + int(r.random() < 0.3) + int(r.random() < 0.1)
            for _ in range(n):
                if d > self.end_day:
                    break
                days.append(d)
                d += days_supply + int(r.integers(-2, 3))
            return days
        hazard = 1.0 - (1.0 - DISCONTINUE_PER_YEAR) ** (CHRONIC_DAYS_SUPPLY / 365.0)
        while d <= self.end_day:
            days.append(d)
            if r.random() < hazard:
                break
            d += CHRONIC_DAYS_SUPPLY + int(r.integers(-3, 4))
        return days

    def _overlaps(self, patient: int, drug_id: str, first: int, last: int) -> bool:
        """Reject a new episode if the same drug is dispensed in [first-365, last]."""
        lo = first - inject.LOOKBACK_DAYS
        for ep in self.eps[patient]:
            if ep.drug_id == drug_id and ep.days and ep.days[0] <= last and ep.days[-1] >= lo:
                return True
        return False

    def add_episode(self, patient: int, drug_id: str, start: int, origin: str, r: np.random.Generator,
                    prescriber: int | None = None, first_dispense: int | None = None) -> Episode | None:
        if start > self.end_day:
            return None
        d = self.cat.by_id[drug_id]
        acute = self._is_acute(drug_id, origin)
        days_supply = (7 if d.atc_l4.startswith("J01") else 10) if acute else CHRONIC_DAYS_SUPPLY
        first = start if first_dispense is None else first_dispense
        days = self._dispense_days(first, acute, days_supply, r)
        if not days or self._overlaps(patient, drug_id, days[0], days[-1]):
            return None
        p = self.patients[patient]
        dose_up = -1
        if not acute and len(days) > 2 and r.random() < 0.15:
            dose_up = int(r.integers(1, len(days)))
        ep = Episode(patient, drug_id, d.atc_l4, start, prescriber if prescriber is not None
                     else self._prescriber(p, r), days, origin,
                     dose_mult=float(r.choice([0.5, 1.0, 1.0, 1.0, 2.0])), dose_up_at=dose_up,
                     days_supply=days_supply)
        self.eps[patient].append(ep)
        return ep

    def novacor_share(self, day: int) -> float:
        if day < self.launch_day:
            return 0.0
        return NOVACOR_MAX_SHARE * min(1.0, (day - self.launch_day) / NOVACOR_RAMP_DAYS)

    def _maybe_novacor(self, drug_id: str, day: int, r: np.random.Generator) -> str:
        if self.cat.by_id[drug_id].atc_l4 in ANTIHYPERTENSIVE_L4 and r.random() < self.novacor_share(day):
            return "DNOVA"
        return drug_id

    def make_condition_episodes(self) -> None:
        r = self.rng_cond
        names = list(CONDITIONS)
        for p in self.patients:
            g = p.age_group
            weights = np.array([CONDITIONS[c][0][g] if (CONDITIONS[c][3] in (None, p.sex)) else 0.0
                                for c in names])
            k = int(r.choice(4, p=N_CONDITIONS_P[AGE_GROUPS[g]])) + 1
            k = min(k, int((weights > 0).sum()))
            chosen = r.choice(len(names), size=k, replace=False, p=weights / weights.sum())
            for ci in chosen:
                cond = names[int(ci)]
                p.conditions.append(cond)
                _, drugs, max_drugs, _ = CONDITIONS[cond]
                n_drugs = 1 if max_drugs == 1 else int(r.integers(1, max_drugs + 1))
                picks = r.choice(len(drugs), size=min(n_drugs, len(drugs)), replace=False)
                prevalent = r.random() < PREVALENT_P
                onset = int(r.integers(0, self.end_day + 1))
                for j, di in enumerate(picks):
                    drug_id = self.cat.drug(drugs[int(di)]).drug_id
                    if prevalent:
                        start = -int(r.integers(30, 2000))
                        self.add_episode(p.idx, drug_id, start, "prevalent", r,
                                         first_dispense=int(r.integers(0, 30)))
                    else:
                        start = onset if j == 0 else int(r.integers(onset, self.end_day + 1))
                        drug_id = self._maybe_novacor(drug_id, start, r)
                        self.add_episode(p.idx, drug_id, start, "condition", r)

    def make_background_episodes(self) -> None:
        r = self.rng_bg
        years = (self.end_day + 1) / 365.0
        for l4 in self.cat.classes:
            drugs = [d for d in self.cat.by_l4[l4] if not d.is_fictional]
            if not drugs:
                continue
            rate = acute_rate(l4) or CHRONIC_BACKGROUND_RATE
            n_starts = int(r.poisson(rate * years * self.n))
            pats = r.integers(0, self.n, size=n_starts)
            days = r.integers(0, self.end_day + 1, size=n_starts)
            picks = r.integers(0, len(drugs), size=n_starts)
            for pi, day, di in zip(pats, days, picks, strict=True):
                drug_id = self._maybe_novacor(drugs[int(di)].drug_id, int(day), r)
                self.add_episode(int(pi), drug_id, int(day), "background", r)

    def _eligible_for(self, name: str, p: Patient, r: np.random.Generator) -> bool:
        if name == "tamsulosin":
            return p.sex == "M" or r.random() < 0.3  # also used short-term for ureteric stones
        if name == "donepezil":
            return p.age >= 55
        return True

    def _count_incident(self, drug_ids: set[str], lo: int, hi: int) -> int:
        n = 0
        for eps in self.eps.values():
            by_drug: dict[str, list[Episode]] = {}
            for ep in eps:
                by_drug.setdefault(ep.drug_id, []).append(ep)
            for did in drug_ids & by_drug.keys():
                for ep in by_drug[did]:
                    if lo <= ep.start <= hi and inject.is_drug_incident(ep, by_drug[did]):
                        n += 1
        return n

    def top_up_index_starts(self, specs: list[inject.PairSpec]) -> dict[str, dict[str, int]]:
        """Add incident index-drug starts so the injected counts can meet the assertions."""
        r = self.rng_top
        report: dict[str, dict[str, int]] = {}
        for spec in specs:
            if spec.hidden:
                continue
            minimum = inject.scaled_minimum(MIN_KNOWN_EVENTS, self.n)
            target = inject.topup_target(minimum, spec.p)
            hi = self.end_day - spec.lag_max
            have = self._count_incident(set(spec.index_drug_ids), 0, hi)
            added = 0
            index_ids = sorted(spec.index_drug_ids)
            attempts = 0
            while have + added < target and attempts < target * 50:
                attempts += 1
                drug_id = index_ids[int(r.integers(len(index_ids)))]
                p = self.patients[int(r.integers(self.n))]
                if not self._eligible_for(self.cat.by_id[drug_id].generic_name, p, r):
                    continue
                if self.add_episode(p.idx, drug_id, int(r.integers(0, hi + 1)), "topup", r):
                    added += 1
            report[spec.pair_id] = {"target_incident_starts": target, "existing": have, "added": added}
        return report

    def top_up_novacor(self, spec: inject.PairSpec) -> dict[str, int]:
        r = self.rng_nova
        minimum = inject.scaled_minimum(MIN_HIDDEN_EVENTS, self.n)
        target = inject.topup_target(minimum, spec.p)
        hi = self.end_day - spec.lag_max
        have = self._count_incident({"DNOVA"}, self.launch_day, hi)
        hypertensive = [p.idx for p in self.patients if "hypertension" in p.conditions]
        older = [p.idx for p in self.patients if p.age >= 40]
        added = attempts = 0
        span = hi - self.launch_day
        while have + added < target and attempts < target * 50:
            attempts += 1
            pool = hypertensive if (hypertensive and r.random() < 0.8) else older
            patient = pool[int(r.integers(len(pool)))]
            day = self.launch_day + int(span * math.sqrt(r.random()))  # density rises linearly (ramp)
            if self.add_episode(patient, "DNOVA", day, "novacor", r):
                added += 1
        return {"target_incident_starts": target, "existing": have, "added": added}

    # ---------- injection ----------
    def pair_specs(self) -> list[inject.PairSpec]:
        specs = []
        for k in self.cat.known:
            specs.append(inject.PairSpec(
                k.pair_id, frozenset(self.cat.drug(n).drug_id for n in k.index_drugs),
                tuple(self.cat.drug(n).drug_id for n in k.marker_drugs), None,
                k.lag_min, k.lag_max, k.p_cascade))
        specs.append(inject.PairSpec(
            H1["pair_id"], frozenset({self.cat.drug(H1["index"]).drug_id}),
            (self.cat.drug(H1["marker"]).drug_id,), None, H1["lag_min"], H1["lag_max"], H1["p"], hidden=True))
        return specs

    def run_injection(self, specs: list[inject.PairSpec]) -> tuple[list[inject.Injection], int]:
        r = self.rng_inj

        def make(inj: inject.Injection) -> Episode:
            d = self.cat.by_id[inj.marker_drug_id]
            acute = acute_rate(d.atc_l4) is not None
            days_supply = (7 if d.atc_l4.startswith("J01") else 10) if acute else CHRONIC_DAYS_SUPPLY
            ep = Episode(inj.patient, d.drug_id, d.atc_l4, inj.day, inj.prescriber,
                         self._dispense_days(inj.day, acute, days_supply, r), "inject",
                         days_supply=days_supply, label=(inj.pair_id, inj.index_episode.drug_id))
            if inj.prescriber not in self.patients[inj.patient].prescribers:
                self.patients[inj.patient].prescribers.append(inj.prescriber)
            return ep

        planned = inject.plan_injections(
            specs, self.eps, self.end_day, r, lambda i: self.patients[i].prescribers, N_PRESCRIBERS)
        return inject.accept_injections(planned, self.eps, lambda did: self.cat.by_id[did].atc_l4, make)

    # ---------- output ----------
    def write(self, out: Path) -> dict[str, int]:
        out.mkdir(parents=True, exist_ok=True)
        r = self.rng_exp
        drug_ids = [d.drug_id for d in self.cat.drugs]
        drug_index = {d: i for i, d in enumerate(drug_ids)}
        col_day, col_sec, col_pat, col_drug = array("i"), array("i"), array("i"), array("i")
        col_ph, col_pr, col_ds, col_lab = array("i"), array("i"), array("i"), array("i")
        col_dose = array("d")
        labels: list[tuple[str, str, bool]] = []  # (pair_id, index drug_id, hidden)
        for pid, eps in self.eps.items():
            p = self.patients[pid]
            for ep in eps:
                d = self.cat.by_id[ep.drug_id]
                lab = -1
                if ep.label is not None:
                    labels.append((ep.label[0], ep.label[1], ep.label[0] == "H1"))
                    lab = len(labels) - 1
                for j, day in enumerate(ep.days):
                    col_day.append(day)
                    col_sec.append(int(r.integers(8 * 3600, 21 * 3600)))
                    col_pat.append(pid)
                    col_drug.append(drug_index[ep.drug_id])
                    col_ph.append(p.home_pharmacy if r.random() < HOME_PHARMACY_P
                                  else int(r.integers(N_PHARMACIES)))
                    col_pr.append(ep.prescriber)
                    col_ds.append(ep.days_supply)
                    mult = ep.dose_mult * (2.0 if 0 <= ep.dose_up_at <= j else 1.0)
                    col_dose.append(round(d.dose_mg * mult, 4))
                    col_lab.append(lab if j == 0 else -1)
        n = len(col_day)
        order = np.lexsort((np.frombuffer(col_pat, dtype=np.int32), np.frombuffer(col_sec, dtype=np.int32),
                            np.frombuffer(col_day, dtype=np.int32)))
        raw = self.rng_ids.bytes(16 * n)
        day_str = [(START + timedelta(days=i)).isoformat() for i in range(self.end_day + 1)]
        label_rows: list[list[str]] = []
        hidden_rows: list[list[str]] = []
        with gzip.open(out / "events.csv.gz", "wt", newline="", encoding="utf-8", compresslevel=3) as f:
            w = csv.writer(f)
            w.writerow(["event_id", "ts", "pharmacy_id", "patient_id", "prescriber_id", "age_band", "sex",
                        "drug_id", "atc_code", "atc_l4", "atc_l3", "qty", "days_supply", "dose_mg"])
            for k, i in enumerate(order.tolist()):
                eid = str(uuid.UUID(bytes=raw[16 * k:16 * k + 16], version=4))
                s = col_sec[i]
                d = self.cat.drugs[col_drug[i]]
                p = self.patients[col_pat[i]]
                w.writerow([eid, f"{day_str[col_day[i]]}T{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}",
                            f"PH{col_ph[i] + 1:03d}", f"P{col_pat[i] + 1:06d}", f"DR{col_pr[i] + 1:04d}",
                            p.age_band, p.sex, d.drug_id, d.atc_code, d.atc_l4, d.atc_l3, col_ds[i],
                            col_ds[i], col_dose[i]])
                if col_lab[i] >= 0:
                    pair_id, index_drug, hidden = labels[col_lab[i]]
                    (hidden_rows if hidden else label_rows).append([eid, "1", index_drug, pair_id])
        for name, rows in (("labels.csv", label_rows), ("labels_hidden.csv", hidden_rows)):
            with (out / name).open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["event_id", "is_cascade", "cascade_index_drug", "pair_id"])
                w.writerows(rows)
        return {"events": n, "labels": len(label_rows), "labels_hidden": len(hidden_rows)}


def verify_labels_incident(out: Path) -> int:
    """Independent check on the written files: every labelled event is a class-incident start."""
    labelled: set[str] = set()
    for name in ("labels.csv", "labels_hidden.csv"):
        with (out / name).open(newline="", encoding="utf-8") as f:
            labelled |= {r["event_id"] for r in csv.DictReader(f)}
    last_seen: dict[tuple[str, str], date] = {}
    bad = 0
    with gzip.open(out / "events.csv.gz", "rt", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["patient_id"], row["atc_l4"])
            day = date.fromisoformat(row["ts"][:10])
            if row["event_id"] in labelled:
                prev = last_seen.get(key)
                if prev is not None and (day - prev).days <= inject.LOOKBACK_DAYS:
                    bad += 1
            last_seen[key] = day
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CASCADE synthetic data generator")
    ap.add_argument("--patients", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-inject", action="store_true", help="null run: no cascades are injected")
    args = ap.parse_args(argv)
    out = args.out or get_settings().generated_dir
    t0 = time.time()
    cat = load_catalogue()
    g = Generator(cat, args.patients, args.seed, args.years)
    g.make_population()
    g.make_condition_episodes()
    g.make_background_episodes()
    specs = g.pair_specs()
    topups = g.top_up_index_starts(specs)
    topups["H1"] = g.top_up_novacor(next(s for s in specs if s.hidden))
    accepted: list[inject.Injection] = []
    rejected = 0
    if not args.no_inject:
        accepted, rejected = g.run_injection(specs)
    counts = {s.pair_id: 0 for s in specs}
    for inj in accepted:
        counts[inj.pair_id] += 1
    written = g.write(out)
    bad = verify_labels_incident(out)

    min_known = inject.scaled_minimum(MIN_KNOWN_EVENTS, args.patients)
    min_hidden = inject.scaled_minimum(MIN_HIDDEN_EVENTS, args.patients)
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "patients": args.patients, "seed": args.seed,
        "years": args.years, "start": START.isoformat(), "end_day": g.end_day, "no_inject": args.no_inject,
        "novacor_launch": NOVACOR_LAUNCH.isoformat(), **written, "injected": counts,
        "injection_rejected_not_incident": rejected, "topups": topups,
        "min_required": {"known": min_known, "hidden": min_hidden}, "labels_not_incident": bad,
        "seconds": round(time.time() - t0, 1),
    }
    (out / "generation_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"events written: {written['events']:,} -> {out}")
    print("injected events per pair:")
    for pid, c in counts.items():
        need = min_hidden if pid == "H1" else min_known
        print(f"  {pid}: {c:5d}  (required >= {need}{', skipped: --no-inject' if args.no_inject else ''})")
    failures = []
    if bad:
        failures.append(f"{bad} labelled events are not incident starts")
    if not args.no_inject:
        failures += [f"{pid} has {c} < {min_known} injected events" for pid, c in counts.items()
                     if pid != "H1" and c < min_known]
        if counts["H1"] < min_hidden:
            failures.append(f"H1 has {counts['H1']} < {min_hidden} injected events")
    if failures:
        print("GENERATOR ASSERTION FAILED: " + "; ".join(failures), file=sys.stderr)
        return 1
    print(f"assertions passed in {report['seconds']} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
