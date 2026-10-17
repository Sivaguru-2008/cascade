# CASCADE: Quickstart & Technical Evaluation Guide

**Project:** CASCADE — Confidential AI Surveillance of Clinical Adverse Cascades & Drug Events  
**Built for:** IBM Z Datathon 2026  
**Team:** `sudo make us win`  
**Target Architectures:** IBM LinuxONE / s390x (Production) & x86_64 / arm64 (Cross-Platform Dev)  

---

## ⚡ 3 Ways to Evaluate CASCADE

Depending on your evaluation environment and available time, CASCADE provides three immediate pathways for technical assessment:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. Zero-Friction Dashboard (30 sec)    → python start_frontend.py           │
│ 2. Automated Scientific Audit (0.1 sec) → python -m eval.verify_all          │
│ 3. Enterprise LinuxONE Compose (5 min)  → bash deploy/run_all.sh             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Option 1: 1-Click Interactive Dashboard (Local Dev / Any OS)

Launch the full interactive clinical surveillance console with zero external dependencies (no Docker or Redis required — uses in-memory streams and SQLite with included pre-trained weights).

### 1. Install Python Dependencies
Requires Python 3.10, 3.11, or 3.12:
```bash
# Clone the repository
git clone https://github.com/Sivaguru-2008/cascade.git
cd cascade

# Install dependencies (virtual environment recommended)
pip install -r requirements.txt
```

### 2. Start the Frontend & Scoring Engine
```bash
python start_frontend.py
```

### 3. Open the Surveillance Dashboard
Open your web browser at:
👉 **`http://127.0.0.1:8000`**

### What to Explore in the UI:
- **Live Dispensing Stream:** Watch events stream into the pharmacy counter.
- **Microsecond Alert Drawer:** Click on any alert (e.g., *Amlodipine ➔ Furosemide*) to inspect feature attributions and clinical rationale.
- **Population Discovery (Loop 2):** View the real-time PSSA matrix and discovered unlisted cascades (such as the planted fictional novel pair `Novacor ➔ PPI`).
- **GenAI Clinical Dossier:** Generate a tamper-proof PvPI Adverse Drug Reaction (ADR) report and prescriber advisory note with zero hallucinations.
- **Hardware Telemetry:** Observe simulated or real IBM Z CPACF crypto co-processor throughput, SIMD vectorization, and memory safety stats.

---

## Option 2: 1-Click Scientific Audit Suite

Run our automated verification suite to independently substantiate all headline metrics in **0.07 seconds**:

```bash
python -m eval.verify_all
```

### Expected Output:
```text
============================================================================
CASCADE: Automated Verification Suite
   Built for IBM Z Datathon 2026 • Team sudo make us win
   Architecture: x86_64 / s390x | Python: 3.11.x | OS: Linux / Windows / macOS
============================================================================

[1/5] Verifying Clinical Catalogues & Ground Truth...
  [+] Known prescribing cascades: 9 peer-reviewed pairs loaded
  [+] Drug catalogue:             121 unique pharmaceutical products
  [+] Drug classes:               28 ATC Level 4 drug classes

[2/5] Verifying Loop 1 Real-Time Scorer & Benchmark Targets...
  [+] Operational Blend Precision: 0.815 (Target: >=0.80) -> PASS
  [+] Operational Blend Recall:    0.980 (Target: >=0.70) -> PASS
  [+] Operational F1 Score:        0.889 (PR-AUC: 0.9412)
  [+] Confusion Matrix: 48 TP, 11 FP, 1 FN, 290 TN

[3/5] Verifying Loop 2 Streaming PSSA & Novel Cascade (H1) Detection...
  [+] Novel Cascade H1 (Novacor -> PPI) detected at: Day 69 (Target: <=90 days) -> PASS
  [+] Benjamini-Hochberg FDR control: 14 true signals preserved

[4/5] Verifying GenAI Regulatory Dossier Clinical Validator...
  [+] Compliant note test: PASSED (0 validation errors)
  [+] Coercive / banned phrase rejection: PASSED (Successfully Blocked)

[5/5] Verifying Cryptographic Primitives & Confidentiality...
  [+] Zero-Knowledge Edge HMAC-SHA256 Tokenization: Deterministic & Irreversible
  [+] AES-256-GCM Authenticated Encryption at Rest: Verified

============================================================================
SUMMARY AUDIT SCORECARD
============================================================================
  [PASS]  Clinical Catalogues & Ground Truth
  [PASS]  Loop 1 Scorer (Precision >= 0.80, Recall >= 0.70)
  [PASS]  Loop 2 PSSA Novel Cascade Discovery (H1 <= 90d)
  [PASS]  GenAI Regulatory Clinical Validator
  [PASS]  Cryptographic Primitives & Confidentiality
============================================================================
ALL AUDIT BENCHMARKS VERIFIED SUCCESSFULLY (0.07s)
   Ready for technical judging evaluation.
```

---

## Option 3: Enterprise Mainframe Deployment (IBM LinuxONE / s390x)

To run the complete distributed multi-container architecture on an IBM LinuxONE Community Cloud instance:

### 1. SSH into the LinuxONE s390x Instance
```bash
ssh linux1@<LINUXONE_IP>
```

### 2. Clone & Launch Enterprise Stack
```bash
git clone https://github.com/Sivaguru-2008/cascade.git
cd cascade

# Generates zero-trust secrets, builds s390x containers, and starts services
bash deploy/run_all.sh
```

### 3. Generate Hardware Co-processor Audit Evidence
Run the hardware verification script to inspect Linux kernel crypto drivers, `/proc/cpuinfo` `msa` flags, and OpenSSL hardware AES-256-GCM performance:
```bash
bash deploy/evidence.sh
```
This script audits:
- Host architecture: `uname -m` ➔ `s390x`
- Kernel drivers: `gcm-aes-s390` (priority 900 hardware coprocessor)
- Hardware acceleration flags: `msa`, `vx`, `vxp`, `sort`, `dflt`, `nnpa`
- Role-based separation: Confirms regulator keys cannot decrypt patient-level PII (403 Forbidden).

---

## 🧪 Full Unit & Integration Test Suite

CASCADE includes comprehensive automated testing:

```bash
# Run all 87 unit and integration tests
pytest -v
```

All 87 tests cover:
- Cross-platform numerical reproducibility across big-endian (`s390x`) and little-endian (`x86_64`) machines.
- HMAC-SHA256 edge tokenization and Zero-Knowledge leakage prevention.
- Benjamini–Hochberg False Discovery Rate (FDR) control for streaming PSSA.
- GenAI zero-hallucination constraint validation.
- RBAC security policies and audit logging.

---

## 👥 Team `sudo make us win`
- **Submission:** IBM Z Datathon 2026
- **Repository:** [https://github.com/Sivaguru-2008/cascade](https://github.com/Sivaguru-2008/cascade)
- **Documentation:** See [`README.md`](../README.md), [`BENCHMARK_METHODOLOGY.md`](BENCHMARK_METHODOLOGY.md), and [`EVALUATION_AUDIT.md`](EVALUATION_AUDIT.md).
