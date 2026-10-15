import csv
import gzip
from datetime import date

import numpy as np
import pytest
from scipy.stats import beta

from datagen import generate
from loop2 import pssa
from loop2.stream_counts import PState, StreamingPSSA

T = 1100


def _result(firsts):
    return pssa.analyse(firsts, T, ["A", "B"])


def _pair(res, a="A", b="B", W=90):
    i, j = res.classes.index(a), res.classes.index(b)
    s = res.stats[W]
    return s, i, j, bool(res.signal[i, j])


def _independent(rng, n, b_sampler=None):
    out = []
    for _ in range(n):
        p = {}
        if rng.random() < 0.5:
            p["A"] = int(rng.integers(0, T))
        if rng.random() < 0.5:
            p["B"] = int(b_sampler() if b_sampler else rng.integers(0, T))
        out.append(p)
    return out


def test_symmetric_random_data_no_signal():
    rng = np.random.default_rng(1)
    res = _result(_independent(rng, 6000))
    s, i, j, sig = _pair(res)
    assert s.n[i, j] > 100
    assert 0.8 < s.asr[i, j] < 1.25
    assert s.lo[i, j] < 1 < s.hi[i, j]
    assert not sig and not res.signal[j, i]


def test_injected_asymmetry_is_signal():
    rng = np.random.default_rng(2)
    firsts = _independent(rng, 6000)
    for _ in range(300):
        d = int(rng.integers(0, T - 60))
        firsts.append({"A": d, "B": d + int(rng.integers(1, 30))})
    res = _result(firsts)
    s, i, j, sig = _pair(res)
    assert sig and s.lo[i, j] > 1 and s.asr[i, j] > 2
    assert not res.signal[j, i]


def test_trend_alone_is_corrected_by_nsr():
    rng = np.random.default_rng(3)
    # every patient starts A (flat incidence) and B (incidence rising over time), independently
    firsts = [{"A": int(rng.integers(0, T)), "B": int(T * rng.random() ** 0.25)} for _ in range(40000)]
    res = _result(firsts)
    s, i, j, sig = _pair(res)
    assert s.n[i, j] > 4000
    assert s.csr[i, j] > 1.1 and s.nsr[i, j] > 1.1  # crude ratio is inflated by the trend ...
    assert abs(np.log(s.asr[i, j])) < 0.08  # ... and the null-effect ratio removes it
    assert not sig


def test_nsr_matrix_matches_definition():
    rng = np.random.default_rng(4)
    inc = rng.poisson(0.4, size=(3, 200)).astype(float)
    for W in (5, 30, 90):
        m = pssa.nsr_matrix(inc, W)
        for a in range(3):
            for b in range(3):
                assert m[a, b] == pytest.approx(pssa.nsr_pair(inc[a], inc[b], W))


def test_clopper_pearson_and_ratio_mapping():
    s = pssa.sequence_stats(np.array([30.0]), np.array([10.0]), np.array([1.5]))
    p_lo, p_hi = beta.ppf(0.025, 30, 11), beta.ppf(0.975, 31, 10)
    assert s.asr[0] == pytest.approx(2.0)
    assert s.lo[0] == pytest.approx(p_lo / (1 - p_lo) / 1.5)
    assert s.hi[0] == pytest.approx(p_hi / (1 - p_hi) / 1.5)
    zero = pssa.sequence_stats(np.array([12.0]), np.array([0.0]), np.array([1.0]))
    assert zero.asr[0] == pssa.ASR_CAP and zero.hi[0] == pssa.ASR_CAP and zero.lo[0] > 1


def test_signal_needs_min_n_and_second_window():
    mk = lambda nab, nba: pssa.sequence_stats(np.array([[0, nab], [nba, 0]], float),  # noqa: E731
                                              np.array([[0, nba], [nab, 0]], float), np.ones((2, 2)))
    strong, weak = mk(40.0, 5.0), mk(7.0, 0.0)
    assert pssa.signal_mask({30: strong, 90: strong, 180: strong})[0, 1]
    assert not pssa.signal_mask({30: weak, 90: weak, 180: weak})[0, 1]  # n < 10
    assert not pssa.signal_mask({30: mk(6.0, 6.0), 90: strong, 180: mk(6.0, 6.0)})[0, 1]


def test_benjamini_hochberg():
    p = np.array([0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216])
    assert pssa.benjamini_hochberg(p, 0.05).tolist() == [True, True] + [False] * 8


def _stream(per_patient):
    eng = StreamingPSSA()
    events = sorted((d, k, pid, c) for pid, seq in per_patient.items() for k, (d, c) in enumerate(seq))
    states = {}
    for d, _, pid, c in events:
        eng.process(states.setdefault(pid, PState()), c, d)
    return eng


def test_batch_and_streaming_identical_random():
    rng = np.random.default_rng(5)
    classes = [f"C{i}" for i in range(8)]
    per_patient = {}
    for pid in range(1500):
        days = np.sort(rng.integers(0, T, size=int(rng.integers(1, 40))))
        per_patient[pid] = [(int(d), classes[int(rng.integers(len(classes)))]) for d in days]
    firsts = [pssa.first_incident_starts(seq) for seq in per_patient.values()]
    batch = pssa.pair_counts(firsts)
    eng = _stream(per_patient)
    assert eng.count_dict() == batch
    idx = {c: i for i, c in enumerate(eng.classes)}
    assert np.array_equal(pssa.daily_incidence(firsts, idx, eng.inc.shape[1]), eng.inc)


def test_batch_and_streaming_identical_on_generated_data(tmp_path):
    generate.main(["--patients", "400", "--seed", "9", "--out", str(tmp_path)])
    per_patient = {}
    with gzip.open(tmp_path / "events.csv.gz", "rt") as f:
        for r in csv.DictReader(f):
            day = (date.fromisoformat(r["ts"][:10]) - date(2023, 1, 1)).days
            per_patient.setdefault(r["patient_id"], []).append((day, r["atc_l4"]))
    firsts = [pssa.first_incident_starts(seq) for seq in per_patient.values()]
    eng = _stream(per_patient)
    assert eng.count_dict() == pssa.pair_counts(firsts)
    batch = pssa.analyse(firsts, eng.max_day + 1, eng.classes)
    snap = eng.snapshot()
    for W in pssa.WINDOWS:
        order = [batch.classes.index(c) for c in snap.classes]
        b_asr = batch.stats[W].asr[np.ix_(order, order)]
        assert np.allclose(np.nan_to_num(b_asr), np.nan_to_num(snap.stats[W].asr))


def test_washout_and_lookback():
    # first seen day 0; class X at day 100 (washout) and again at day 500 (still within 365 of 100? no: 400 > 365)
    seq = [(0, "Y"), (100, "X"), (500, "X"), (600, "Z"), (650, "Z")]
    assert pssa.first_incident_starts(seq) == {"X": 500, "Z": 600}
    seq2 = [(0, "Y"), (300, "X"), (400, "X")]
    assert pssa.first_incident_starts(seq2) == {}
