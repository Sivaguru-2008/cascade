"""Cascade injection (section 4.4).

For each known pair, every patient with an incident start of drug A (no A in the prior 365 days)
gets, with probability p_cascade, an incident start of drug B at A_start + lag. The lag is gamma
distributed and clipped to [lag_min, lag_max]. With 65% probability B comes from a different
prescriber. B must be an incident start of its ATC level-4 class (no dispense of that class in
the prior 365 days, including other injected B starts); otherwise the injection is skipped.

This module knows nothing about how episodes are expanded into dispenses: the generator passes
callbacks, so the injection logic stays small and testable.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

DIFFERENT_PRESCRIBER_P = 0.65
LOOKBACK_DAYS = 365


class EpisodeLike(Protocol):
    patient: int
    drug_id: str
    atc_l4: str
    start: int
    prescriber: int
    days: list[int]
    origin: str


@dataclass(frozen=True)
class PairSpec:
    pair_id: str
    index_drug_ids: frozenset[str]
    marker_drug_ids: tuple[str, ...]
    marker_l4: str | None  # None = depends on the chosen marker drug
    lag_min: int
    lag_max: int
    p: float
    hidden: bool = False


@dataclass
class Injection:
    pair_id: str
    patient: int
    index_episode: EpisodeLike
    marker_drug_id: str
    day: int
    prescriber: int
    hidden: bool
    episode: EpisodeLike | None = None  # filled when accepted


def draw_lag(rng: np.random.Generator, lag_min: int, lag_max: int) -> int:
    """Gamma(shape=2) lag whose mean sits one third of the way into the window, clipped."""
    mean = lag_min + (lag_max - lag_min) / 3.0
    scale = max((mean - lag_min) / 2.0, 1e-6)
    lag = lag_min + rng.gamma(2.0, scale)
    return int(min(max(round(lag), lag_min), lag_max))


def is_drug_incident(ep: EpisodeLike, same_drug_eps: Sequence[EpisodeLike]) -> bool:
    """A start inside the simulation with no dispense of the same drug in the prior 365 days."""
    if ep.start < 0 or not ep.days or ep.days[0] != ep.start:
        return False
    lo = ep.start - LOOKBACK_DAYS
    for other in same_drug_eps:
        if other is ep:
            continue
        if any(lo <= d < ep.start for d in other.days):
            return False
    return True


def class_free(day: int, l4: str, patient_eps: Sequence[EpisodeLike]) -> bool:
    """True when the patient has no dispense of class l4 in [day-365, day]."""
    lo = day - LOOKBACK_DAYS
    for ep in patient_eps:
        if ep.atc_l4 == l4 and any(lo <= d <= day for d in ep.days):
            return False
    return True


def plan_injections(
    pairs: Sequence[PairSpec],
    episodes_by_patient: dict[int, list[EpisodeLike]],
    end_day: int,
    rng: np.random.Generator,
    prescribers_of: Callable[[int], list[int]],
    n_prescribers: int,
    patient_susceptibility: Callable[[int], float] | None = None,
) -> list[Injection]:
    """Draw candidate injections (before the incidence check on B)."""
    planned: list[Injection] = []
    for patient in sorted(episodes_by_patient):
        eps = episodes_by_patient[patient]
        by_drug: dict[str, list[EpisodeLike]] = {}
        for ep in eps:
            by_drug.setdefault(ep.drug_id, []).append(ep)
        susc = 1.0 if patient_susceptibility is None else patient_susceptibility(patient)
        for spec in pairs:
            p_effective = min(max(spec.p * susc, 0.005), 0.35)
            for drug_id in sorted(spec.index_drug_ids & by_drug.keys()):
                for ep in by_drug[drug_id]:
                    if ep.origin == "inject" or not is_drug_incident(ep, by_drug[drug_id]):
                        continue
                    if rng.random() >= p_effective:
                        continue
                    day = ep.start + draw_lag(rng, spec.lag_min, spec.lag_max)
                    marker = spec.marker_drug_ids[int(rng.integers(len(spec.marker_drug_ids)))]
                    if day > end_day:
                        continue
                    prescriber = ep.prescriber
                    if rng.random() < DIFFERENT_PRESCRIBER_P:
                        others = [p for p in prescribers_of(patient) if p != ep.prescriber]
                        if others:
                            prescriber = others[int(rng.integers(len(others)))]
                        else:
                            prescriber = int(rng.integers(n_prescribers - 1))
                            if prescriber >= ep.prescriber:
                                prescriber += 1
                    planned.append(Injection(spec.pair_id, patient, ep, marker, day, prescriber, spec.hidden))
    return planned


def accept_injections(
    planned: list[Injection],
    episodes_by_patient: dict[int, list[EpisodeLike]],
    l4_of: Callable[[str], str],
    make_episode: Callable[[Injection], EpisodeLike],
) -> tuple[list[Injection], int]:
    """Accept planned injections in time order per patient if B is class-incident.

    Accepted episodes are appended to episodes_by_patient so later candidates see them.
    Returns (accepted, n_rejected).
    """
    accepted: list[Injection] = []
    rejected = 0
    planned_sorted = sorted(planned, key=lambda inj: (inj.patient, inj.day, inj.pair_id))
    for inj in planned_sorted:
        eps = episodes_by_patient.setdefault(inj.patient, [])
        if not class_free(inj.day, l4_of(inj.marker_drug_id), eps):
            rejected += 1
            continue
        inj.episode = make_episode(inj)
        eps.append(inj.episode)
        accepted.append(inj)
    return accepted, rejected


def scaled_minimum(base: int, n_patients: int, reference: int = 20000) -> int:
    """Assertion thresholds scale linearly with population (see DECISIONS.md)."""
    return max(1, math.ceil(base * n_patients / reference))


def topup_target(min_events: int, p: float, acceptance: float = 0.8) -> int:
    """Index starts needed so that Binomial(n, p*acceptance) clears min_events by ~4 SD."""
    return math.ceil((min_events + 4.0 * math.sqrt(min_events)) / (p * acceptance))
