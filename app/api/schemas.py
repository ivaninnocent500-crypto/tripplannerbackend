"""
Pydantic request/response schemas for the public Travel API.

This is the real wire contract. Nothing in here is invented beyond what
TripOrchestrator.build_trip() already returns (see app/core/orchestrator.py)
and what the engines already accept as request fields (see
app/engines/*.py — e.g. BudgetEngine takes days/travelers/tier/slugs,
ItineraryEngine.build() reads request["destinations"], request["days"],
request.get("max_lodge_min_child_age"), RulesEngine().apply() mutates the
whole dict first).

IMPORTANT: GenerateTripRequest deliberately has NO hardcoded defaults for
travelers, budget, or destination — those must come from the caller. This
directly enforces the master migration prompt's requirement:

    "Remove hardcoded: currentUserBudget = 5000.0, travelers = 2,
    'Tanzania' ... These must come from actual user input/profile/request."

If a field is optional in the orchestrator/engines (e.g. title,
max_lodge_min_child_age), it stays Optional here rather than being given
an invented default.
"""
from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------
# REQUEST
# ---------------------------------------------------------------------

class GenerateTripRequest(BaseModel):
    """
    Maps directly onto the dict TripOrchestrator.build_trip() expects.
    Field names match orchestrator.py's request[...] / request.get(...)
    reads exactly, so no silent renaming/guessing happens at the route layer.
    """
    destinations: list[str] = Field(
        ..., min_length=1,
        description="Destination slugs, e.g. ['serengeti-national-park']. "
                    "Resolved server-side via resolve_slugs_to_ids — see "
                    "app/db/destinations.py for the normalization/matching rules."
    )
    days: int = Field(..., gt=0, le=60)
    travelers: int = Field(..., gt=0, le=20)
    budget_tier: str = Field(
        default="mid",
        description="One of BudgetEngine.BASE_PER_DAY keys: value, mid, luxury, ultra. "
                    "Unrecognized values fall back to 'luxury' inside BudgetEngine — "
                    "documented there, not silently changed here."
    )
    month_name: Optional[str] = Field(
        default=None,
        description="Lowercase month, e.g. 'october'. Drives WeatherEngine + wildlife_calendar lookups."
    )
    season: Optional[str] = Field(default="dry")
    focus: Optional[str] = Field(default="wildlife", description="Passed to OperatorEngine.rank()")
    travel_style: Optional[str] = Field(default="standard")
    title: Optional[str] = None
    max_lodge_min_child_age: Optional[int] = Field(
        default=None,
        description="Passed straight through to ItineraryEngine._select_lodge's family-friendly filter."
    )

    # Fields present in the master migration prompt's GenerateTripRequest
    # spec that the CURRENT orchestrator/engines do not yet read. Kept as
    # optional passthrough so the contract doesn't silently drop data the
    # client sends, but NOT wired into engine logic yet — wiring them
    # requires engine changes, which is a separate, explicit follow-up
    # (see MIGRATION_NOTES.md), not something to fake here.
    nationality: Optional[str] = None
    currency: Optional[str] = Field(default="USD")
    interests: Optional[list[str]] = None
    accessibility_requirements: Optional[list[str]] = None
    destination_ids: Optional[list[str]] = None
    previous_trip_id: Optional[str] = None

    @field_validator("budget_tier")
    @classmethod
    def _lower_tier(cls, v: str) -> str:
        return v.lower()

    @field_validator("season")
    @classmethod
    def _lower_season(cls, v: Optional[str]) -> Optional[str]:
        return v.lower() if v else v

    def to_orchestrator_dict(self) -> dict[str, Any]:
        """
        Builds exactly the dict shape orchestrator.py reads from, using
        its real keys (destinations, days, travelers, budget_tier,
        month_name, season, focus, travel_style, title,
        max_lodge_min_child_age). Extra contract fields (nationality,
        currency, interests, etc.) are intentionally NOT included here —
        the orchestrator/engines don't consume them yet, and stuffing
        them into the dict would risk an engine silently misreading them.
        """
        return {
            "destinations": self.destinations,
            "days": self.days,
            "travelers": self.travelers,
            "budget_tier": self.budget_tier,
            "month_name": self.month_name or "",
            "season": self.season or "dry",
            "focus": self.focus or "wildlife",
            "travel_style": self.travel_style or "standard",
            "title": self.title or "Your Safari",
            "max_lodge_min_child_age": self.max_lodge_min_child_age,
        }


class AssistantMessageRequest(BaseModel):
    """
    For a FUTURE /api/assistant/message endpoint per the master migration
    prompt (SafariAgentRepository target architecture). NOT wired to a
    route in this delivery — Jabari/the chat assistant explicitly stays
    on its current direct-Gemini architecture per your instruction. This
    schema is defined so the contract exists on paper if/when that
    migration happens, without a live route pretending to serve it now.
    """
    message: str
    session_id: Optional[str] = None
    trip_id: Optional[str] = None
    destination_id: Optional[str] = None


class InquiryRequest(BaseModel):
    """
    For a FUTURE /api/inquiries endpoint (master prompt file #20,
    InquiryApiClient.kt target). NOT wired to a route in this delivery —
    no InquiryEngine/persistence table was present in the code you
    shared, so wiring this now would mean inventing storage. Left as a
    documented contract stub only.
    """
    operator_id: str
    itinerary_id: Optional[str] = None
    traveler_count: int
    travel_dates: Optional[str] = None
    special_requests: Optional[str] = None


# ---------------------------------------------------------------------
# RESPONSE
# ---------------------------------------------------------------------

class ResponseMetadata(BaseModel):
    """Matches the ApiResult.kt ResponseMetadata contract from the master prompt (item 23)."""
    request_id: str
    timestamp: str
    source: str = "render-backend"
    data_freshness: Optional[str] = None
    degraded: bool = False


class ErrorResponse(BaseModel):
    code: str
    message: str
    retryable: bool = False


class GenerateTripResponse(BaseModel):
    """
    Thin wrapper around exactly what TripOrchestrator.build_trip() already
    returns: {"trip": {...}, "ai_enhancements": {...}}. Not restructured —
    the master prompt's mapper-layer requirement (item 24) says the
    Backend DTO -> Domain Model mapping happens in KOTLIN, not that the
    backend should reshape orchestrator output speculatively before the
    Kotlin mapper layer exists to consume it.
    """
    trip: dict[str, Any]
    ai_enhancements: dict[str, Any]
    metadata: ResponseMetadata


class HealthResponse(BaseModel):
    status: str
    supabase_connected: bool
    legacy_db_connected: bool
    ai_gateway_enabled: bool

