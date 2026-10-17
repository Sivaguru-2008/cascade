"""Start CASCADE frontend server in single-process mode on Windows/local dev."""
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

# Load generated secrets
load_env_file(ROOT / "secrets" / "app.env")
load_env_file(ROOT / "secrets" / "edge.env")

# Configure single-process mode with SQLite
os.environ["CASCADE_MODE"] = "single"
os.environ["CASCADE_DB"] = "sqlite"
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
    import uvicorn
    uvicorn.run("api.main:app", host=os.environ["BIND_HOST"], port=int(os.environ["API_PORT"]), log_level="info")
