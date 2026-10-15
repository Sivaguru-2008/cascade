"""API role rules (section 8.5): pharmacist sees own pharmacy only, regulator sees aggregates only."""

import base64
import os
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from common.bus import MemoryBus
from common.config import reload_settings
from common.crypto import Cipher
from common.db import Database
from common.schemas import Alert

PH1, PH2, REG = "k-ph1-" + "x" * 20, "k-ph2-" + "y" * 20, "k-reg-" + "z" * 20


def _alert(i, pharmacy):
    return Alert(alert_id=f"AL-{i:06d}", event_id=f"e{i}", ts=datetime(2025, 3, 1, 10), pharmacy_id=pharmacy,
                 patient_token="a" * 64, index_drug="Amlodipine", marker_drug="Furosemide", index_atc_l4="C08CA",
                 marker_atc_l4="C03CA", days_between=18, score=0.91, reasons=["r1", "r2", "r3"], pair_aSR=3.4,
                 pair_aSR_ci=[2.6, 4.5], known_source="ThinkCascades", index_drug_id="D001", marker_drug_id="D014",
                 index_dose_mg=5.0, marker_dose_mg=40.0, suspected_reaction="Ankle oedema", age_band="70-74", sex="F",
                 n_active_drugs=7, different_prescriber=True, pair_status="known")


@pytest.fixture
def client(tmp_path, monkeypatch):
    key = os.urandom(32)
    monkeypatch.setenv("DATA_KEY", base64.b64encode(key).decode())
    monkeypatch.setenv("PHARMACIST_KEY_PH001", PH1)
    monkeypatch.setenv("PHARMACIST_KEY_PH002", PH2)
    monkeypatch.setenv("REGULATOR_KEY", REG)
    monkeypatch.setenv("GENAI_BACKEND", "template")
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("GENAI_URL", "")  # draft in-process (no genai container in unit tests)
    s = reload_settings()
    from api.main import Context, app

    db = Database.connect("sqlite", sqlite_path=tmp_path / "t.sqlite3")
    db.init_schema()
    cipher = Cipher(key)
    for i, ph in ((1, "PH001"), (2, "PH001"), (3, "PH002")):
        a = _alert(i, ph)
        db.execute("INSERT INTO alerts (alert_id, event_id, ts, pharmacy_id, score, status, index_atc_l4, "
                   "marker_atc_l4, pair_status, enc_blob, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (a.alert_id, a.event_id, "2025-03-01T10:00:00", ph, a.score, "open", "C08CA", "C03CA", "known",
                    cipher.encrypt_json(a.model_dump(mode="json"), aad=a.alert_id), f"2026-01-0{i}", f"2026-01-0{i}"))
    db.executemany("INSERT INTO signals_latest (a_l4, b_l4, win, sim_day, n_ab, n_ba, nsr, asr, lo, hi, p_value, "
                   "is_signal, is_signal_bh, status, first_signal_day, updated_ts) VALUES "
                   "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   [("C09XX", "A02BC", 90, 950, 40, 3, 1.1, 12.1, 4.0, 40.0, 1e-6, 1, 1, "new", 940, "x"),
                    ("C08CA", "C03CA", 90, 950, 4, 0, 1.0, 999.0, 0.6, 999.0, 0.06, 0, 0, "known", None, "x")])
    db.executemany("INSERT INTO signals (snapshot_ts, sim_day, a_l4, b_l4, win, n_ab, n_ba, nsr, asr, lo, hi, "
                   "p_value, is_signal, is_signal_bh, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   [("x", 930, "C09XX", "A02BC", 90, 3, 1, 1.0, 3.0, 0.3, 30.0, 0.3, 0, 0, "new"),
                    ("x", 950, "C09XX", "A02BC", 90, 40, 3, 1.1, 12.1, 4.0, 40.0, 1e-6, 1, 1, "new")])
    app.state.ctx = Context(s, db, cipher, MemoryBus(), httpx.AsyncClient(), None)
    yield TestClient(app), db
    reload_settings()


def h(key):
    return {"X-API-Key": key}


@pytest.mark.parametrize("path", ["/alerts", "/alerts/AL-000001", "/signals", "/signals/C09XX/A02BC/history",
                                  "/metrics"])
def test_missing_or_invalid_key_is_401(client, path):
    c, _ = client
    assert c.get(path).status_code == 401
    assert c.get(path, headers=h("wrong")).status_code == 401


@pytest.mark.parametrize("method,path", [("get", "/alerts"), ("get", "/alerts/AL-000001"),
                                         ("post", "/alerts/AL-000001/draft"), ("post", "/alerts/AL-000001/approve")])
def test_regulator_gets_403_on_alert_endpoints(client, method, path):
    c, _ = client
    assert getattr(c, method)(path, headers=h(REG)).status_code == 403


def test_regulator_403_on_dismiss_and_score(client):
    c, _ = client
    assert c.post("/alerts/AL-000001/dismiss", json={"reason": "x"}, headers=h(REG)).status_code == 403
    assert c.post("/score", json={}, headers=h(REG)).status_code in (403, 422)


@pytest.mark.parametrize("path", ["/signals", "/signals/C09XX/A02BC/history"])
def test_pharmacist_gets_403_on_signals(client, path):
    c, _ = client
    assert c.get(path, headers=h(PH1)).status_code == 403


def test_pharmacist_sees_only_own_alerts(client):
    c, _ = client
    r = c.get("/alerts", headers=h(PH1))
    assert r.status_code == 200
    ids = {a["alert_id"] for a in r.json()}
    assert ids == {"AL-000001", "AL-000002"}
    assert all("patient_token" not in a for a in r.json())
    assert c.get("/alerts/AL-000003", headers=h(PH1)).status_code == 403
    assert c.get("/alerts/AL-000003", headers=h(PH2)).status_code == 200
    assert c.post("/alerts/AL-000003/draft", headers=h(PH1)).status_code == 403
    assert c.post("/alerts/AL-000003/dismiss", json={"reason": "x"}, headers=h(PH1)).status_code == 403
    assert c.get("/alerts/AL-999999", headers=h(PH1)).status_code == 404


def test_score_other_pharmacy_event_403(client):
    c, _ = client
    ev = {"event_id": "e", "ts": "2025-01-01T10:00:00", "pharmacy_id": "PH002", "patient_token": "a" * 64,
          "prescriber_token": "b" * 64, "age_band": "70-74", "sex": "F", "drug_id": "D014", "atc_code": "C03CA01",
          "atc_l4": "C03CA", "atc_l3": "C03C", "qty": 30, "days_supply": 30, "dose_mg": 40}
    assert c.post("/score", json=ev, headers=h(PH1)).status_code == 403


def test_regulator_signals_with_small_cell_suppression(client):
    c, _ = client
    r = c.get("/signals", headers=h(REG))
    assert r.status_code == 200
    body = r.json()
    assert [s["status"] for s in body["signals"]] == ["new"]
    s0 = body["signals"][0]
    assert s0["n_ab"] == 40 and s0["n_ba"] == "<5" and s0["a"]["fictional"]
    allp = c.get("/signals?all=1", headers=h(REG)).json()["signals"]
    weak = next(s for s in allp if s["a"]["atc_l4"] == "C08CA")
    assert weak["n"] == "<5" and weak["n_ab"] == "<5" and weak["n_ba"] == "<5"
    hist = c.get("/signals/C09XX/A02BC/history", headers=h(REG)).json()
    assert [p["n"] for p in hist["points"]] == ["<5", 43]


def test_metrics_both_roles_and_suppression(client):
    c, _ = client
    m1 = c.get("/metrics", headers=h(PH1)).json()
    mr = c.get("/metrics", headers=h(REG)).json()
    assert m1["alerts"] == {"open": 2} and m1["alerts_total"] == 2
    assert mr["alerts"] == {"open": "<5"} and mr["alerts_total"] == "<5"
    assert m1["arch"] and m1["latency_ms"] is None and m1["events_per_s"] is None


def test_draft_approve_dismiss_and_audit(client):
    c, db = client
    assert c.get("/alerts/AL-000001", headers=h(PH1)).status_code == 200
    d = c.post("/alerts/AL-000001/draft", headers=h(PH1))
    assert d.status_code == 200 and d.json()["validator_pass"] and d.json()["generator"] == "template"
    a = c.post("/alerts/AL-000001/approve", headers=h(PH1)).json()
    assert a["status"] == "approved" and base64.b64decode(a["file_base64"]).startswith(b"%PDF")
    assert a["report"]["A_patient"]["patient_initials"] is None
    assert c.post("/alerts/AL-000002/dismiss", json={"reason": "already reviewed"}, headers=h(PH1)).status_code == 200
    actions = [r[0] for r in db.fetchall("SELECT action FROM audit ORDER BY id")]
    assert actions == ["alert_view", "draft", "approve", "export", "dismiss"]
    roles = {r[0] for r in db.fetchall("SELECT role FROM audit")}
    assert roles == {"pharmacist:PH001"}
    st = dict(db.fetchall("SELECT alert_id, status FROM alerts"))
    assert st["AL-000001"] == "approved" and st["AL-000002"] == "dismissed"
    raw = db.fetchone("SELECT note_text, report_json FROM drafts WHERE alert_id = 'AL-000001'")
    assert b"Prescriber" not in bytes(raw[0]) and b"A_patient" not in bytes(raw[1])
    fb = db.fetchone("SELECT reason_enc FROM feedback")
    assert b"already reviewed" not in bytes(fb[0])


def test_alert_rows_are_ciphertext(client):
    _, db = client
    blob = bytes(db.fetchone("SELECT enc_blob FROM alerts LIMIT 1")[0])
    assert b"Amlodipine" not in blob and b"aaaa" not in blob


def test_demo_endpoint_disabled_without_demo_mode(client):
    c, _ = client
    assert c.post("/admin/demo/start", headers=h(PH1)).status_code == 404
