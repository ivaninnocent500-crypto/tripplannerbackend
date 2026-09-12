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
    allocate_days_for_route()
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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.engines.day_archetype import DayArchetypeEngine
from app.engines.itineraryPlanningEngine import ItineraryPlanningEngine
from app.engines.pipeline_adapters import (
    allocate_days_for_route,
    archetypes_by_day_number,
    day_records_from_route_analysis,
    overnight_required_from_day_plan,
    schedule_input_from_cabinet,
    transit_days_from_day_plan,
)
from app.engines.route_geography import RouteGeographyEngine
from app.engines.rules import RulesEngine
from app.engines.schedule_repair import ScheduleRepairEngine
from app.engines.validation import ValidationEngine


logger = logging.getLogger(__name__)


class ItineraryGenerationError(Exception):
    """Raised when the itinerary generation pipeline cannot proceed."""


@dataclass
class ItineraryGenerationResult:
    """Complete result of the itinerary generation pipeline."""

    cabinet: Any | None = None

    rules_result: dict[str, Any] | None = None
    route_analysis: Any | None = None
    day_plan: Any | None = None
    schedule_repair_result: Any | None = None
    validation_result: dict[str, Any] | None = None

    warnings: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return (
            self.cabinet is not None
            and self.validation_result is not None
            and self.validation_result.get("status") == "valid"
        )

    @property
    def status(self) -> str:
        if self.cabinet is None:
            return "failed"

        if self.validation_result is None:
            return "unvalidated"

        return self.validation_result.get("status", "unknown")


class ItineraryOrchestrator:
    """Sequences the complete trip-request to persisted-cabinet pipeline."""

    def __init__(self, db: Session):
        self.db = db

        self.rules_engine = RulesEngine()
        self.route_geography_engine = RouteGeographyEngine(db)
        self.day_archetype_engine = DayArchetypeEngine()
        self.itinerary_planning_engine = ItineraryPlanningEngine(db)
        self.schedule_repair_engine = ScheduleRepairEngine()
        self.validation_engine = ValidationEngine(db)

    def generate(
        self,
        request: dict[str, Any],
        destination_ids: list[str],
        *,
        allow_coordinate_estimate: bool = False,
    ) -> ItineraryGenerationResult:

        result = ItineraryGenerationResult()

        # ---------------------------------------------------------
        # 1. Rules
        # ---------------------------------------------------------

        rules_input = self._rules_input_from_request(
            request,
            destination_ids,
        )

        rules_result = self.rules_engine.evaluate_rules(rules_input)
        result.rules_result = rules_result

        if not rules_result["validated"]:
            logger.warning(
                "Itinerary request failed RulesEngine validation: %s",
                rules_result["errors"],
            )

            result.warnings.extend(rules_result["errors"])
            result.warnings.extend(rules_result["warnings"])

            return result

        result.warnings.extend(rules_result["warnings"])

        # ---------------------------------------------------------
        # 2. Route geography
        # ---------------------------------------------------------

        route_analysis = self.route_geography_engine.analyze(
            destination_ids,
            allow_coordinate_estimate=allow_coordinate_estimate,
        )

        result.route_analysis = route_analysis
        result.warnings.extend(route_analysis.warnings)

        if route_analysis.stop_count == 0:
            raise ItineraryGenerationError(
                "RouteGeographyEngine could not resolve any destinations "
                "for this request."
            )

        # ---------------------------------------------------------
        # 3. Prepare destination allocation
        # ---------------------------------------------------------

        total_days = self._safe_int(
            request.get("days"),
            0,
        )

        cleaned_destination_ids = list(
            dict.fromkeys(
                str(destination_id)
                for destination_id in destination_ids
            )
        )

        if (
            total_days > 0
            and len(cleaned_destination_ids) > total_days
        ):
            logger.warning(
                "Trip requests %s destinations but only %s days. "
                "Only the first %s destinations will receive an "
                "overnight.",
                len(cleaned_destination_ids),
                total_days,
                total_days,
            )

            cleaned_destination_ids = cleaned_destination_ids[:total_days]

        destination_meta = (
            self.itinerary_planning_engine.fetch_destination_meta(
                cleaned_destination_ids
            )
        )

        travel_style = request.get("travel_style") or []

        if isinstance(travel_style, str):
            travel_style = [travel_style]

        travel_style = list(
            dict.fromkeys(
                str(style).lower()
                for style in travel_style
                if style
            )
        )

        day_allocation, allocation_warnings = (
            allocate_days_for_route(
                destination_ids=cleaned_destination_ids,
                meta=destination_meta,
                total_days=total_days,
                travel_style=travel_style,
            )
        )

        result.warnings.extend(allocation_warnings)

        # ---------------------------------------------------------
        # 4. Pre-planning day records
        # ---------------------------------------------------------

        day_records = day_records_from_route_analysis(
            route_analysis=route_analysis,
            destination_order=cleaned_destination_ids,
            nights_per_destination=day_allocation,
            total_days=total_days,
        )

        # ---------------------------------------------------------
        # 5. Pre-planning archetype classification
        # ---------------------------------------------------------

        pre_planning_day_plan = (
            self.day_archetype_engine.analyze(
                day_records
            )
        )

        # ---------------------------------------------------------
        # 6. Determine transit days before planning
        # ---------------------------------------------------------

        transit_days = transit_days_from_day_plan(
            pre_planning_day_plan,
            day_records=day_records,
        )

        # ---------------------------------------------------------
        # 7. Build itinerary
        # ---------------------------------------------------------

        try:
            build_result = self.itinerary_planning_engine.build(
                request=request,
                destination_ids=destination_ids,
                day_allocation=day_allocation,
                transit_days=transit_days,
            )

        except ValueError as exc:
            raise ItineraryGenerationError(
                "ItineraryPlanningEngine could not build a cabinet: "
                f"{exc}"
            ) from exc

        cabinet = build_result.cabinet

        result.cabinet = cabinet
        result.warnings.extend(build_result.warnings)

        # ---------------------------------------------------------
        # 8. Reconstruct actual destination allocation
        # ---------------------------------------------------------

        nights_per_destination = (
            self._nights_per_destination_from_cabinet(
                cabinet
            )
        )

        # ---------------------------------------------------------
        # 9. Count actual activities
        # ---------------------------------------------------------

        activity_counts_by_day = {
            shelf.day_number: sum(
                1
                for drawer in shelf.drawers
                if drawer.activity_type == "EXPERIENCE"
            )
            for shelf in cabinet.shelves
        }

        # ---------------------------------------------------------
        # 10. Re-run day classification using built cabinet
        # ---------------------------------------------------------

        final_destination_order = list(
            dict.fromkeys(destination_ids)
        )[: len(nights_per_destination)]

        day_records = day_records_from_route_analysis(
            route_analysis=route_analysis,
            destination_order=final_destination_order,
            nights_per_destination=nights_per_destination,
            total_days=cabinet.duration_days,
            activity_counts_by_day=activity_counts_by_day,
        )

        day_plan = self.day_archetype_engine.analyze(
            day_records
        )

        result.day_plan = day_plan
        result.warnings.extend(day_plan.warnings)

        # ---------------------------------------------------------
        # 11. Apply day themes
        # ---------------------------------------------------------

        self._apply_day_themes(
            cabinet,
            day_plan,
        )

        # ---------------------------------------------------------
        # 12. Schedule repair
        # ---------------------------------------------------------

        schedule_input = schedule_input_from_cabinet(
            cabinet
        )

        archetypes = archetypes_by_day_number(
            day_plan
        )

        repair_result = self.schedule_repair_engine.repair(
            schedule_input,
            archetypes=archetypes,
        )

        result.schedule_repair_result = repair_result
        result.warnings.extend(repair_result.warnings)

        if repair_result.actions:
            logger.info(
                "ScheduleRepairEngine applied %s repair action(s) "
                "to cabinet %s.",
                len(repair_result.actions),
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

        # ---------------------------------------------------------
        # 13. Final validation
        # ---------------------------------------------------------

        overnight_required = (
            overnight_required_from_day_plan(
                day_plan
            )
        )

        validation_result = self.validation_engine.validate(
            cabinet,
            extra_warnings=build_result.warnings,
            overnight_required=overnight_required,
        )

        result.validation_result = validation_result
        result.warnings.extend(
            validation_result["warnings"]
        )

        return result

    # =============================================================
    # Helpers
    # =============================================================

    @staticmethod
    def _safe_int(
        value: Any,
        default: int = 0,
    ) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _rules_input_from_request(
        request: dict[str, Any],
        destination_ids: list[str],
    ) -> dict[str, Any]:

        return {
            "days": request.get("days"),
            "travelers": request.get("travelers", 1),
            "destination_ids": destination_ids,
            "budget_tier": request.get(
                "budget_tier",
                "mid",
            ),
            "start_date": request.get("start_date"),
            "end_date": request.get("end_date"),
        }

    @staticmethod
    def _nights_per_destination_from_cabinet(
        cabinet: Any,
    ) -> list[int]:

        nights: list[int] = []
        current_destination = None

        for shelf in cabinet.shelves:
            if shelf.destination_id != current_destination:
                nights.append(1)
                current_destination = shelf.destination_id
            else:
                nights[-1] += 1

        return nights

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
            shelf = shelf_by_day_number.get(
                day_result.day_number
            )

            if shelf is None:
                continue

            if getattr(
                shelf,
                "day_kind",
                "STANDARD",
            ) == "TRANSIT":
                continue

            derived_theme = _theme_from_archetype(
                day_result.archetype
            )

            if derived_theme:
                shelf.theme = derived_theme

    @staticmethod
    def _apply_repair_actions_to_cabinet(
        cabinet: Any,
        repair_result: Any,
    ) -> None:

        from datetime import time as dt_time

        drawer_by_id: dict[Any, Any] = {
            drawer.id: drawer
            for shelf in cabinet.shelves
            for drawer in shelf.drawers
        }

        shelf_by_day_number: dict[int, Any] = {
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

            if action.to_start_minutes is not None:
                drawer.start_time = dt_time(
                    action.to_start_minutes // 60,
                    action.to_start_minutes % 60,
                )

            if action.to_day != action.from_day:

                source_shelf = shelf_by_day_number.get(
                    action.from_day
                )

                destination_shelf = shelf_by_day_number.get(
                    action.to_day
                )

                if (
                    source_shelf is not None
                    and destination_shelf is not None
                ):
                    if drawer in source_shelf.drawers:
                        source_shelf.drawers.remove(
                            drawer
                        )

                    destination_shelf.drawers.append(
                        drawer
                    )

                    drawer.shelf_id = (
                        destination_shelf.id
                    )


def _theme_from_archetype(
    archetype: Any,
) -> str | None:

    value = getattr(
        archetype,
        "value",
        archetype,
    )

    mapping = {
        "safari": "Wildlife & wide horizons",
        "wildlife": "Wildlife & wide horizons",
        "beach": "Coast, water & open horizons",
        "relaxation": "Coast, water & open horizons",
        "cultural": "Culture & discovery",
        "city_exploration": "Culture & discovery",
        "nature": "Nature & exploration",
        "adventure": "Nature & exploration",
        "exploration": "Explore the destination",
        "mixed": "Explore the destination",
        "free": "Free time at the lodge",
        "recovery": "Rest & recovery",
    }

    return mapping.get(value)


def generate_itinerary(
    db: Session,
    request: dict[str, Any],
    destination_ids: list[str],
    *,
    allow_coordinate_estimate: bool = False,
) -> ItineraryGenerationResult:

    return ItineraryOrchestrator(db).generate(
        request,
        destination_ids,
        allow_coordinate_estimate=allow_coordinate_estimate,
    )


__all__ = [
    "ItineraryGenerationError",
    "ItineraryGenerationResult",
    "ItineraryOrchestrator",
    "generate_itinerary",
]
