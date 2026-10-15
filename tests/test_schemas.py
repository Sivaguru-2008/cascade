from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.main import app
from common.schemas import DispenseEvent

TOKEN = "a" * 64


def _event(**overrides):
    base = dict(
        event_id="e1", ts=datetime(2025, 1, 1, 9, 0), pharmacy_id="PH001", patient_token=TOKEN,
        prescriber_token="b" * 64, age_band="70-74", sex="F", drug_id="D001", atc_code="C08CA01",
        atc_l4="C08CA", atc_l3="C08C", qty=30, days_supply=30, dose_mg=5.0,
    )
    base.update(overrides)
    return DispenseEvent(**base)


def test_event_accepts_tokens():
    assert _event().patient_token == TOKEN


@pytest.mark.parametrize("bad", ["P000123", "A" * 64, "a" * 63])
def test_event_rejects_raw_ids(bad):
    with pytest.raises(ValidationError):
        _event(patient_token=bad)


def test_event_has_no_label_fields():
    with pytest.raises(ValidationError):
        _event(is_cascade=1)


def test_health():
    r = TestClient(app).get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
