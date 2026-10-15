# OVERCLOCKED_IBM Z Datathon 2026 Project — CASCADE

Real-time detection of prescribing cascades from the pharmacy dispensing stream.

> Work in progress. See `docs/AGENT_PROMPT.md` for the specification and `docs/DECISIONS.md` for decisions.

## Quick start (development)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest
.venv/bin/uvicorn api.main:app --port 8000   # GET /health
```

## Third-party resources

To be completed (see section 0, rule 8).
