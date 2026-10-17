"""/metrics (both roles): only measured values; null until data exists."""

from __future__ import annotations

import platform
import time
from datetime import timedelta
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends, Request

from api.auth import Principal, require_any, suppress

router = APIRouter(tags=["metrics"])
RATE_WINDOW_S = 10.0
LATENCY_SAMPLE = 20000


@router.get("/metrics")
async def metrics(request: Request, p: Principal = Depends(require_any)) -> dict[str, Any]:
    ctx = request.app.state.ctx
    db = ctx.db
    now = time.time()
    recent = db.fetchone("SELECT COUNT(*) FROM latency WHERE wall_ts > ?", (now - RATE_WINDOW_S,))
    total = db.fetchone("SELECT COUNT(*) FROM latency")
    ms = [r[0] for r in db.fetchall("SELECT ms FROM latency ORDER BY id DESC LIMIT ?", (LATENCY_SAMPLE,))]
    lat = None
    if ms:
        arr = np.asarray(ms, dtype=float)
        lat = {"p50": round(float(np.percentile(arr, 50)), 3), "p95": round(float(np.percentile(arr, 95)), 3),
               "p99": round(float(np.percentile(arr, 99)), 3), "sample": len(ms)}
    if p.role == "pharmacist":
        rows = db.fetchall("SELECT status, COUNT(*) FROM alerts WHERE pharmacy_id = ? GROUP BY status",
                           (p.pharmacy_id,))
    else:
        rows = db.fetchall("SELECT status, COUNT(*) FROM alerts GROUP BY status")
    alerts = {r[0]: suppress(int(r[1]), p.role) for r in rows}
    alerts_total = sum(int(r[1]) for r in rows)
    d = db.fetchone("SELECT COUNT(*), SUM(validator_pass) FROM drafts")
    gen = {r[0]: int(r[1]) for r in db.fetchall("SELECT generator, COUNT(*) FROM drafts GROUP BY generator")}
    sim_day = db.get_state("loop2_sim_day")
    s = ctx.settings
    return {
        "arch": platform.machine(),
        "mode": s.mode, "db": ctx.db.dialect,
        "events_per_s": round(int(recent[0]) / RATE_WINDOW_S, 1) if recent and recent[0] else None,
        "events_scored_total": int(total[0]) if total else 0,
        "latency_ms": lat,
        "alerts": alerts, "alerts_total": suppress(alerts_total, p.role),
        "validator_pass_rate": round(int(d[1] or 0) / int(d[0]), 4) if d and d[0] else None,
        "drafts_total": int(d[0]) if d else 0,
        "generator_mix": gen,
        "encryption": {"at_rest": "AES-256-GCM" if s.data_key else None,
                       "tls": bool(s.tls_cert_file and s.tls_key_file), "tokenisation": "HMAC-SHA256 at the edge"},
        "loop2_sim_date": None if sim_day is None else (s.epoch + timedelta(days=int(sim_day))).isoformat(),
        "role": p.label,
    }


@router.get("/audit")
async def audit_logs(request: Request, limit: int = 100, p: Principal = Depends(require_any)) -> list[dict[str, Any]]:
    ctx = request.app.state.ctx
    db = ctx.db
    sql = "SELECT id, ts, role, action, object_id FROM audit"
    params: list[Any] = []
    if p.role == "pharmacist":
        sql += " WHERE role = ? OR role = 'pharmacist' OR role LIKE ?"
        params.extend([p.label, f"pharmacist:{p.pharmacy_id}"])
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(min(max(limit, 1), 500))
    rows = db.fetchall(sql, params)
    return [{"id": r[0], "ts": r[1], "role": r[2], "action": r[3], "object_id": r[4]} for r in rows]
