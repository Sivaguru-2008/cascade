#!/usr/bin/env bash
# CASCADE: bring the whole system up. Idempotent: secrets, image and containers are reused when present.
#
#   ./deploy/run_all.sh                     containers (podman preferred, docker fallback)
#   ./deploy/run_all.sh --generate --train  generate data, train models and build the demo plan first
#   ./deploy/run_all.sh --single-process    no containers, no Redis/Postgres: one Python process + SQLite
#   ./deploy/run_all.sh --test              run pytest only
#   ./deploy/run_all.sh --down              stop containers (data volumes kept)
#   ./deploy/run_all.sh --reset             stop containers AND delete Redis/Postgres/Loop 2 volumes
# Options: --patients N (default 20000)  --seed S (default 7)  --demo (DEMO_MODE=1)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
SINGLE=0; GENERATE=0; TRAIN=0; DOWN=0; RESET=0; TEST=0; DEMO="${DEMO_MODE:-1}"; PATIENTS=20000; SEED=7
while [ $# -gt 0 ]; do
  case "$1" in
    --single-process) SINGLE=1 ;;
    --generate) GENERATE=1 ;;
    --train) TRAIN=1 ;;
    --down) DOWN=1 ;;
    --reset) RESET=1 ;;
    --test) TEST=1 ;;
    --demo) DEMO=1 ;;
    --no-demo) DEMO=0 ;;
    --patients) PATIENTS="$2"; shift ;;
    --seed) SEED="$2"; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
log() { printf '[cascade] %s\n' "$*"; }

# ---------------------------------------------------------------- 1. architecture
ARCH="$(uname -m)"
log "architecture: $ARCH$([ "$ARCH" = s390x ] && echo ' (IBM Z / LinuxONE)')"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi; fi

if [ "$TEST" = 1 ]; then exec "$PY" -m pytest -q; fi

ENGINE=""; COMPOSE=()
if command -v podman >/dev/null 2>&1; then
  ENGINE=podman
  if podman compose version >/dev/null 2>&1; then COMPOSE=(podman compose)
  elif command -v podman-compose >/dev/null 2>&1; then COMPOSE=(podman-compose); fi
elif command -v docker >/dev/null 2>&1; then
  ENGINE=docker
  if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then COMPOSE=(docker-compose); fi
fi
compose() { "${COMPOSE[@]}" -f deploy/compose.yaml "$@"; }

if [ "$DOWN" = 1 ] || [ "$RESET" = 1 ]; then
  [ ${#COMPOSE[@]} -gt 0 ] || { echo "no compose tool found" >&2; exit 1; }
  if [ "$RESET" = 1 ]; then log "stopping containers and DELETING volumes (redis, postgres, loop2 state)"; compose down -v
  else log "stopping containers (volumes kept)"; compose down; fi
  exit 0
fi

# ---------------------------------------------------------------- 2. secrets
umask 077
mkdir -p secrets/tls
rand_hex() { openssl rand -hex "$1"; }
kv() { printf '%s=%s\n' "$1" "$2"; }
if [ ! -f secrets/edge.env ]; then
  log "creating secrets/edge.env (HMAC tokenisation secret; edge only)"
  { kv EDGE_SECRET "$(rand_hex 32)"; } > secrets/edge.env
fi
if [ ! -f secrets/postgres.env ]; then
  kv POSTGRES_PASSWORD "$(rand_hex 24)" > secrets/postgres.env
fi
if [ ! -f secrets/app.env ]; then
  log "creating secrets/app.env (AES-256 data key, API keys)"
  {
    kv DATA_KEY "$(openssl rand -base64 32)"
    kv REGULATOR_KEY "reg_$(rand_hex 20)"
    for i in $(seq -w 1 50); do kv "PHARMACIST_KEY_PH0${i}" "ph${i}_$(rand_hex 20)"; done
    grep POSTGRES_PASSWORD secrets/postgres.env
    kv TLS_CERT_FILE /tls/cert.pem
    kv TLS_KEY_FILE /tls/key.pem
  } > secrets/app.env
fi
if [ ! -f secrets/tls/cert.pem ]; then
  log "creating self-signed TLS certificate (secrets/tls)"
  openssl req -x509 -newkey rsa:3072 -sha256 -days 30 -nodes -subj "/CN=cascade.local" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" -keyout secrets/tls/key.pem -out secrets/tls/cert.pem 2>/dev/null
  chmod 755 secrets/tls; chmod 644 secrets/tls/key.pem secrets/tls/cert.pem  # container user must read; secrets/ is 0700
fi
chmod 700 secrets
grep -q '^DEMO_MODE=' secrets/app.env && sed -i "s/^DEMO_MODE=.*/DEMO_MODE=${DEMO}/" secrets/app.env \
  || kv DEMO_MODE "$DEMO" >> secrets/app.env
umask 022

# shared steps that run either in a container or with the local Python
data_steps() {  # $@ = command prefix that runs python
  if [ "$GENERATE" = 1 ]; then
    log "generating synthetic data (--patients $PATIENTS --seed $SEED)"
    "$@" -m datagen.generate --patients "$PATIENTS" --seed "$SEED"
  fi
  if [ "$TRAIN" = 1 ]; then
    log "training Loop 1 models (full, no_known, no_asr)"
    "$@" -m loop1.train --rebuild-dataset
    log "building the demo plan (docs/demo_script_events.json)"
    "$@" -m datagen.replay --build-demo-plan
  fi
}
print_access() {  # $1 = URL
  local demo_ph
  demo_ph="$("$PY" -c "import json;print(json.load(open('docs/demo_script_events.json'))['demo_pharmacy'])" 2>/dev/null || echo PH001)"
  echo
  log "dashboard: $1   (self-signed certificate: accept the browser warning; on L1CC use an SSH tunnel)"
  log "regulator key       : $(grep -E '^REGULATOR_KEY[=]' secrets/app.env | cut -d= -f2-)"
  log "pharmacist key ${demo_ph}: $(grep "^PHARMACIST_KEY_${demo_ph}=" secrets/app.env | cut -d= -f2-)"
  log "(all keys: secrets/app.env; keys are entered in the dashboard and kept in memory only)"
}
wait_http() {  # $1 = url, $2 = seconds
  local i
  for i in $(seq 1 "$2"); do curl -skf "$1" >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}

# ---------------------------------------------------------------- single-process fallback
if [ "$SINGLE" = 1 ]; then
  log "single-process mode: in-memory bus + SQLite, no containers (CASCADE_MODE=single)"
  set -a; . secrets/app.env; . secrets/edge.env; set +a
  export CASCADE_MODE=single CASCADE_DB=sqlite TLS_CERT_FILE="$ROOT/secrets/tls/cert.pem" TLS_KEY_FILE="$ROOT/secrets/tls/key.pem"
  export SQLITE_PATH="${SQLITE_PATH:-$ROOT/data/generated/cascade.sqlite3}"
  data_steps "$PY"
  [ -f loop1/model/blend.json ] || { echo "no trained model: run with --generate --train first" >&2; exit 1; }
  mkdir -p data/generated
  if command -v tmux >/dev/null 2>&1; then
    tmux kill-session -t cascade 2>/dev/null || true
    tmux new-session -d -s cascade "cd '$ROOT' && exec '$PY' -m api.main 2>&1 | tee data/generated/cascade-single.log"
    log "started in tmux session 'cascade' (tmux attach -t cascade)"
  else
    pkill -f "[p]ython.* -m api.main" 2>/dev/null || true
    nohup "$PY" -m api.main > data/generated/cascade-single.log 2>&1 &
    log "started in background (log: data/generated/cascade-single.log)"
  fi
  wait_http https://localhost:8443/health 90 || { echo "API did not become healthy; see the log" >&2; exit 1; }
  log "health: $(curl -sk https://localhost:8443/health)"
  print_access https://localhost:8443/
  exit 0
fi

# ---------------------------------------------------------------- 3. image
[ -n "$ENGINE" ] || { echo "podman/docker not found; install podman or use --single-process" >&2; exit 1; }
[ ${#COMPOSE[@]} -gt 0 ] || { echo "no compose tool (podman compose / podman-compose / docker compose)" >&2; exit 1; }
log "container engine: $ENGINE (${COMPOSE[*]})"
if ! $ENGINE image exists localhost/cascade-app:latest 2>/dev/null && ! $ENGINE image inspect localhost/cascade-app:latest >/dev/null 2>&1 \
   || [ "${REBUILD:-0}" = 1 ]; then
  log "building image localhost/cascade-app:latest for $ARCH"
  $ENGINE build -f deploy/Containerfile.app -t localhost/cascade-app:latest .
else
  log "image localhost/cascade-app:latest exists (REBUILD=1 to rebuild)"
fi
mkdir -p data/generated loop1/model
if [ "$GENERATE" = 1 ] || [ "$TRAIN" = 1 ]; then
  data_steps $ENGINE run --rm --user 0 -v "$ROOT/data:/app/data" -v "$ROOT/loop1/model:/app/loop1/model" \
    -v "$ROOT/docs:/app/docs" -e CASCADE_ML="${CASCADE_ML:-auto}" localhost/cascade-app:latest python
fi
[ -f loop1/model/blend.json ] || { echo "no trained model: run with --generate --train first" >&2; exit 1; }

# ---------------------------------------------------------------- 4-5. redis + postgres + schema
log "starting redis + postgres"
compose up -d redis postgres
for i in $(seq 1 60); do
  $ENGINE exec cascade-postgres pg_isready -U cascade -d cascade >/dev/null 2>&1 && break; sleep 1
done
log "applying deploy/init_db.sql"
$ENGINE exec -i cascade-postgres psql -q -v ON_ERROR_STOP=1 -U cascade -d cascade < deploy/init_db.sql

# ---------------------------------------------------------------- 6-7. app services + health
log "starting app services: edge scorer signals genai api"
compose up -d edge scorer signals genai api
log "waiting for health checks"
for c in cascade-edge cascade-scorer cascade-signals cascade-genai cascade-api; do
  ok=0
  for i in $(seq 1 120); do
    st="$($ENGINE inspect --format '{{.State.Health.Status}}' "$c" 2>/dev/null || echo unknown)"
    [ "$st" = healthy ] && { ok=1; break; }
    sleep 1
  done
  [ "$ok" = 1 ] && log "  $c healthy" || { log "  $c NOT healthy (logs: $ENGINE logs $c)"; exit 1; }
done
log "api health: $(curl -sk https://localhost:8443/health)"

# ---------------------------------------------------------------- 8. access
print_access https://localhost:8443/
