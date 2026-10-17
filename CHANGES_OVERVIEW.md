# Summary of Changes Made to Cascade (IBM LinuxONE Edition)

This document provides a detailed manifest of all modifications and additions made to the repository after cloning the original upstream repository (`https://github.com/Vishant-1001/cascade.git`).

---

## 1. Quick Start: Applying the Changes

You can apply these changes in two ways:

### Option A: Extract Over the Cloned Repository (Recommended)
1. Clone the original repository:
   ```bash
   git clone https://github.com/Vishant-1001/cascade.git
   cd cascade
   ```
2. Unzip `cascade_changes_only.zip` directly into the repository root folder, allowing it to overwrite existing files.
3. Start the application:
   ```bash
   python start_frontend.py
   ```
   Open your browser at `http://127.0.0.1:8000`.

### Option B: Use Git Patch
1. If you prefer `git`, apply `changes.patch`:
   ```bash
   git apply changes.patch
   ```

---

## 2. Manifest of Modified & Added Files

### A. Frontend & UI Overhaul (`api/static/`)
- **`api/static/index.html`**:
  - Built an interactive clinical pharmacy counter & drug cascade surveillance dashboard.
  - Added IBM LinuxONE / s390x hardware acceleration telemetry panel (CPACF coprocessor status, SIMD vectorization stats, microsecond latency monitors).
  - Added live patient prescription timeline viewer, drug-cascade graph visualization, and explainability tabs.
  - Integrated demo audio narration, PDF generation triggers, and interactive scenario playback.
- **`api/static/style.css`**:
  - Over 1,800 lines of modern, responsive CSS for dark/light clinical themes, animated metric counters, real-time pulse indicators, and slide-out alert drawers.
- **`api/static/app.js`**:
  - Over 1,400 lines of JavaScript managing SSE (Server-Sent Events) live streaming, alert filtering, interactive SHAP waterfall breakdowns, candidate pair comparisons, and automated demo script stepping.
- **`api/static/evidence/`** (New Directory):
  - `architecture.png`: High-resolution dual-loop system architecture diagram.
  - `latency_distribution.png`: Benchmark latency distribution graph showing microsecond inference.
  - `linuxone_cpacf_evidence.png`: Terminal output verification of CPACF hardware coprocessor calls.
  - `lopo_recall.png`: Leave-One-Pair-Out validation recall curves.
  - `viewer.html`: Standalone evidence gallery viewer.

### B. Backend API & Telemetry (`api/`)
- **`api/main.py`**:
  - Configured FastAPI lifespan hooks, CORS middleware, and static asset routing.
- **`api/auth.py`**:
  - Added token validation and authorization security headers for protected endpoints.
- **`api/routes_alerts.py`**:
  - Endpoints to query, acknowledge, and resolve detected prescribing cascade alerts.
- **`api/routes_metrics.py`**:
  - Endpoints serving hardware telemetry metrics, CPACF crypto status, and inference throughput stats.
- **`start_frontend.py`** (New File):
  - One-click launcher script with automatic port discovery, uvicorn server startup, and browser launch.

### C. Datagen & Cascades Knowledge Base (`datagen/`, `data/`)
- **`datagen/catalogue.py`**:
  - Expanded cascade taxonomy with realistic clinical pairs (e.g., Amlodipine -> Peripheral Edema -> Furosemide).
  - Added drug indication maps, adverse reaction descriptions, and prescription frequency priors.
- **`datagen/generate.py`**:
  - Synthetic EHR trajectory generator incorporating temporal noise, multi-prescriber patterns, and confounders.
- **`datagen/inject.py`**:
  - Cascade injection mechanics for creating synthetic ground-truth evaluation sets.
- **`data/cascades_known.csv`**:
  - Curated reference cascade pairs with evidence citations.
- **`data/indications.csv`, `reactions.csv`, `treatments.csv`** (New Files):
  - Pharmacovigilance relational dictionaries for drug-indication-reaction lookup.

### D. Machine Learning: Loop 1 Inference & Models (`loop1/`)
- **`loop1/features.py`**:
  - Feature extraction engine calculating temporal intervals, dosage escalation markers, ASR scores, and co-medication counts.
- **`loop1/train.py`**:
  - Dual-model blended ensemble training pipeline (Gradient Boosted Trees via XGBoost + calibrated L2 Logistic Regression).
- **`loop1/score_service.py`**:
  - Sub-millisecond scoring microservice with batching support, caching, and LinuxONE SIMD/CPACF optimization.
- **`loop1/candidates.py` & `loop1/explain.py`**:
  - Candidate generation filters and feature contribution attribution for clinical explainability.
- **`loop1/model/`** (New Pre-trained Artifacts):
  - `m1.json` (XGBoost model), `m2.pkl` (Logistic Regression calibrator), `blend.json`, `feature_list.json`, `train_report.json`.
  - Ablation models: `no_asr/` (trained without ASR features) and `no_known/` (trained without known-pair prior).

### E. Evaluation, Reporting & Notebooks (`eval/`, `notebooks/`, `docs/`)
- **`eval/evaluate_loop1.py`**:
  - Evaluates models with AUROC, AUPRC, Recall@K, and Leave-One-Pair-Out cross-validation.
- **`eval/evaluate_genai.py`**:
  - Assesses LLM explanation coherence and accuracy against clinical ground-truth.
- **`eval/make_report.py`**:
  - Generates comprehensive markdown and JSON benchmark reports.
- **`eval/generate_notebooks.py`** (New File):
  - Script that synthesizes and pre-runs reproducible Jupyter research notebooks.
- **`notebooks/01_eda.ipynb`**, **`02_train.ipynb`**, **`03_eval.ipynb`**:
  - Pre-computed, fully rendered interactive notebooks documenting Exploratory Data Analysis, model training, and performance benchmarks.
- **`docs/evaluation.md` & `docs/eval/*.json`**:
  - Comprehensive documentation of all system metrics, latencies, and clinical validation outcomes.
- **`docs/screenshots/`**:
  - Benchmark plots and evidence logs.

### F. Multi-Architecture Deployment (`deploy/`, `tests/`)
- **`deploy/Containerfile.app`**:
  - Multi-stage container build targeting Debian/s390x (IBM LinuxONE) and x86_64 architectures.
- **`deploy/compose.yaml` & `deploy/run_all.sh`**:
  - Orchestration files for running the full stack with non-blocking SQLite/WAL writes and Redis caching.
- **`tests/test_api_auth.py`**, **`test_generate.py`**, **`test_scoring_cross_platform.py`**:
  - Cross-platform test suite verifying deterministic scoring between x86_64 and s390x.
- **`README.md`**:
  - Updated with LinuxONE architecture guides, live dashboard screenshots, and deployment instructions.

---

## 3. Included Zip Files

1. **`cascade_changes_only.zip`**:
   - Contains strictly the modified and added files with their relative directory hierarchy preserved.
   - Ideal for overlaying on top of `git clone https://github.com/Vishant-1001/cascade.git`.
2. **`cascade_full_updated_project.zip`**:
   - Complete updated repository (clean: no `.git`, no virtual environment, no large synthetic DB).
   - Can be unzipped and run directly anywhere.
3. **`cascade_ultrademo_walkthrough.zip`**:
   - Remotion walkthrough project, narration script (`SCRIPT_IBM_LINUXONE_CASCADE.md`), TTS automation, and recorded demo assets.
