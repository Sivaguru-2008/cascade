# Deploying CASCADE on the IBM LinuxONE Community Cloud (L1CC)

> [!NOTE]
> **Active Verified Instance Reference:**
> CASCADE was successfully provisioned, deployed, and benchmarked on an active IBM LinuxONE Community Cloud virtual server:
> - **VM Name:** `cascadevm`
> - **Flavor Profile:** `AI_LAB` (3 vCPUs, 8,192 MB RAM, 50 GB Root Disk)
> - **OS:** Ubuntu 22.04 LTS (`Linux cascadevm 5.15.0-56-generic s390x`)
> - **Assigned Public IP:** `[REDACTED]` (Masked for Security)
> - **Linux User & Key Pair:** `linux1` &bull; `cascade`
> - **Verified Portal Proof:** [`docs/screenshots/ibm_linuxone_instance_proof.png`](../docs/screenshots/ibm_linuxone_instance_proof.png)

Target: one L1CC virtual server (architecture `s390x`). Record all setup decisions and fallbacks in `docs/DECISIONS.md`.

## 0. Create the VM

1. Sign in to the LinuxONE Community Cloud and create a virtual server (Ubuntu 22.04/24.04, RHEL 9 or
   SLES 15 are the usual images; the commands below are for Ubuntu and RHEL).
2. In the project/server description, enter the event code **SSF2026**.
3. Download the SSH key and note the public IP.

## 1. SSH in

```bash
chmod 600 ~/Downloads/<key>.pem
ssh -i ~/Downloads/<key>.pem linux1@<public-ip>      # user name is shown in the L1CC console
uname -m                                             # must print s390x
```

## 2. Install podman (or docker), Python, git, build tools

Ubuntu:
```bash
sudo apt-get update
sudo apt-get install -y podman podman-compose git python3 python3-venv python3-pip build-essential openssl curl tmux
```
RHEL 9:
```bash
sudo dnf install -y podman git python3.11 python3.11-pip gcc openssl curl tmux
pip3.11 install --user podman-compose
```
Check: `podman --version`, `podman compose version` or `podman-compose --version`.

## 3. Clone and start

```bash
git clone <your repo URL> cascade && cd cascade
./deploy/run_all.sh --generate --train      # first run: builds the image, generates data, trains, starts all
./deploy/run_all.sh                          # later runs (idempotent)
```

What `run_all.sh` does: prints the architecture, creates `secrets/` (EDGE_SECRET, DATA_KEY, API keys,
Postgres password, self-signed TLS cert), builds `localhost/cascade-app:latest` from
`deploy/Containerfile.app`, starts Redis + Postgres, applies `deploy/init_db.sql`, starts edge / scorer /
signals / genai / api, waits for every health check, then prints the dashboard URL and keys.

The image installs numpy / scipy / scikit-learn / cryptography from **Debian apt packages**, because PyPI
publishes no s390x wheels for them. XGBoost comes from `python3-xgboost` if Debian has it for s390x.
Otherwise training logs `backend=hgb` (scikit-learn fallback). Write down which one you got.

## 4. Open the dashboard

Only port 8443 is published. The safest way to reach it is an SSH tunnel from your laptop:
```bash
ssh -i <key>.pem -L 8443:localhost:8443 linux1@<public-ip>
```
Then open https://localhost:8443/ and accept the self-signed certificate warning. Paste the regulator
key and the demo pharmacy key that `run_all.sh` printed. They stay in browser memory only. The top strip
must show `Architecture: s390x`.

## 5. Demo

Optional, before recording: pre-run the long warm-up inside the edge container so the live part starts fast:
```bash
podman exec -it cascade-edge python -m edge.pharmacy_edge --demo-script --warmup-only
```
Then click **Run demo script** in the dashboard. It continues from where the warm-up stopped: the K1 event
at 1 event/s, the coincidental event, then fast-forward until Loop 2 flags Novacor → PPI.
To start over: `./deploy/run_all.sh --reset && ./deploy/run_all.sh`.

## 6. Evidence and evaluation

```bash
./deploy/evidence.sh                                   # -> docs/screenshots/evidence.txt (s390x only)
podman run --rm --network cascade_internal --env-file secrets/app.env \
  -e CASCADE_MODE=services -e CASCADE_DB=postgres -e REDIS_URL=redis://redis:6379/0 \
  -e DATABASE_URL=postgresql://cascade@postgres:5432/cascade -e CASCADE_STATE_DIR=/tmp \
  -v "$PWD/data:/app/data" -v "$PWD/loop1/model:/app/loop1/model" -v "$PWD/docs:/app/docs" \
  localhost/cascade-app:latest python -m eval.make_report
```
Take screenshots of the dashboard (with `s390x` visible) and of `podman ps` for `docs/screenshots/`.

## Fallbacks (switch within one attempt, then log it in DECISIONS.md)

| Problem | Fallback |
|---|---|
| podman/docker or compose will not work | `./deploy/run_all.sh --single-process` (one Python process, in-memory bus, SQLite; needs a venv: `python3 -m venv --system-site-packages .venv` after `apt-get install python3-numpy python3-scipy python3-sklearn python3-cryptography python3-reportlab python3-jinja2`, then `.venv/bin/pip install -r deploy/requirements-container.txt`) |
| Redis will not start | same `--single-process` mode (`CASCADE_MODE=single`) |
| Postgres will not start | `CASCADE_DB=sqlite` |
| XGBoost unavailable | `CASCADE_ML=hgb` (automatic with `auto`) |
| LLM backend unavailable | `GENAI_BACKEND=template` (the default) |
