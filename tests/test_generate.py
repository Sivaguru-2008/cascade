import csv
import gzip
import json

from datagen import generate, inject
from datagen.catalogue import load_catalogue


def _run(tmp_path, *extra):
    assert generate.main(["--patients", "300", "--seed", "3", "--out", str(tmp_path), *extra]) == 0
    return json.loads((tmp_path / "generation_report.json").read_text())


def test_generator_labels_are_incident_and_hidden_is_separate(tmp_path):
    report = _run(tmp_path)
    assert report["labels_not_incident"] == 0
    with (tmp_path / "labels.csv").open() as f:
        known = list(csv.DictReader(f))
    with (tmp_path / "labels_hidden.csv").open() as f:
        hidden = list(csv.DictReader(f))
    assert known and hidden
    assert {r["pair_id"] for r in known} <= {f"K{i}" for i in range(1, 10)}
    assert {r["pair_id"] for r in hidden} == {"H1"}
    assert all(r["cascade_index_drug"] == "DNOVA" for r in hidden)


def test_events_sorted_and_carry_no_labels(tmp_path):
    _run(tmp_path)
    with gzip.open(tmp_path / "events.csv.gz", "rt") as f:
        reader = csv.DictReader(f)
        assert "is_cascade" not in reader.fieldnames and "pair_id" not in reader.fieldnames
        ts = [r["ts"] for r in reader]
    assert ts == sorted(ts)


def test_novacor_never_before_launch(tmp_path):
    _run(tmp_path)
    with gzip.open(tmp_path / "events.csv.gz", "rt") as f:
        nova = [r["ts"] for r in csv.DictReader(f) if r["drug_id"] == "DNOVA"]
    assert nova and min(nova) >= "2025-07-01"


def test_no_inject_null_run(tmp_path):
    report = _run(tmp_path, "--no-inject")
    assert report["labels"] == 0 and report["labels_hidden"] == 0


def test_seed_reproducible(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for out in (a, b):
        generate.main(["--patients", "150", "--seed", "11", "--out", str(out)])
    assert gzip.open(a / "events.csv.gz").read() == gzip.open(b / "events.csv.gz").read()


def test_known_list_excludes_novacor():
    cat = load_catalogue()
    assert all("Novacor" not in k.index_drugs + k.marker_drugs for k in cat.known)
    assert all(len(k.source) > 5 and not k.source.startswith("VERIFY:") for k in cat.known)


def test_scaled_minimum_and_lag():
    import numpy as np
    assert inject.scaled_minimum(150, 20000) == 150 and inject.scaled_minimum(150, 2000) == 15
    rng = np.random.default_rng(0)
    lags = [inject.draw_lag(rng, 7, 90) for _ in range(2000)]
    assert min(lags) >= 7 and max(lags) <= 90
