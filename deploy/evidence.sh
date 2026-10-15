#!/usr/bin/env bash
# CASCADE: collect platform / security evidence (section 8.8).
# On s390x the output is saved to docs/screenshots/evidence.txt; on any other architecture it is saved to
# docs/screenshots/evidence_<arch>.txt so that a dev-machine run can never be mistaken for LinuxONE evidence.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
ARCH="$(uname -m)"
OUT="docs/screenshots/evidence.txt"
[ "$ARCH" = "s390x" ] || OUT="docs/screenshots/evidence_${ARCH}.txt"
mkdir -p docs/screenshots
# shellcheck disable=SC1091
[ -f secrets/app.env ] && set -a && . secrets/app.env && set +a
API_URL="${API_URL:-https://localhost:8443}"
PY="${PYTHON:-python3}"; [ -x .venv/bin/python ] && PY=.venv/bin/python
ENGINE=""; command -v podman >/dev/null 2>&1 && ENGINE=podman; [ -z "$ENGINE" ] && command -v docker >/dev/null 2>&1 && ENGINE=docker

{
  echo "== CASCADE evidence =="
  echo "date (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "git commit: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo
  echo "== uname -m"; uname -m
  echo "== uname -a"; uname -a
  echo
  echo "== containers ($ENGINE)"
  if [ -n "$ENGINE" ]; then $ENGINE ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}" 2>&1; else echo "podman/docker not found on this host"; fi
  echo
  echo "== CPU feature flags from /proc/cpuinfo (printed as found; nothing assumed)"
  grep -m1 -iE "^(features|flags)[[:space:]]*:" /proc/cpuinfo || echo "no features/flags line"
  echo "-- crypto-related flags present:"
  grep -m1 -iE "^(features|flags)[[:space:]]*:" /proc/cpuinfo | tr ' ' '\n' | grep -iwE "msa[0-9_a-z]*|aes|vaes|pclmulqdq|sha_ni|sha1|sha2|sha256|sha512" | sort -u | tr '\n' ' '; echo
  [ -r /proc/crypto ] && { echo "-- /proc/crypto drivers for gcm(aes) / aes:"; awk '/^name/{n=$3} /^driver/{d=$3} /^priority/{if (n ~ /^(gcm\(aes\)|aes)$/) print "  " n " -> " d " (priority " $3 ")"}' /proc/crypto | sort -u | head -8; }
  echo
  echo "== openssl"
  openssl version
  echo "-- openssl speed -evp aes-256-gcm -seconds 3 (last lines)"
  openssl speed -evp aes-256-gcm -seconds 3 2>/dev/null | tail -3
  echo
  echo "== one encrypted DB row"
  if [ -n "$ENGINE" ] && $ENGINE ps --format '{{.Names}}' 2>/dev/null | grep -q '^cascade-api$'; then
    $ENGINE exec cascade-api python -m common.db --encrypted-sample 2>&1
  else
    "$PY" -m common.db --encrypted-sample 2>&1
  fi
  echo
  echo "== regulator key on an alert endpoint (expect 403)"
  if [ -n "${REGULATOR_KEY:-}" ]; then
    echo "curl -sk -H 'X-API-Key: <REGULATOR key, redacted>' $API_URL/alerts"
    code=$(curl -sk -o /dev/null -w "%{http_code}" -H "X-API-Key: ${REGULATOR_KEY}" "$API_URL/alerts")
    echo "HTTP status: $code"
  else
    echo "REGULATOR key not available (secrets/app.env missing): skipped"
  fi
} 2>&1 | tee "$OUT"
echo "[evidence] saved to $OUT"
