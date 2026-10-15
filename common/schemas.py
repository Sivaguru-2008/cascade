"""Pydantic schemas shared by every service.

Labels (is_cascade, pair_id, ...) are never fields of DispenseEvent and never travel on the bus.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

HEX64 = re.compile(r"^[0-9a-f]{64}$")
AGE_BAND = re.compile(r"^(\d{1,3}-\d{1,3}|\d{1,3}\+)$")


class DispenseEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    ts: datetime
    pharmacy_id: str
    patient_token: str
    prescriber_token: str
    age_band: str
    sex: Literal["M", "F", "U"]
    drug_id: str
    atc_code: str
    atc_l4: str
    atc_l3: str
    qty: int = Field(ge=0)
    days_supply: int = Field(ge=1)
    dose_mg: float = Field(ge=0)

    @field_validator("patient_token", "prescriber_token")
    @classmethod
    def _is_hmac_hex(cls, v: str) -> str:
        if not HEX64.match(v):
            raise ValueError("token must be 64 lowercase hex characters (HMAC-SHA256 output)")
        return v

    @field_validator("age_band")
    @classmethod
    def _age_band(cls, v: str) -> str:
        if not AGE_BAND.match(v):
            raise ValueError("age_band must look like '70-74' or '90+'")
        return v


class Alert(BaseModel):
    alert_id: str
    event_id: str
    ts: datetime
    pharmacy_id: str
    patient_token: str
    index_drug: str
    marker_drug: str
    index_atc_l4: str
    marker_atc_l4: str
    days_between: int
    score: float
    reasons: list[str] = Field(min_length=1, max_length=3)
    pair_aSR: float | None = None
    pair_aSR_ci: list[float | None] = Field(default_factory=lambda: [None, None])
    known_source: str | None = None
    status: Literal["open", "drafted", "approved", "dismissed"] = "open"
    # Extra context needed to build the GenAI input contract (section 7.1).
    index_drug_id: str = ""
    marker_drug_id: str = ""
    index_dose_mg: float | None = None
    marker_dose_mg: float | None = None
    suspected_reaction: str = "unknown — pattern detected statistically"
    age_band: str = ""
    sex: str = "U"
    n_active_drugs: int = 0
    different_prescriber: bool = False
    pair_status: Literal["known", "corroborated", "new"] = "new"


class ScoreDecision(BaseModel):
    event_id: str
    incident: bool
    scored: bool
    n_candidates: int = 0
    score: float | None = None
    threshold: float | None = None
    alerted: bool = False
    alert: Alert | None = None
    latency_ms: float = 0.0


# ---------- GenAI contract (section 7.1): the only data the model sees ----------


class DraftEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: float
    aSR: float | None
    aSR_ci: list[float | None]
    known_source: str | None


class DraftPatient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    age_band: str
    sex: Literal["M", "F", "U"]
    n_active_drugs: int


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alert_id: str
    index_drug: str
    marker_drug: str
    days_between: int
    suspected_reaction: str
    evidence: DraftEvidence
    patient: DraftPatient
    different_prescriber: bool


# ---------- PvPI draft (section 7.2), keyed to data/pvpi_form_fields.json ----------


class PvpiPatient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patient_initials: str | None = None
    age_at_onset: str | None = None
    sex: str | None = None
    weight_kg: float | None = None


class PvpiReaction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reaction_start_date: str | None = None
    reaction_stop_date: str | None = None
    reaction_description: str | None = None
    relevant_investigations: str | None = None
    relevant_medical_history: str | None = None
    seriousness: str | None = None
    outcome: str | None = None


class PvpiSuspectDrug(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drug_name: str | None = None
    manufacturer: str | None = None
    batch_lot_no: str | None = None
    expiry_date: str | None = None
    dose: str | None = None
    route: str | None = None
    frequency: str | None = None
    therapy_start_date: str | None = None
    therapy_stop_date: str | None = None
    indication: str | None = None
    dechallenge: str | None = None
    rechallenge: str | None = None


class PvpiConcomitant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drug_name: str | None = None
    dose: str | None = None
    therapy_start_date: str | None = None
    indication: str | None = None


class PvpiReporter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reporter_name: str | None = None
    professional_address: str | None = None
    email: str | None = None
    contact_number: str | None = None
    occupation: str | None = None
    report_date: str | None = None


class PvpiDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    form: Literal["PvPI Suspected Adverse Drug Reaction Reporting Form (draft)"] = (
        "PvPI Suspected Adverse Drug Reaction Reporting Form (draft)"
    )
    A_patient: PvpiPatient
    B_reaction: PvpiReaction
    C_suspected_medications: list[PvpiSuspectDrug] = Field(min_length=1)
    concomitant_medications: list[PvpiConcomitant] = Field(default_factory=list)
    D_reporter: PvpiReporter
    disclaimer: str = "DRAFT — synthetic demo data — requires reporter completion"


class DraftResult(BaseModel):
    alert_id: str
    note_text: str
    report: PvpiDraft
    generator: str
    validator_pass: bool
    reject_reasons: list[str] = Field(default_factory=list)
    gen_ms: float
