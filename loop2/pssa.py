"""Prescription Sequence Symmetry Analysis: pure functions (section 6.1).

Unit: ordered ATC level-4 class pairs (A, B), A != B.
Incident start: first dispense of a class with no dispense of that class in the prior 365 days,
after a 365-day washout from the patient's first observed dispense. Only the *first* incident
start of each class per patient enters the analysis (classic PSSA).

n_AB(W): patients whose incident A precedes incident B by 1..W days (same-day ties ignored).
nSR(W) = sum_t a(t) * sum_{u=t+1}^{t+W} b(u) / sum_t a(t) * sum_{u=t-W}^{t-1} b(u)
aSR = cSR / nSR, with a Clopper-Pearson 95% CI on p = n_AB / (n_AB + n_BA) mapped to r = p/(1-p).

Run `python -m loop2.pssa` for a batch analysis of data/generated/events.csv.gz.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np

WASHOUT_DAYS = 365
LOOKBACK_DAYS = 365
WINDOWS: tuple[int, ...] = (30, 90, 180)
DISPLAY_WINDOW = 90
MIN_N = 10
ALPHA = 0.05
ASR_CAP = 999.0  # stored instead of +inf (n_BA = 0)

try:  # primary: SciPy
    from scipy.stats import beta as _beta
    from scipy.stats import binom as _binom

    def _beta_ppf(q: float, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        return _beta.ppf(q, a, b)

    def _binom_sf(k: np.ndarray, n: np.ndarray, p: np.ndarray) -> np.ndarray:
        return _binom.sf(k, n, p)

    STATS_BACKEND = "scipy"
except ImportError:  # pragma: no cover - fallback listed in section 2
    from statsmodels.stats.proportion import proportion_confint

    def _beta_ppf(q: float, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        raise RuntimeError("use proportion_confint path")

    def _binom_sf(k: np.ndarray, n: np.ndarray, p: np.ndarray) -> np.ndarray:
        from statsmodels.stats.proportion import binom_test

        return np.array([binom_test(int(x) + 1, int(m), float(q), alternative="larger") if m else 1.0
                         for x, m, q in zip(np.ravel(k), np.ravel(n), np.ravel(p), strict=True)]).reshape(np.shape(k))

    STATS_BACKEND = "statsmodels"


# ----------------------------------------------------------------------------- incidence
def first_incident_starts(dispenses: Sequence[tuple[int, str]]) -> dict[str, int]:
    """Batch definition for one patient. dispenses = [(day, atc_l4), ...] in time order."""
    if not dispenses:
        return {}
    first_seen = dispenses[0][0]
    last: dict[str, int] = {}
    first_inc: dict[str, int] = {}
    for day, cls in dispenses:
        prev = last.get(cls)
        incident = day - first_seen >= WASHOUT_DAYS and (prev is None or day - prev > LOOKBACK_DAYS)
        if incident and cls not in first_inc:
            first_inc[cls] = day
        last[cls] = day
    return first_inc


def pair_counts(first_inc_by_patient: Iterable[Mapping[str, int]],
                windows: Sequence[int] = WINDOWS) -> dict[tuple[str, str], list[int]]:
    """n[(A, B)][w] = patients with incident A then incident B within 1..windows[w] days."""
    counts: dict[tuple[str, str], list[int]] = {}
    wmax = max(windows)
    for inc in first_inc_by_patient:
        items = sorted(inc.items(), key=lambda kv: kv[1])
        for i, (a, da) in enumerate(items):
            for b, db in items[i + 1:]:
                delta = db - da
                if delta > wmax:
                    break
                if delta < 1:
                    continue
                row = counts.setdefault((a, b), [0] * len(windows))
                for w, W in enumerate(windows):
                    if delta <= W:
                        row[w] += 1
    return counts


def daily_incidence(first_inc_by_patient: Iterable[Mapping[str, int]], class_index: Mapping[str, int],
                    horizon: int) -> np.ndarray:
    inc = np.zeros((len(class_index), horizon), dtype=np.float64)
    for p in first_inc_by_patient:
        for cls, day in p.items():
            inc[class_index[cls], day] += 1
    return inc


# ----------------------------------------------------------------------------- statistics
def nsr_matrix(inc: np.ndarray, window: int) -> np.ndarray:
    """Null-effect sequence ratio for every ordered class pair, via cumulative sums (O(1) windows).

    inc: (C, T) daily incidence. Returns (C, C) with nsr[a, b]; 1.0 where undefined.
    """
    C, T = inc.shape
    cs = np.concatenate([np.zeros((C, 1)), np.cumsum(inc, axis=1)], axis=1)  # cs[:, k] = sum_{u<k}
    t = np.arange(T)
    fwd = cs[:, np.minimum(t + window, T - 1) + 1] - cs[:, t + 1]  # sum_{u=t+1}^{t+W}
    bwd = cs[:, t] - cs[:, np.maximum(t - window, 0)]  # sum_{u=t-W}^{t-1}
    num = inc @ fwd.T
    den = inc @ bwd.T
    with np.errstate(divide="ignore", invalid="ignore"):
        nsr = np.where((num > 0) & (den > 0), num / den, 1.0)
    return nsr


def nsr_pair(a: np.ndarray, b: np.ndarray, window: int) -> float:
    """Direct (slow) definition for one pair; used by tests to check nsr_matrix."""
    T = len(a)
    num = sum(a[t] * b[t + 1:min(T, t + window + 1)].sum() for t in range(T))
    den = sum(a[t] * b[max(0, t - window):t].sum() for t in range(T))
    return float(num / den) if num > 0 and den > 0 else 1.0


@dataclass
class PairStats:
    n_ab: np.ndarray
    n_ba: np.ndarray
    nsr: np.ndarray
    csr: np.ndarray
    asr: np.ndarray
    lo: np.ndarray
    hi: np.ndarray
    p_value: np.ndarray

    @property
    def n(self) -> np.ndarray:
        return self.n_ab + self.n_ba


def sequence_stats(n_ab: np.ndarray, n_ba: np.ndarray, nsr: np.ndarray, alpha: float = ALPHA) -> PairStats:
    """Vectorised cSR, aSR, Clopper-Pearson CI and one-sided binomial p-value (H1: aSR > 1)."""
    n_ab = np.asarray(n_ab, dtype=np.float64)
    n_ba = np.asarray(n_ba, dtype=np.float64)
    nsr = np.asarray(nsr, dtype=np.float64)
    n = n_ab + n_ba
    with np.errstate(divide="ignore", invalid="ignore"):
        csr = np.where(n_ba > 0, n_ab / np.where(n_ba > 0, n_ba, 1), np.where(n_ab > 0, np.inf, np.nan))
        if STATS_BACKEND == "scipy":
            p_lo = np.where(n_ab > 0, _beta_ppf(alpha / 2, np.maximum(n_ab, 1e-12), n_ba + 1), 0.0)
            p_hi = np.where(n_ba > 0, _beta_ppf(1 - alpha / 2, n_ab + 1, np.maximum(n_ba, 1e-12)), 1.0)
        else:  # pragma: no cover
            lo_hi = [proportion_confint(int(x), int(m), alpha=alpha, method="beta") if m else (0.0, 1.0)
                     for x, m in zip(np.ravel(n_ab), np.ravel(n), strict=True)]
            p_lo = np.array([v[0] for v in lo_hi]).reshape(n.shape)
            p_hi = np.array([v[1] for v in lo_hi]).reshape(n.shape)
        r_lo = p_lo / (1 - p_lo)
        r_hi = np.where(p_hi < 1, p_hi / (1 - p_hi), np.inf)
        asr = csr / nsr
        lo = r_lo / nsr
        hi = r_hi / nsr
        p0 = nsr / (1 + nsr)
        p_value = np.where(n > 0, _binom_sf(n_ab - 1, n, p0), 1.0)
    cap = lambda x: np.where(np.isnan(x), np.nan, np.minimum(x, ASR_CAP))  # noqa: E731
    return PairStats(n_ab, n_ba, nsr, cap(csr), cap(asr), cap(lo), cap(hi), np.asarray(p_value, dtype=float))


def signal_mask(stats_by_window: Mapping[int, PairStats], min_n: int = MIN_N) -> np.ndarray:
    """n >= 10 and lower CI > 1 at W=90, and lower CI > 1 at W=30 or W=180."""
    s90 = stats_by_window[90]
    other = np.zeros_like(s90.lo, dtype=bool)
    for w in (30, 180):
        if w in stats_by_window:
            other |= np.nan_to_num(stats_by_window[w].lo, nan=0.0) > 1
    return (s90.n >= min_n) & (np.nan_to_num(s90.lo, nan=0.0) > 1) & other


def benjamini_hochberg(p_values: np.ndarray, q: float = 0.05) -> np.ndarray:
    """Boolean rejection mask (BH step-up) over the given p-values."""
    p = np.asarray(p_values, dtype=float)
    m = p.size
    if m == 0:
        return np.zeros(0, dtype=bool)
    order = np.argsort(p)
    ranked = p[order]
    below = ranked <= q * (np.arange(1, m + 1) / m)
    reject = np.zeros(m, dtype=bool)
    if below.any():
        k = np.max(np.nonzero(below)[0])
        reject[order[:k + 1]] = True
    return reject


def classify(a_l4: str, b_l4: str, known_pairs: Mapping[tuple[str, str], object],
             faers_pairs: set[tuple[str, str]]) -> str:
    if (a_l4, b_l4) in known_pairs:
        return "known"
    if (a_l4, b_l4) in faers_pairs:
        return "corroborated"
    return "new"


def faers_supported_pairs(faers: Mapping) -> set[tuple[str, str]]:
    return {(p["index_atc_l4"], p["marker_atc_l4"]) for p in faers.get("pairs", [])
            if int(p.get("count", 0) or 0) > 0 and p.get("index_atc_l4") and p.get("marker_atc_l4")}


# ----------------------------------------------------------------------------- batch analysis
@dataclass
class BatchResult:
    classes: list[str]
    counts: dict[int, np.ndarray]  # window -> (C, C) n_AB matrix
    stats: dict[int, PairStats]
    signal: np.ndarray
    signal_bh: np.ndarray
    tested: np.ndarray


def analyse(first_inc_by_patient: list[Mapping[str, int]], horizon: int, classes: Sequence[str] | None = None,
            windows: Sequence[int] = WINDOWS, q: float = 0.05) -> BatchResult:
    classes = sorted(set(classes or []) | {c for p in first_inc_by_patient for c in p})
    idx = {c: i for i, c in enumerate(classes)}
    inc = daily_incidence(first_inc_by_patient, idx, horizon)
    raw = pair_counts(first_inc_by_patient, windows)
    C = len(classes)
    counts = {W: np.zeros((C, C)) for W in windows}
    for (a, b), row in raw.items():
        for w, W in enumerate(windows):
            counts[W][idx[a], idx[b]] = row[w]
    stats = {W: sequence_stats(counts[W], counts[W].T, nsr_matrix(inc, W)) for W in windows}
    sig = signal_mask(stats)
    np.fill_diagonal(sig, False)
    tested = (stats[DISPLAY_WINDOW].n >= MIN_N) & ~np.eye(C, dtype=bool)
    bh = np.zeros_like(sig)
    bh[tested] = benjamini_hochberg(stats[DISPLAY_WINDOW].p_value[tested], q)
    return BatchResult(classes, counts, stats, sig, sig & bh, tested)


def load_first_incidents(path: Path, epoch: date = date(2023, 1, 1),
                         until: str | None = None) -> tuple[list[dict[str, int]], int]:
    """Read raw events and return per-patient first incident starts (+ horizon in days)."""
    per_patient: dict[str, list[tuple[int, str]]] = {}
    max_day = 0
    day_cache: dict[str, int] = {}
    with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = row["ts"]
            if until is not None and ts >= until:
                break
            d10 = ts[:10]
            day = day_cache.get(d10)
            if day is None:
                day = day_cache[d10] = (date.fromisoformat(d10) - epoch).days
            per_patient.setdefault(row["patient_id"], []).append((day, row["atc_l4"]))
            max_day = max(max_day, day)
    return [first_incident_starts(v) for v in per_patient.values()], max_day + 1


def main() -> None:
    from common.config import get_settings
    from datagen.catalogue import load_catalogue, load_faers

    ap = argparse.ArgumentParser(description="Batch PSSA over generated events")
    ap.add_argument("--events", type=Path, default=get_settings().generated_dir / "events.csv.gz")
    ap.add_argument("--until", default=None, help="only events with ts < this ISO date")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()
    cat = load_catalogue()
    firsts, horizon = load_first_incidents(args.events, until=args.until)
    res = analyse(firsts, horizon, cat.classes)
    faers = faers_supported_pairs(load_faers())
    s90 = res.stats[DISPLAY_WINDOW]
    rows = []
    for i, j in zip(*np.nonzero(res.signal), strict=True):
        a, b = res.classes[i], res.classes[j]
        rows.append({"a": a, "b": b, "status": classify(a, b, cat.known_class_pairs, faers),
                     "n_ab": int(s90.n_ab[i, j]), "n_ba": int(s90.n_ba[i, j]), "asr": round(float(s90.asr[i, j]), 2),
                     "lo": round(float(s90.lo[i, j]), 2), "hi": round(float(s90.hi[i, j]), 2),
                     "bh": bool(res.signal_bh[i, j])})
    rows.sort(key=lambda r: -r["lo"])
    print(f"patients={len(firsts)} classes={len(res.classes)} tested_pairs={int(res.tested.sum())} "
          f"signals={len(rows)} signals_bh={int(res.signal_bh.sum())}")
    for r in rows[:args.top]:
        print(json.dumps(r))
    known_ids = {}
    for (a, b), k in cat.known_class_pairs.items():
        i, j = res.classes.index(a), res.classes.index(b)
        known_ids.setdefault(k.pair_id, []).append(bool(res.signal[i, j]))
    print("known pairs flagged:", {k: any(v) for k, v in sorted(known_ids.items())})
    i, j = res.classes.index("C09XX"), res.classes.index("A02BC")
    print("H1 (C09XX->A02BC) flagged:", bool(res.signal[i, j]))


if __name__ == "__main__":
    main()
