#!/usr/bin/env bash
# CASCADE: bring the whole system up (skeleton; filled in phase 9).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "[cascade] architecture: $(uname -m)"
echo "[cascade] phase-0 skeleton: running tests"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="python3"
"$PY" -m pytest -q
