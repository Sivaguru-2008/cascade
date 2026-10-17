# Build prompt: CASCADE (IBM Z Datathon 2026, Team sudo make us win)

> **How to use this prompt.** Paste everything below the line into your AI coding agent (Claude Code, Cursor, Copilot agent, etc.) **after the Datathon kickoff on 17 Oct 2026**. The rules forbid project code written before kickoff. Run the agent once per phase: tell it "Do Phase N only", review its output, commit, then continue. Do not let it run all phases unattended. Keep this file in the repo as `docs/AGENT_PROMPT.md`.

---

## 0. Your role and the non-negotiable rules

You are the lead engineer building **CASCADE**, a real-time AI system for a 24-hour hackathon (IBM Z Datathon 2026). You write production-quality, runnable code with tests, and you work in small, verifiable steps.

Follow these rules at all times:

1. **No fabricated results.** Never write a metric, latency figure, accuracy number or claim into code comments, README, slides or logs unless a script in this repo produced it. Where a number is not yet measured, write `TBD (run eval/…)`.
2. **No fabricated capabilities.** Do not claim the system uses IBM Secure Execution, the Telum AI accelerator, quantum-safe crypto or any IBM product unless code in this repo actually calls it and you have shown it running. Mark optional items "stretch" until they run.
3. **No clinical overreach.** Alerts and generated text say "possible" and "please review". They never tell anyone to stop, start or change a drug.
4. **Synthetic data only.** No real patient data anywhere. The hidden "discovery" drug is fictional (`Novacor`) so we make no false claim about a real drug.
5. **s390x first.** The final system must run on an IBM LinuxONE Community Cloud VM (architecture `s390x`, Linux, Podman or Docker). Prefer pure-Python or widely multi-arch dependencies. When a package fails to install on s390x, switch to the listed fallback within one attempt and record the switch in `docs/DECISIONS.md`.
6. **Small steps, always runnable.** After every phase, `./deploy/run_all.sh` (or the phase's test command) must succeed. Never leave the main branch broken.
7. **Explain decisions briefly** in `docs/DECISIONS.md` (one line each: date-time, decision, reason).
8. **Attribution.** List every open-source library and dataset with its link and licence in `README.md` → "Third-party resources". The hackathon rules require it.
9. **Ask, don't guess, when something is ambiguous** and the choice is hard to undo. Otherwise pick the simplest option that meets the spec and note it in DECISIONS.md.

---

## 1. What CASCADE is (product context)

A **prescribing cascade** happens when a drug's side-effect is mistaken for a new illness and treated with a second drug. Example: amlodipine causes ankle swelling, and the patient is then given furosemide. The two prescriptions often come from different doctors, so no single doctor sees the pattern. The **pharmacy dispensing stream** sees both.

CASCADE has three parts that run on one LinuxONE VM:

- **Loop 1, real-time scorer.** For every dispensing event, decide in milliseconds whether the newly started drug may be treating a side-effect of a drug the patient started recently. If so, alert the pharmacist with reasons.
- **Loop 2, population signal engine.** Run streaming Prescription Sequence Symmetry Analysis (PSSA) across all pharmacies. Flag drug-class pairs where B starts after A significantly more often than before A, including pairs on no published list. Its live statistics feed Loop 1 as a feature.
- **Generative layer.** Turn a confirmed alert into (a) a short note to the prescriber and (b) a pre-filled draft of India's PvPI Suspected ADR Report. A validator checks every output, with a deterministic template fallback. A human approves; nothing is sent automatically.

Plus a privacy-by-design layer: HMAC tokenisation at the pharmacy edge, encryption at rest and in transit, role-separated views, small-cell suppression and an audit log.

### The demo must prove three things

1. **Real-time interception:** event in → alert with reasons on the dashboard in under one second, scored on the LinuxONE VM.
2. **Discovery:** Loop 2 flags the planted fictional `Novacor → PPI` cascade, which is in no list and has no training labels.
3. **Closing the reporting gap:** one click makes a validated prescriber note + PvPI draft; approve exports PDF + JSON.

---

## 2. Architecture (build exactly this)

```
[edge: pharmacy simulator] --tokens only--> [Redis: stream:dispense, patient state, pair counts]
        |                                         |                        |
        |                                  [scorer: Loop 1] <--live aSR-- [signals: Loop 2]
        |                                         |                        |
        |                                  [genai: note + PvPI draft] --> [PostgreSQL: encrypted rows]
        |                                                                  |
        +---------------------------------------- [api + dashboard: FastAPI, role-based keys]
                                                   |                       |
                                             Pharmacist view        Regulator view (aggregates only)
```

All services except `edge` run as separate containers on the VM; `edge` may run there too for the demo.

### Services

| Service | Container name | Responsibility |
|---|---|---|
| edge | cascade-edge | Replays synthetic events in time order; HMAC-tokenises patient + prescriber IDs; publishes to `stream:dispense` |
| scorer | cascade-scorer | Loop 1; consumes `stream:dispense`; writes alerts to `stream:alerts` + Postgres `alerts`; logs latency |
| signals | cascade-signals | Loop 2; consumes `stream:dispense`; maintains pair counts; recomputes stats; writes Redis (for scorer) + Postgres `signals` |
| genai | cascade-genai | Builds note + PvPI draft on request; validates; falls back to template |
| api | cascade-api | REST + Server-Sent Events; serves the static dashboard; role-based API keys; audit log |
| redis, postgres | official images | Bus, state, storage |

### Tech stack and s390x fallbacks

| Layer | Primary | Fallback if it fails on s390x |
|---|---|---|
| Runtime | Python 3.11+, Podman or Docker, `compose.yaml` | Plain processes under `tmux` via `run_all.sh` |
| Bus/state | Redis 7 (Streams + hashes) | `asyncio.Queue` in a single-process mode (`CASCADE_MODE=single`) |
| DB | PostgreSQL 15+ | SQLite (`CASCADE_DB=sqlite`) |
| ML | XGBoost (or LightGBM), scikit-learn, SHAP | scikit-learn `HistGradientBoostingClassifier`; reasons from logistic-regression coefficient × value |
| Stats | NumPy, SciPy (`scipy.stats.beta` for Clopper–Pearson) | `statsmodels.stats.proportion.proportion_confint(method="beta")` |
| API | FastAPI + uvicorn | Flask |
| GenAI | Same stack as the IBM "Predictive + Generative AI" lab → small open instruct model (e.g. IBM Granite small) via `transformers` or `llama.cpp` | Jinja2 template (always present) |
| Dashboard | Static HTML + vanilla JS + Chart.js from CDN, served by FastAPI | — |
| PDF export | `reportlab` | HTML download |
| Crypto | `cryptography` (AES-256-GCM, HMAC-SHA256; OpenSSL uses on-chip CPACF on IBM Z) | — |

**Do not use Streamlit** (its `pyarrow` dependency is risky on s390x). Make every backend switch an environment variable, so a fallback is a config change, not a rewrite.

---

## 3. Repository layout (create exactly this)

```
cascade/
  README.md
  LICENSE                      # Apache-2.0
  .gitignore                   # include .env, secrets/, data/generated/, *.key
  .env.example                 # all env vars with safe defaults, no secrets
  docs/
    AGENT_PROMPT.md            # this prompt
    DECISIONS.md
    evaluation.md              # written ONLY by eval scripts
    architecture.png           # exported later
    screenshots/               # LinuxONE evidence
  data/
    drug_catalogue.csv
    cascades_known.csv         # with a source column for every row
    pvpi_form_fields.json
    faers_cache.json           # optional, fetched by a script
    generated/                 # git-ignored: events.parquet/csv, labels.csv
  datagen/
    catalogue.py  generate.py  inject.py  replay.py  synthea_map.py  faers_fetch.py
  common/
    config.py  schemas.py  crypto.py  bus.py  db.py  logging.py
  edge/pharmacy_edge.py
  loop1/
    features.py  candidates.py  train.py  score_service.py  explain.py  model/
  loop2/
    pssa.py  stream_counts.py  signal_service.py
  genai/
    prompts/note.txt  prompts/pvpi.txt  generate.py  validate.py  templates/note.j2  templates/pvpi.j2  pdf.py
  api/
    main.py  auth.py  routes_alerts.py  routes_signals.py  routes_metrics.py  sse.py
    static/index.html  static/app.js  static/style.css
  eval/
    evaluate_loop1.py  evaluate_loop2.py  evaluate_genai.py  latency_report.py  make_report.py
  tests/
    test_pssa.py  test_candidates.py  test_features.py  test_validate.py  test_crypto.py  test_api_auth.py  test_end_to_end.py
  deploy/
    Containerfile.app  compose.yaml  run_all.sh  init_db.sql  linuxone_setup.md  evidence.sh
  notebooks/
    01_eda.ipynb  02_train.ipynb  03_eval.ipynb
```

One `Containerfile.app` builds a Python image used by all app services, with a different `CMD` per service.

---

## 4. Data specification

### 4.1 Drug catalogue (`data/drug_catalogue.csv`)

Columns: `drug_id, generic_name, atc_code, atc_l4, atc_l3, is_marker_class, typical_daily_dose_mg`.

- Include about 120 commonly dispensed drugs. They must cover every drug in the cascade table below, plus common chronic medicines (antihypertensives, antidiabetics, statins, antiplatelets, PPIs, analgesics, antibiotics, antidepressants, antipsychotics, antiparkinson agents, urological drugs, antihistamines, laxatives, hypnotics).
- Take ATC codes from the WHO ATC index. If you are unsure of a code, leave a `# VERIFY` note in a separate `data/CATALOGUE_METHODOLOGY.md`. Do not invent codes.
- Add the fictional drug `Novacor` with `drug_id=DNOVA`, `atc_code=C09XX99` (clearly fake, flagged fictional) and `atc_l3=C09` (renin–angiotensin agents).
- `is_marker_class = 1` for: loop and thiazide diuretics, antitussives, antiparkinson agents, anticholinergics, urinary antimuscarinics, antiemetics, vestibular sedatives, laxatives, PPIs, antihistamines, potassium supplements, allopurinol, hypnotics.

### 4.2 Known cascades (`data/cascades_known.csv`)

Columns: `pair_id, index_drug, index_atc_l4, marker_drug, marker_atc_l4, reaction, lag_min_days, lag_max_days, p_cascade, source`.

| pair_id | Index (A) | Marker (B) | Reaction | Lag (days) | p_cascade | Source |
|---|---|---|---|---|---|---|
| K1 | Amlodipine | Furosemide | Ankle oedema | 7–90 | 0.06 | ThinkCascades (McCarthy et al., Drugs & Aging 2022) |
| K2 | Enalapril / ramipril | Antitussive | Dry cough | 7–60 | 0.05 | PSSA literature |
| K3 | Metoclopramide | Levodopa | Drug-induced parkinsonism | 14–120 | 0.04 | Classic cascade (Rochon) |
| K4 | Haloperidol | Trihexyphenidyl | Extrapyramidal symptoms | 3–60 | 0.07 | Prescribing-cascade case literature |
| K5 | Donepezil | Oxybutynin | Urinary incontinence | 14–120 | 0.04 | ThinkCascades |
| K6 | Tamsulosin | Betahistine | Dizziness | 3–60 | 0.03 | PIPC list (Eur Geriatr Med 2025) |
| K7 | Pregabalin / gabapentin | Furosemide | Oedema | 14–90 | 0.04 | ThinkCascades |
| K8 | Hydrochlorothiazide | Allopurinol | Gout / hyperuricaemia | 30–180 | 0.03 | PSSA literature |
| K9 | Diclofenac | New antihypertensive | Raised BP | 14–90 | 0.03 | PSSA literature |

**The team will verify each row against its source before use.** Include the table as given, but put `VERIFY:` in front of each source string until a human removes it.

### 4.3 Base population (`datagen/generate.py`)

Implement a **pure-Python generator** first, because it is fully controllable. Synthea import (`datagen/synthea_map.py`) is optional and only used if the team provides Synthea CSVs.

- 20,000 patients (`--patients` flag), 3 simulated years starting 2023-01-01.
- 50 pharmacies and 300 prescribers. Each patient has a home pharmacy (90% of dispenses) and 1–4 prescribers.
- Age distribution: 40% aged 60+. Sex is about 50/50.
- 1–4 chronic conditions per patient, sampled by age band. Each condition has a list of first-line drugs; patients start them at random dates.
- Repeat dispenses every `days_supply` (30) ± 3 days while the drug is active. Each drug has a 10%/year discontinuation chance.
- **Background starts.** Every drug, including marker drugs, also starts for unrelated reasons at a base rate (0.5–3% per patient-year by class). These happen before and after index drugs, so coincidental pairs exist. That is essential for honest evaluation.
- Seeded with `--seed` for reproducibility.

### 4.4 Injection (`datagen/inject.py`)

For each known pair: for each patient who has an **incident** start of drug A (no A in the prior 365 days), with probability `p_cascade` schedule an incident start of drug B at `A_start + lag`. Draw the lag from a gamma distribution clipped to `[lag_min, lag_max]`. The B prescription comes from a **different prescriber** with 65% probability.

Write the label to `data/generated/labels.csv`: `event_id, is_cascade=1, cascade_index_drug=A, pair_id`.

**Hidden cascade.**
- `Novacor` launches on 2025-07-01. From then on, it is prescribed to new antihypertensive starters at a ramping market share (0% → 15% over 6 months).
- Inject `Novacor → pantoprazole` at `p=0.06`, lag 7–45 days, with `pair_id=H1`.
- H1 labels go to a **separate** file, `labels_hidden.csv`. That file is never used for training, and `Novacor` is not in `cascades_known.csv`.

**Generator assertions (fail loudly):**
- each known pair has ≥ 150 injected events;
- H1 has ≥ 80;
- no label is attached to an event that isn't an incident start.

### 4.5 Event schema (`common/schemas.py`, Pydantic)

`DispenseEvent`:

| Field | Type | Notes |
|---|---|---|
| event_id | str (UUID) | |
| ts | datetime | |
| pharmacy_id | str | |
| patient_token | str (64 hex) | HMAC output |
| prescriber_token | str (64 hex) | HMAC output |
| age_band | str | e.g. "70-74" |
| sex | "M" \| "F" \| "U" | |
| drug_id, atc_code, atc_l4, atc_l3 | str | |
| qty, days_supply | int | |
| dose_mg | float | |

Labels are **never** fields of `DispenseEvent` and never travel on the bus.

### 4.6 Replay (`datagen/replay.py` + `edge/pharmacy_edge.py`)

- Reads generated raw events (with raw `patient_id`, `prescriber_id`) sorted by `ts`. Tokenises each with `HMAC-SHA256(key=EDGE_SECRET, msg=id)` and publishes a `DispenseEvent` JSON to `stream:dispense`.
- Speed modes: `--speed days_per_sec=1` (default), `--speed max` (as fast as possible, for throughput tests).
- **`--demo-script` mode** plays a curated sequence:
    1. A warm-up of history so Loop 2 has statistics.
    2. A slowed segment (one event per second) containing a K1 amlodipine → furosemide cascade event.
    3. A coincidental A→B event that is **not** a cascade (label 0) and that Loop 1 must not flag. Pick one the trained model scores below threshold; if none exists, report that honestly.
    4. Fast-forward through the Novacor ramp until Loop 2 flags H1.

  It writes `docs/demo_script_events.json` listing the chosen event IDs.

### 4.7 FAERS cache (`datagen/faers_fetch.py`, optional)

- Queries openFDA (`https://api.fda.gov/drug/event.json`) for each known pair's index drug + reaction term. Store counts in `data/faers_cache.json` with the query URL and fetch date.
- Rate-limit and handle errors; the system must work without this file.
- Novacor is fictional and gets no FAERS entry; the UI says "no external data (fictional drug)".

---

## 5. Loop 1: real-time scorer

### 5.1 Patient state (Redis hash `pstate:{patient_token}`)

Maps `drug_id → {first_ts, last_ts, prescriber_token, last_dose_mg, prev_dose_mg}`. Also store `class:{atc_l4} → last_ts` for the incident check. Lookups and updates are O(1).

### 5.2 Algorithm per event

1. Update state.
2. **Incident check.** If the patient had any dispense of the same `atc_l4` in the prior 365 days, it is not incident. Return with no score, but still record latency.
3. **Candidates.** Every drug A whose `first_ts` is within the last 180 days and whose `atc_l4` differs from B's.
4. Build a feature row per (A → B) pair (5.3), score it (5.4), and keep the max-scoring pair.
5. If `score ≥ THRESHOLD`, write an alert (5.5).
6. Record `latency_ms` = time from message read to alert write, or to the no-alert decision.

### 5.3 Features (`loop1/features.py`)

| Feature | Notes |
|---|---|
| days_since_A_start | int |
| A_atc_l3, B_atc_l3 | categorical; one-hot or native categorical |
| B_is_marker_class | 0/1 |
| pair_in_known_list | 0/1, class-level match against `cascades_known.csv`; **must be switchable off** via `--no-known-prior` |
| pair_aSR_live, pair_aSR_lower_ci | from Loop 2's Redis keys; default 1.0 / 0.0 when missing |
| different_prescriber | 0/1 |
| n_active_drugs | drugs dispensed within days_supply + 15 days |
| age_band_ordinal | int |
| sex | categorical |
| A_dose_increase_30d | 0/1 |
| B_started_before_A_ever | 0/1 |

Training must compute `pair_aSR_live` **as it was at that event's time**, by replaying Loop 2 over the training period. Never use end-of-period statistics; that would leak future information. Write a test that proves this (`tests/test_features.py`).

### 5.4 Models (`loop1/train.py`)

- **Training set:** all candidate pairs generated by replaying simulated years 1–2.
- **Label:** 1 if B's event is labelled cascade **and** `cascade_index_drug == A`, else 0.
- **Split by time:**
    - train = years 1–2;
    - validation = 2025-01-01 → 2025-06-30;
    - test = 2025-07-01 → 2025-12-31.
- **Models:**
    - M1: XGBoost (or fallback), with class weighting for imbalance;
    - M2: logistic regression (standardised features), the baseline.
- **Final score:** `0.6·M1 + 0.2·sigmoid(log(max(aSR_lower_ci, 1e-3))) + 0.2·pair_in_known_list`. Tune these weights on validation only and save them to `loop1/model/blend.json`.
- **Threshold:** the lowest threshold whose validation precision is ≥ 0.80. Save it to `blend.json`. If precision 0.80 is unreachable, choose the best-F1 threshold and say so in DECISIONS.md.
- **Save:** `m1.json` (or equivalent), `m2.pkl`, `feature_list.json`, `blend.json`, `train_report.json` (sizes, class balance, validation metrics).
- **Train twice:** with and without `pair_in_known_list` (the ablation). Report both.

### 5.5 Alert payload (Pydantic `Alert`)

`alert_id, event_id, ts, pharmacy_id, patient_token, index_drug, marker_drug, index_atc_l4, marker_atc_l4, days_between, score, reasons[3], pair_aSR, pair_aSR_ci, known_source | null, status="open"`

`reasons` are the top 3 SHAP contributions from M1, in human words. For example: "Started 18 days after amlodipine", "Furosemide is a common side-effect treatment", "Population signal aSR 3.4 (2.6–4.5)". Fall back to M2 coefficient × value if SHAP is unavailable.

### 5.6 Service (`loop1/score_service.py`)

- Async consumer group on `stream:dispense`.
- Loads the models once at startup.
- `POST /score` (internal) accepts one `DispenseEvent` and returns the decision. It is used by the demo and the latency test.
- Writes latency rows to Postgres `latency(event_id, ms, alerted)`.

**Stretch (only if IBM SMEs confirm accelerator access on the VM):** export M1 to ONNX, compile with the IBM Z Deep Learning Compiler, and compare latency. Claim it only if it ran.

---

## 6. Loop 2: streaming PSSA

### 6.1 Definitions (`loop2/pssa.py`, pure functions, fully unit-tested)

- **Unit:** ATC level-4 class pairs (A, B), A ≠ B. Incident start = first dispense of a class with no dispense of that class in the prior 365 days. Skip each patient's first 365 simulated days (washout).
- **Window W** ∈ {30, 90, 180}; the default for display is 90.
- **n_AB:** patients whose incident A precedes incident B by 1..W days. **n_BA:** the reverse. Ties (same day) are ignored.
- **Crude sequence ratio:** cSR = n_AB / n_BA.
- **Null-effect sequence ratio,** from daily population incidence counts a(t), b(t):
  `nSR = Σ_t a(t)·Σ_{u=t+1}^{t+W} b(u)  /  Σ_t a(t)·Σ_{u=t−W}^{t−1} b(u)`
  Implement it with NumPy cumulative sums, so each window sum is O(1).
- **Adjusted sequence ratio:** aSR = cSR / nSR.
- **95% CI:** Clopper–Pearson interval [p_lo, p_hi] for p = n_AB / (n_AB + n_BA). Convert to ratios r = p / (1 − p), then divide by nSR.
- **Signal:**
    - n_AB + n_BA ≥ 10;
    - the lower CI of aSR > 1 at W = 90;
    - also true at W = 30 or W = 180.
- **Status:**
    - `known` if the pair matches `cascades_known.csv` (class level);
    - `corroborated` if not known but `faers_cache.json` has supporting counts;
    - else `new`.
- **Stretch:** Benjamini–Hochberg FDR at q = 0.05 over all tested pairs. Show the signal list both with and without it.

### 6.2 Streaming (`loop2/stream_counts.py`, `signal_service.py`)

- On each incident event (patient P, class X, day d):
    - for every class Y that P started incidently in [d − W, d), increment `n[(Y, X), W]` for each W;
    - increment the daily incidence `inc[X][d]`.
- Pairs where X came first get counted when the later event arrives, so each patient pair is counted exactly once.
- Keep per-patient incident history in Redis `pinc:{token}` (class → first incident day). Counts live in Redis hashes `pair:{A}:{B}`; daily incidence lives in NumPy arrays in the service, snapshotted to disk every minute.
- **Recompute** nSR, aSR and CI for changed pairs every 5,000 events or 10 s, whichever comes first. Write:
    - Redis `asr:{A}:{B}` = `{asr, lo, hi, n}`, read by Loop 1;
    - Postgres `signals` snapshot rows, used for the history chart.
- **Unit tests** (`tests/test_pssa.py`):
    - symmetric random data → aSR ≈ 1 and no signal;
    - injected A→B asymmetry → signal;
    - a growing B trend alone → nSR > 1 and aSR ≈ 1 (trend correction works);
    - the batch implementation and the streaming implementation give identical counts on the same data.

---

## 7. Generative layer (`genai/`)

### 7.1 Input contract (the only data the model sees)

```json
{"alert_id":"AL-000123","index_drug":"Amlodipine 5 mg","marker_drug":"Furosemide 40 mg",
 "days_between":18,"suspected_reaction":"Ankle oedema",
 "evidence":{"score":0.91,"aSR":3.4,"aSR_ci":[2.6,4.5],"known_source":"ThinkCascades"},
 "patient":{"age_band":"70-74","sex":"F","n_active_drugs":7},
 "different_prescriber":true}
```

- `suspected_reaction` comes from `cascades_known.csv`. For `new` signals it is `"unknown — pattern detected statistically"`.
- **Never** pass tokens, pharmacy IDs or prescriber IDs to the model.

### 7.2 Outputs

**Prescriber note.**
- At most 90 words, from the pharmacist, polite.
- States both drugs, the gap in days, the suspected reaction and the evidence source.
- Ends with a request to review. Never uses "stop", "discontinue", "diagnose", "definitely" or "must".

**PvPI draft.** JSON keyed to `data/pvpi_form_fields.json`. The team fills that file from the current official PvPI Suspected ADR Reporting Form. Until then, use these sections:
- A, Patient information;
- B, Suspected adverse reaction;
- C, Suspected medication(s);
- concomitant medications;
- D, Reporter details.

Fields we cannot know (initials, weight, batch number, manufacturer, outcome, seriousness, reporter details) are set to `null` and rendered as "To be completed by reporter".

### 7.3 Generation (`genai/generate.py`)

- Backend chosen by `GENAI_BACKEND` = `lab` | `transformers` | `llamacpp` | `template`.
- Low temperature (≤ 0.2), with max tokens capped.
- Timeout at 10 s, then fall back to the template.
- Every output passes through the validator; on rejection, fall back to the template.
- Store `drafts(alert_id, note_text, report_json, generator, validator_pass, reject_reasons, gen_ms, created_ts)`.

### 7.4 Validator (`genai/validate.py`, unit-tested)

Reject the output when any of these is true:
- a drug name, number or date in the output is not present in the input JSON;
- the output contains a banned phrase (the list in 7.2);
- the JSON fails its Pydantic schema;
- the note is longer than 90 words.

Return `(ok, reasons[])`.

### 7.5 Export (`genai/pdf.py`)

The Approve action renders the note and PvPI draft into a one-page PDF, labelled "DRAFT — synthetic demo data — requires reporter completion". It also returns the JSON.

---

## 8. Privacy and security (`common/crypto.py`, `api/auth.py`)

1. **HMAC-SHA256 tokenisation** at the edge. `EDGE_SECRET` lives only in the edge's environment, never in the server's.
2. **Field minimisation:** age band only; no names, phone numbers or addresses exist anywhere in the schema.
3. **AES-256-GCM encryption at rest** for `alerts.enc_blob` and the draft contents. The key comes from env var `DATA_KEY` (generated by `deploy/run_all.sh` into `secrets/`, which is git-ignored). Show that a raw DB row is ciphertext.
4. **TLS** for the API: a self-signed cert generated by `run_all.sh`. If supported, also enable TLS on Redis/Postgres connections; otherwise note in DECISIONS.md that they are bound to localhost on the VM.
5. **Roles:** API keys `PHARMACIST_KEY_<pharmacy_id>` and `REGULATOR_KEY`.
    - The pharmacist sees only their own pharmacy's alerts.
    - The regulator sees only `/signals*` and `/metrics`, with any count below 5 shown as "<5".
    - Every other access gets a 403.
    - Write tests for each rule.
6. **Containers:** run as a non-root user, read-only root filesystem where possible, and expose only the API port.
7. **Audit log:** every alert view, draft, approve, dismiss and export is written to `audit(ts, role, action, object_id)`.
8. **Evidence script** (`deploy/evidence.sh`): prints the following, saved to `docs/screenshots/evidence.txt`:
    - `uname -m`;
    - `podman ps` (or docker);
    - CPU crypto feature flags from `/proc/cpuinfo` (look for `msa` / CPACF-related flags; print what's there, don't assume);
    - `openssl version` and `openssl speed -evp aes-256-gcm -seconds 3`;
    - one encrypted DB row;
    - a curl showing the 403 for the regulator key on an alert endpoint.

---

## 9. API (`api/`)

| Method | Path | Role | Behaviour |
|---|---|---|---|
| POST | /score | pharmacist | Score one event (demo, latency tests) |
| GET | /alerts?since=&status= | pharmacist | Own pharmacy only |
| GET | /alerts/{id} | pharmacist | Detail + reasons + evidence |
| POST | /alerts/{id}/draft | pharmacist | Create or return note + PvPI draft |
| POST | /alerts/{id}/approve | pharmacist | Mark approved; return PDF + JSON |
| POST | /alerts/{id}/dismiss | pharmacist | Body `{reason}`; stored as a feedback label |
| GET | /signals?status=&window= | regulator | Ranked by lower CI; small cells suppressed |
| GET | /signals/{a}/{b}/history | regulator | aSR + CI time series |
| GET | /metrics | both | Events/s, latency p50/p95/p99, alert counts, validator pass rate, generator mix |
| GET | /stream | both | SSE: new events (pharmacist: own pharmacy), new alerts, signal changes |
| GET | / | public | Dashboard (keys entered in UI, kept in memory only) |

Use the OpenAPI docs at `/docs`.

---

## 10. Dashboard (`api/static/`)

One page with a clean, clinical look: white background, a single blue accent `#1f5fbf`, Inter or system sans-serif, and no gradients. The layout has three regions.

1. **Top strip (system).** Events/s, scoring latency p95 (live), `Architecture: s390x` (from `/metrics`, which reads `platform.machine()`), encryption status, validator pass rate. Show only measured values; show "—" until data exists.
2. **Left panel: Pharmacy counter (role pharmacist).**
    - A live feed of dispensing events (drug, time, pharmacy) as a compact list.
    - When an alert arrives, a card slides in showing: the two drugs, "started N days after", the score, three reasons, a "Known: ThinkCascades" or "Population signal" badge, and a **Draft note** button.
    - The drawer shows:
        - the prescriber note;
        - the PvPI draft rendered as the form, with unknown fields highlighted amber;
        - which generator produced it (LLM or template);
        - **Approve & export** and **Dismiss** buttons.
3. **Right panel: Population signals (role regulator).**
    - A ranked table: pair (drug classes and example drugs), aSR [95% CI], n, window, and a status chip (known / corroborated / **new**).
    - Clicking a row opens a Chart.js line chart of aSR over simulated time, with a CI band and a dashed line at 1.
    - When H1 (`Novacor → PPI`) becomes a signal, its row highlights and a small toast says "New signal: not on any known list".

Include a **role switcher** for the demo (two keys) and a **"Run demo script"** button that calls an admin endpoint to start `replay --demo-script`. That endpoint is enabled only when `DEMO_MODE=1`.

The dashboard must be keyboard-navigable and readable at 1280×720, which is the video-recording size.

---

## 11. Evaluation (`eval/`): every number in the video comes from here

All eval scripts run on the LinuxONE VM. `make_report.py` writes `docs/evaluation.md` with a timestamp, git commit hash and `uname -m`.

**Loop 1** (`evaluate_loop1.py`, on the time-split test set):
- precision, recall, F1, PR-AUC and the confusion matrix at the saved threshold, for M1, M2 and the blend;
- per-pair recall for K1–K9;
- ablation without `pair_in_known_list`;
- ablation without the `pair_aSR_*` features;
- false-alert rate on coincidental A→B events (label 0).

**Loop 2** (`evaluate_loop2.py`):
- a detection table for K1–K9 + H1: flagged? aSR [CI] at W = 90, n, first simulated date crossing the threshold;
- days from Novacor launch to the H1 signal at each window;
- false-signal count (flagged pairs not injected), with and without BH;
- a **null run:** regenerate with zero injections (`--no-inject`), run Loop 2, and report the number of signals.

**GenAI** (`evaluate_genai.py`):
- drafts for 50 test alerts;
- validator pass rate, template fallback rate, and mean/p95 generation time;
- 3 verbatim examples;
- a CSV for a 10-draft human review (columns: usable / needs edit / wrong). Humans fill it; the script never fills it.

**System** (`latency_report.py`):
- replay at `--speed max` for 5 minutes;
- report sustained events/s and latency p50/p95/p99 from the `latency` table.

Wherever a target was missed, the report states it plainly. Targets:
- Loop 1 precision ≥ 0.80;
- recall ≥ 0.70;
- H1 detected;
- latency measured.

---

## 12. Deployment on LinuxONE (`deploy/`)

- `deploy/linuxone_setup.md`: step-by-step for the team.
    - SSH into the L1CC VM.
    - Install podman or docker, python3.11, git, build tools.
    - Clone the repo; run `./deploy/run_all.sh`.
    - Note the event code SSF2026 in the L1CC project description.
- `deploy/run_all.sh` is idempotent. It:
    1. checks the architecture and prints it;
    2. creates `secrets/` (EDGE_SECRET, DATA_KEY, API keys, TLS cert);
    3. builds the image;
    4. starts redis + postgres;
    5. runs `init_db.sql`;
    6. starts the app services;
    7. waits for health checks;
    8. prints the dashboard URL and keys.

  Flags:
    - `--single-process`: for the Redis/Postgres fallback;
    - `--generate`: runs the data generator first;
    - `--train`: trains the models first.
- `compose.yaml`: health checks on every service; `restart: unless-stopped`.
- Every service exposes `/health`.

---

## 13. Tests and quality bar

- `pytest` must pass before every commit.
- Required tests:
    - PSSA maths (6.2);
    - candidate generation and incident logic;
    - time-correct features (no leakage);
    - validator rules;
    - crypto round-trip and HMAC determinism;
    - API role rules (403s);
    - end-to-end: replay 2,000 events in single-process mode → at least one alert and a non-empty signals table.
- Type hints everywhere; `ruff` clean. Log to stdout as JSON lines.
- No secrets in git. Run `git grep -nE "(SECRET|KEY)=" -- . ':!*.example'` before each commit and expect empty output.

---

## 14. Build phases (do ONE phase per run, then stop and report)

Each phase ends with:
1. the commands to verify it;
2. what was done;
3. anything that failed or was swapped to a fallback;
4. the files touched.

Commit after each phase with message `phase N: <summary>`.

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | Repo skeleton, config, schemas, `.env.example`, README stub, LICENSE, `run_all.sh` skeleton, health-check FastAPI | `pytest` passes; `uvicorn api.main:app` serves `/health` |
| 1 | Drug catalogue, cascades file, generator, injector, labels, assertions | `python -m datagen.generate --patients 2000 --seed 7` succeeds and prints per-pair injected counts |
| 2 | Edge + replay + Redis bus (+ single-process fallback) | Events visible on `stream:dispense`; tokens are 64-hex; no raw IDs on the bus |
| 3 | Loop 2 batch + streaming PSSA, with tests | `test_pssa.py` passes; batch PSSA on generated data flags K1 |
| 4 | Loop 1 features, training, saved models, train report | Validation metrics printed; models saved; leakage test passes |
| 5 | Loop 1 scoring service, alerts, latency logging, Loop 2 → Loop 1 feed | End-to-end test produces alerts with reasons |
| 6 | GenAI: template first, then LLM backend, validator, PDF | Draft endpoint returns a validated note + PvPI JSON; validator tests pass |
| 7 | Security: crypto at rest, TLS, roles, audit, evidence script | Role tests pass; `evidence.sh` output is saved |
| 8 | Dashboard: three panels, SSE, demo-script button | Demo flow runs start to finish in the browser |
| 9 | Containers + compose + LinuxONE deployment guide | `run_all.sh` brings everything up on the VM; `uname -m` = s390x shown on the dashboard |
| 10 | Evaluation scripts + `docs/evaluation.md` on the VM | Report generated from real runs; all four sections present |
| 11 | README finalisation | Problem, architecture image, how to run, dataset/library attributions with links and licences, honest limitations, team, the hackathon title `sudo make us win_IBM Z Datathon 2026 Project` |

**Priority if time runs short** (cut from the bottom of this list first):
- **Never cut:** Loop 1 live alert, the H1 discovery, LinuxONE evidence, the evaluation report.
- **Cut first, in this order:**
    1. regulator LLM summary;
    2. BH correction;
    3. SHAP (use M2 reasons);
    4. TLS on internal links;
    5. FAERS cache;
    6. LLM backend (keep the template).

---

## 15. README must also include

- A plain-language problem statement. The team adds the citations; you leave `[CITE]` placeholders for every statistic. Do not invent numbers.
- An honest "Limitations" section:
    - synthetic data;
    - injected rates were chosen by us;
    - pseudonymous ≠ anonymous;
    - not a medical device;
    - outputs require professional review.
- "Real-world next step": a pilot on de-identified dispensing data with a pharmacy chain or a public generic-medicine network, validated against PvPI data.

---

## 16. Start now

Begin with **Phase 0 only**. Before writing code:
1. list the files you will create;
2. state any assumption you're making.

Then build Phase 0, run its check, and stop with the phase report.
