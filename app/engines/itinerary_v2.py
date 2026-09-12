"""
itinerary_v2
============

Top-level orchestrator for itinerary generation.

Pipeline:

    Trip Request
        |
        v
    RulesEngine
        |
        v
    RouteGeographyEngine
        |
        v
    Destination Feasibility
        |
        v
    Re-run RouteGeographyEngine for final route
        |
        v
    allocate_days_for_route()
        |
        v
    Day Records
        |
        v
    DayArchetypeEngine
        |
        v
    ItineraryPlanningEngine
        |
        v
    ScheduleRepairEngine
        |
        v
    ValidationEngine
        |
        v
    Persisted Cabinet

Important
---------

The requested trip duration is authoritative.

If a user requests 7 days, the generated Cabinet must contain
exactly 7 calendar days.

Destination feasibility happens before day allocation.

TRANSIT days are calendar days inside the requested trip duration.
They are never added on top of the requested duration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.engines.day_archetype import DayArchetypeEngine
from app.engines.itineraryPlanningEngine import (
    ItineraryPlanningEngine,
)
from app.engines.pipeline_adapters import (
    allocate_days_for_route,
    archetypes_by_day_number,
    day_records_from_route_analysis,
    find_feasible_destination_order,
    overnight_required_from_day_plan,
    schedule_input_from_cabinet,
    transit_days_from_day_plan,
)
from app.engines.route_geography import (
    RouteGeographyEngine,
)
from app.engines.rules import RulesEngine
from app.engines.schedule_repair import (
    ScheduleRepairEngine,
)
from app.engines.validation import (
    ValidationEngine,
)


logger = logging.getLogger(
    __name__
)


class ItineraryGenerationError(
    Exception
):
    """Raised when the itinerary generation pipeline cannot proceed."""


@dataclass
class ItineraryGenerationResult:
    """Complete result of the itinerary generation pipeline."""

    cabinet: Any | None = None

    rules_result: dict[
        str,
        Any,
    ] | None = None

    route_analysis: Any | None = None

    day_plan: Any | None = None

    schedule_repair_result: Any | None = None

    validation_result: dict[
        str,
        Any,
    ] | None = None

    warnings: list[str] = field(
        default_factory=list
    )

    destination_ids: list[str] = field(
        default_factory=list
    )

    @property
    def succeeded(
        self,
    ) -> bool:

        return (
            self.cabinet is not None
            and self.validation_result
            is not None
            and self.validation_result.get(
                "status"
            )
            == "valid"
        )

    @property
    def status(
        self,
    ) -> str:

        if self.cabinet is None:
            return "failed"

        if self.validation_result is None:
            return "unvalidated"

        return self.validation_result.get(
            "status",
            "unknown",
        )


class ItineraryOrchestrator:
    """Sequences the complete itinerary generation pipeline."""

    def __init__(
        self,
        db: Session,
    ):
        self.db = db

        self.rules_engine = (
            RulesEngine()
        )

        self.route_geography_engine = (
            RouteGeographyEngine(
                db
            )
        )

        self.day_archetype_engine = (
            DayArchetypeEngine()
        )

        self.itinerary_planning_engine = (
            ItineraryPlanningEngine(
                db
            )
        )

        self.schedule_repair_engine = (
            ScheduleRepairEngine()
        )

        self.validation_engine = (
            ValidationEngine(
                db
            )
        )

    # ========================================================================
    # MAIN PIPELINE
    # ========================================================================

    def generate(
        self,
        request: dict[str, Any],
        destination_ids: list[str],
        *,
        allow_coordinate_estimate: bool = False,
    ) -> ItineraryGenerationResult:

        result = (
            ItineraryGenerationResult()
        )

        # ====================================================================
        # 1. REQUEST NORMALIZATION
        # ====================================================================

        cleaned_destination_ids = list(
            dict.fromkeys(
                str(destination_id)
                for destination_id in destination_ids
                if destination_id
            )
        )

        if not cleaned_destination_ids:
            raise ItineraryGenerationError(
                "No destinations were provided."
            )

        total_days = self._safe_int(
            request.get(
                "days"
            ),
            0,
        )

        if total_days <= 0:
            raise ItineraryGenerationError(
                "Trip duration must be greater than zero."
            )

        # ====================================================================
        # 2. RULES
        # ====================================================================

        rules_input = (
            self._rules_input_from_request(
                request,
                cleaned_destination_ids,
            )
        )

        rules_result = (
            self.rules_engine.evaluate_rules(
                rules_input
            )
        )

        result.rules_result = (
            rules_result
        )

        if not rules_result[
            "validated"
        ]:

            logger.warning(
                "Itinerary request failed RulesEngine validation: %s",
                rules_result[
                    "errors"
                ],
            )

            result.warnings.extend(
                rules_result[
                    "errors"
                ]
            )

            result.warnings.extend(
                rules_result[
                    "warnings"
                ]
            )

            return result

        result.warnings.extend(
            rules_result[
                "warnings"
            ]
        )

        # ====================================================================
        # 3. INITIAL ROUTE GEOGRAPHY
        # ====================================================================
        #
        # This analysis is used only to understand the originally requested
        # route and its factual geography.
        #
        # If feasibility later removes destinations, we MUST rerun this
        # engine for the final route.
        # ====================================================================

        route_analysis = (
            self.route_geography_engine.analyze(
                cleaned_destination_ids,
                allow_coordinate_estimate=(
                    allow_coordinate_estimate
                ),
            )
        )

        result.route_analysis = (
            route_analysis
        )

        result.warnings.extend(
            route_analysis.warnings
        )

        if (
            route_analysis.stop_count
            == 0
        ):
            raise ItineraryGenerationError(
                "RouteGeographyEngine could not resolve any "
                "destinations for this request."
            )

        # ====================================================================
        # 4. DESTINATION METADATA
        # ====================================================================

        destination_meta = (
            self.itinerary_planning_engine
            .fetch_destination_meta(
                cleaned_destination_ids
            )
        )

        # ====================================================================
        # 5. DESTINATION FEASIBILITY
        # ====================================================================

        (
            feasible_destination_ids,
            feasibility_warnings,
        ) = (
            find_feasible_destination_order(
                destination_ids=(
                    cleaned_destination_ids
                ),
                meta=destination_meta,
                total_days=total_days,
                route_analysis=route_analysis,
            )
        )

        result.warnings.extend(
            feasibility_warnings
        )

        if not feasible_destination_ids:
            raise ItineraryGenerationError(
                "No feasible destination route could be constructed "
                f"within {total_days} days."
            )

        if (
            feasible_destination_ids
            != cleaned_destination_ids
        ):

            logger.warning(
                "Destination route reduced for feasibility. "
                "Requested=%s Final=%s",
                cleaned_destination_ids,
                feasible_destination_ids,
            )

        # The feasible route is now authoritative.
        cleaned_destination_ids = (
            feasible_destination_ids
        )

        result.destination_ids = list(
            cleaned_destination_ids
        )

        # ====================================================================
        # 6. RE-RUN ROUTE GEOGRAPHY
        # ====================================================================
        #
        # IMPORTANT.
        #
        # The original route may have been:
        #
        # A -> B -> C -> D
        #
        # while the final route is:
        #
        # A -> B -> C
        #
        # Therefore the old RouteAnalysis must never be passed to the planner.
        # ====================================================================

        route_analysis = (
            self.route_geography_engine.analyze(
                cleaned_destination_ids,
                allow_coordinate_estimate=(
                    allow_coordinate_estimate
                ),
            )
        )

        result.route_analysis = (
            route_analysis
        )

        result.warnings.extend(
            route_analysis.warnings
        )

        if (
            route_analysis.stop_count
            != len(
                cleaned_destination_ids
            )
        ):
            raise ItineraryGenerationError(
                "Final RouteGeographyEngine analysis does not contain "
                "all selected destinations: "
                f"{route_analysis.stop_count} != "
                f"{len(cleaned_destination_ids)}"
            )

        # ====================================================================
        # 7. FINAL TRAVEL STYLE
        # ====================================================================

        travel_style = (
            request.get(
                "travel_style"
            )
            or []
        )

        if isinstance(
            travel_style,
            str,
        ):
            travel_style = [
                travel_style
            ]

        travel_style = list(
            dict.fromkeys(
                str(style).lower()
                for style in travel_style
                if style
            )
        )

        # ====================================================================
        # 8. FINAL DESTINATION METADATA
        # ====================================================================
        #
        # Re-fetch using the final selected route so no later stage depends
        # on metadata for destinations that were removed.
        # ====================================================================

        destination_meta = (
            self.itinerary_planning_engine
            .fetch_destination_meta(
                cleaned_destination_ids
            )
        )

        # ====================================================================
        # 9. EXACT DAY ALLOCATION
        # ====================================================================

        (
            day_allocation,
            allocation_warnings,
        ) = (
            allocate_days_for_route(
                destination_ids=(
                    cleaned_destination_ids
                ),
                meta=destination_meta,
                total_days=total_days,
                travel_style=travel_style,
                route_analysis=route_analysis,
            )
        )

        result.warnings.extend(
            allocation_warnings
        )

        # HARD INVARIANT:
        # The destination allocation MUST equal the requested calendar days.

        if len(
            day_allocation
        ) != len(
            cleaned_destination_ids
        ):
            raise ItineraryGenerationError(
                "Destination allocation length does not match selected "
                "destination count: "
                f"{len(day_allocation)} != "
                f"{len(cleaned_destination_ids)}"
            )

        if any(
            value < 1
            for value in day_allocation
        ):
            raise ItineraryGenerationError(
                "A selected destination received fewer than one calendar "
                f"day: {day_allocation}"
            )

        allocated_destination_days = sum(
            day_allocation
        )

        if (
            allocated_destination_days
            != total_days
        ):
            raise ItineraryGenerationError(
                "Destination allocation does not match requested trip "
                f"duration: {allocated_destination_days} != "
                f"{total_days}"
            )

        logger.info(
            "Final itinerary allocation: destinations=%s allocation=%s "
            "total_days=%s",
            cleaned_destination_ids,
            day_allocation,
            total_days,
        )

        # ====================================================================
        # 10. PRE-PLANNING DAY RECORDS
        # ====================================================================
        #
        # This must now generate exactly total_days records.
        # ====================================================================

        day_records = (
            day_records_from_route_analysis(
                route_analysis=route_analysis,
                destination_order=(
                    cleaned_destination_ids
                ),
                nights_per_destination=(
                    day_allocation
                ),
                total_days=total_days,
            )
        )

        if len(
            day_records
        ) != total_days:
            raise ItineraryGenerationError(
                "Pre-planning day records do not match requested trip "
                f"duration: {len(day_records)} != {total_days}"
            )

        # ====================================================================
        # 11. PRE-PLANNING ARCHETYPES
        # ====================================================================

        pre_planning_day_plan = (
            self.day_archetype_engine.analyze(
                day_records
            )
        )

        result.warnings.extend(
            pre_planning_day_plan.warnings
        )

        # ====================================================================
        # 12. TRANSIT CLASSIFICATION
        # ====================================================================

        transit_days = (
            transit_days_from_day_plan(
                pre_planning_day_plan,
                day_records=day_records,
            )
        )

        # ====================================================================
        # 13. BUILD CABINET
        # ====================================================================
        #
        # The planner receives:
        #
        # final destination IDs
        # exact day allocation
        # route-aware transit classification
        # authoritative final route facts
        #
        # The planner must not independently reconstruct geography.
        # ====================================================================

        try:

            build_result = (
                self.itinerary_planning_engine.build(
                    request=request,
                    destination_ids=(
                        cleaned_destination_ids
                    ),
                    day_allocation=(
                        day_allocation
                    ),
                    transit_days=(
                        transit_days
                    ),
                    route_facts=(
                        route_analysis.legs
                    ),
                )
            )

        except ValueError as exc:

            raise ItineraryGenerationError(
                "ItineraryPlanningEngine could not build a cabinet: "
                f"{exc}"
            ) from exc

        cabinet = (
            build_result.cabinet
        )

        result.cabinet = (
            cabinet
        )

        result.warnings.extend(
            build_result.warnings
        )

        # ====================================================================
        # 14. CABINET DAY COUNT INVARIANT
        # ====================================================================

        cabinet_day_count = len(
            cabinet.shelves
        )

        if (
            cabinet_day_count
            != total_days
        ):
            raise ItineraryGenerationError(
                "ItineraryPlanningEngine produced the wrong number of "
                "calendar days: "
                f"{cabinet_day_count} != requested {total_days}"
            )

        if (
            getattr(
                cabinet,
                "duration_days",
                total_days,
            )
            != total_days
        ):
            raise ItineraryGenerationError(
                "Cabinet duration does not match requested trip duration: "
                f"{cabinet.duration_days} != {total_days}"
            )

        # ====================================================================
        # 15. RECONSTRUCT ACTUAL DESTINATION ALLOCATION
        # ====================================================================

        nights_per_destination = (
            self._nights_per_destination_from_cabinet(
                cabinet
            )
        )

        if sum(
            nights_per_destination
        ) != total_days:
            raise ItineraryGenerationError(
                "Persisted Cabinet destination allocation does not match "
                f"requested duration: "
                f"{sum(nights_per_destination)} != {total_days}"
            )

        # ====================================================================
        # 16. COUNT ACTUAL ACTIVITIES
        # ====================================================================

        activity_counts_by_day = {
            shelf.day_number: sum(
                1
                for drawer in shelf.drawers
                if drawer.activity_type
                == "EXPERIENCE"
            )
            for shelf in cabinet.shelves
        }

        # ====================================================================
        # 17. FINAL DESTINATION ORDER
        # ====================================================================

        final_destination_order = (
            self._destination_order_from_cabinet(
                cabinet
            )
        )

        if (
            final_destination_order
            != cleaned_destination_ids
        ):
            raise ItineraryGenerationError(
                "Persisted Cabinet destination order does not match the "
                "authoritative selected route. "
                f"Expected={cleaned_destination_ids} "
                f"Actual={final_destination_order}"
            )

        # ====================================================================
        # 18. REBUILD DAY RECORDS FROM ACTUAL CABINET
        # ====================================================================

        day_records = (
            day_records_from_route_analysis(
                route_analysis=route_analysis,
                destination_order=(
                    final_destination_order
                ),
                nights_per_destination=(
                    nights_per_destination
                ),
                total_days=cabinet.duration_days,
                activity_counts_by_day=(
                    activity_counts_by_day
                ),
            )
        )

        if len(
            day_records
        ) != total_days:
            raise ItineraryGenerationError(
                "Final day records do not match requested trip duration: "
                f"{len(day_records)} != {total_days}"
            )

        # ====================================================================
        # 19. FINAL DAY CLASSIFICATION
        # ====================================================================

        day_plan = (
            self.day_archetype_engine.analyze(
                day_records
            )
        )

        result.day_plan = (
            day_plan
        )

        result.warnings.extend(
            day_plan.warnings
        )

        # ====================================================================
        # 20. APPLY DAY THEMES
        # ====================================================================

        self._apply_day_themes(
            cabinet,
            day_plan,
        )

        # ====================================================================
        # 21. SCHEDULE REPAIR
        # ====================================================================

        schedule_input = (
            schedule_input_from_cabinet(
                cabinet
            )
        )

        archetypes = (
            archetypes_by_day_number(
                day_plan
            )
        )

        repair_result = (
            self.schedule_repair_engine.repair(
                schedule_input,
                archetypes=archetypes,
            )
        )

        result.schedule_repair_result = (
            repair_result
        )

        result.warnings.extend(
            repair_result.warnings
        )

        if repair_result.actions:

            logger.info(
                "ScheduleRepairEngine applied %s repair action(s) "
                "to cabinet %s.",
                len(
                    repair_result.actions
                ),
                cabinet.id,
            )

            self._apply_repair_actions_to_cabinet(
                cabinet,
                repair_result,
            )

        if not repair_result.fully_repaired:

            result.warnings.append(
                "One or more schedule conflicts remain after "
                "automated repair and require manual review."
            )

        # ====================================================================
        # 22. FINAL VALIDATION
        # ====================================================================

        overnight_required = (
            overnight_required_from_day_plan(
                day_plan
            )
        )

        validation_result = (
            self.validation_engine.validate(
                cabinet,
                extra_warnings=(
                    build_result.warnings
                ),
                overnight_required=(
                    overnight_required
                ),
            )
        )

        result.validation_result = (
            validation_result
        )

        result.warnings.extend(
            validation_result[
                "warnings"
            ]
        )

        return result

    # ========================================================================
    # HELPERS
    # ========================================================================

    @staticmethod
    def _safe_int(
        value: Any,
        default: int = 0,
    ) -> int:

        try:
            return int(
                value
            )
        except (
            TypeError,
            ValueError,
        ):
            return default

    @staticmethod
    def _rules_input_from_request(
        request: dict[str, Any],
        destination_ids: list[str],
    ) -> dict[str, Any]:

        return {
            "days": request.get(
                "days"
            ),
            "travelers": request.get(
                "travelers",
                1,
            ),
            "destination_ids": (
                destination_ids
            ),
            "budget_tier": request.get(
                "budget_tier",
                "mid",
            ),
            "start_date": request.get(
                "start_date"
            ),
            "end_date": request.get(
                "end_date"
            ),
        }

    @staticmethod
    def _nights_per_destination_from_cabinet(
        cabinet: Any,
    ) -> list[int]:

        nights: list[int] = []

        current_destination = None

        for shelf in cabinet.shelves:

            destination_id = (
                shelf.destination_id
            )

            if (
                destination_id
                != current_destination
            ):

                nights.append(
                    1
                )

                current_destination = (
                    destination_id
                )

            else:

                nights[
                    -1
                ] += 1

        return nights

    @staticmethod
    def _destination_order_from_cabinet(
        cabinet: Any,
    ) -> list[str]:

        order: list[str] = []

        seen: set[str] = set()

        for shelf in cabinet.shelves:

            destination_id = (
                shelf.destination_id
            )

            if (
                destination_id
                and destination_id
                not in seen
            ):

                seen.add(
                    destination_id
                )

                order.append(
                    destination_id
                )

        return order

    @staticmethod
    def _apply_day_themes(
        cabinet: Any,
        day_plan: Any,
    ) -> None:

        shelf_by_day_number = {
            shelf.day_number: shelf
            for shelf in cabinet.shelves
        }

        for day_result in day_plan.days:

            shelf = (
                shelf_by_day_number.get(
                    day_result.day_number
                )
            )

            if shelf is None:
                continue

            # TRANSIT days have their dedicated downstream presentation.
            if (
                getattr(
                    shelf,
                    "day_kind",
                    "STANDARD",
                )
                == "TRANSIT"
            ):
                continue

            derived_theme = (
                _theme_from_archetype(
                    day_result.archetype
                )
            )

            if derived_theme:
                shelf.theme = (
                    derived_theme
                )

    @staticmethod
    def _apply_repair_actions_to_cabinet(
        cabinet: Any,
        repair_result: Any,
    ) -> None:

        from datetime import time as dt_time

        drawer_by_id: dict[
            Any,
            Any,
        ] = {
            drawer.id: drawer
            for shelf in cabinet.shelves
            for drawer in shelf.drawers
        }

        shelf_by_day_number: dict[
            int,
            Any,
        ] = {
            shelf.day_number: shelf
            for shelf in cabinet.shelves
        }

        for action in repair_result.actions:

            drawer = drawer_by_id.get(
                action.activity_id
            )

            if drawer is None:

                logger.warning(
                    "ScheduleRepairEngine referenced activity_id "
                    "%s which does not match any persisted Drawer; "
                    "skipping this repair action.",
                    action.activity_id,
                )

                continue

            if (
                action.to_start_minutes
                is not None
            ):

                drawer.start_time = (
                    dt_time(
                        action.to_start_minutes
                        // 60,
                        action.to_start_minutes
                        % 60,
                    )
                )

            if (
                action.to_day
                != action.from_day
            ):

                source_shelf = (
                    shelf_by_day_number.get(
                        action.from_day
                    )
                )

                destination_shelf = (
                    shelf_by_day_number.get(
                        action.to_day
                    )
                )

                if (
                    source_shelf is not None
                    and destination_shelf
                    is not None
                ):

                    if (
                        drawer
                        in source_shelf.drawers
                    ):

                        source_shelf.drawers.remove(
                            drawer
                        )

                    destination_shelf.drawers.append(
                        drawer
                    )

                    drawer.shelf_id = (
                        destination_shelf.id
                    )


# ============================================================================
# DAY THEMES
# ============================================================================

def _theme_from_archetype(
    archetype: Any,
) -> str | None:

    value = getattr(
        archetype,
        "value",
        archetype,
    )

    mapping = {
        "safari": (
            "Wildlife & wide horizons"
        ),
        "wildlife": (
            "Wildlife & wide horizons"
        ),
        "beach": (
            "Coast, water & open horizons"
        ),
        "relaxation": (
            "Coast, water & open horizons"
        ),
        "cultural": (
            "Culture & discovery"
        ),
        "city_exploration": (
            "Culture & discovery"
        ),
        "nature": (
            "Nature & exploration"
        ),
        "adventure": (
            "Nature & exploration"
        ),
        "exploration": (
            "Explore the destination"
        ),
        "mixed": (
            "Explore the destination"
        ),
        "free": (
            "Free time at the lodge"
        ),
        "recovery": (
            "Rest & recovery"
        ),
    }

    return mapping.get(
        value
    )


# ============================================================================
# PUBLIC ENTRY POINT
# ============================================================================

def generate_itinerary(
    db: Session,
    request: dict[str, Any],
    destination_ids: list[str],
    *,
    allow_coordinate_estimate: bool = False,
) -> ItineraryGenerationResult:

    return ItineraryOrchestrator(
        db
    ).generate(
        request,
        destination_ids,
        allow_coordinate_estimate=(
            allow_coordinate_estimate
        ),
    )


__all__ = [
    "ItineraryGenerationError",
    "ItineraryGenerationResult",
    "ItineraryOrchestrator",
    "generate_itinerary",
]
