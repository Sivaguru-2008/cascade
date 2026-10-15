"""End-to-end (single-process mode): replay ~2,000 events -> alerts with reasons + a signals table."""

import asyncio
import base64
import csv
import gzip
import os
from collections import Counter

import pytest

from common.bus import STREAM_ALERTS, STREAM_DISPENSE, MemoryBus
from common.config import reload_settings
from common.crypto import Cipher
from common.db import Database


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    from datagen import generate

    root = tmp_path_factory.mktemp("e2e")
    gen, models = root / "generated", root / "model"
    assert generate.main(["--patients", "1500", "--seed", "13", "--out", str(gen)]) == 0
    env = {"CASCADE_GENERATED_DIR": str(gen), "CASCADE_MODEL_DIR": str(models), "CASCADE_MODE": "single",
           "CASCADE_DB": "sqlite", "SQLITE_PATH": str(root / "db.sqlite3"), "CASCADE_STATE_DIR": str(root / "state"),
           "DATA_KEY": base64.b64encode(os.urandom(32)).decode(), "LOOP2_RECOMPUTE_EVENTS": "500"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    reload_settings()
    from datagen.catalogue import load_catalogue
    from loop1 import features
    from loop1.train import model_dir_for, train_variant

    rows = features.build_dataset()
    train_variant(rows, "full", model_dir_for("full"), "xgboost", load_catalogue())
    yield root, gen
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    reload_settings()


def _select_events(gen, target=2000):
    """Full histories (up to 2024-12-31) of patients with a labelled cascade in 2024, ~target events."""
    with (gen / "labels.csv").open() as f:
        labelled = {r["event_id"]: r["pair_id"] for r in csv.DictReader(f)}
    with gzip.open(gen / "events.csv.gz", "rt") as f:
        rows = [r for r in csv.DictReader(f) if r["ts"] < "2025-01-01"]
    cascade_patients = [r["patient_id"] for r in rows if r["event_id"] in labelled and r["ts"] >= "2024-02-01"]
    by_patient = Counter(r["patient_id"] for r in rows)
    chosen, total = set(), 0
    for pid in dict.fromkeys(cascade_patients):
        if total + by_patient[pid] > target:
            continue
        chosen.add(pid)
        total += by_patient[pid]
    return [r for r in rows if r["patient_id"] in chosen]


def test_replay_produces_alerts_and_signals(trained):
    root, gen = trained
    events = _select_events(gen)
    assert 1500 <= len(events) <= 2000
    from api.runtime import Runtime
    from datagen.replay import Pacer
    from edge.pharmacy_edge import Edge

    async def run():
        bus = MemoryBus()
        db = Database.connect("sqlite", sqlite_path=root / "db.sqlite3")
        rt = Runtime(db=db, bus=bus)
        await rt.start(with_edge=False)
        tail_from = await bus.last_id(STREAM_ALERTS)
        n = await Edge(bus, os.urandom(32)).publish_rows(events, Pacer.parse("max"))
        assert n == len(events)
        assert await bus.wait_until_drained(STREAM_DISPENSE, timeout_s=120)
        await rt.signals.force_recompute()
        alerts = await bus.tail(STREAM_ALERTS, tail_from, count=1000, block_ms=100)
        await rt.stop()
        return db, alerts

    db, alerts = asyncio.run(run())
    assert alerts, "expected at least one alert"
    for _, a in alerts:
        assert len(a["reasons"]) == 3 and all(isinstance(x, str) and x for x in a["reasons"])
        assert a["score"] >= 0 and a["index_drug"] and a["marker_drug"]
    assert db.fetchone("SELECT COUNT(*) FROM alerts")[0] == len(alerts)
    assert db.fetchone("SELECT COUNT(*) FROM signals")[0] > 0
    assert db.fetchone("SELECT COUNT(*) FROM signals_latest")[0] > 0
    assert db.fetchone("SELECT COUNT(*) FROM latency")[0] == len(events)
    # at rest the alert row is ciphertext; it decrypts with the key
    blob = db.fetchone("SELECT enc_blob, alert_id FROM alerts LIMIT 1")
    assert b"reasons" not in bytes(blob[0])
    c = Cipher(base64.b64decode(os.environ["DATA_KEY"]))
    assert c.decrypt_json(blob[0], aad=blob[1])["alert_id"] == blob[1]
