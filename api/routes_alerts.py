"""Pharmacist routes: /score and /alerts* (own pharmacy only)."""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from api.auth import Principal, audit, require_pharmacist
from common.schemas import Alert, DispenseEvent, DraftResult, ScoreDecision
from genai.generate import build_input, draft_and_store, load_draft
from genai.pdf import render_pdf

router = APIRouter(tags=["pharmacist"])

ALERT_COLS = "alert_id, pharmacy_id, status, enc_blob, created_ts, updated_ts"


class DismissBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _public(alert: Alert, row_status: str, created_ts: str) -> dict[str, Any]:
    """Alert as shown to the pharmacist: status from the clear column, no patient token (minimisation)."""
    d = alert.model_dump(mode="json")
    d.pop("patient_token", None)
    d["status"] = row_status
    d["created_ts"] = created_ts
    return d


def _load(ctx: Any, alert_id: str, p: Principal) -> tuple[Alert, str, str]:
    row = ctx.db.fetchone(f"SELECT {ALERT_COLS} FROM alerts WHERE alert_id = ?", (alert_id,))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert not found")
    if row[1] != p.pharmacy_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "alert belongs to another pharmacy")
    alert = Alert.model_validate(ctx.cipher.decrypt_json(row[3], aad=row[0]))
    return alert, row[2], row[4]


def _set_status(ctx: Any, alert_id: str, new: str) -> None:
    ctx.db.execute("UPDATE alerts SET status = ?, updated_ts = ? WHERE alert_id = ?", (new, _now(), alert_id))


@router.post("/score", response_model=ScoreDecision)
async def score(event: DispenseEvent, request: Request, p: Principal = Depends(require_pharmacist)) -> ScoreDecision:
    if event.pharmacy_id != p.pharmacy_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "event belongs to another pharmacy")
    ctx = request.app.state.ctx
    if ctx.runtime is not None:
        return (await ctx.runtime.scorer.process([event], time.perf_counter()))[0]
    r = await ctx.http.post(f"{ctx.settings.scorer_url}/score", json=event.model_dump(mode="json"), timeout=10)
    r.raise_for_status()
    return ScoreDecision.model_validate(r.json())


@router.get("/alerts")
async def list_alerts(request: Request, since: str | None = None, status_: str | None = Query(None, alias="status"),
                      limit: int = Query(50, ge=1, le=500),
                      p: Principal = Depends(require_pharmacist)) -> list[dict[str, Any]]:
    ctx = request.app.state.ctx
    sql = f"SELECT {ALERT_COLS} FROM alerts WHERE pharmacy_id = ?"
    params: list[Any] = [p.pharmacy_id]
    if since:
        sql += " AND created_ts > ?"
        params.append(since)
    if status_:
        sql += " AND status = ?"
        params.append(status_)
    sql += " ORDER BY created_ts DESC LIMIT ?"
    params.append(limit)
    rows = ctx.db.fetchall(sql, params)
    audit(ctx.db, p, "alerts_list", p.pharmacy_id)
    return [_public(Alert.model_validate(ctx.cipher.decrypt_json(r[3], aad=r[0])), r[2], r[4]) for r in rows]


@router.get("/alerts/{alert_id}")
async def get_alert(alert_id: str, request: Request, p: Principal = Depends(require_pharmacist)) -> dict[str, Any]:
    ctx = request.app.state.ctx
    alert, st, created = _load(ctx, alert_id, p)
    audit(ctx.db, p, "alert_view", alert_id)
    out = _public(alert, st, created)
    draft = load_draft(ctx.db, ctx.cipher, alert_id)
    out["draft"] = draft.model_dump(mode="json") if draft else None
    out["evidence"] = {"aSR": alert.pair_aSR, "aSR_ci": alert.pair_aSR_ci, "known_source": alert.known_source,
                       "pair_status": alert.pair_status, "faers": _faers_note(ctx, alert)}
    return out


def _faers_note(ctx: Any, alert: Alert) -> str:
    from datagen.catalogue import load_catalogue, load_faers

    cat = load_catalogue()
    drug = cat.by_id.get(alert.index_drug_id)
    if drug is not None and drug.is_fictional:
        return "no external data (fictional drug)"
    faers = load_faers()
    for p in faers.get("pairs", []):
        if p.get("index_atc_l4") == alert.index_atc_l4 and p.get("marker_atc_l4") == alert.marker_atc_l4:
            return (f"openFDA FAERS: {p.get('count')} reports for {p.get('index_drug')} + '{p.get('reaction')}' "
                    f"(fetched {faers.get('fetched', '?')}; reports are not proof of causation)")
    return "no external data cached"


async def _ensure_draft(ctx: Any, alert: Alert) -> DraftResult:
    existing = load_draft(ctx.db, ctx.cipher, alert.alert_id)
    if existing is not None:
        return existing
    inp = build_input(alert)
    if ctx.runtime is not None or not ctx.settings.genai_url:
        return await draft_and_store(ctx.db, ctx.cipher, inp)
    r = await ctx.http.post(f"{ctx.settings.genai_url}/draft", json={"input": inp.model_dump(mode="json")},
                            timeout=ctx.settings.genai_timeout_s * 2 + 5)
    r.raise_for_status()
    return DraftResult.model_validate(r.json())


@router.post("/alerts/{alert_id}/draft", response_model=DraftResult)
async def draft(alert_id: str, request: Request, p: Principal = Depends(require_pharmacist)) -> DraftResult:
    ctx = request.app.state.ctx
    alert, st, _ = _load(ctx, alert_id, p)
    d = await _ensure_draft(ctx, alert)
    if st == "open":
        _set_status(ctx, alert_id, "drafted")
    audit(ctx.db, p, "draft", alert_id)
    return d


@router.post("/alerts/{alert_id}/approve")
async def approve(alert_id: str, request: Request, p: Principal = Depends(require_pharmacist)) -> dict[str, Any]:
    ctx = request.app.state.ctx
    alert, st, _ = _load(ctx, alert_id, p)
    if st == "dismissed":
        raise HTTPException(status.HTTP_409_CONFLICT, "alert was dismissed")
    d = await _ensure_draft(ctx, alert)
    approved_ts = _now()
    _set_status(ctx, alert_id, "approved")
    audit(ctx.db, p, "approve", alert_id)
    pdf = render_pdf(d, {"approved_ts": approved_ts})
    audit(ctx.db, p, "export", alert_id)
    is_pdf = pdf.startswith(b"%PDF")
    return {"alert_id": alert_id, "status": "approved", "approved_ts": approved_ts, "note": d.note_text,
            "report": d.report.model_dump(mode="json"), "generator": d.generator,
            "validator_pass": d.validator_pass, "file_name": f"{alert_id}_pvpi_draft.{'pdf' if is_pdf else 'html'}",
            "file_type": "application/pdf" if is_pdf else "text/html",
            "file_base64": base64.b64encode(pdf).decode("ascii")}


@router.post("/alerts/{alert_id}/dismiss")
async def dismiss(alert_id: str, body: DismissBody, request: Request,
                  p: Principal = Depends(require_pharmacist)) -> dict[str, str]:
    ctx = request.app.state.ctx
    _load(ctx, alert_id, p)
    _set_status(ctx, alert_id, "dismissed")
    ctx.db.execute("INSERT INTO feedback (alert_id, label, reason_enc, ts) VALUES (?, ?, ?, ?)",
                   (alert_id, "dismissed", ctx.cipher.encrypt_text(body.reason, aad=alert_id), _now()))
    audit(ctx.db, p, "dismiss", alert_id)
    return {"alert_id": alert_id, "status": "dismissed"}
