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
    allocate_days_for_route() <- how many nights per destination
        | (pipeline_adapters.py, pure function)
        v
    DayArchetypeEngine <- classify each day's operational role,
        | INCLUDING detecting long-haul TRANSIT days,
        | using the allocation above
        v
    ItineraryPlanningEngine <- build the persisted Cabinet/Shelf/Drawer/...
        | now TRANSIT-day-aware at construction
        | time (see change log below)
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

CHANGE LOG (this rewrite -- transit-day fix, backend stage 3, final)
--------------------------------------------------------------------
PROBLEM: ItineraryPlanningEngine.build() previously computed its own
day allocation internally (_allocate_days(), now deleted from that
file) and constructed every Shelf's Drawers immediately, all before
DayArchetypeEngine ever ran. DayArchetypeEngine's LONG_TRANSFER
classification -- the signal that a day is a long-haul/intercontinental
transfer day, not a normal park day -- was therefore only ever computed
AFTER the (already wrong) Drawers had been built, and was used only for
ValidationEngine's warnings, never to influence what got built in the
first place. This produced itineraries where a day consumed by an
international flight (Tanzania -> Ethiopia, Tanzania -> Madagascar) was
rendered with a fabricated normal-day activity ("Night Lemur Walk")
bolted on top of the transfer.

FIX (three-stage, all now in place):
  1. pipeline_adapters.allocate_days_for_route() -- day-allocation
     logic extracted out of ItineraryPlanningEngine into a standalone
     pure function, so it can run independently of that engine.
  2. models_furniture.Shelf.day_kind ("STANDARD"|"TRANSIT") + migration
     006_shelf_day_kind.sql -- persists the classification so the API/
     Android client don't need to re-derive it from raw Hinge data.
  3. THIS FILE: generate() below now calls allocate_days_for_route()
     and DayArchetypeEngine.analyze() BEFORE
     ItineraryPlanningEngine.build(), and passes both the resulting
     day_allocation and a transit_days lookup (day_number -> bool, via
     pipeline_adapters.transit_days_from_day_plan()) INTO build() as
     required/optional parameters. ItineraryPlanningEngine now branches
     on transit_days at Drawer-construction time (see
     itineraryPlanningEngine.py's _populate_transit_day_drawers()) --
     the classification actually influences construction, not just
     post-hoc validation.

STAGE ORDERING RATIONALE (updated):
This orchestrator now runs, in order: RulesEngine -> RouteGeographyEngine
-> allocate_days_for_route() -> DayArchetypeEngine -> ItineraryPlanningEngine
-> ScheduleRepairEngine -> ValidationEngine. The previous ordering note
about ScheduleRepairEngine running AFTER planning (because it can only
shift/move/flag activities that already exist) is UNCHANGED and still
correct -- that relationship between planning and repair was not
affected by this rewrite. What changed is strictly earlier in the
pipeline: day-allocation and day-archetype classification moved from
"a byproduct computed after planning, for validation's benefit" to "an
input planning itself consumes."

A NEW small piece of duplicated work is introduced by this reordering:
ItineraryPlanningEngine.build() still calls its own
_fetch_destination_meta() internally (for country/destination_type
lookups it needs regardless), and this orchestrator ALSO needs that
same meta before build() runs, to hand to allocate_days_for_route().
Rather than thread `meta` through build()'s public signature as a new
required parameter (which would leak an internal implementation detail
of the engine into its API), ItineraryPlanningEngine now exposes
`fetch_destination_meta()` as a public, reusable method (see that
file's change log) -- this orchestrator calls it once, and build()
calls its own internal copy again. This is one extra indexed query per
generation request, not a correctness concern.

CHANGE LOG (prior, unchanged by this rewrite)
--------------------------------------------------------------------
CORRECTION (reversed a previous, incorrect "fix"): an earlier pass
"fixed" the ItineraryPlanningEngine import from
`app.engines.itineraryPlanningEngine` to
`app.engines.ItineraryPlanningEngine` (PascalCase), on the theory the
real file was PascalCase. That was wrong -- confirmed directly against
the repository on GitHub, the real, committed file is
itineraryPlanningEngine.py, lowercase i. Deploying the "fixed" version
produced ModuleNotFoundError on Render. Reverted, and left reverted
here. If this import is ever changed again, verify it against
`ls app/engines/` or the GitHub file browser directly, not against a
retyped tree.

The theme-repetition bug fix (_apply_day_themes(), writing
DayArchetypeEngine's per-day classification back onto each Shelf.theme)
remains in place and is unaffected by this rewrite -- although note
that ItineraryPlanningEngine now sets Shelf.theme itself at
construction time (via _theme_for(), now transit-day-aware), so
_apply_day_themes()'s job is now narrower: it should NOT overwrite a
"Travel day" theme that ItineraryPlanningEngine already set correctly
for a TRANSIT shelf. See that method below for the added guard.
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

        # --- NEW: allocation + archetype classification, BEFORE planning ---
        #
        # This is the reordering described in this module's change
        # log: day allocation and DayArchetypeEngine classification
        # (specifically, LONG_TRANSFER detection) now happen before
        # ItineraryPlanningEngine.build() runs, so the planning engine
        # can consult transit_days at Drawer-construction time instead
        # of only after the fact.

        total_days = self._safe_int(request.get("days"), 0)

        cleaned_destination_ids = list(dict.fromkeys(str(d) for d in destination_ids))
        if total_days > 0 and len(cleaned_destination_ids) > total_days:
            logger.warning(
                "Trip requests %s destinations but only %s days; only "
                "the first %s destinations can receive an overnight. "
                "(ItineraryPlanningEngine.build() will independently "
                "apply the same trim -- this pre-trim exists so "
                "allocation/archetype classification see the same "
                "destination set that build() will actually construct.)",
                len(cleaned_destination_ids), total_days, total_days,
            )
            cleaned_destination_ids = cleaned_destination_ids[:total_days]

        destination_meta = self.itinerary_planning_engine.fetch_destination_meta(
            cleaned_destination_ids
        )

        travel_style = request.get("travel_style") or []
        if isinstance(travel_style, str):
            travel_style = [travel_style]
        travel_style = list(dict.fromkeys(str(s).lower() for s in travel_style if s))

        day_allocation, allocation_warnings = allocate_days_for_route(
            destination_ids=cleaned_destination_ids,
            meta=destination_meta,
            total_days=total_days,
            travel_style=travel_style,
        )
        result.warnings.extend(allocation_warnings)

       day_records = day_records_from_route_analysis(
            route_analysis=route_analysis,
            destination_order=cleaned_destination_ids,
            nights_per_destination=day_allocation,
            total_days=total_days,
        )

        pre_planning_day_plan = self.day_archetype_engine.analyze(day_records)

        transit_days = transit_days_from_day_plan(
            pre_planning_day_plan,
            day_records=day_records,
        )


        # --- Planning, now transit-day-aware at construction time ---

        try:
            build_result = self.itinerary_planning_engine.build(
                request=request,
                destination_ids=destination_ids,
                day_allocation=day_allocation,
                transit_days=transit_days,
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

        # Re-run day_records_from_route_analysis with REAL activity
        # counts now that planning has happened, and re-classify. This
        # second pass is what ValidationEngine/ScheduleRepairEngine
        # consume downstream (overnight_required_from_day_plan,
        # archetypes_by_day_number) -- it is deliberately kept as a
        # SEPARATE pass from pre_planning_day_plan above rather than
        # merged, because pre_planning_day_plan's job was narrowly to
        # decide transit_days before any Drawer existed, while this
        # pass reflects the actually-built cabinet for validation
        # purposes. nights_per_destination is re-derived from the
        # built cabinet (not reused from day_allocation) because
        # ItineraryPlanningEngine.build() may have trimmed
        # destination_ids internally in a way that shifts per-
        # destination night counts relative to the pre-planning
        # calculation -- reading it back from the persisted Shelves is
        # the ground truth.
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

        self._apply_day_themes(cabinet, day_plan)

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
    def _safe_int(value: Any, default: int = 0) -> int:
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
    def _apply_day_themes(cabinet: Any, day_plan: Any) -> None:
        """
        Writes DayArchetypeEngine's per-day classification back onto
        each Shelf.theme, fixing the theme-repetition bug (every day
        of a multi-night stay previously showing the same generic
        theme).

        GUARD (new in this rewrite): a Shelf that
        ItineraryPlanningEngine already set to day_kind="TRANSIT" has
        its theme correctly set to "Travel day" at construction time
        (see itineraryPlanningEngine.py's _theme_for()). This method
        must not overwrite that with a generic archetype-derived theme
        string -- doing so would silently undo the transit-day fix at
        the very last step of the pipeline. Every other shelf continues
        to have its theme rewritten from the archetype classification
        exactly as before.
        """

        shelf_by_day_number = {shelf.day_number: shelf for shelf in cabinet.shelves}

        for day_result in day_plan.days:
            shelf = shelf_by_day_number.get(day_result.day_number)
            if shelf is None:
                continue

            if getattr(shelf, "day_kind", "STANDARD") == "TRANSIT":
                # Already correctly themed at construction time; do
                # not overwrite.
                continue

            derived_theme = _theme_from_archetype(day_result.archetype)
            if derived_theme:
                shelf.theme = derived_theme

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


def _theme_from_archetype(archetype: Any) -> str | None:
    """
    Maps a DayArchetype enum value to the display theme string used
    elsewhere in this app (matching the existing theme vocabulary
    ItineraryPlanningEngine._theme_for() already produces, e.g.
    "Wildlife & wide horizons", "Culture & discovery"). Returns None
    for archetypes that should keep whatever theme
    ItineraryPlanningEngine already assigned (e.g. ARRIVAL/DEPARTURE,
    which have their own dedicated themes set at construction time).
    """

    value = getattr(archetype, "value", archetype)

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
        # Deliberately NOT mapped (return None -> leave existing theme
        # untouched): "arrival", "departure", "transfer",
        # "long_transfer", "overnight_transition", "unknown" -- these
        # either already have a dedicated, more specific theme set at
        # construction time, or don't have enough signal to safely
        # override what's already there.
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

