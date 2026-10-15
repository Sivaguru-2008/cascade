"""Time-correct features: no information from the future may reach a training row."""

import csv
import gzip
from datetime import date

import numpy as np
import pytest

from datagen import generate
from datagen.catalogue import load_catalogue
from loop1.candidates import Ev
from loop1.features import FeatureSpace, asr_transform, read_labels, replay_rows
from loop2 import pssa
from loop2.stream_counts import PState, StreamingPSSA

REFRESH = 400


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    out = tmp_path_factory.mktemp("feat")
    generate.main(["--patients", "500", "--seed", "21", "--out", str(out)])
    with gzip.open(out / "events.csv.gz", "rt") as f:
        events = list(csv.DictReader(f))
    return out, events


def _rows(events, out):
    return replay_rows(events, load_catalogue(), read_labels(out / "labels.csv"), refresh_every=REFRESH)


def test_prefix_rows_identical_to_full_rows(data):
    out, events = data
    cut = len(events) // 2
    full = _rows(events, out)
    prefix = _rows(events[:cut], out)
    cut_ids = {e["event_id"] for e in events[:cut]}
    full_prefix = [r for r in full if r.event_id in cut_ids]
    assert len(prefix) == len(full_prefix) > 0
    for a, b in zip(prefix, full_prefix, strict=True):
        assert (a.event_id, a.a_drug, a.label) == (b.event_id, b.a_drug, b.label)
        assert a.features == b.features, "a feature changed when future events were appended"


def test_asr_feature_equals_batch_pssa_on_past_events_only(data):
    out, events = data
    rows = [r for r in _rows(events, out) if r.features["_aSR_n"] > 0]
    assert rows
    rng = np.random.default_rng(0)
    sample = [rows[i] for i in rng.choice(len(rows), size=min(12, len(rows)), replace=False)]
    def day(ts):
        return (date.fromisoformat(ts[:10]) - date(2023, 1, 1)).days

    for r in sample:
        past = events[:r.asof_events]  # what Loop 2 had seen at its last refresh before this row
        assert all(e["ts"] <= r.ts for e in past)
        per_patient = {}
        for e in past:
            per_patient.setdefault(e["patient_id"], []).append((day(e["ts"]), e["atc_l4"]))
        firsts = [pssa.first_incident_starts(v) for v in per_patient.values()]
        horizon = max(d for v in per_patient.values() for d, _ in v) + 1
        res = pssa.analyse(firsts, horizon)
        i, j = res.classes.index(r.a_l4), res.classes.index(r.b_l4)
        s = res.stats[90]
        assert r.features["_aSR_n"] == int(s.n[i, j])
        assert r.features["pair_aSR_lower_ci"] == pytest.approx(float(s.lo[i, j]), rel=1e-9)
        expected_asr = float(s.asr[i, j]) if np.isfinite(s.asr[i, j]) else 1.0
        assert r.features["pair_aSR_live"] == pytest.approx(expected_asr, rel=1e-9)


def test_features_differ_from_end_of_period_statistics(data):
    """Guards against a silent switch to end-of-period stats (which would leak)."""
    out, events = data
    rows = _rows(events, out)
    eng, states = StreamingPSSA(), {}
    for e in events:
        ev = Ev.from_raw(e, date(2023, 1, 1))
        eng.process(states.setdefault(ev.patient, PState()), ev.atc_l4, ev.day)
    end = eng.snapshot().asr_lookup()
    differs = sum(1 for r in rows if (r.a_l4, r.b_l4) in end
                  and abs(end[(r.a_l4, r.b_l4)][1] - r.features["pair_aSR_lower_ci"]) > 1e-9)
    assert differs > 0


def test_feature_space_vector():
    space = FeatureSpace.build(load_catalogue(), drop=("pair_in_known_list",))
    assert "pair_in_known_list" not in space.columns
    f = {n: 0 for n in space.numeric} | {"pair_aSR_live": 3.0, "pair_aSR_lower_ci": 0.0, "A_atc_l3": "C08C",
                                          "B_atc_l3": "ZZZZ", "sex": "F"}
    x = space.vector(f)
    assert x[space.columns.index("pair_aSR_live")] == pytest.approx(asr_transform(3.0))
    assert x[space.columns.index("A_atc_l3=C08C")] == 1.0
    assert not any(x[i] for i, c in enumerate(space.columns) if c.startswith("B_atc_l3="))  # unseen category
    assert x[space.columns.index("sex=F")] == 1.0
