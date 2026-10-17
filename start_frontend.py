"""Start CASCADE frontend server in single-process mode on Windows/local dev.

Team: sudo make us win (IBM Z Datathon 2026)
One-click frictionless launcher for technical evaluation.
"""
import base64
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()

# Load generated secrets if present
load_env_file(ROOT / "secrets" / "app.env")
load_env_file(ROOT / "secrets" / "edge.env")

# Ensure required runtime cryptographic keys exist for seamless local execution
if not os.environ.get("DATA_KEY"):
    os.environ["DATA_KEY"] = base64.b64encode(b"cascade-dev-secret-key-32bytes!!").decode("ascii")

if not os.environ.get("EDGE_SECRET"):
    os.environ["EDGE_SECRET"] = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

if not os.environ.get("PHARMACIST_KEY_P1"):
    os.environ["PHARMACIST_KEY_P1"] = "demo-pharmacist-key-p1"

if not os.environ.get("REGULATOR_KEY"):
    os.environ["REGULATOR_KEY"] = "demo-regulator-key"

# Configure single-process mode with SQLite
os.environ["CASCADE_MODE"] = "single"
os.environ["CASCADE_DB"] = "sqlite"
(ROOT / "data" / "generated").mkdir(parents=True, exist_ok=True)
os.environ["SQLITE_PATH"] = str((ROOT / "data" / "generated" / "cascade.sqlite3").resolve())
os.environ["DEMO_MODE"] = "1"

# Run plain HTTP on port 8000 to avoid browser self-signed TLS certificate warnings
os.environ.pop("TLS_CERT_FILE", None)
os.environ.pop("TLS_KEY_FILE", None)
os.environ["BIND_HOST"] = os.environ.get("BIND_HOST", "127.0.0.1")
os.environ["API_PORT"] = os.environ.get("API_PORT", "8000")

if __name__ == "__main__":
    from common.config import reload_settings
    reload_settings()
    host = os.environ["BIND_HOST"]
    port = int(os.environ["API_PORT"])
    print("=" * 76)
    print("CASCADE: Clinical Prescribing Cascade Surveillance Platform")
    print("   IBM Z Datathon 2026 • Team sudo make us win")
    print(f"   Dashboard Live URL: http://{host}:{port}")
    print("=" * 76)
    import uvicorn
    uvicorn.run("api.main:app", host=host, port=port, log_level="info")
