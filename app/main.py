"""
FastAPI application entrypoint for tripplannerbackend.

Run locally:
    uvicorn app.main:app --reload --port 8000

Render deploys this via the start command (see render.yaml / dashboard
"Start Command"), typically:
    uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.trip import router as trip_router

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Africa Travel Intelligence API",
    description="Production travel-data and AI orchestration layer for the Africa Safari Guide app.",
    version="1.0.0",
)

# CORS: the Android app itself doesn't need CORS (native HTTP client,
# not a browser), but this stays permissive-by-header-allowlist rather
# than wide open, in case a future web client or admin dashboard is added.
# Tighten allow_origins to real domains before that happens.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ALLOWED_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-Api-Key", "Content-Type"],
)

app.include_router(trip_router, prefix="/api")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Last-resort catch-all so an unexpected error returns a clean JSON
    error body (matching ErrorResponse's shape) instead of an HTML
    traceback page — the Android ApiResult.Error mapper expects JSON.
    """
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "code": "INTERNAL_ERROR",
            "message": "An unexpected error occurred.",
            "retryable": True,
        },
    )


@app.get("/")
def root() -> dict:
    return {"service": "Africa Travel Intelligence API", "status": "running", "docs": "/docs"}

