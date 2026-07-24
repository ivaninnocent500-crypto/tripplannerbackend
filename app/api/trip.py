"""
Trip generation + AI Gateway Q&A endpoints. UPDATED: generate_trip now
depends on get_supabase_db instead of get_db, since TripOrchestrator's
engines all query the new Travel Intelligence schema. GenerationLog
writes still happen (orchestrator.py imports the legacy GenerationLog
model directly and writes via a separate legacy session internally —
see the note in orchestrator.py about why GenerationLog stays on the old
DB during the migration period).

ask_trip_question is UNCHANGED — it doesn't touch any database, it only
calls the AI Gateway over data the client already sent back.
"""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_supabase_db, get_db
from app.api.auth import require_api_key
from app.api.schemas import (
    TripRequest, TripResponse, AskTripQuestionRequest, AskTripQuestionResponse
)
from app.core.orchestrator import TripOrchestrator
from app.ai.gateway import get_ai_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trip", tags=["trip"])


@router.post("/generate", response_model=TripResponse, dependencies=[Depends(require_api_key)])
def generate_trip(
    request: TripRequest,
    supabase_db: Session = Depends(get_supabase_db),
    legacy_db: Session = Depends(get_db),
) -> TripResponse:
    try:
        orchestrator = TripOrchestrator(supabase_db, legacy_db)
        result = orchestrator.build_trip(request.model_dump())
        return TripResponse(**result)
    except RuntimeError as e:
        # Specifically catches the "SUPABASE_DATABASE_URL not configured"
        # error from session.py with a clear 503, distinct from a generic
        # 500 — makes the pending-connection-string state visible in the
        # API response itself, not just server logs.
        logger.error("Trip generation failed: Supabase not configured: %s", str(e))
        raise HTTPException(
            status_code=503,
            detail="Travel Intelligence database is not yet configured. Please try again later."
        )
    except Exception as e:
        logger.error("Trip generation failed unexpectedly: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Trip generation failed. Please try again.")


@router.post("/ask", response_model=AskTripQuestionResponse, dependencies=[Depends(require_api_key)])
def ask_trip_question(request: AskTripQuestionRequest) -> AskTripQuestionResponse:
    ai_gateway = get_ai_gateway()

    if not ai_gateway.is_available():
        return AskTripQuestionResponse(
            available=False,
            error="AI Gateway is not currently available. Trip details are still fully accessible in the trip data."
        )

    result = ai_gateway.answer_question(request.trip, request.question)
    return AskTripQuestionResponse(
        available=result.available,
        answer=result.summary,
        error=result.error,
    )
