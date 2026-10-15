"""Regulator routes: /signals* (aggregates only, counts below 5 shown as '<5')."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from api.auth import Principal, require_regulator, suppress
from datagen.catalogue import load_catalogue

router = APIRouter(tags=["regulator"])
COLS = ("a_l4, b_l4, win, sim_day, n_ab, n_ba, nsr, asr, lo, hi, p_value, is_signal, is_signal_bh, status, "
        "first_signal_day")


def _class_info(l4: str) -> dict[str, Any]:
    cat = load_catalogue()
    drugs = cat.by_l4.get(l4, [])
    return {"atc_l4": l4, "examples": [d.display_name for d in drugs[:3]],
            "fictional": any(d.is_fictional for d in drugs)}


def _date(ctx: Any, day: int | None) -> str | None:
    return None if day is None else (ctx.settings.epoch + timedelta(days=int(day))).isoformat()


@router.get("/signals")
async def list_signals(request: Request, status_: str | None = Query(None, alias="status"),
                       window: int = Query(90), include_all: bool = Query(False, alias="all"),
                       bh: bool = Query(False), limit: int = Query(100, ge=1, le=1000),
                       p: Principal = Depends(require_regulator)) -> dict[str, Any]:
    ctx = request.app.state.ctx
    if window not in ctx.settings.windows:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"window must be one of {ctx.settings.windows}")
    sql = f"SELECT {COLS} FROM signals_latest WHERE win = ?"
    params: list[Any] = [window]
    if not include_all:
        sql += " AND is_signal_bh = 1" if bh else " AND is_signal = 1"
    if status_:
        sql += " AND status = ?"
        params.append(status_)
    sql += " ORDER BY lo DESC LIMIT ?"
    params.append(limit)
    out = []
    for r in ctx.db.fetchall(sql, params):
        n = int(r[4]) + int(r[5])
        out.append({"a": _class_info(r[0]), "b": _class_info(r[1]), "window": r[2], "sim_day": r[3],
                    "sim_date": _date(ctx, r[3]), "n_ab": suppress(r[4], p.role), "n_ba": suppress(r[5], p.role),
                    "n": suppress(n, p.role), "nsr": r[6], "asr": r[7], "lo": r[8], "hi": r[9], "p_value": r[10],
                    "is_signal": bool(r[11]), "is_signal_bh": bool(r[12]), "status": r[13],
                    "first_signal_date": _date(ctx, r[14])})
    totals = ctx.db.fetchone("SELECT COUNT(*), SUM(is_signal), SUM(is_signal_bh) FROM signals_latest WHERE win = ?",
                             (window,)) or (0, 0, 0)
    return {"window": window, "bh": bh, "signals": out, "tested_pairs": int(totals[0] or 0),
            "n_signals": int(totals[1] or 0), "n_signals_bh": int(totals[2] or 0),
            "sim_date": _date(ctx, ctx.db.get_state("loop2_sim_day"))}


@router.get("/signals/{a}/{b}/history")
async def history(a: str, b: str, request: Request, window: int = Query(90),
                  p: Principal = Depends(require_regulator)) -> dict[str, Any]:
    ctx = request.app.state.ctx
    rows = ctx.db.fetchall("SELECT sim_day, n_ab, n_ba, asr, lo, hi, is_signal, status FROM signals "
                           "WHERE a_l4 = ? AND b_l4 = ? AND win = ? ORDER BY sim_day, id", (a, b, window))
    points = [{"sim_day": r[0], "sim_date": _date(ctx, r[0]), "n": suppress(int(r[1]) + int(r[2]), p.role),
               "asr": r[3], "lo": r[4], "hi": r[5], "is_signal": bool(r[6])} for r in rows]
    return {"a": _class_info(a), "b": _class_info(b), "window": window, "points": points,
            "status": rows[-1][7] if rows else None}
