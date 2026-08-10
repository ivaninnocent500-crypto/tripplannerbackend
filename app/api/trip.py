"""
Trip planner API routes.

This is the FIRST real FastAPI route file for this project — per the
master migration prompt's own rule ("If an endpoint does not exist, do
not silently fake it in Kotlin. Document it as a backend contract
requirement"), the Android TravelApi.kt must be built against THIS file,
not the conceptual /api/... list from the planning document. Only routes
that are actually implemented and wired to real engines are exposed here.

Endpoints in this delivery:
    GET /api/health
    POST /api/itineraries/generate

Endpoints intentionally NOT included (see schemas.py for why):
    /api/assistant/message -> Jabari stays on its current direct-Gemini
                                architecture per explicit instruction;
                                not part of this migration.
    /api/inquiries -> no InquiryEngine/persistence exists yet;
                                wiring this now would mean inventing
                                storage rather than connecting real code.
    /api/destinations/* -> no DestinationEngine/aggregation service
                                exists yet in the shared code; see
                                MIGRATION_NOTES.md for the follow-up.
    /api/operators/compare -> OperatorEngine.rank() exists and IS wired
                                below via the trip response's operators
                                block; a standalone comparison endpoint
                                would need new engine logic, not yet built.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.schemas import (
    ErrorResponse,
    GenerateTripRequest,
    GenerateTripResponse,
    HealthResponse,
    ResponseMetadata,
)
from app.core.orchestrator import TripOrchestrator
from app.db.session import (
    DatabaseNotConfiguredError,
    check_legacy_connection,
    check_supabase_connection,
    get_legacy_db,
    get_supabase_db,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["trip"])


@router.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    """
    Unauthenticated on purpose — Render's own health checks and simple
    uptime monitors need to hit this without a key. It reports connection
    status but never leaks connection strings or credentials.
    """
    supabase_ok = False
    legacy_ok = False

    # Check Supabase Database Connection
    try:
        supabase_ok = check_supabase_connection()
        if not supabase_ok:
            logger.error("❌ Health Check: check_supabase_connection() returned False.")
    except Exception as exc:
        logger.exception("❌ Health Check - Supabase Connection Exception: %s", exc)

    # Check Legacy Database Connection
    try:
        legacy_ok = check_legacy_connection()
        if not legacy_ok:
            logger.error("❌ Health Check: check_legacy_connection() returned False.")
    except Exception as exc:
        logger.exception("❌ Health Check - Legacy DB Connection Exception: %s", exc)

    # Check AI Gateway Environment Flag
    ai_enabled = os.environ.get("ATI_AI_ENABLED", "false").lower() == "true"
    if not ai_enabled:
        logger.info("ℹ️ Health Check: ATI_AI_ENABLED is set to False or not configured.")

    return HealthResponse(
        status="ok" if (supabase_ok and legacy_ok) else "degraded",
        supabase_connected=supabase_ok,
        legacy_db_connected=legacy_ok,
        ai_gateway_enabled=ai_enabled,
    )


@router.post(
    "/itineraries/generate",
    response_model=GenerateTripResponse,
    responses={
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
def generate_itinerary(
    request: GenerateTripRequest,
    supabase_db: Session = Depends(get_supabase_db),
    legacy_db: Session = Depends(get_legacy_db),
    _auth: None = Depends(require_api_key),
) -> GenerateTripResponse:
    """
    The single most important endpoint in this migration — this is the
    HTTP front door for what your master prompt calls the "new flow":

        Android -> TravelRepository.generateItinerary()
                -> POST /api/itineraries/generate
                -> TripOrchestrator.build_trip()
                -> Supabase + engines + AI gateway
                -> structured JSON
                -> Android ItineraryViewModel

    No business logic lives here. This route's only job is: validate the
    request shape (Pydantic), open sessions, call the orchestrator exactly
    as tests/test_two_database_wiring.py expects it to be called, and wrap
    the result with response metadata. Every calculation, retrieval, and
    scoring decision stays inside orchestrator.py / app/engines/*.py,
    unchanged.
    """
    request_id = str(uuid.uuid4())
    started = time.monotonic()

    orchestrator = TripOrchestrator(supabase_db=supabase_db, legacy_db=legacy_db)

    try:
        result = orchestrator.build_trip(request.to_orchestrator_dict())
    except DatabaseNotConfiguredError as exc:
        logger.error("Database not configured for request %s: %s", request_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Exception as exc: # noqa: BLE001
        # The orchestrator already wraps each engine call in
        # call_engine()/EngineResilienceWrapper with per-engine fallbacks
        # (see orchestrator.py — itinerary_result, operator_result, etc.
        # all degrade gracefully rather than raising). An exception
        # reaching this point means something OUTSIDE that resilience
        # wrapping failed (e.g. the initial RulesEngine().apply() call,
        # or a DB connection dying mid-request) — a genuine 500, not a
        # degraded-but-successful trip.
        logger.exception("Unhandled error generating trip for request %s", request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Trip generation failed unexpectedly. Please try again.",
        ) from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    degraded = bool(result.get("trip", {}).get("generation_meta", {}).get("degraded", False))

    return GenerateTripResponse(
        trip=result["trip"],
        ai_enhancements=result["ai_enhancements"],
        metadata=ResponseMetadata(
            request_id=request_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            source="render-backend",
            data_freshness=f"{elapsed_ms}ms generation time",
            degraded=degraded,
        ),
    )

