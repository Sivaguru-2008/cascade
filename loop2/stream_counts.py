"""Streaming PSSA counts (section 6.2).

On each incident event (patient P, class X, day d): for every class Y that P started incidentally
in [d - W, d), n[(Y, X), W] += 1, and inc[X][d] += 1. Pairs are counted when the later event
arrives, so each patient pair is counted exactly once. Must agree exactly with loop2.pssa (batch).

Per-patient state (Redis `pinc:{token}`): `_f` first observed day, `l:<class>` last dispense day,
`i:<class>` first incident day.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from loop2.pssa import (
    DISPLAY_WINDOW,
    LOOKBACK_DAYS,
    MIN_N,
    WASHOUT_DAYS,
    WINDOWS,
    PairStats,
    benjamini_hochberg,
    nsr_matrix,
    sequence_stats,
    signal_mask,
)


@dataclass
class PState:
    first_seen: int | None = None
    last: dict[str, int] = field(default_factory=dict)
    inc: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_hash(cls, h: Mapping[str, str]) -> PState:
        st = cls()
        for k, v in h.items():
            if k == "_f":
                st.first_seen = int(v)
            elif k.startswith("l:"):
                st.last[k[2:]] = int(v)
            elif k.startswith("i:"):
                st.inc[k[2:]] = int(v)
        return st


@dataclass
class Update:
    fields: dict[str, str]
    increments: list[tuple[str, str, int]]  # (Y, X, window)
    incident: bool


@dataclass
class Snapshot:
    classes: list[str]
    windows: tuple[int, ...]
    stats: dict[int, PairStats]
    signal: np.ndarray
    signal_bh: np.ndarray
    tested: np.ndarray
    sim_day: int

    def index(self, a: str, b: str) -> tuple[int, int] | None:
        try:
            return self.classes.index(a), self.classes.index(b)
        except ValueError:
            return None

    def pair(self, i: int, j: int) -> dict[str, float | int | bool | None]:
        out: dict[str, float | int | bool | None] = {"signal": bool(self.signal[i, j]),
                                                     "signal_bh": bool(self.signal_bh[i, j])}
        for W in self.windows:
            s = self.stats[W]
            out[f"n_ab_{W}"] = int(s.n_ab[i, j])
            out[f"n_ba_{W}"] = int(s.n_ba[i, j])
            out[f"nsr_{W}"] = _num(s.nsr[i, j])
            out[f"asr_{W}"] = _num(s.asr[i, j])
            out[f"lo_{W}"] = _num(s.lo[i, j])
            out[f"hi_{W}"] = _num(s.hi[i, j])
            out[f"p_{W}"] = _num(s.p_value[i, j])
        return out

    def asr_lookup(self, window: int = DISPLAY_WINDOW) -> dict[tuple[str, str], tuple[float, float, float, int]]:
        """(A, B) -> (aSR, lower, upper, n) for every pair with data; the Loop 1 live feature."""
        s = self.stats[window]
        out: dict[tuple[str, str], tuple[float, float, float, int]] = {}
        for i, j in zip(*np.nonzero(s.n > 0), strict=True):
            if i == j:
                continue
            asr = s.asr[i, j]
            out[(self.classes[i], self.classes[j])] = (
                float(asr) if np.isfinite(asr) else 1.0, float(s.lo[i, j]), float(s.hi[i, j]), int(s.n[i, j]))
        return out


def _num(x: float) -> float | None:
    return None if not np.isfinite(x) else round(float(x), 6)


class StreamingPSSA:
    def __init__(self, classes: Sequence[str] = (), windows: Sequence[int] = WINDOWS, horizon: int = 1461) -> None:
        self.windows = tuple(windows)
        self.classes: list[str] = []
        self.idx: dict[str, int] = {}
        self.counts = np.zeros((len(self.windows), 0, 0), dtype=np.int64)
        self.inc = np.zeros((0, horizon), dtype=np.float64)
        self.max_day = 0  # last day with an incident start (incidence horizon)
        self.last_day = 0  # last event day seen (simulated "now")
        self.n_events = 0
        self.n_incident = 0
        for c in classes:
            self._class(c)

    # ---------- growth ----------
    def _class(self, cls: str) -> int:
        i = self.idx.get(cls)
        if i is not None:
            return i
        i = len(self.classes)
        self.classes.append(cls)
        self.idx[cls] = i
        n = len(self.classes)
        counts = np.zeros((len(self.windows), n, n), dtype=np.int64)
        counts[:, :n - 1, :n - 1] = self.counts
        self.counts = counts
        self.inc = np.vstack([self.inc, np.zeros((1, self.inc.shape[1]))])
        return i

    def _ensure_day(self, day: int) -> None:
        if day >= self.inc.shape[1]:
            extra = max(day + 1 - self.inc.shape[1], self.inc.shape[1])
            self.inc = np.hstack([self.inc, np.zeros((self.inc.shape[0], extra))])

    # ---------- per event ----------
    def process(self, st: PState, cls: str, day: int) -> Update:
        """Update patient state and counts for one dispense. Mutates st."""
        self.n_events += 1
        if day > self.last_day:
            self.last_day = day
        fields: dict[str, str] = {}
        if st.first_seen is None:
            st.first_seen = day
            fields["_f"] = str(day)
        prev = st.last.get(cls)
        incident = (day - st.first_seen >= WASHOUT_DAYS) and (prev is None or day - prev > LOOKBACK_DAYS)
        st.last[cls] = day
        fields[f"l:{cls}"] = str(day)
        increments: list[tuple[str, str, int]] = []
        counted = incident and cls not in st.inc
        if counted:
            self.n_incident += 1
            x = self._class(cls)
            wmax = max(self.windows)
            for other, d_other in st.inc.items():
                delta = day - d_other
                if 1 <= delta <= wmax:
                    y = self._class(other)
                    for w, W in enumerate(self.windows):
                        if delta <= W:
                            self.counts[w, y, x] += 1
                            increments.append((other, cls, W))
            st.inc[cls] = day
            fields[f"i:{cls}"] = str(day)
            self._ensure_day(day)
            self.inc[x, day] += 1
            self.max_day = max(self.max_day, day)
        return Update(fields, increments, counted)

    # ---------- persistence helpers ----------
    def count_dict(self) -> dict[tuple[str, str], list[int]]:
        out: dict[tuple[str, str], list[int]] = {}
        for i, j in zip(*np.nonzero(self.counts.sum(axis=0)), strict=True):
            out[(self.classes[i], self.classes[j])] = [int(self.counts[w, i, j]) for w in range(len(self.windows))]
        return out

    def load_counts(self, counts: Iterable[tuple[str, str, int, int]]) -> None:
        """Load (A, B, window, n) rows, e.g. from Redis pair:{A}:{B} hashes after a restart."""
        for a, b, W, n in counts:
            i, j = self._class(a), self._class(b)
            self.counts[self.windows.index(W), i, j] = n

    def load_incidence(self, classes: list[str], inc: np.ndarray) -> None:
        for k, c in enumerate(classes):
            i = self._class(c)
            self._ensure_day(inc.shape[1] - 1)
            self.inc[i, :inc.shape[1]] = inc[k]
        nz = np.nonzero(self.inc.sum(axis=0))[0]
        self.max_day = int(nz.max()) if nz.size else 0

    # ---------- statistics ----------
    def snapshot(self, q: float = 0.05) -> Snapshot:
        horizon = self.max_day + 1
        inc = self.inc[:, :horizon]
        stats: dict[int, PairStats] = {}
        for w, W in enumerate(self.windows):
            n_ab = self.counts[w].astype(np.float64)
            stats[W] = sequence_stats(n_ab, n_ab.T, nsr_matrix(inc, W))
        C = len(self.classes)
        sig = signal_mask(stats) & ~np.eye(C, dtype=bool)
        tested = (stats[DISPLAY_WINDOW].n >= MIN_N) & ~np.eye(C, dtype=bool)
        bh = np.zeros_like(sig)
        if tested.any():
            bh[tested] = benjamini_hochberg(stats[DISPLAY_WINDOW].p_value[tested], q)
        sim_day = max(self.max_day, self.last_day)
        return Snapshot(list(self.classes), self.windows, stats, sig, sig & bh, tested, sim_day)
