"""
itinerary_v2
=============

Top-level orchestrator for itinerary generation.

Wires the full pipeline established in the audit-locked architecture:

    TRIP REQUEST
        |
        v
    RulesEngine <- pre-flight request validation (fail fast)
        |
        v
    RouteGeographyEngine <- factual geographic analysis
        |
        v
    DayArchetypeEngine <- classify each day's operational role
        |
        v
    ItineraryPlanningEngine <- build the persisted Cabinet/Shelf/Drawer/...
        |
        v
    ScheduleRepairEngine <- validate + repair the built schedule
                                (internally normalizes each activity via
                                 ActivityConstraintsEngine -- this
                                 orchestrator does not call that engine
                                 a second time)
        |
        v
    ValidationEngine <- final domain validation, archetype-aware
        |
        v
    Persisted Cabinet (status = "ready" | "draft")

Ordering rationale (per the audit's own open question in doc 15/schedule
repair's docstring: "should repair happen before or after planning?"):

This orchestrator runs ScheduleRepairEngine AFTER
ItineraryPlanningEngine, not before. ItineraryPlanningEngine is the
only component that knows how to construct Drawers with real
Activity IDs pulled from the ranked activity pool -- schedule_repair.py
operates on an already-scheduled day and can only shift, move, or flag
activities that already exist. Running repair first would have nothing
to repair yet. This makes ScheduleRepairEngine a POST-PLANNING safety
net: it catches conflicts ItineraryPlanningEngine's fixed-slot
construction (drawer start times/durations are assigned directly, not
searched for) could not itself avoid -- e.g. a fallback activity
duration that happens to overrun into the next fixed meal slot.

If a future planning engine builds days by consulting
ActivityConstraintsEngine/ScheduleRepairEngine during construction
rather than after, this ordering should be revisited. That is an
explicit, named follow-up, not a silent assumption.

This module does not persist anything itself beyond what
ItineraryPlanningEngine.build() and ValidationEngine.validate() already
do -- it only sequences calls and translates data between engines via
pipeline_adapters.py. The caller (API layer / task queue / etc.) owns
the SQLAlchemy transaction and commit/rollback decision.

CHANGE LOG
-----------
FIX (production incident): the ItineraryPlanningEngine import below
previously read:

    from app.engines.itineraryPlanningEngine import ItineraryPlanningEngine

but the actual file is app/engines/ItineraryPlanningEngine.py
(PascalCase). On Render's case-sensitive Linux filesystem this import
does not resolve to the real file. This is corrected below to match
the real filename exactly. This was the root cause of every day in a
generated itinerary showing identical fixed time slots and a
repeating "Deeper into the park" theme -- with this import broken,
ItineraryOrchestrator itself could never load, so
app/api/trip_v2.py's /generate route (see that file's own fix) was
calling ItineraryPlanningEngine.build() directly with no
RouteGeographyEngine, DayArchetypeEngine, or ScheduleRepairEngine ever
running.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.engines.day_archetype import DayArchetypeEngine
from app.engines.ItineraryPlanningEngine import ItineraryPlanningEngine
from app.engines.pipeline_adapters import (
    archetypes_by_day_number,
    day_records_from_route_analysis,
    overnight_required_from_day_plan,
    schedule_input_from_cabinet,
)
from app.engines.route_geography import RouteGeographyEngine
from app.engines.rules import RulesEngine
from app.engines.schedule_repair import ScheduleRepairEngine
from app.engines.validation import ValidationEngine

logger = logging.getLogger(__name__)


class ItineraryGenerationError(Exception):
    """Raised when the pipeline cannot proceed past a required stage."""


@dataclass
class ItineraryGenerationResult:
    """
    Complete result of running the full generation pipeline.

    ``cabinet`` is populated whenever ItineraryPlanningEngine.build()
    succeeded, even if downstream validation subsequently marked it
    "draft" rather than "ready" -- callers can inspect
    validation_result / rules_result for why.
    """

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
    """
    Sequences the full trip-request -> persisted-cabinet pipeline.
    """

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

        rules_input = self._rules_input_from_request(request, destination_ids)
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

        try:
            build_result = self.itinerary_planning_engine.build(
                request=request,
                destination_ids=destination_ids,
            )
        except ValueError as exc:
            raise ItineraryGenerationError(
                f"ItineraryPlanningEngine could not build a cabinet: {exc}"
            ) from exc

        cabinet = build_result.cabinet
        result.cabinet = cabinet
        result.warnings.extend(build_result.warnings)

        nights_per_destination = self._nights_per_destination_from_cabinet(cabinet)

        activity_counts_by_day = {
            shelf.day_number: sum(
                1 for drawer in shelf.drawers if drawer.activity_type == "EXPERIENCE"
            )
            for shelf in cabinet.shelves
        }

        day_records = day_records_from_route_analysis(
            route_analysis=route_analysis,
            destination_order=list(dict.fromkeys(destination_ids))[: len(nights_per_destination)],
            nights_per_destination=nights_per_destination,
            total_days=cabinet.duration_days,
            activity_counts_by_day=activity_counts_by_day,
        )

        day_plan = self.day_archetype_engine.analyze(day_records)
        result.day_plan = day_plan
        result.warnings.extend(day_plan.warnings)

        schedule_input = schedule_input_from_cabinet(cabinet)
        archetypes = archetypes_by_day_number(day_plan)

        repair_result = self.schedule_repair_engine.repair(
            schedule_input,
            archetypes=archetypes,
        )
        result.schedule_repair_result = repair_result
        result.warnings.extend(repair_result.warnings)

        if repair_result.actions:
            logger.info(
                "ScheduleRepairEngine applied %s repair action(s) to cabinet %s.",
                len(repair_result.actions),
                cabinet.id,
            )
            self._apply_repair_actions_to_cabinet(cabinet, repair_result)

        if not repair_result.fully_repaired:
            result.warnings.append(
                "One or more schedule conflicts remain after automated "
                "repair and require manual review."
            )

        overnight_required = overnight_required_from_day_plan(day_plan)

        validation_result = self.validation_engine.validate(
            cabinet,
            extra_warnings=build_result.warnings,
            overnight_required=overnight_required,
        )
        result.validation_result = validation_result
        result.warnings.extend(validation_result["warnings"])

        return result

    @staticmethod
    def _rules_input_from_request(
        request: dict[str, Any],
        destination_ids: list[str],
    ) -> dict[str, Any]:
        return {
            "days": request.get("days"),
            "travelers": request.get("travelers", 1),
            "destination_ids": destination_ids,
            "budget_tier": request.get("budget_tier", "mid"),
            "start_date": request.get("start_date"),
            "end_date": request.get("end_date"),
        }

    @staticmethod
    def _nights_per_destination_from_cabinet(cabinet: Any) -> list[int]:
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
    def _apply_repair_actions_to_cabinet(cabinet: Any, repair_result: Any) -> None:
        from datetime import time as dt_time

        drawer_by_id: dict[Any, Any] = {
            drawer.id: drawer
            for shelf in cabinet.shelves
            for drawer in shelf.drawers
        }

        shelf_by_day_number: dict[int, Any] = {
            shelf.day_number: shelf for shelf in cabinet.shelves
        }

        for action in repair_result.actions:
            drawer = drawer_by_id.get(action.activity_id)

            if drawer is None:
                logger.warning(
                    "ScheduleRepairEngine referenced activity_id %s "
                    "which does not match any persisted Drawer; "
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
                source_shelf = shelf_by_day_number.get(action.from_day)
                destination_shelf = shelf_by_day_number.get(action.to_day)

                if source_shelf is not None and destination_shelf is not None:
                    if drawer in source_shelf.drawers:
                        source_shelf.drawers.remove(drawer)
                    destination_shelf.drawers.append(drawer)
                    drawer.shelf_id = destination_shelf.id


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

