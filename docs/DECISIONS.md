# Decisions log

One line each: date-time (IST), decision, reason.

- 2026-10-15 09:35, Build started before the 17 Oct kickoff on the team's explicit instruction ("Build anyway, my risk"), reason: team decision; the prompt warns that hackathon rules forbid pre-kickoff code.
- 2026-10-15 09:35, Prompt moved from repo root to `docs/AGENT_PROMPT.md`, reason: section 0 says to keep it there.
- 2026-10-15 09:36, s390x container uses Debian bookworm apt packages for numpy/scipy/scikit-learn/cryptography/reportlab/jinja2 and pip only for pure-Python packages, reason: `pip download --platform manylinux*_s390x --only-binary=:all:` found no s390x wheels on PyPI for numpy, scipy, scikit-learn, xgboost, lightgbm or cryptography (pydantic-core has one).
- 2026-10-15 09:36, PostgreSQL driver is `pg8000` (pure Python), reason: psycopg binary wheels are not published for s390x; pg8000 needs no libpq.
- 2026-10-15 09:36, plain `uvicorn` (no `[standard]` extras), reason: uvloop/httptools are C extensions with uncertain s390x wheels; h11 is pure Python.
- 2026-10-15 09:36, Generated data is written as gzip CSV, not Parquet, reason: avoid pyarrow on s390x (same reason Streamlit is banned).
