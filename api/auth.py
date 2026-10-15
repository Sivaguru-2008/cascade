"""Role-based API keys and the audit log (section 8.5 and 8.7).

Keys: PHARMACIST_KEY_<pharmacy_id> (one per pharmacy) and REGULATOR_KEY, sent as `X-API-Key`.
  * no key / unknown key            -> 401
  * a valid key on the wrong route  -> 403 (pharmacist on /signals*, regulator on /alerts*, /score,
                                       pharmacist on another pharmacy's alert)
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fastapi import Depends, Header, HTTPException, Request, status

from common.db import Database

SMALL_CELL = 5


@dataclass(frozen=True)
class Principal:
    role: Literal["pharmacist", "regulator"]
    pharmacy_id: str | None = None

    @property
    def label(self) -> str:
        return f"pharmacist:{self.pharmacy_id}" if self.role == "pharmacist" else "regulator"


def resolve_key(key: str | None, regulator_key: str | None, pharmacist_keys: dict[str, str]) -> Principal | None:
    """Constant-time comparison against every configured key."""
    if not key:
        return None
    found: Principal | None = None
    candidate = key.encode()
    if regulator_key and hmac.compare_digest(candidate, regulator_key.encode()):
        found = Principal("regulator")
    for k, pharmacy in pharmacist_keys.items():
        if hmac.compare_digest(candidate, k.encode()):
            found = Principal("pharmacist", pharmacy)
    return found


def authenticate(request: Request, x_api_key: str | None = Header(default=None)) -> Principal:
    s = request.app.state.ctx.settings
    p = resolve_key(x_api_key, s.regulator_key, s.pharmacist_keys)
    if p is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid API key",
                            headers={"WWW-Authenticate": "ApiKey"})
    return p


def require_pharmacist(p: Principal = Depends(authenticate)) -> Principal:
    if p.role != "pharmacist":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "pharmacist role required")
    return p


def require_regulator(p: Principal = Depends(authenticate)) -> Principal:
    if p.role != "regulator":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "regulator role required")
    return p


def require_any(p: Principal = Depends(authenticate)) -> Principal:
    return p


def audit(db: Database, p: Principal, action: str, object_id: str | None) -> None:
    db.execute("INSERT INTO audit (ts, role, action, object_id) VALUES (?, ?, ?, ?)",
               (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), p.label, action, object_id))


def suppress(n: int | None, role: str) -> int | str | None:
    """Small-cell suppression for the regulator: any count below 5 is shown as '<5'."""
    if n is None or role != "regulator":
        return n
    return "<5" if int(n) < SMALL_CELL else int(n)


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:8]
