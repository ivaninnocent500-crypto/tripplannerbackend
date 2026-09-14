"""
itinerary_v2
============

Top-level deterministic itinerary generation orchestrator.

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
    Final RouteGeographyEngine
        |
        v
    Exact Day Allocation
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
    Final Integrity Checks
        |
        v
    ValidationEngine
        |
        v
    Persisted Cabinet

Core invariants
---------------

1. Requested trip duration is authoritative.

   A request for 7 days produces exactly 7 calendar days.

2. User-selected destination order is authoritative.

   A -> B -> C remains A -> B -> C.

3. Non-consecutive repeated destinations are valid.

   A -> B -> A remains A -> B -> A.

4. Consecutive duplicates may be normalized.

   A -> A -> B becomes A -> B.

5. Feasibility may NOT silently remove, swap, or reorder a user's
   selected route.

   If the requested route cannot be constructed within the requested
   duration, generation fails rather than silently changing the journey.

6. Transit days are calendar days inside the requested duration.

7. RouteGeographyEngine is the authoritative source for route facts.

8. The planner does not invent transport, duration, airport transfers,
   or destination transitions.

9. ScheduleRepairEngine may repair timing, but may not change geographic
   identity.

10. ValidationEngine is the final persisted integrity gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

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
    normalize_destination_order,
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


logger = logging.getLogger(__name__)


class ItineraryGenerationError(Exception):
    """Raised when the deterministic itinerary pipeline cannot proceed."""


@dataclass
class ItineraryGenerationResult:
    """Complete result of the itinerary generation pipeline."""

    cabinet: Any | None = None

    rules_result: dict[str, Any] | None = None

    route_analysis: Any | None = None

    day_plan: Any | None = None

    schedule_repair_result: Any | None = None

    validation_result: dict[str, Any] | None = None

    warnings: list[str] = field(
        default_factory=list
    )

    destination_ids: list[str] = field(
        default_factory=list
    )

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

        return self.validation_result.get(
            "status",
            "unknown",
        )


class ItineraryOrchestrator:
    """Sequences the complete deterministic itinerary pipeline."""

    def __init__(self, db: Session):
        self.db = db

        self.rules_engine = RulesEngine()

        self.route_geography_engine = RouteGeographyEngine(
            db
        )

        self.day_archetype_engine = DayArchetypeEngine()

        self.itinerary_planning_engine = (
            ItineraryPlanningEngine(db)
        )

        self.schedule_repair_engine = (
            ScheduleRepairEngine()
        )

        self.validation_engine = ValidationEngine(db)

    # ==================================================================
    # MAIN PIPELINE
    # ==================================================================

    def generate(
        self,
        request: dict[str, Any],
        destination_ids: list[str],
        *,
        allow_coordinate_estimate: bool = False,
    ) -> ItineraryGenerationResult:

        result = ItineraryGenerationResult()

        # ==============================================================
        # 1. REQUEST NORMALIZATION
        # ==============================================================

        cleaned_destination_ids = normalize_destination_order(
            destination_ids
        )

        if not cleaned_destination_ids:
            raise ItineraryGenerationError(
                "No destinations were provided."
            )

        total_days = self._safe_int(
            request.get("days"),
            0,
        )

        if total_days <= 0:
            raise ItineraryGenerationError(
                "Trip duration must be greater than zero."
            )

        requested_destination_ids = list(
            cleaned_destination_ids
        )

        logger.info(
            "Starting itinerary generation: route=%s days=%s",
            requested_destination_ids,
            total_days,
        )

        # ==============================================================
        # 2. RULES
        # ==============================================================

        rules_input = self._rules_input_from_request(
            request,
            requested_destination_ids,
        )

        rules_result = self.rules_engine.evaluate_rules(
            rules_input
        )

        result.rules_result = rules_result

        if not rules_result["validated"]:
            logger.warning(
                "Itinerary request failed RulesEngine validation: %s",
                rules_result["errors"],
            )

            result.warnings.extend(
                rules_result["errors"]
            )

            result.warnings.extend(
                rules_result["warnings"]
            )

            return result

        result.warnings.extend(
            rules_result["warnings"]
        )

        # ==============================================================
        # 3. INITIAL ROUTE GEOGRAPHY
        # ==============================================================

        route_analysis = self.route_geography_engine.analyze(
            requested_destination_ids,
            allow_coordinate_estimate=(
                allow_coordinate_estimate
            ),
        )

        result.route_analysis = route_analysis

        result.warnings.extend(
            getattr(
                route_analysis,
                "warnings",
                [],
            )
        )

        self._assert_route_analysis_matches_requested_route(
            route_analysis,
            requested_destination_ids,
            stage="initial",
        )

        # ==============================================================
        # 4. DESTINATION METADATA
        # ==============================================================

        destination_meta = (
            self.itinerary_planning_engine.fetch_destination_meta(
                requested_destination_ids
            )
        )

        # ==============================================================
        # 5. DESTINATION FEASIBILITY
        # ==============================================================

        (
            feasible_destination_ids,
            feasibility_warnings,
        ) = find_feasible_destination_order(
            destination_ids=requested_destination_ids,
            meta=destination_meta,
            total_days=total_days,
            route_analysis=route_analysis,
        )

        result.warnings.extend(
            feasibility_warnings
        )

        feasible_destination_ids = normalize_destination_order(
            feasible_destination_ids
        )

        # ==============================================================
        # HARD USER-INTENT INVARIANT
        # ==============================================================

        if feasible_destination_ids != requested_destination_ids:
            logger.error(
                "Feasibility changed the requested destination route. "
                "Requested=%s Feasible=%s",
                requested_destination_ids,
                feasible_destination_ids,
            )

            raise ItineraryGenerationError(
                "The requested destination route cannot be constructed "
                f"within {total_days} days without changing the user's "
                "selected destinations or their order. "
                f"Requested={requested_destination_ids}; "
                f"Feasible={feasible_destination_ids}"
            )

        cleaned_destination_ids = list(
            requested_destination_ids
        )

        result.destination_ids = list(
            cleaned_destination_ids
        )

        # ==============================================================
        # 6. FINAL ROUTE GEOGRAPHY
        # ==============================================================

        route_analysis = self.route_geography_engine.analyze(
            cleaned_destination_ids,
            allow_coordinate_estimate=(
                allow_coordinate_estimate
            ),
        )

        result.route_analysis = route_analysis

        result.warnings.extend(
            getattr(
                route_analysis,
                "warnings",
                [],
            )
        )

        self._assert_route_analysis_matches_requested_route(
            route_analysis,
            cleaned_destination_ids,
            stage="final",
        )

        # ==============================================================
        # 7. TRAVEL STYLE
        # ==============================================================

        travel_style = request.get(
            "travel_style"
        ) or []

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

        # ==============================================================
        # 8. FINAL DESTINATION METADATA
        # ==============================================================

        destination_meta = (
            self.itinerary_planning_engine.fetch_destination_meta(
                cleaned_destination_ids
            )
        )

        # ==============================================================
        # 9. EXACT DAY ALLOCATION
        # ==============================================================

        (
            day_allocation,
            allocation_warnings,
        ) = allocate_days_for_route(
            destination_ids=cleaned_destination_ids,
            meta=destination_meta,
            total_days=total_days,
            travel_style=travel_style,
            route_analysis=route_analysis,
        )

        result.warnings.extend(
            allocation_warnings
        )

        if len(day_allocation) != len(
            cleaned_destination_ids
        ):
            raise ItineraryGenerationError(
                "Destination allocation length does not match "
                f"selected destination count: "
                f"{len(day_allocation)} != "
                f"{len(cleaned_destination_ids)}"
            )

        if any(
            value < 1
            for value in day_allocation
        ):
            raise ItineraryGenerationError(
                "A selected destination received fewer than one "
                f"calendar day: {day_allocation}"
            )

        if sum(day_allocation) != total_days:
            raise ItineraryGenerationError(
                "Destination allocation does not match requested "
                f"trip duration: "
                f"{sum(day_allocation)} != {total_days}"
            )

        logger.info(
            "Final itinerary allocation: destinations=%s "
            "allocation=%s total_days=%s",
            cleaned_destination_ids,
            day_allocation,
            total_days,
        )

        # ==============================================================
        # 10. PRE-PLANNING DAY RECORDS
        # ==============================================================

        day_records = day_records_from_route_analysis(
            route_analysis=route_analysis,
            destination_order=cleaned_destination_ids,
            nights_per_destination=day_allocation,
            total_days=total_days,
        )

        if len(day_records) != total_days:
            raise ItineraryGenerationError(
                "Pre-planning day records do not match requested "
                f"trip duration: {len(day_records)} != {total_days}"
            )

        self._assert_day_record_route_integrity(
            day_records,
            cleaned_destination_ids,
            total_days,
            stage="pre-planning",
        )

        # ==============================================================
        # 11. PRE-PLANNING ARCHETYPES
        # ==============================================================

        pre_planning_day_plan = (
            self.day_archetype_engine.analyze(
                day_records
            )
        )

        result.warnings.extend(
            getattr(
                pre_planning_day_plan,
                "warnings",
                [],
            )
        )

        # ==============================================================
        # 12. TRANSIT CLASSIFICATION
        # ==============================================================

        transit_days = transit_days_from_day_plan(
            pre_planning_day_plan,
            day_records=day_records,
        )

        transit_days = set(
            transit_days or []
        )

        # ==============================================================
        # 13. BUILD CABINET
        # ==============================================================

        try:
            build_result = (
                self.itinerary_planning_engine.build(
                    request=request,
                    destination_ids=cleaned_destination_ids,
                    day_allocation=day_allocation,
                    transit_days=transit_days,
                    route_facts=route_analysis.legs,
                )
            )

        except ValueError as exc:
            raise ItineraryGenerationError(
                "ItineraryPlanningEngine could not build a cabinet: "
                f"{exc}"
            ) from exc

        cabinet = build_result.cabinet

        result.cabinet = cabinet

        result.warnings.extend(
            getattr(
                build_result,
                "warnings",
                [],
            )
        )

        # ==============================================================
        # 14. CABINET DAY COUNT
        # ==============================================================

        cabinet_day_count = len(
            cabinet.shelves or []
        )

        if cabinet_day_count != total_days:
            raise ItineraryGenerationError(
                "ItineraryPlanningEngine produced the wrong number "
                "of calendar days: "
                f"{cabinet_day_count} != requested {total_days}"
            )

        if getattr(
            cabinet,
            "duration_days",
            total_days,
        ) != total_days:
            raise ItineraryGenerationError(
                "Cabinet duration does not match requested trip "
                f"duration: {cabinet.duration_days} != {total_days}"
            )

        # ==============================================================
        # 15. IMMEDIATE CABINET ROUTE INTEGRITY
        # ==============================================================

        actual_destination_order = (
            self._destination_order_from_cabinet(
                cabinet
            )
        )

        if actual_destination_order != cleaned_destination_ids:
            raise ItineraryGenerationError(
                "ItineraryPlanningEngine changed the authoritative "
                "destination route. "
                f"Expected={cleaned_destination_ids} "
                f"Actual={actual_destination_order}"
            )

        self._assert_shelf_sequence_matches_route(
            cabinet,
            cleaned_destination_ids,
            stage="post-planning",
        )

        # ==============================================================
        # 16. RECONSTRUCT ACTUAL DESTINATION ALLOCATION
        # ==============================================================

        nights_per_destination = (
            self._nights_per_destination_from_cabinet(
                cabinet
            )
        )

        if sum(nights_per_destination) != total_days:
            raise ItineraryGenerationError(
                "Persisted Cabinet destination allocation does not "
                "match requested duration: "
                f"{sum(nights_per_destination)} != {total_days}"
            )

        if len(nights_per_destination) != len(
            cleaned_destination_ids
        ):
            raise ItineraryGenerationError(
                "Persisted Cabinet contains an unexpected number "
                "of destination segments. "
                f"Expected={len(cleaned_destination_ids)} "
                f"Actual={len(nights_per_destination)}"
            )

        # ==============================================================
        # 17. ACTIVITY COUNTS
        # ==============================================================

        activity_counts_by_day = {
            shelf.day_number: sum(
                1
                for drawer in shelf.drawers or []
                if drawer.activity_type == "EXPERIENCE"
            )
            for shelf in cabinet.shelves or []
        }

        # ==============================================================
        # 18. REBUILD DAY RECORDS FROM ACTUAL CABINET
        # ==============================================================

        day_records = day_records_from_route_analysis(
            route_analysis=route_analysis,
            destination_order=cleaned_destination_ids,
            nights_per_destination=nights_per_destination,
            total_days=cabinet.duration_days,
            activity_counts_by_day=activity_counts_by_day,
        )

        if len(day_records) != total_days:
            raise ItineraryGenerationError(
                "Final day records do not match requested trip "
                f"duration: {len(day_records)} != {total_days}"
            )

        self._assert_day_record_route_integrity(
            day_records,
            cleaned_destination_ids,
            total_days,
            stage="post-planning",
        )

        # ==============================================================
        # 19. FINAL DAY CLASSIFICATION
        # ==============================================================

        day_plan = self.day_archetype_engine.analyze(
            day_records
        )

        result.day_plan = day_plan

        result.warnings.extend(
            getattr(
                day_plan,
                "warnings",
                [],
            )
        )

        # ==============================================================
        # 20. APPLY DAY THEMES
        # ==============================================================

        self._apply_day_themes(
            cabinet,
            day_plan,
        )

        # ==============================================================
        # 21. SCHEDULE REPAIR
        # ==============================================================

        schedule_input = schedule_input_from_cabinet(
            cabinet
        )

        archetypes = archetypes_by_day_number(
            day_plan
        )

        repair_result = (
            self.schedule_repair_engine.repair(
                schedule_input,
                archetypes=archetypes,
            )
        )

        result.schedule_repair_result = repair_result

        result.warnings.extend(
            getattr(
                repair_result,
                "warnings",
                [],
            )
        )

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

        # ==============================================================
        # 22. POST-REPAIR ROUTE INTEGRITY
        # ==============================================================

        repaired_destination_order = (
            self._destination_order_from_cabinet(
                cabinet
            )
        )

        if repaired_destination_order != cleaned_destination_ids:
            raise ItineraryGenerationError(
                "ScheduleRepairEngine changed the itinerary "
                "destination order. "
                f"Expected={cleaned_destination_ids} "
                f"Actual={repaired_destination_order}"
            )

        self._assert_shelf_sequence_matches_route(
            cabinet,
            cleaned_destination_ids,
            stage="post-repair",
        )

        self._assert_drawer_destination_integrity(
            cabinet,
            stage="post-repair",
        )

        # ==============================================================
        # 23. FINAL VALIDATION
        # ==============================================================

        overnight_required = (
            overnight_required_from_day_plan(
                day_plan
            )
        )

        validation_input_warnings = list(
            result.warnings
        )

        validation_result = (
            self.validation_engine.validate(
                cabinet,
                extra_warnings=validation_input_warnings,
                overnight_required=overnight_required,
            )
        )

        result.validation_result = validation_result

        result.warnings.extend(
            validation_result.get(
                "warnings",
                [],
            )
        )

        result.warnings = self._dedupe_strings(
            result.warnings
        )

        return result

    # ==================================================================
    # ROUTE ASSERTIONS
    # ==================================================================

    @staticmethod
    def _assert_route_analysis_matches_requested_route(
        route_analysis: Any,
        destination_ids: list[str],
        *,
        stage: str,
    ) -> None:
        """
        Ensure RouteGeographyEngine preserved the requested ordered route.

        We intentionally inspect ordered legs rather than relying solely
        on stop_count because a count does not prove sequence integrity.
        """

        expected = normalize_destination_order(
            destination_ids
        )

        if not expected:
            raise ItineraryGenerationError(
                f"{stage} route analysis received no destinations."
            )

        legs = list(
            getattr(
                route_analysis,
                "legs",
                [],
            )
            or []
        )

        if len(expected) == 1:
            if legs:
                raise ItineraryGenerationError(
                    f"{stage} route analysis contains route legs "
                    "for a single-destination route."
                )
            return

        if len(legs) != len(expected) - 1:
            raise ItineraryGenerationError(
                f"{stage} route analysis contains the wrong number "
                "of route legs: "
                f"{len(legs)} != {len(expected) - 1}"
            )

        for index, leg in enumerate(legs):
            expected_from = expected[index]
            expected_to = expected[index + 1]

            actual_from = getattr(
                leg,
                "from_destination_id",
                None,
            )

            actual_to = getattr(
                leg,
                "to_destination_id",
                None,
            )

            if actual_from != expected_from:
                raise ItineraryGenerationError(
                    f"{stage} route analysis changed route order at "
                    f"leg {index + 1}: expected from "
                    f"{expected_from}, found {actual_from}."
                )

            if actual_to != expected_to:
                raise ItineraryGenerationError(
                    f"{stage} route analysis changed route order at "
                    f"leg {index + 1}: expected to "
                    f"{expected_to}, found {actual_to}."
                )

    @staticmethod
    def _assert_day_record_route_integrity(
        day_records: list[Any],
        destination_ids: list[str],
        total_days: int,
        *,
        stage: str,
    ) -> None:
        """
        Verify that day-level records preserve the exact requested
        destination sequence.

        Day records produced by pipeline_adapters are dictionaries, so
        destination identity must be read from the mapping rather than
        through getattr().

        Non-consecutive repeated destinations remain valid:

            A -> B -> A

        Consecutive duplicates are collapsed only for route comparison.
        """

        if len(day_records) != total_days:
            raise ItineraryGenerationError(
                f"{stage} day-record count is invalid: "
                f"{len(day_records)} != {total_days}"
            )

        expected_segments = normalize_destination_order(
            destination_ids
        )

        actual_destinations: list[str] = []

        for record in day_records:

            if isinstance(record, Mapping):
                destination_id = record.get(
                    "destination_id"
                )
            else:
                destination_id = getattr(
                    record,
                    "destination_id",
                    None,
                )

            if destination_id is not None:
                destination_id = str(
                    destination_id
                ).strip()

                if destination_id:
                    actual_destinations.append(
                        destination_id
                    )

        actual_segments = normalize_destination_order(
            actual_destinations
        )

        if actual_segments != expected_segments:
            raise ItineraryGenerationError(
                f"{stage} day records changed destination order. "
                f"Expected={expected_segments} "
                f"Actual={actual_segments}"
            )

    @staticmethod
    def _assert_shelf_sequence_matches_route(
        cabinet: Any,
        destination_ids: list[str],
        *,
        stage: str,
    ) -> None:

        shelves = sorted(
            list(cabinet.shelves or []),
            key=lambda shelf: (
                shelf.day_number
                if shelf.day_number is not None
                else 10**9,
                str(shelf.id),
            ),
        )

        actual_destinations = [
            shelf.destination_id
            for shelf in shelves
            if getattr(
                shelf,
                "destination_id",
                None,
            )
        ]

        actual_segments = normalize_destination_order(
            actual_destinations
        )

        expected_segments = normalize_destination_order(
            destination_ids
        )

        if actual_segments != expected_segments:
            raise ItineraryGenerationError(
                f"{stage} shelf destination sequence does not "
                "match the authoritative route. "
                f"Expected={expected_segments} "
                f"Actual={actual_segments}"
            )

    @staticmethod
    def _assert_drawer_destination_integrity(
        cabinet: Any,
        *,
        stage: str,
    ) -> None:

        for shelf in cabinet.shelves or []:
            shelf_destination = getattr(
                shelf,
                "destination_id",
                None,
            )

            if not shelf_destination:
                raise ItineraryGenerationError(
                    f"{stage}: Day {shelf.day_number} has no "
                    "destination_id."
                )

            for drawer in shelf.drawers or []:
                drawer_destination = getattr(
                    drawer,
                    "destination_id",
                    None,
                )

                if not drawer_destination:
                    raise ItineraryGenerationError(
                        f"{stage}: Day {shelf.day_number} activity "
                        f"'{drawer.name}' has no destination_id."
                    )

                if drawer_destination != shelf_destination:
                    raise ItineraryGenerationError(
                        f"{stage}: Day {shelf.day_number} activity "
                        f"'{drawer.name}' belongs to destination "
                        f"{drawer_destination}, while the shelf belongs "
                        f"to {shelf_destination}."
                    )

    # ==================================================================
    # CABINET HELPERS
    # ==================================================================

    @staticmethod
    def _safe_int(
        value: Any,
        default: int = 0,
    ) -> int:

        try:
            return int(value)
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
            "days": request.get("days"),
            "travelers": request.get(
                "travelers",
                1,
            ),
            "destination_ids": destination_ids,
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

        """
        Reconstruct destination-day segments from persisted shelves.

        This intentionally preserves:

            A -> B -> A

        as:

            [days_at_A, days_at_B, days_at_A]

        rather than merging both A segments together.
        """

        nights: list[int] = []

        current_destination = None

        shelves = sorted(
            list(cabinet.shelves or []),
            key=lambda shelf: (
                shelf.day_number
                if shelf.day_number is not None
                else 10**9,
                str(shelf.id),
            ),
        )

        for shelf in shelves:
            destination_id = shelf.destination_id

            if destination_id != current_destination:
                nights.append(1)
                current_destination = destination_id
            else:
                nights[-1] += 1

        return nights

    @staticmethod
    def _destination_order_from_cabinet(
        cabinet: Any,
    ) -> list[str]:

        """
        Return the ordered geographic sequence represented by shelves.

        Only consecutive duplicates are removed.

        Therefore:

            A -> A -> B -> B -> A

        becomes:

            A -> B -> A

        not:

            A -> B
        """

        destinations = [
            shelf.destination_id
            for shelf in sorted(
                list(cabinet.shelves or []),
                key=lambda shelf: (
                    shelf.day_number
                    if shelf.day_number is not None
                    else 10**9,
                    str(shelf.id),
                ),
            )
            if getattr(
                shelf,
                "destination_id",
                None,
            )
        ]

        return normalize_destination_order(
            destinations
        )

    @staticmethod
    def _apply_day_themes(
        cabinet: Any,
        day_plan: Any,
    ) -> None:

        shelf_by_day_number = {
            shelf.day_number: shelf
            for shelf in cabinet.shelves or []
        }

        for day_result in getattr(
            day_plan,
            "days",
            [],
        ):

            shelf = shelf_by_day_number.get(
                day_result.day_number
            )

            if shelf is None:
                continue

            if (
                str(
                    getattr(
                        shelf,
                        "day_kind",
                        "STANDARD",
                    )
                ).upper()
                == "TRANSIT"
            ):
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

        drawer_by_id = {
            drawer.id: drawer
            for shelf in cabinet.shelves or []
            for drawer in shelf.drawers or []
        }

        shelf_by_day_number = {
            shelf.day_number: shelf
            for shelf in cabinet.shelves or []
        }

        for action in getattr(
            repair_result,
            "actions",
            [],
        ):

            drawer = drawer_by_id.get(
                action.activity_id
            )

            if drawer is None:
                logger.warning(
                    "ScheduleRepairEngine referenced activity_id %s "
                    "which does not match any persisted Drawer; "
                    "skipping this repair action.",
                    action.activity_id,
                )
                continue

            if action.to_start_minutes is not None:

                minutes = max(
                    0,
                    int(
                        action.to_start_minutes
                    ),
                )

                drawer.start_time = dt_time(
                    minutes // 60,
                    minutes % 60,
                )

            if action.to_day != action.from_day:

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
                    source_shelf is None
                    or destination_shelf is None
                ):
                    logger.warning(
                        "ScheduleRepairEngine requested movement of "
                        "activity %s between missing days %s -> %s.",
                        action.activity_id,
                        action.from_day,
                        action.to_day,
                    )
                    continue

                # ------------------------------------------------------
                # Final destination safety.
                #
                # ScheduleRepairEngine should already guarantee this,
                # but the orchestrator must never move an activity into
                # a different geographic destination.
                # ------------------------------------------------------

                drawer_destination = getattr(
                    drawer,
                    "destination_id",
                    None,
                )

                target_destination = getattr(
                    destination_shelf,
                    "destination_id",
                    None,
                )

                if (
                    drawer_destination is None
                    or target_destination is None
                    or drawer_destination
                    != target_destination
                ):
                    raise ItineraryGenerationError(
                        "ScheduleRepairEngine attempted to move activity "
                        f"'{drawer.name}' to a different destination. "
                        f"Activity destination={drawer_destination}; "
                        f"target shelf destination={target_destination}"
                    )

                if drawer in source_shelf.drawers:
                    source_shelf.drawers.remove(
                        drawer
                    )

                if drawer not in destination_shelf.drawers:
                    destination_shelf.drawers.append(
                        drawer
                    )

                drawer.shelf_id = (
                    destination_shelf.id
                )

    # ==================================================================
    # WARNING HELPERS
    # ==================================================================

    @staticmethod
    def _dedupe_strings(
        values: list[str],
    ) -> list[str]:

        result: list[str] = []
        seen: set[str] = set()

        for value in values:
            if not value:
                continue

            if value in seen:
                continue

            seen.add(value)
            result.append(value)

        return result


# ========================================================================
# DAY THEMES
# ========================================================================

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


# ========================================================================
# PUBLIC ENTRY POINT
# ========================================================================

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
        allow_coordinate_estimate=allow_coordinate_estimate,
    )


__all__ = [
    "ItineraryGenerationError",
    "ItineraryGenerationResult",
    "ItineraryOrchestrator",
    "generate_itinerary",
]
