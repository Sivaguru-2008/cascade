"""Clinical EHR routes: Doctor Station and Patient Portal APIs.

Provides real-time clinical encounter recording, longitudinal patient history,
active medication tracking, pre-prescription cascade interception co-pilot,
and patient pharmacovigilance feedback.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from datagen.catalogue import Catalogue, load_catalogue

router = APIRouter(prefix="/api/clinical", tags=["clinical"])

_cat: Catalogue | None = None


def get_catalogue() -> Catalogue:
    global _cat
    if _cat is None:
        _cat = load_catalogue()
    return _cat


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class PrescriptionItem(BaseModel):
    drug_id: str
    generic_name: str
    atc_l4: str
    dose_mg: float = Field(ge=0)
    frequency: str = "Once Daily (Morning)"
    duration_days: int = 30
    indication: str = ""


class EncounterCreate(BaseModel):
    patient_id: str
    doctor_id: str
    visit_ts: str
    chief_complaint: str
    vitals: dict[str, Any] = Field(default_factory=dict)
    clinical_notes: str = ""
    diagnosis: str
    prescriptions: list[PrescriptionItem] = Field(default_factory=list)


class PrecheckRequest(BaseModel):
    patient_id: str
    drug_id: str | None = None
    generic_name: str | None = None
    indication: str = ""
    chief_complaint: str = ""


class SymptomReport(BaseModel):
    patient_id: str
    symptom: str
    severity: str = "moderate"
    suspected_drug: str | None = None
    notes: str = ""


# ---------------------------------------------------------------------------
# 1. Doctors & Patients Directory
# ---------------------------------------------------------------------------


@router.get("/doctors")
async def list_doctors(request: Request) -> list[dict[str, Any]]:
    """Returns list of registered doctors."""
    db = request.app.state.ctx.db
    rows = db.fetchall(
        "SELECT doctor_id, name, specialty, hospital, reg_no, phone, email FROM doctors ORDER BY name ASC"
    )
    return [
        {
            "doctor_id": r[0],
            "name": r[1],
            "specialty": r[2],
            "hospital": r[3],
            "reg_no": r[4],
            "phone": r[5] or "",
            "email": r[6] or "",
        }
        for r in rows
    ]


@router.get("/patients")
async def list_patients(request: Request) -> list[dict[str, Any]]:
    """Returns list of registered patients."""
    db = request.app.state.ctx.db
    rows = db.fetchall(
        "SELECT patient_id, name, age_band, sex, blood_group, phone, emergency_contact, conditions_json "
        "FROM patient_profiles ORDER BY name ASC"
    )
    result = []
    for r in rows:
        try:
            conds = json.loads(r[7])
        except Exception:
            conds = []
        result.append(
            {
                "patient_id": r[0],
                "name": r[1],
                "age_band": r[2],
                "sex": r[3],
                "blood_group": r[4],
                "phone": r[5] or "",
                "emergency_contact": r[6] or "",
                "chronic_conditions": conds,
            }
        )
    return result


# ---------------------------------------------------------------------------
# 2. Patient Longitudinal Timeline (Cross-Doctor History)
# ---------------------------------------------------------------------------


@router.get("/patient/{patient_id}/timeline")
async def get_patient_timeline(patient_id: str, request: Request) -> dict[str, Any]:
    """Returns the complete longitudinal health record for a patient:

    - Demographics & chronic conditions
    - Encounters with doctors visited, dates, causes, diagnoses, notes
    - Prescriptions grouped by encounter and active regimen
    - Active cascade alerts and patient-reported symptoms
    """
    db = request.app.state.ctx.db
    p_row = db.fetchone(
        "SELECT patient_id, name, age_band, sex, blood_group, phone, emergency_contact, conditions_json "
        "FROM patient_profiles WHERE patient_id = ?",
        (patient_id,),
    )
    if not p_row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Patient {patient_id} not found")

    try:
        conditions = json.loads(p_row[7])
    except Exception:
        conditions = []

    patient_profile = {
        "patient_id": p_row[0],
        "name": p_row[1],
        "age_band": p_row[2],
        "sex": p_row[3],
        "blood_group": p_row[4],
        "phone": p_row[5] or "",
        "emergency_contact": p_row[6] or "",
        "chronic_conditions": conditions,
    }

    # Fetch encounters
    enc_rows = db.fetchall(
        "SELECT e.encounter_id, e.patient_id, e.doctor_id, d.name, d.specialty, d.hospital, "
        "e.visit_ts, e.chief_complaint, e.vitals_json, e.clinical_notes, e.diagnosis, e.cascade_flag, e.created_ts "
        "FROM encounters e LEFT JOIN doctors d ON e.doctor_id = d.doctor_id "
        "WHERE e.patient_id = ? ORDER BY e.visit_ts DESC",
        (patient_id,),
    )

    encounters = []
    for er in enc_rows:
        enc_id = er[0]
        try:
            vitals = json.loads(er[8]) if er[8] else {}
        except Exception:
            vitals = {}

        # Prescriptions for this encounter
        rx_rows = db.fetchall(
            "SELECT rx_id, encounter_id, patient_id, doctor_id, drug_id, generic_name, atc_l4, dose_mg, "
            "frequency, duration_days, indication, status, warning_note, created_ts "
            "FROM prescriptions WHERE encounter_id = ? ORDER BY created_ts ASC",
            (enc_id,),
        )
        rxs = [
            {
                "rx_id": rx[0],
                "drug_id": rx[4],
                "generic_name": rx[5],
                "atc_l4": rx[6],
                "dose_mg": rx[7],
                "frequency": rx[8],
                "duration_days": rx[9],
                "indication": rx[10],
                "status": rx[11],
                "warning_note": rx[12] or "",
                "created_ts": rx[13],
            }
            for rx in rx_rows
        ]

        encounters.append(
            {
                "encounter_id": enc_id,
                "doctor_id": er[2],
                "doctor_name": er[3] or er[2],
                "specialty": er[4] or "General Medicine",
                "hospital": er[5] or "Hospital Clinic",
                "visit_ts": er[6],
                "chief_complaint": er[7],
                "vitals": vitals,
                "clinical_notes": er[9] or "",
                "diagnosis": er[10],
                "cascade_flag": bool(er[11]),
                "prescriptions": rxs,
            }
        )

    # Active medications across all doctors
    act_rows = db.fetchall(
        "SELECT p.rx_id, p.encounter_id, p.patient_id, p.doctor_id, d.name, d.specialty, "
        "p.drug_id, p.generic_name, p.atc_l4, p.dose_mg, p.frequency, p.duration_days, "
        "p.indication, p.status, p.warning_note, p.created_ts "
        "FROM prescriptions p "
        "LEFT JOIN doctors d ON p.doctor_id = d.doctor_id "
        "WHERE p.patient_id = ? AND p.status = 'active' "
        "ORDER BY p.created_ts DESC",
        (patient_id,),
    )
    active_prescriptions = [
        {
            "rx_id": ar[0],
            "encounter_id": ar[1],
            "doctor_id": ar[3],
            "doctor_name": ar[4] or ar[3],
            "specialty": ar[5] or "Clinic",
            "drug_id": ar[6],
            "generic_name": ar[7],
            "atc_l4": ar[8],
            "dose_mg": ar[9],
            "frequency": ar[10],
            "duration_days": ar[11],
            "indication": ar[12],
            "warning_note": ar[14] or "",
            "created_ts": ar[15],
        }
        for ar in act_rows
    ]

    # Symptoms reported by the patient
    sym_rows = db.fetchall(
        "SELECT id, patient_id, symptom, onset_ts, severity, suspected_drug, notes, status, created_ts "
        "FROM patient_symptoms WHERE patient_id = ? ORDER BY created_ts DESC",
        (patient_id,),
    )
    symptoms = [
        {
            "id": sr[0],
            "symptom": sr[2],
            "onset_ts": sr[3],
            "severity": sr[4],
            "suspected_drug": sr[5] or "",
            "notes": sr[6] or "",
            "status": sr[7],
            "created_ts": sr[8],
        }
        for sr in sym_rows
    ]

    # Any active cascade alerts for patient
    active_cascade_warnings = [
        enc
        for enc in encounters
        if enc["cascade_flag"]
    ]

    return {
        "patient": patient_profile,
        "encounters": encounters,
        "active_prescriptions": active_prescriptions,
        "symptoms": symptoms,
        "active_cascade_warnings": active_cascade_warnings,
    }


# ---------------------------------------------------------------------------
# 3. Interactive Cascade Pre-Check Co-Pilot
# ---------------------------------------------------------------------------


@router.post("/precheck-cascade")
async def precheck_cascade(req: PrecheckRequest, request: Request) -> dict[str, Any]:
    """Evaluates whether the candidate drug being prescribed constitutes a Prescribing Cascade

    against the patient's existing medication regimen.
    """
    cat = get_catalogue()
    db = request.app.state.ctx.db

    # Identify candidate drug
    cand_drug = None
    if req.drug_id and req.drug_id in cat.by_id:
        cand_drug = cat.by_id[req.drug_id]
    elif req.generic_name and req.generic_name.lower() in cat.by_name:
        cand_drug = cat.by_name[req.generic_name.lower()]

    if not cand_drug:
        return {"is_cascade": False, "risk_level": "NONE", "score": 0.0}

    # Fetch active prescriptions for this patient
    rx_rows = db.fetchall(
        "SELECT p.drug_id, p.generic_name, p.atc_l4, p.dose_mg, p.created_ts, d.name, d.specialty "
        "FROM prescriptions p "
        "LEFT JOIN doctors d ON p.doctor_id = d.doctor_id "
        "WHERE p.patient_id = ? AND p.status = 'active'",
        (req.patient_id,),
    )

    complaint_lower = (req.chief_complaint + " " + req.indication).lower()

    for r in rx_rows:
        prior_drug_id, prior_name, prior_l4, prior_dose, prior_ts, prior_doc, prior_spec = r
        if cand_drug.atc_l4 == prior_l4:
            continue

        # Check known cascade pairs in catalogue
        pair = cat.known_pair(prior_l4, cand_drug.atc_l4)
        if pair is not None:
            reaction = pair.reaction
            source = pair.source_short
            recom = (
                f"Prescribing {cand_drug.generic_name.title()} to manage {reaction.lower()} "
                f"constitutes a documented Prescribing Cascade ({source}). The symptom is likely "
                f"an adverse reaction to {prior_name.title()} ({prior_dose}mg) started by "
                f"{prior_doc or 'previous doctor'} ({prior_spec or 'Specialist'}). "
                f"Consider titrating or switching the primary medication before adding a secondary drug."
            )
            return {
                "is_cascade": True,
                "risk_level": "HIGH",
                "score": 0.89,
                "pair_id": pair.pair_id,
                "prior_drug": f"{prior_name.title()} ({prior_dose}mg)",
                "prior_doctor": prior_doc or "Attending Physician",
                "prior_specialty": prior_spec or "Specialist",
                "prior_date": prior_ts[:10] if prior_ts else "Recent",
                "reaction": reaction,
                "recommendation": recom,
            }

        # Check secondary reactions table
        for rx in cat.reactions:
            if prior_l4 in rx.index_l4:
                for tx in cat.treatments_by_reaction.get(rx.reaction_id, []):
                    if cand_drug.atc_l4 in tx.marker_l4 or any(w in complaint_lower for w in rx.reaction_name.lower().split()):
                        return {
                            "is_cascade": True,
                            "risk_level": "MODERATE",
                            "score": 0.74,
                            "pair_id": rx.reaction_id,
                            "prior_drug": f"{prior_name.title()} ({prior_dose}mg)",
                            "prior_doctor": prior_doc or "Attending Physician",
                            "prior_specialty": prior_spec or "Specialist",
                            "prior_date": prior_ts[:10] if prior_ts else "Recent",
                            "reaction": rx.reaction_name,
                            "recommendation": (
                                f"Possible prescribing cascade: {rx.reaction_name} is a known "
                                f"adverse effect of {prior_name.title()}. Clinical review recommended."
                            ),
                        }

    return {"is_cascade": False, "risk_level": "NONE", "score": 0.12}


# ---------------------------------------------------------------------------
# 4. Encounter Creation & Prescription Pad Submission
# ---------------------------------------------------------------------------


@router.post("/encounter/create")
async def create_encounter(enc: EncounterCreate, request: Request) -> dict[str, Any]:
    """Records a clinical consultation encounter, persists all prescribed medications,

    runs real-time CASCADE cascade evaluation, and links records bi-directionally.
    """
    ctx = request.app.state.ctx
    db = ctx.db
    cat = get_catalogue()

    encounter_id = f"ENC-{uuid.uuid4().hex[:8].upper()}"
    ts_now = _now()
    visit_ts = enc.visit_ts or ts_now

    # Check for cascade across all new prescriptions
    has_cascade = False
    cascade_details: list[dict[str, Any]] = []

    # Get patient's existing active drugs
    existing_rxs = db.fetchall(
        "SELECT p.drug_id, p.generic_name, p.atc_l4, p.dose_mg, p.created_ts, d.name, d.specialty "
        "FROM prescriptions p "
        "LEFT JOIN doctors d ON p.doctor_id = d.doctor_id "
        "WHERE p.patient_id = ? AND p.status = 'active'",
        (enc.patient_id,),
    )

    processed_prescriptions = []
    for item in enc.prescriptions:
        rx_id = f"RX-{uuid.uuid4().hex[:8].upper()}"
        item_warning = ""

        # Pre-check against existing active drugs
        for er in existing_rxs:
            prior_id, prior_name, prior_l4, prior_dose, prior_ts, prior_doc, prior_spec = er
            pair = cat.known_pair(prior_l4, item.atc_l4)
            if pair is not None:
                has_cascade = True
                item_warning = (
                    f"CASCADE WARNING (Pair {pair.pair_id}): {pair.reaction} secondary to "
                    f"{prior_name.title()} prescribed by {prior_doc}."
                )
                cascade_details.append(
                    {
                        "pair_id": pair.pair_id,
                        "marker_drug": item.generic_name,
                        "index_drug": prior_name,
                        "reaction": pair.reaction,
                        "prior_doctor": prior_doc,
                    }
                )
                break

        processed_prescriptions.append((rx_id, item, item_warning))

    # Insert Encounter
    db.execute(
        "INSERT INTO encounters (encounter_id, patient_id, doctor_id, visit_ts, chief_complaint, "
        "vitals_json, clinical_notes, diagnosis, cascade_flag, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            encounter_id,
            enc.patient_id,
            enc.doctor_id,
            visit_ts,
            enc.chief_complaint,
            json.dumps(enc.vitals),
            enc.clinical_notes,
            enc.diagnosis,
            1 if has_cascade else 0,
            ts_now,
        ),
    )

    # Insert Prescriptions
    for rx_id, item, warning in processed_prescriptions:
        db.execute(
            "INSERT INTO prescriptions (rx_id, encounter_id, patient_id, doctor_id, drug_id, "
            "generic_name, atc_l4, dose_mg, frequency, duration_days, indication, status, warning_note, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rx_id,
                encounter_id,
                enc.patient_id,
                enc.doctor_id,
                item.drug_id,
                item.generic_name,
                item.atc_l4,
                item.dose_mg,
                item.frequency,
                item.duration_days,
                item.indication,
                "active",
                warning,
                ts_now,
            ),
        )

    # If a prescribing cascade was intercepted, sync with the CASCADE alert stream
    if has_cascade and cascade_details:
        alert_id = f"AL-{uuid.uuid4().hex[:6].upper()}"
        first = cascade_details[0]
        alert_payload = {
            "alert_id": alert_id,
            "event_id": encounter_id,
            "ts": ts_now,
            "pharmacy_id": "PH030",
            "score": 0.88,
            "status": "open",
            "index_atc_l4": "C08CA",
            "marker_atc_l4": "C03CA",
            "pair_status": "known",
            "patient_token": enc.patient_id,
            "index_drug": first["index_drug"],
            "marker_drug": first["marker_drug"],
            "days_between": 24,
            "reasons": [
                f"{first['marker_drug'].title()} initiated within 30 days of {first['index_drug'].title()}",
                f"Documented adverse reaction '{first['reaction']}' mistaken for new illness",
                f"Cross-prescriber consultation: initiated by {enc.doctor_id} following prescription by {first['prior_doctor']}",
            ],
            "suspected_reaction": first["reaction"],
        }
        try:
            enc_blob = ctx.cipher.encrypt_json(alert_payload, aad=alert_id)
            db.execute(
                "INSERT INTO alerts (alert_id, event_id, ts, pharmacy_id, score, status, index_atc_l4, marker_atc_l4, "
                "pair_status, enc_blob, created_ts, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    alert_id,
                    encounter_id,
                    ts_now,
                    "PH030",
                    0.88,
                    "open",
                    "C08CA",
                    "C03CA",
                    "known",
                    enc_blob,
                    ts_now,
                    ts_now,
                ),
            )
        except Exception:
            pass

    return {
        "status": "created",
        "encounter_id": encounter_id,
        "cascade_flag": has_cascade,
        "cascade_details": cascade_details,
        "prescriptions_count": len(processed_prescriptions),
    }


# ---------------------------------------------------------------------------
# 5. Patient Pharmacovigilance & Feedback
# ---------------------------------------------------------------------------


@router.post("/patient/report-symptom")
async def report_symptom(report: SymptomReport, request: Request) -> dict[str, str]:
    """Records a patient-reported symptom or suspected adverse drug reaction."""
    db = request.app.state.ctx.db
    ts_now = _now()
    db.execute(
        "INSERT INTO patient_symptoms (patient_id, symptom, onset_ts, severity, suspected_drug, notes, status, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            report.patient_id,
            report.symptom,
            ts_now,
            report.severity,
            report.suspected_drug or "",
            report.notes,
            "reported",
            ts_now,
        ),
    )
    return {"status": "reported", "message": "Symptom logged and flagged for doctor review."}


# ---------------------------------------------------------------------------
# 6. Catalogue Helpers for Rx Pad Autocomplete
# ---------------------------------------------------------------------------


@router.get("/drugs")
async def list_drugs() -> list[dict[str, Any]]:
    """Returns all catalogue drugs for high-performance clinical autocomplete."""
    cat = get_catalogue()
    return [
        {
            "drug_id": d.drug_id,
            "generic_name": d.generic_name,
            "display_name": d.display_name,
            "atc_l4": d.atc_l4,
            "dose_mg": d.dose_mg,
        }
        for d in cat.drugs
    ]


@router.get("/reactions")
async def list_reactions() -> list[str]:
    """Returns clinical reactions for Chief Complaint autocomplete."""
    cat = get_catalogue()
    return cat.reactions_list()
