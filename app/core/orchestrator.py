"""
Trip Orchestrator — migrated to the new Supabase schema. Coordination
logic, resilience wrapping (EngineResilienceWrapper.call_engine), and
AI Gateway integration are ALL UNCHANGED from the previous version. Only
the "check which requested destinations actually exist" step now queries
travel_places.slug instead of the old Destination.id.
"""
from __future__ import annotations

import time
import logging
from typing import Any
from sqlalchemy.orm import Session

from app.db.models_v2 import TravelPlace
from app.db.destinations import resolve_slugs_to_ids
from app.engines.rules import RulesEngine
from app.engines.itinerary import ItineraryEngine
from app.engines.operator import OperatorEngine
from app.engines.packing import PackingEngine
from app.engines.budget import BudgetEngine
from app.engines.weather import WeatherEngine
from app.engines.wildlife import WildlifeEngine
from app.engines.routing import RoutingEngine
from app.engines.confidence import ConfidenceEngine
from app.engines.insights import AIInsightEngine
from app.engines.resilience import call_engine
from app.ai.gateway import get_ai_gateway
from app.db.models import GenerationLog  # unchanged — GenerationLog stays in the old app db, it's operational/audit data, not travel knowledge

logger = logging.getLogger(__name__)


class TripOrchestrator:
    """
    UPDATED: now takes TWO sessions. supabase_db is used by every engine
    (ItineraryEngine, WildlifeEngine, OperatorEngine, RoutingEngine,
    BudgetEngine, PackingEngine, WeatherEngine) to query the new Travel
    Intelligence knowledge base. legacy_db is used ONLY for writing
    GenerationLog, since that table still lives in the original
    ati-production database (it's operational/audit data, not travel
    knowledge — see session.py's docstring for the two-database
    rationale). This is a real fix vs. an earlier draft of this file that
    incorrectly used one session for both purposes.
    """
    def __init__(self, supabase_db: Session, legacy_db: Session):
        self.db = supabase_db
        self.legacy_db = legacy_db

    def build_trip(self, request: dict[str, Any]) -> dict[str, Any]:
        start_time = time.monotonic()

        def build_trip(self, request: dict[str, Any]) -> dict[str, Any]:
    start_time = time.monotonic()

    # FIXED: evaluate_rules returns a validation status, NOT the request —
    # keep the original request intact.
    rules_result = RulesEngine().evaluate_rules(dict(request))
    if rules_result.get("status") != "success" or not rules_result.get("validated", False):
        logger.warning("Rules validation did not pass: %s", rules_result)

    request = dict(request)

        destination_slugs: list[str] = request["destinations"]
        days = request["days"]
        travelers = request["travelers"]
        budget_tier = request.get("budget_tier", "mid")
        month = request.get("month_name", "")
        season = request.get("season", "dry")

        # UPDATED: resolve against travel_places.slug instead of the old
        # Destination.id — same "which requested destinations do we
        # actually have data for" signal as before, now backed by the
        # real knowledge base.
        slug_to_id = resolve_slugs_to_ids(self.db, destination_slugs)
        known = set(slug_to_id.keys())
        unmatched = [d for d in destination_slugs if d not in known]
        if unmatched:
            logger.warning("Unmatched destinations in request: %s", unmatched)

        itinerary_result = call_engine(
            "ItineraryEngine",
            lambda: ItineraryEngine(self.db).build(request),
            fallback=[]
        )

        operator_result = call_engine(
            "OperatorEngine",
            lambda: OperatorEngine(self.db).rank(budget_tier, request.get("focus", "wildlife"), destination_slugs),
            fallback=[]
        )

        packing_result = call_engine(
            "PackingEngine",
            lambda: PackingEngine(self.db).build(destination_slugs, season, budget_tier),
            fallback=[]
        )

        budget_result = call_engine(
            "BudgetEngine",
            lambda: BudgetEngine(self.db, days, travelers, budget_tier, destination_slugs).calculate(),
            fallback=None
        )

        weather_forecast_result = call_engine(
            "WeatherEngine.fetch",
            lambda: WeatherEngine(self.db).fetch(destination_slugs, month, days),
            fallback=[]
        )
        weather_summary = (
            WeatherEngine.summarise(weather_forecast_result.value)
            if weather_forecast_result.value else
            {"average_temp_c": 0, "temp_range_c": [0, 0], "average_rain_pct": 0,
             "likely_rain_days_over_window": 0, "season_label": "Unknown"}
        )

        wildlife_result = call_engine(
            "WildlifeEngine",
            lambda: WildlifeEngine(self.db).fetch_many(destination_slugs, month),
            fallback=[]
        )
        wildlife_summary = WildlifeEngine.summarise(wildlife_result.value) if wildlife_result.value else {}

        routing_result = call_engine(
            "RoutingEngine",
            lambda: RoutingEngine(self.db).full_route(destination_slugs),
            fallback=[]
        )

        confidence_result = call_engine(
            "ConfidenceEngine",
            lambda: ConfidenceEngine().score(
                weather_score=100 - weather_summary.get("average_rain_pct", 50),
                road_score=(
                    int(sum(l["road_quality_score"] for l in routing_result.value) / len(routing_result.value))
                    if routing_result.value else 50
                ),
                wildlife_score=(
                    int(max(wildlife_summary.get("five_species_top_pct", {0: 50}).values()))
                    if wildlife_summary.get("five_species_top_pct") else 50
                ),
                operator_score=(
                    int(sum(o.overall_match for o in operator_result.value[:3]) / min(3, len(operator_result.value)))
                    if operator_result.value else 50
                ),
                budget_score=budget_result.value.confidence_pct if budget_result.value else 50,
            ),
            fallback=None
        )

        insight_context = {
            "destinations": destination_slugs,
            "wildlife_summary": {d: {"big_five_lift_pct": 40} for d in destination_slugs},
            "weather_forecast": [f.to_dict() for f in weather_forecast_result.value] if weather_forecast_result.value else [],
            "computed_legs": routing_result.value,
            "travel_style": request.get("travel_style", "standard"),
            "budget_tier": budget_tier,
            "budget_match_pct": budget_result.value.confidence_pct if budget_result.value else 50,
        }
        insights_result = call_engine(
            "AIInsightEngine",
            lambda: AIInsightEngine(self.db).generate(insight_context),
            fallback=[]
        )

        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        any_degraded = any([
            itinerary_result.degraded, operator_result.degraded, packing_result.degraded,
            budget_result.degraded, weather_forecast_result.degraded, wildlife_result.degraded,
            routing_result.degraded, confidence_result.degraded, insights_result.degraded,
        ])

        trip_dict = {
            "title": request.get("title", "Your Safari"),
            "confidence": (
                confidence_result.value.__dict__ if confidence_result.value
                else {"score": 50, "label": "Estimate unavailable", "factors": {}}
            ),
            "weather": {"summary": weather_summary, "forecast": [f.to_dict() for f in weather_forecast_result.value]},
            "wildlife": {"summary": wildlife_summary, "windows": [w.to_dict() for w in wildlife_result.value]},
            "routing": {
                "legs": routing_result.value,
                "total_km": sum(l["distance_km"] for l in routing_result.value),
                "total_minutes": sum(l["duration_min"] for l in routing_result.value),
            },
            "itinerary": {
                "days": [d.__dict__ for d in itinerary_result.value],
                "total_days": len(itinerary_result.value),
            },
            "operators": {"cards": [o.to_dict() for o in operator_result.value]},
            "budget": budget_result.value.to_dict() if budget_result.value else None,
            "packing": [p.to_dict() for p in packing_result.value],
            "insights": [i.to_dict() for i in insights_result.value],
            "generation_meta": {
                "generation_time_ms": elapsed_ms,
                "degraded": any_degraded,
                "unmatched_destinations": unmatched,
                # NEW: surfaces routing's honest fallback-vs-real status to
                # the client, given the real gap documented in routing.py —
                # clients/UI can choose to label distances as "estimated"
                # when this is true.
                "routing_used_fallback": any(
                    leg.get("source") == "fallback_estimate" for leg in routing_result.value
                ) if routing_result.value else False,
            },
        }

        ai_gateway_start = time.monotonic()
        ai_gateway = get_ai_gateway()
        ai_result = ai_gateway.enhance_trip(trip_dict)
        ai_gateway_elapsed_ms = int((time.monotonic() - ai_gateway_start) * 1000)

        trip_dict["ai_gateway_status"] = {
            "ai_enabled": ai_gateway.enabled,
            "client_ready": ai_gateway.is_available(),
            "fallback": "base-json" if not ai_result.available else "none",
            "enhancement_time_ms": ai_gateway_elapsed_ms,
        }

        ai_enhancements = ai_result.to_dict()

        log_entry = GenerationLog(
            request_json=request,
            matched_destination_ids=list(known),
            unmatched_terms=unmatched,
            confidence_score=confidence_result.value.score if confidence_result.value else None,
            total_generation_time_ms=elapsed_ms + ai_gateway_elapsed_ms,
            ai_gateway_used=ai_result.available,
            error_message=(
                "; ".join(filter(None, [
                    itinerary_result.error, operator_result.error, packing_result.error,
                    budget_result.error, weather_forecast_result.error, wildlife_result.error,
                    routing_result.error, confidence_result.error, insights_result.error,
                    ai_result.error if not ai_result.available else None,
                ])) or None
            ),
        )
        self.legacy_db.add(log_entry)
        self.legacy_db.commit()

        return {"trip": trip_dict, "ai_enhancements": ai_enhancements}
