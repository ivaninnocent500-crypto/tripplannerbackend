"""
Trip Orchestrator — migrated to the new Supabase schema.

Coordination logic, resilience wrapping (EngineResilienceWrapper.call_engine),
and AI Gateway integration are unchanged.

The destination existence check queries travel_places.slug instead of the
old Destination.id.

IMPORTANT:
- RulesEngine.evaluate_rules() returns a validation result/status, NOT the
  request itself.
- The original request is preserved.
- supabase_db is used by travel/knowledge engines.
- legacy_db is used only for GenerationLog operational/audit data.
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
from app.db.models import GenerationLog

logger = logging.getLogger(__name__)


class TripOrchestrator:
    """
    Coordinates all travel intelligence engines.

    Database responsibilities:

    supabase_db:
        Used by the travel intelligence engines to query the new
        Supabase-backed knowledge base.

    legacy_db:
        Used only for GenerationLog because operational/audit data
        remains in the legacy application database.
    """

    def __init__(self, supabase_db: Session, legacy_db: Session):
        self.db = supabase_db
        self.legacy_db = legacy_db

    def build_trip(self, request: dict[str, Any]) -> dict[str, Any]:
        start_time = time.monotonic()

        # ================================================================
        # RULE VALIDATION
        # ================================================================
        #
        # FIX:
        # evaluate_rules() returns a validation result, NOT the request.
        #
        # Therefore DO NOT do:
        #
        # request = RulesEngine().evaluate_rules(request)
        #
        # because that would replace the actual request with the validation
        # response.
        #
        # Validate a copy and keep the original request intact.
        # ================================================================

        rules_result = RulesEngine().evaluate_rules(dict(request))

        if (
            rules_result.get("status") != "success"
            or not rules_result.get("validated", False)
        ):
            logger.warning(
                "Rules validation did not pass: %s",
                rules_result,
            )

        # Preserve the original request for all downstream engines.
        request = dict(request)

        # ================================================================
        # REQUEST PARAMETERS
        # ================================================================

        destination_slugs: list[str] = request["destinations"]
        days = request["days"]
        travelers = request["travelers"]

        budget_tier = request.get("budget_tier", "mid")
        month = request.get("month_name", "")
        season = request.get("season", "dry")

        # ================================================================
        # DESTINATION RESOLUTION
        # ================================================================
        #
        # Resolve requested destination slugs against the new
        # travel_places table.
        #
        # resolve_slugs_to_ids() returns:
        #
        # {slug: database_id}
        #
        # This preserves the existing downstream behaviour while moving
        # destination lookup to the new Supabase schema.
        # ================================================================

        slug_to_id = resolve_slugs_to_ids(
            self.db,
            destination_slugs,
        )

        known = set(slug_to_id.keys())

        unmatched = [
            destination
            for destination in destination_slugs
            if destination not in known
        ]

        if unmatched:
            logger.warning(
                "Unmatched destinations in request: %s",
                unmatched,
            )

        # ================================================================
        # ITINERARY ENGINE
        # ================================================================

        itinerary_result = call_engine(
            "ItineraryEngine",
            lambda: ItineraryEngine(self.db).build(request),
            fallback=[],
        )

        # ================================================================
        # OPERATOR ENGINE
        # ================================================================

        operator_result = call_engine(
            "OperatorEngine",
            lambda: OperatorEngine(self.db).rank(
                budget_tier,
                request.get("focus", "wildlife"),
                destination_slugs,
            ),
            fallback=[],
        )

        # ================================================================
        # PACKING ENGINE
        # ================================================================

        packing_result = call_engine(
            "PackingEngine",
            lambda: PackingEngine(self.db).build(
                destination_slugs,
                season,
                budget_tier,
            ),
            fallback=[],
        )

        # ================================================================
        # BUDGET ENGINE
        # ================================================================

        budget_result = call_engine(
            "BudgetEngine",
            lambda: BudgetEngine(
                self.db,
                days,
                travelers,
                budget_tier,
                destination_slugs,
            ).calculate(),
            fallback=None,
        )

        # ================================================================
        # WEATHER ENGINE
        # ================================================================

        weather_forecast_result = call_engine(
            "WeatherEngine.fetch",
            lambda: WeatherEngine(self.db).fetch(
                destination_slugs,
                month,
                days,
            ),
            fallback=[],
        )

        weather_summary = (
            WeatherEngine.summarise(
                weather_forecast_result.value
            )
            if weather_forecast_result.value
            else {
                "average_temp_c": 0,
                "temp_range_c": [0, 0],
                "average_rain_pct": 0,
                "likely_rain_days_over_window": 0,
                "season_label": "Unknown",
            }
        )

        # ================================================================
        # WILDLIFE ENGINE
        # ================================================================

        wildlife_result = call_engine(
            "WildlifeEngine",
            lambda: WildlifeEngine(self.db).fetch_many(
                destination_slugs,
                month,
            ),
            fallback=[],
        )

        wildlife_summary = (
            WildlifeEngine.summarise(
                wildlife_result.value
            )
            if wildlife_result.value
            else {}
        )

        # ================================================================
        # ROUTING ENGINE
        # ================================================================

        routing_result = call_engine(
            "RoutingEngine",
            lambda: RoutingEngine(self.db).full_route(
                destination_slugs
            ),
            fallback=[],
        )

        # ================================================================
        # CONFIDENCE ENGINE
        # ================================================================

        confidence_result = call_engine(
            "ConfidenceEngine",
            lambda: ConfidenceEngine().score(
                weather_score=(
                    100
                    - weather_summary.get(
                        "average_rain_pct",
                        50,
                    )
                ),
                road_score=(
                    int(
                        sum(
                            leg["road_quality_score"]
                            for leg in routing_result.value
                        )
                        / len(routing_result.value)
                    )
                    if routing_result.value
                    else 50
                ),
                wildlife_score=(
                    int(
                        max(
                            wildlife_summary.get(
                                "five_species_top_pct",
                                {0: 50},
                            ).values()
                        )
                    )
                    if wildlife_summary.get(
                        "five_species_top_pct"
                    )
                    else 50
                ),
                operator_score=(
                    int(
                        sum(
                            operator.overall_match
                            for operator in operator_result.value[:3]
                        )
                        / min(
                            3,
                            len(operator_result.value),
                        )
                    )
                    if operator_result.value
                    else 50
                ),
                budget_score=(
                    budget_result.value.confidence_pct
                    if budget_result.value
                    else 50
                ),
            ),
            fallback=None,
        )

        # ================================================================
        # AI INSIGHT CONTEXT
        # ================================================================

        insight_context = {
            "destinations": destination_slugs,

            "wildlife_summary": {
                destination: {
                    "big_five_lift_pct": 40
                }
                for destination in destination_slugs
            },

            "weather_forecast": (
                [
                    forecast.to_dict()
                    for forecast in weather_forecast_result.value
                ]
                if weather_forecast_result.value
                else []
            ),

            "computed_legs": routing_result.value,

            "travel_style": request.get(
                "travel_style",
                "standard",
            ),

            "budget_tier": budget_tier,

            "budget_match_pct": (
                budget_result.value.confidence_pct
                if budget_result.value
                else 50
            ),
        }

        # ================================================================
        # AI INSIGHT ENGINE
        # ================================================================

        insights_result = call_engine(
            "AIInsightEngine",
            lambda: AIInsightEngine(self.db).generate(
                insight_context
            ),
            fallback=[],
        )

        # ================================================================
        # GENERATION TIMING
        # ================================================================

        elapsed_ms = int(
            (time.monotonic() - start_time) * 1000
        )

        # ================================================================
        # DEGRADED STATE
        # ================================================================

        any_degraded = any(
            [
                itinerary_result.degraded,
                operator_result.degraded,
                packing_result.degraded,
                budget_result.degraded,
                weather_forecast_result.degraded,
                wildlife_result.degraded,
                routing_result.degraded,
                confidence_result.degraded,
                insights_result.degraded,
            ]
        )

        # ================================================================
        # BASE TRIP RESPONSE
        # ================================================================

        trip_dict = {
            "title": request.get(
                "title",
                "Your Safari",
            ),

            "confidence": (
                confidence_result.value.__dict__
                if confidence_result.value
                else {
                    "score": 50,
                    "label": "Estimate unavailable",
                    "factors": {},
                }
            ),

            "weather": {
                "summary": weather_summary,
                "forecast": [
                    forecast.to_dict()
                    for forecast in weather_forecast_result.value
                ],
            },

            "wildlife": {
                "summary": wildlife_summary,
                "windows": [
                    window.to_dict()
                    for window in wildlife_result.value
                ],
            },

            "routing": {
                "legs": routing_result.value,
                "total_km": sum(
                    leg["distance_km"]
                    for leg in routing_result.value
                ),
                "total_minutes": sum(
                    leg["duration_min"]
                    for leg in routing_result.value
                ),
            },

            "itinerary": {
                "days": [
                    day.__dict__
                    for day in itinerary_result.value
                ],
                "total_days": len(
                    itinerary_result.value
                ),
            },

            "operators": {
                "cards": [
                    operator.to_dict()
                    for operator in operator_result.value
                ],
            },

            "budget": (
                budget_result.value.to_dict()
                if budget_result.value
                else None
            ),

            "packing": [
                item.to_dict()
                for item in packing_result.value
            ],

            "insights": [
                insight.to_dict()
                for insight in insights_result.value
            ],

            "generation_meta": {
                "generation_time_ms": elapsed_ms,

                "degraded": any_degraded,

                "unmatched_destinations": unmatched,

                # Allows clients/UI to distinguish real routing data
                # from honest fallback estimates.
                "routing_used_fallback": (
                    any(
                        leg.get("source") == "fallback_estimate"
                        for leg in routing_result.value
                    )
                    if routing_result.value
                    else False
                ),
            },
        }

        # ================================================================
        # AI GATEWAY
        # ================================================================

        ai_gateway_start = time.monotonic()

        ai_gateway = get_ai_gateway()

        ai_result = ai_gateway.enhance_trip(
            trip_dict
        )

        ai_gateway_elapsed_ms = int(
            (time.monotonic() - ai_gateway_start) * 1000
        )

        trip_dict["ai_gateway_status"] = {
            "ai_enabled": ai_gateway.enabled,

            "client_ready": ai_gateway.is_available(),

            "fallback": (
                "base-json"
                if not ai_result.available
                else "none"
            ),

            "enhancement_time_ms": (
                ai_gateway_elapsed_ms
            ),
        }

        ai_enhancements = ai_result.to_dict()

        # ================================================================
        # GENERATION LOG
        # ================================================================

        try:
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
        except Exception as log_err:
            self.legacy_db.rollback()
            logger.error("Failed to write GenerationLog: %s", log_err)

        # ================================================================
        # FINAL RESPONSE
        # ================================================================

        return {
            "trip": trip_dict,
            "ai_enhancements": ai_enhancements,
        }
