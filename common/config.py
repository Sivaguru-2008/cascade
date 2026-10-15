"""Central configuration. Every backend switch is an environment variable.

Secrets (EDGE_SECRET, DATA_KEY, API keys) are read from the environment only; nothing here
has a default secret. EDGE_SECRET is deliberately *not* part of Settings: only the edge reads it
(see edge/pharmacy_edge.py), so server-side services never load it.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PHARMACIST_KEY_PREFIX = "PHARMACIST_KEY_"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _float_or_none(name: str) -> float | None:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else None


def _path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw) if raw else default


def _data_key() -> bytes | None:
    raw = os.environ.get("DATA_KEY")
    if not raw:
        return None
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise ValueError("DATA_KEY must be base64 of exactly 32 bytes (AES-256)")
    return key


def _pharmacist_keys() -> dict[str, str]:
    """Map API key -> pharmacy_id from env vars named PHARMACIST_KEY_<pharmacy_id>."""
    keys: dict[str, str] = {}
    for name, value in os.environ.items():
        if name.startswith(PHARMACIST_KEY_PREFIX) and value:
            keys[value] = name[len(PHARMACIST_KEY_PREFIX):]
    return keys


@dataclass(frozen=True)
class Settings:
    # runtime switches
    mode: str  # "services" (Redis bus) | "single" (in-process asyncio bus)
    db_backend: str  # "postgres" | "sqlite"
    ml_backend: str  # "auto" | "xgboost" | "hgb"
    genai_backend: str  # "lab" | "transformers" | "llamacpp" | "template"
    demo_mode: bool
    no_known_prior: bool

    # connections
    redis_url: str
    database_url: str
    sqlite_path: Path
    scorer_url: str
    genai_url: str

    # paths
    data_dir: Path
    generated_dir: Path
    model_dir: Path
    state_dir: Path

    # crypto / auth (never logged)
    data_key: bytes | None = field(repr=False)
    regulator_key: str | None = field(repr=False)
    pharmacist_keys: dict[str, str] = field(repr=False)
    tls_cert_file: str | None = None
    tls_key_file: str | None = None

    # domain parameters
    epoch: date = date(2023, 1, 1)
    lookback_days: int = 365
    candidate_window_days: int = 180
    windows: tuple[int, ...] = (30, 90, 180)
    display_window: int = 90
    min_pair_n: int = 10
    recompute_every_events: int = 5000
    recompute_every_seconds: float = 10.0
    small_cell: int = 5
    threshold_override: float | None = None

    # genai
    genai_timeout_s: float = 10.0
    genai_max_tokens: int = 220
    genai_temperature: float = 0.1
    genai_model: str = "ibm-granite/granite-3.3-2b-instruct"
    genai_lab_url: str = ""
    genai_lab_model: str = ""
    llamacpp_model_path: str = ""


def _load() -> Settings:
    data_dir = _path("CASCADE_DATA_DIR", ROOT / "data")
    return Settings(
        mode=_env("CASCADE_MODE", "services"),
        db_backend=_env("CASCADE_DB", "postgres"),
        ml_backend=_env("CASCADE_ML", "auto"),
        genai_backend=_env("GENAI_BACKEND", "template"),
        demo_mode=_bool("DEMO_MODE"),
        no_known_prior=_bool("LOOP1_NO_KNOWN_PRIOR"),
        redis_url=_env("REDIS_URL", "redis://localhost:6379/0"),
        database_url=_env("DATABASE_URL", "postgresql://cascade@localhost:5432/cascade"),
        sqlite_path=_path("SQLITE_PATH", ROOT / "data" / "generated" / "cascade.sqlite3"),
        scorer_url=_env("SCORER_URL", "http://localhost:8001"),
        genai_url=_env("GENAI_URL", "http://localhost:8003"),
        data_dir=data_dir,
        generated_dir=_path("CASCADE_GENERATED_DIR", data_dir / "generated"),
        model_dir=_path("CASCADE_MODEL_DIR", ROOT / "loop1" / "model"),
        state_dir=_path("CASCADE_STATE_DIR", data_dir / "generated" / "state"),
        data_key=_data_key(),
        regulator_key=os.environ.get("REGULATOR_KEY") or None,
        pharmacist_keys=_pharmacist_keys(),
        tls_cert_file=os.environ.get("TLS_CERT_FILE") or None,
        tls_key_file=os.environ.get("TLS_KEY_FILE") or None,
        epoch=date.fromisoformat(_env("CASCADE_EPOCH", "2023-01-01")),
        recompute_every_events=_int("LOOP2_RECOMPUTE_EVENTS", 5000),
        recompute_every_seconds=float(_env("LOOP2_RECOMPUTE_SECONDS", "10")),
        threshold_override=_float_or_none("LOOP1_THRESHOLD"),
        genai_timeout_s=float(_env("GENAI_TIMEOUT_S", "10")),
        genai_max_tokens=_int("GENAI_MAX_TOKENS", 220),
        genai_temperature=min(0.2, float(_env("GENAI_TEMPERATURE", "0.1"))),
        genai_model=_env("GENAI_MODEL", "ibm-granite/granite-3.3-2b-instruct"),
        genai_lab_url=_env("GENAI_LAB_URL", ""),
        genai_lab_model=_env("GENAI_LAB_MODEL", ""),
        llamacpp_model_path=_env("LLAMACPP_MODEL_PATH", ""),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return _load()


def reload_settings() -> Settings:
    """Re-read the environment (used by tests and the single-process runner)."""
    get_settings.cache_clear()
    return get_settings()
