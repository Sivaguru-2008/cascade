"""CASCADE API + dashboard.

    uvicorn api.main:app --port 8000                       # development (HTTP)
    python -m api.main                                     # uses TLS_CERT_FILE / TLS_KEY_FILE when set

CASCADE_MODE=single also runs the scorer, Loop 2 and (if EDGE_SECRET is present) the edge in this
process, with an in-memory bus: the fallback when Redis / containers are unavailable.
OpenAPI docs: /docs.
"""

from __future__ import annotations

import contextlib
import os
import platform
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api import routes_alerts, routes_clinical, routes_metrics, routes_signals, sse
from api.auth import Principal, audit, require_any
from common.bus import STREAM_CONTROL, Bus, get_bus
from common.config import Settings, get_settings
from common.crypto import Cipher, cipher_from_settings
from common.db import Database
from common.logging import get_logger

log = get_logger("api")
STATIC = Path(__file__).resolve().parent / "static"


@dataclass
class Context:
    settings: Settings
    db: Database
    cipher: Cipher
    bus: Bus
    http: httpx.AsyncClient
    runtime: Any = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    db = Database.connect()
    db.init_schema()
    bus = get_bus()
    runtime = None
    if s.mode == "single":
        from api.runtime import Runtime

        runtime = Runtime(s, db, bus)
        await runtime.start(with_edge=True)
    app.state.ctx = Context(s, db, cipher_from_settings(), bus, httpx.AsyncClient(), runtime)
    log.info("api started", extra={"mode": s.mode, "db": db.dialect, "arch": platform.machine(),
                                   "pharmacist_keys": len(s.pharmacist_keys), "regulator_key": bool(s.regulator_key)})
    yield
    if runtime is not None:
        await runtime.stop()
    await app.state.ctx.http.aclose()


app = FastAPI(title="CASCADE API", version="1.0.0", lifespan=lifespan,
              description="Real-time prescribing-cascade detection. Synthetic data only; outputs require "
                          "professional review. Send the API key in the `X-API-Key` header.")
app.include_router(routes_alerts.router)
app.include_router(routes_signals.router)
app.include_router(routes_metrics.router)
app.include_router(routes_clinical.router)
app.include_router(sse.router)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/", include_in_schema=False)
@app.get("/overview", include_in_schema=False)
@app.get("/monitoring", include_in_schema=False)
@app.get("/system", include_in_schema=False)
@app.get("/doctor", include_in_schema=False)
@app.get("/patient", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/pvpi/fields", include_in_schema=False)
async def pvpi_fields() -> dict[str, Any]:
    """Public: labels of the PvPI draft form (no data), used by the dashboard to render drafts."""
    from genai.pdf import form_fields

    return form_fields()


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    ctx: Context | None = getattr(request.app.state, "ctx", None)
    return {"status": "ok", "service": "api", "arch": platform.machine(),
            "mode": ctx.settings.mode if ctx else None}


@app.post("/admin/demo/start")
async def demo_start(request: Request, p: Principal = Depends(require_any)) -> dict[str, str]:
    """Starts `replay --demo-script` on the edge. Exists only when DEMO_MODE=1."""
    ctx: Context = request.app.state.ctx
    if not ctx.settings.demo_mode:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    await ctx.bus.publish(STREAM_CONTROL, {"cmd": "demo"})
    audit(ctx.db, p, "demo_start", None)
    return {"status": "requested"}


@app.get("/admin/demo/status")
async def demo_status(request: Request, p: Principal = Depends(require_any)) -> dict[str, Any]:
    ctx: Context = request.app.state.ctx
    if not ctx.settings.demo_mode:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    import json

    raw = await ctx.bus.get("demo:status")
    return json.loads(raw) if raw else {"stage": "idle"}


def main() -> None:
    import uvicorn

    s = get_settings()
    kwargs: dict[str, Any] = {}
    if s.tls_cert_file and s.tls_key_file:
        kwargs = {"ssl_certfile": s.tls_cert_file, "ssl_keyfile": s.tls_key_file}
    uvicorn.run("api.main:app", host=os.environ.get("BIND_HOST", "0.0.0.0"), port=int(os.environ.get("API_PORT", "8443"
                if kwargs else "8000")), log_level="warning", **kwargs)


if __name__ == "__main__":
    main()
