# Decisions log

One line each: date-time (IST), decision, reason.

- 2026-10-15 09:35, Build started before the 17 Oct kickoff on the team's explicit instruction ("Build anyway, my risk"), reason: team decision; the prompt warns that hackathon rules forbid pre-kickoff code.
- 2026-10-15 09:35, Prompt moved from repo root to `docs/AGENT_PROMPT.md`, reason: section 0 says to keep it there.
- 2026-10-15 09:36, s390x container uses Debian bookworm apt packages for numpy/scipy/scikit-learn/cryptography/reportlab/jinja2 and pip only for pure-Python packages, reason: `pip download --platform manylinux*_s390x --only-binary=:all:` found no s390x wheels on PyPI for numpy, scipy, scikit-learn, xgboost, lightgbm or cryptography (pydantic-core has one).
- 2026-10-15 09:36, PostgreSQL driver is `pg8000` (pure Python), reason: psycopg binary wheels are not published for s390x; pg8000 needs no libpq.
- 2026-10-15 09:36, plain `uvicorn` (no `[standard]` extras), reason: uvloop/httptools are C extensions with uncertain s390x wheels; h11 is pure Python.
- 2026-10-15 09:36, Generated data is written as gzip CSV, not Parquet, reason: avoid pyarrow on s390x (same reason Streamlit is banned).
- 2026-10-15 09:45, Novacor atc_l3 = `C09X` (not `C09` as written in 4.1), reason: every other row uses the 4-character ATC level 3; C09 is level 2. C09X is the real level-3 group "other agents acting on the renin-angiotensin system", and the drug stays clearly fictional through `C09XX`/`C09XX99` and `is_fictional=1` (an extra catalogue column).
- 2026-10-15 09:45, K9 "new antihypertensive" marker = amlodipine | losartan | telmisartan (classes C08CA | C09CA), and multi-drug cells use `|`, reason: the table names a drug group, not one drug.
- 2026-10-15 09:45, Generator assertion thresholds scale linearly with population (150 and 80 at 20,000 patients, so 15 and 8 at 2,000), reason: the phase-1 check runs at 2,000 patients, where fixed thresholds are unreachable.
- 2026-10-15 09:45, Index-drug starts are topped up beyond realistic rates ("topup" episodes; see generation_report.json), reason: ≥150 injected events at p_cascade=0.03 needs ≈5,000+ incident index starts in 20,000 patients, far above natural incidence. Top-ups are spread uniformly in time (no trend artefact). Eligibility: tamsulosin men, or 30% of others (short-term ureteric-stone use); donepezil age ≥55.
- 2026-10-15 09:45, Novacor: the specified 0→15% share of new antihypertensive starts is kept, plus a switch/add-on channel whose start density ramps linearly from launch, reason: the share alone yields ≈60 Novacor starts at 20,000 patients, but ≥80 H1 events at p=0.06 need ≈1,350+.
- 2026-10-15 09:45, Index A incidence is drug-level (as specified); marker B incidence is class-level (ATC L4, as Loop 1 and PSSA define it); injection uses base episodes only (no chained cascades), reason: simplest reading that keeps "no label on a non-incident start" checkable.
- 2026-10-15 09:45, Acute courses (antibiotics, NSAIDs, antitussives, antiemetics, laxatives, antihistamines, short PPI and steroid courses, hypnotics) last 1–3 dispenses of 7–10 days; everything else follows the 30±3-day repeat with 10%/year discontinuation, reason: the spec only defines chronic repeats.
- 2026-10-15 09:45, Age band is fixed at baseline for the 3 simulated years, reason: simplicity; it is a 5-year band.
