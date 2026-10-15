"""CASCADE API + dashboard (phase 0: health check only)."""

from __future__ import annotations

import platform

from fastapi import FastAPI

app = FastAPI(title="CASCADE API", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "api", "arch": platform.machine()}
