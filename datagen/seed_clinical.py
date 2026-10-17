"""Seeds clinical EHR database with realistic doctors, patient profiles,

longitudinal multi-doctor encounters, and documented prescribing cascades.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from common.config import reload_settings
from common.db import Database


def seed() -> None:
    # Ensure SQLite mode if running locally
    if os.environ.get("CASCADE_DB") != "sqlite" and not os.environ.get("DATABASE_URL"):
        os.environ["CASCADE_DB"] = "sqlite"
        root = Path(__file__).resolve().parent.parent
        os.environ["SQLITE_PATH"] = str((root / "data" / "generated" / "cascade.sqlite3").resolve())

    reload_settings()
    db = Database.connect()
    db.init_schema()

    print(f"[*] Seeding clinical records into {db.dialect} database...")

    # 1. Doctors
    doctors = [
        ("DR0010", "Dr. Ramesh Sharma", "Cardiology", "Apex Heart & Vascular Institute", "MCI-48291", "+91-98401-11001", "r.sharma@apexheart.org"),
        ("DR0020", "Dr. Priya Patel", "Internal Medicine", "City Multi-Specialty Clinic", "MCI-53102", "+91-98401-11002", "drpriya.patel@cityclinic.in"),
        ("DR0030", "Dr. Sunita Rao", "Pulmonology & Chest", "Metro Chest & Allergy Centre", "MCI-62184", "+91-98401-11003", "sunita.rao@metrochest.com"),
        ("DR0040", "Dr. Arvind Swaminathan", "Neurology", "Brain & Nerve Institute", "MCI-39042", "+91-98401-11004", "a.swaminathan@brainnerve.org"),
        ("DR0050", "Dr. Rajesh Mukherjee", "Orthopaedics", "Bone & Joint Care Clinic", "MCI-44910", "+91-98401-11005", "rmukherjee@bonejoint.in"),
        ("DR0060", "Dr. Meenakshi Sundaram", "Geriatric Medicine", "Senior Care Health Pavilion", "MCI-51928", "+91-98401-11006", "msundaram@seniorcare.org"),
        ("DR0070", "Dr. Anand Kulkarni", "Nephrology", "National Kidney Care Centre", "MCI-47201", "+91-98401-11007", "akulkarni@kidneycare.in"),
        ("DR0080", "Dr. Fatima Sheikh", "Family Medicine", "Community Primary Health Centre", "MCI-58812", "+91-98401-11008", "fsheikh@communityhealth.in"),
    ]

    for d in doctors:
        db.execute(
            "INSERT INTO doctors (doctor_id, name, specialty, hospital, reg_no, phone, email) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (doctor_id) DO UPDATE SET name = excluded.name, specialty = excluded.specialty, "
            "hospital = excluded.hospital, reg_no = excluded.reg_no, phone = excluded.phone, email = excluded.email",
            d,
        )
    print(f"  [+] Seeded {len(doctors)} specialist and primary care doctors")

    # 2. Patient Profiles
    patients = [
        ("P001142", "Kavita Raman", "70-74", "F", "B+", "+91-98401-44129", "Son: Amit Raman (+91-98402-99102)", json.dumps(["hypertension", "dyslipidaemia"])),
        ("P002381", "Arun Verma", "60-74", "M", "O+", "+91-94440-12849", "Wife: Shobha Verma (+91-94440-12850)", json.dumps(["hypertension", "diabetes", "nephropathy"])),
        ("P003920", "Meera Sen", "55-59", "F", "A+", "+91-98841-88201", "Daughter: Ananya Sen (+91-98841-88202)", json.dumps(["diabetes", "gerd"])),
        ("P004105", "K. Balasubramanian", "75+", "M", "AB+", "+91-97910-33012", "Son: Ganesh B (+91-97910-33013)", json.dumps(["bph", "osteoarthritis", "hypertension"])),
        ("P005892", "Deepak Joshi", "60-64", "M", "B+", "+91-98111-20941", "Brother: Suresh Joshi (+91-98111-20942)", json.dumps(["depression", "insomnia"])),
        ("P006214", "Sunita Deshmukh", "65-69", "F", "O-", "+91-99203-77401", "Husband: Vilas Deshmukh (+91-99203-77402)", json.dumps(["osteoarthritis", "hypertension"])),
        ("P007451", "Harpreet Singh", "70-74", "M", "A+", "+91-98722-66100", "Daughter: Jaspreet Kaur (+91-98722-66101)", json.dumps(["parkinson", "constipation"])),
        ("P008102", "Shanti Devi", "75+", "F", "B-", "+91-94191-55209", "Son: Rajeev Kumar (+91-94191-55210)", json.dumps(["dementia", "hypertension"])),
    ]

    for p in patients:
        db.execute(
            "INSERT INTO patient_profiles (patient_id, name, age_band, sex, blood_group, phone, emergency_contact, conditions_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (patient_id) DO UPDATE SET name = excluded.name, age_band = excluded.age_band, "
            "sex = excluded.sex, blood_group = excluded.blood_group, phone = excluded.phone, "
            "emergency_contact = excluded.emergency_contact, conditions_json = excluded.conditions_json",
            p,
        )
    print(f"  [+] Seeded {len(patients)} comprehensive patient profiles")

    # 3. Longitudinal Encounters
    encounters = [
        # Kavita Raman: Encounter 1 (24 days ago with Dr. Ramesh Sharma)
        (
            "ENC-1001",
            "P001142",
            "DR0010",
            "2026-09-24T10:15:00",
            "Routine blood pressure check-up; morning occipital headache",
            json.dumps({"bp": "154/96", "pulse": "76", "spo2": "98%", "weight": "64.0 kg", "temp": "98.4 F"}),
            "Patient has Stage 2 essential hypertension with suboptimal response to dietary sodium restriction. Cardiovascular exam normal. Commencing dihydropyridine calcium channel blocker monotherapy. Follow-up in 4 weeks.",
            "Essential Hypertension (Stage 2)",
            0,
            "2026-09-24T10:30:00",
        ),
        # Kavita Raman: Encounter 2 (4 days ago with Dr. Priya Patel) - Intercepted Cascade!
        (
            "ENC-1002",
            "P001142",
            "DR0020",
            "2026-10-14T11:45:00",
            "Bilateral ankle swelling and tight footwear for 10 days; denies dyspnoea or orthopnoea",
            json.dumps({"bp": "134/82", "pulse": "74", "spo2": "99%", "weight": "65.4 kg", "temp": "98.2 F"}),
            "Bilateral 2+ pitting dependent ankle oedema up to lower third of tibia. JVP normal, lung bases clear on auscultation. Commenced loop diuretic for symptomatic relief.",
            "Peripheral dependent oedema",
            1,
            "2026-10-14T12:05:00",
        ),
        # Arun Verma: Encounter 1 (30 days ago with Dr. Ramesh Sharma)
        (
            "ENC-2001",
            "P002381",
            "DR0010",
            "2026-09-18T09:30:00",
            "Diabetic nephropathy review; elevated systolic blood pressure (150/90)",
            json.dumps({"bp": "150/90", "pulse": "78", "spo2": "97%", "weight": "72.5 kg", "temp": "98.6 F"}),
            "Urine albumin-to-creatinine ratio elevated. Initiating ACE-inhibitor therapy for dual blood pressure control and renal protection.",
            "Diabetic Nephropathy with Hypertension",
            0,
            "2026-09-18T09:50:00",
        ),
        # Arun Verma: Encounter 2 (5 days ago with Dr. Sunita Rao) - Intercepted Cascade!
        (
            "ENC-2002",
            "P002381",
            "DR0030",
            "2026-10-13T16:00:00",
            "Persistent dry, hacking, non-productive cough for 2 weeks; disturbed sleep",
            json.dumps({"bp": "132/80", "pulse": "80", "spo2": "98%", "weight": "72.0 kg", "temp": "98.4 F"}),
            "Chest clear on auscultation. No fever, no sputum production. Prescribing cough suppressant syrup.",
            "Chronic non-productive cough",
            1,
            "2026-10-13T16:25:00",
        ),
        # Meera Sen: Encounter 1 (45 days ago with Dr. Priya Patel)
        (
            "ENC-3001",
            "P003920",
            "DR0020",
            "2026-09-03T11:00:00",
            "Quarterly Type 2 Diabetes check-up; fasting blood sugar 148 mg/dL",
            json.dumps({"bp": "126/80", "pulse": "72", "spo2": "99%", "weight": "61.0 kg", "temp": "98.6 F"}),
            "HbA1c 7.4%. Titrating biguanide monotherapy and reinforced dietary counselling.",
            "Type 2 Diabetes Mellitus",
            0,
            "2026-09-03T11:20:00",
        ),
        # Meera Sen: Encounter 2 (10 days ago with Dr. Priya Patel)
        (
            "ENC-3002",
            "P003920",
            "DR0020",
            "2026-10-08T10:30:00",
            "Follow-up visit; fasting blood sugar improved to 118 mg/dL",
            json.dumps({"bp": "122/78", "pulse": "70", "spo2": "99%", "weight": "60.5 kg", "temp": "98.4 F"}),
            "Patient reports excellent adherence. Fasting blood sugars normalised. Glycemic maintenance continued.",
            "Type 2 Diabetes Mellitus (Controlled)",
            0,
            "2026-10-08T10:45:00",
        ),
    ]

    for enc in encounters:
        db.execute(
            "INSERT INTO encounters (encounter_id, patient_id, doctor_id, visit_ts, chief_complaint, "
            "vitals_json, clinical_notes, diagnosis, cascade_flag, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (encounter_id) DO UPDATE SET chief_complaint = excluded.chief_complaint, "
            "diagnosis = excluded.diagnosis, cascade_flag = excluded.cascade_flag",
            enc,
        )
    print(f"  [+] Seeded {len(encounters)} multi-doctor longitudinal encounters")

    # 4. Prescriptions
    prescriptions = [
        # Kavita Raman - Encounter 1: Amlodipine 5mg
        (
            "RX-1001A",
            "ENC-1001",
            "P001142",
            "DR0010",
            "D001",
            "amlodipine",
            "C08CA",
            5.0,
            "Once Daily (Morning)",
            30,
            "Essential Hypertension Stage 2",
            "active",
            "",
            "2026-09-24T10:30:00",
        ),
        # Kavita Raman - Encounter 2: Furosemide 20mg (Cascade marker)
        (
            "RX-1002A",
            "ENC-1002",
            "P001142",
            "DR0020",
            "D021",
            "furosemide",
            "C03CA",
            20.0,
            "Once Daily (Morning)",
            30,
            "Peripheral dependent oedema",
            "active",
            "CASCADE INTERCEPTION (Pair K1): Ankle oedema is a documented adverse effect of Amlodipine initiated 24 days ago by Dr. Ramesh Sharma. Consider reducing CCB dose or substituting with ARB rather than initiating loop diuretic.",
            "2026-10-14T12:05:00",
        ),
        # Arun Verma - Encounter 1: Enalapril 10mg
        (
            "RX-2001A",
            "ENC-2001",
            "P002381",
            "DR0010",
            "D003",
            "enalapril",
            "C09AA",
            10.0,
            "Once Daily (Morning)",
            30,
            "Renoprotection in Hypertension",
            "active",
            "",
            "2026-09-18T09:50:00",
        ),
        # Arun Verma - Encounter 2: Dextromethorphan (Cascade marker)
        (
            "RX-2002A",
            "ENC-2002",
            "P002381",
            "DR0030",
            "D058",
            "dextromethorphan",
            "R05DA",
            10.0,
            "Thrice Daily (After Food)",
            14,
            "Cough suppression",
            "active",
            "CASCADE INTERCEPTION (Pair K2): ACE-inhibitor-induced dry cough due to bradykinin accumulation secondary to Enalapril prescribed by Dr. Ramesh Sharma. Antitussives are ineffective; recommend switching to ARB (Telmisartan/Losartan).",
            "2026-10-13T16:25:00",
        ),
        # Meera Sen - Encounter 1 & 2: Metformin 500mg
        (
            "RX-3001A",
            "ENC-3001",
            "P003920",
            "DR0020",
            "D014",
            "metformin",
            "A10BA",
            500.0,
            "Twice Daily (After Meals)",
            60,
            "Glycemic control",
            "completed",
            "",
            "2026-09-03T11:20:00",
        ),
        (
            "RX-3002A",
            "ENC-3002",
            "P003920",
            "DR0020",
            "D014",
            "metformin",
            "A10BA",
            500.0,
            "Twice Daily (After Meals)",
            60,
            "Glycemic maintenance",
            "active",
            "",
            "2026-10-08T10:45:00",
        ),
    ]

    for rx in prescriptions:
        db.execute(
            "INSERT INTO prescriptions (rx_id, encounter_id, patient_id, doctor_id, drug_id, "
            "generic_name, atc_l4, dose_mg, frequency, duration_days, indication, status, warning_note, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (rx_id) DO UPDATE SET status = excluded.status, warning_note = excluded.warning_note",
            rx,
        )
    print(f"  [+] Seeded {len(prescriptions)} structured prescriptions with cascade warnings")

    # 5. Patient-reported symptoms
    symptoms = [
        ("P001142", "Bilateral feet and ankle swelling with warmth", "2026-10-12T08:30:00", "moderate", "amlodipine", "Noticed shoes getting tight in the evening since 10 days.", "under_review", "2026-10-12T09:00:00"),
        ("P002381", "Dry tickly cough keeping me awake at night", "2026-10-11T22:15:00", "mild", "enalapril", "No phlegm, constant throat tickle.", "under_review", "2026-10-12T07:30:00"),
    ]
    for sym in symptoms:
        db.execute(
            "INSERT INTO patient_symptoms (patient_id, symptom, onset_ts, severity, suspected_drug, notes, status, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            sym,
        )
    print(f"  [+] Seeded patient-reported pharmacovigilance symptom reports")

    print("[OK] Clinical database seeding successfully completed.")


if __name__ == "__main__":
    seed()
