"""
Pipeline Adapters
==================

Translation layer between the persistence-facing engines
(RouteGeographyEngine, ItineraryPlanningEngine, ValidationEngine -- all
of which operate on SQLAlchemy Sessions and ORM rows / typed
dataclasses in minutes) and the four pure, DB-free planning engines
(day_archetype.py, activity_constraints.py, schedule_repair.py -- all
of which operate on plain Mapping[str, Any] records in HOURS).

This file exists because those two groups of engines were designed
independently and do not share a data contract:

    route_geography.RouteLeg.duration_minutes -> int | None, MINUTES
    day_archetype.DaySignals.travel_hours -> float, HOURS

    ItineraryPlanningEngine's Cabinet/Shelf/Drawer ORM rows
        -> day_archetype.py / activity_constraints.py /
           schedule_repair.py's Mapping[str, Any] record shape

Nothing here invents data. Every adapter function either:
  (a) carries a value through with a unit conversion, or
  (b) passes through Nones/absences as absences (never a fabricated
      default that could be mistaken for a real fact), or
  (c) attaches a domain object (DayArchetypeResult, RouteLeg) as
      context that a downstream engine can consult explicitly.

This module does NOT create or persist ORM records
(Cabinet/Shelf/Drawer/Headboard/Armrest/Tray/Hinge). It only reads
existing ORM state and RouteAnalysis output, and produces plain
dict/dataclass values for the four planning engines to consume. That
responsibility boundary matches the audit-locked architecture: the new
engines return domain results, they do not touch persistence.

CHANGE LOG (this rewrite -- transit-day fix, backend stage 1)
--------------------------------------------------------------------
PROBLEM BEING FIXED: DayArchetypeEngine (which classifies a day as
LONG_TRANSFER when travel_hours crosses day_archetype.py's own
LONG_TRAVEL_HOURS/VERY_LONG_TRAVEL_HOURS thresholds) previously ran
AFTER ItineraryPlanningEngine.build() had already constructed every
Drawer for every day -- including forcing a normal activity template
(arrival transfer + lunch + an activity + sundowner) onto a day that
was, in reality, consumed by an intercontinental flight (e.g.
Tanzania -> Ethiopia, Tanzania -> Madagascar). The archetype
classification existed but was computed too late to influence what got
built; it was only ever used afterward, for ValidationEngine's
warnings.

ROOT CAUSE: day_records_from_route_analysis() (used to build
DayArchetypeEngine's input) requires nights_per_destination, which was
only known AFTER ItineraryPlanningEngine._allocate_days() ran --
creating a real circular dependency (planning needs archetypes to
avoid the transit-day bug; archetypes need day allocation; day
allocation used to live inside planning).

FIX: day-allocation logic is extracted out of ItineraryPlanningEngine
entirely and into allocate_days_for_route() below -- a pure function
with no DB access and no side effects, taking exactly the inputs
ItineraryPlanningEngine._allocate_days() used to close over
(destination_ids, per-destination meta, total_days, travel_style).
This breaks the circular dependency: the orchestrator can now call
allocate_days_for_route() first, feed its result into
day_records_from_route_analysis() to get DayArchetypeEngine's
classification, and THEN call ItineraryPlanningEngine.build() with
that classification already available -- see itinerary_v2.py's
reordered generate() method.

ItineraryPlanningEngine.build() no longer performs its own day
allocation; it accepts the already-computed allocation as a parameter.
This is a deliberate responsibility move (planning engine no longer
owns "how many nights per destination"), not a duplication -- the old
_allocate_days() method's logic is preserved VERBATIM below, just
relocated and stripped of its `self` dependency (it never used `self`
for anything except being a method in the first place).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from app.engines.route_geography import RouteAnalysis, RouteLeg

logger = logging.getLogger(__name__)


# ============================================================================
# UNIT CONVERSION
# ============================================================================

def minutes_to_hours(minutes: int | None) -> float:
    """
    Convert minutes to hours for the hour-based engines.

    A None (unavailable) duration converts to 0.0 hours rather than
    being silently dropped -- callers that need to distinguish "no
    travel" from "unknown travel duration" should check the
    originating RouteLeg.is_unavailable flag directly, which this
    module always makes available alongside the numeric value (see
    day_record_for_leg below). Converting None -> 0.0 only at this
    single, narrow boundary (rather than upstream) keeps the
    "unavailable" fact itself intact for anything that inspects the
    RouteLeg/Hinge directly.
    """

    if minutes is None:
        return 0.0

    return round(minutes / 60.0, 3)


def hours_to_minutes(hours: float | None) -> int | None:
    if hours is None:
        return None

    return int(round(hours * 60))


# ============================================================================
# DAY ALLOCATION (extracted from ItineraryPlanningEngine -- see module
# docstring for why)
# ============================================================================

# Preserved verbatim from ItineraryPlanningEngine's own module-level
# constant. Kept here (not re-imported from itineraryPlanningEngine.py)
# to avoid a reverse import (that module now imports FROM this one, for
# allocate_days_for_route -- importing back would create a cycle).
BORDER_BUFFER_NIGHTS = 1


def allocate_days_for_route(
    *,
    destination_ids: list[str],
    meta: dict[str, dict[str, Any]],
    total_days: int,
    travel_style: list[str],
) -> tuple[list[int], list[str]]:
    """
    Decide how many nights each destination in the route receives,
    given the trip's total day count.

    This is the exact logic that previously lived as
    ItineraryPlanningEngine._allocate_days() -- moved here unchanged
    (aside from dropping `self`, which the original method never
    actually used) so it can run BEFORE ItineraryPlanningEngine.build(),
    breaking the circular dependency described in this module's
    docstring. `meta` is the same per-destination metadata dict
    ItineraryPlanningEngine._fetch_destination_meta() already produces
    (country, headline_label, destination_type, min_nights) --
    callers should fetch that first and pass it in unchanged.

    `travel_style` is accepted for signature compatibility with the
    original method (and in case a future revision wants to factor it
    into allocation) but is not currently read by the allocation logic
    itself -- same as before this extraction.
    """

    n = len(destination_ids)
    if n == 0:
        return [], []

    if total_days < n:
        allocation = [0] * n
        for index in range(total_days):
            allocation[index] = 1
        return allocation, [
            "Trip duration is shorter than the number of requested "
            "destinations; only the first destinations can receive a day."
        ]

    allocation = [total_days // n] * n
    remainder = total_days % n
    for index in range(remainder):
        allocation[index] += 1

    warnings: list[str] = []

    for i in range(1, n):
        previous_destination = destination_ids[i - 1]
        current_destination = destination_ids[i]
        previous_country = meta.get(previous_destination, {}).get("country")
        current_country = meta.get(current_destination, {}).get("country")

        if not (previous_country and current_country and previous_country != current_country):
            continue
        if BORDER_BUFFER_NIGHTS <= 0:
            continue

        donor_candidates: list[tuple[int, int]] = []
        for donor_index in range(n):
            minimum = meta.get(destination_ids[donor_index], {}).get("min_nights", 1)
            slack = allocation[donor_index] - minimum
            # >= BORDER_BUFFER_NIGHTS, not > 0, so a destination can
            # never be donated from below its recommended minimum.
            if slack >= BORDER_BUFFER_NIGHTS:
                donor_candidates.append((slack, donor_index))

        if not donor_candidates:
            label = meta.get(current_destination, {}).get("headline_label", current_destination)
            warnings.append(
                f"Could not add a border-buffer night before entering {label} "
                "without shortening another destination below its recommended "
                "minimum stay."
            )
            continue

        _, donor_index = max(donor_candidates, key=lambda item: item[0])
        if donor_index == i:
            continue

        allocation[donor_index] -= BORDER_BUFFER_NIGHTS
        allocation[i] += BORDER_BUFFER_NIGHTS

    if any(value < 0 for value in allocation):
        logger.error("Negative day allocation detected: %s", allocation)
        allocation = [max(0, value) for value in allocation]
        while sum(allocation) < total_days:
            allocation[-1] += 1

    if sum(allocation) != total_days:
        logger.error("Day allocation invariant violated: %s != %s", sum(allocation), total_days)
        allocation[-1] += total_days - sum(allocation)

    return allocation, warnings


# ============================================================================
# ROUTE GEOGRAPHY -> DAY ARCHETYPE INPUT
# ============================================================================

def day_record_from_route_leg(
    *,
    day_number: int,
    total_days: int,
    is_first_day: bool,
    is_last_day: bool,
    is_arrival_day: bool,
    leg: RouteLeg | None,
    activity_count: int,
    destination_type: str | None,
) -> dict[str, Any]:
    """
    Build one day_archetype.py-compatible day record.

    ``leg`` is the RouteLeg that lands on this day (i.e. the transfer
    INTO the destination for this shelf), or None if this day has no
    transfer (a normal activity day mid-stay).

    day_archetype.py's _signals_from_record() reads travel_hours,
    transfer flag, crosses_country, border_crossing, and the
    has_safari/has_beach/etc. flags via a free-text scan of
    destination_type -- see _extract_activity_flags in that file. We
    populate destination_type directly and let that engine's own text
    matching derive the has_* flags; we do not attempt to duplicate
    that classification logic here.
    """

    record: dict[str, Any] = {
        "arrival": is_first_day,
        "departure": is_last_day,
        "destination_type": destination_type,
        "activity_count": activity_count,
    }

    if leg is not None:
        record["transfer"] = True
        record["travel_hours"] = minutes_to_hours(leg.duration_minutes)
        record["travel_distance_km"] = leg.distance_km or 0.0
        record["crosses_country"] = leg.is_inter_country
        record["border_crossing"] = leg.requires_border_crossing
        # Carry the raw leg forward for anything that wants to inspect
        # the un-converted, un-lossy source fact (e.g. whether the
        # duration is genuinely unavailable vs. a real zero).
        record["_route_leg"] = leg
    else:
        record["transfer"] = is_arrival_day
        record["travel_hours"] = 0.0
        record["travel_distance_km"] = 0.0
        record["crosses_country"] = False
        record["border_crossing"] = False
        record["_route_leg"] = None

    return record


def day_records_from_route_analysis(
    *,
    route_analysis: RouteAnalysis,
    destination_order: list[str],
    nights_per_destination: list[int],
    total_days: int,
    activity_counts_by_day: Mapping[int, int] | None = None,
) -> list[dict[str, Any]]:
    """
    Build the full ordered list of day_archetype.py day records for an
    itinerary, given:

    - the RouteGeographyEngine's analysis of the destination route
      (which produces one RouteLeg per destination-to-destination
      transition, NOT one per day),
    - how many nights were allocated to each destination in order
      (from allocate_days_for_route(), called by the orchestrator
      BEFORE ItineraryPlanningEngine.build() -- see this module's
      docstring for why that ordering changed),
    - and, optionally, per-day activity counts (day_number -> count) if
      already known; days not present default to 0.

    NOTE ON activity_counts_by_day: since this function can now run
    BEFORE ItineraryPlanningEngine.build() has constructed any Drawers,
    callers invoking it pre-planning should simply omit this parameter
    (every day defaults to activity_count=0) -- day_archetype.py's
    classify_day() does not require a non-zero activity_count to
    correctly detect LONG_TRANSFER; that classification is driven by
    travel_hours/transfer flags from the leg data, not by activity
    counts. activity_counts_by_day remains available for any caller
    that wants to re-classify AFTER planning (e.g. for a post-hoc
    ValidationEngine pass) with real counts.

    The mapping from "N legs between destinations" to "1 day per
    calendar day" is: the leg immediately preceding a destination lands
    on that destination's FIRST day only. Every other day at that
    destination has no leg (transfer=False unless later marked
    otherwise by the caller).
    """

    if len(destination_order) != len(nights_per_destination):
        raise ValueError(
            "destination_order and nights_per_destination must be the "
            "same length "
            f"({len(destination_order)} != {len(nights_per_destination)})."
        )

    activity_counts = activity_counts_by_day or {}

    # Legs are keyed by (from_destination_id, to_destination_id) in
    # RouteAnalysis.legs, in the same order as consecutive DISTINCT
    # destinations in destination_order. Build a lookup by the
    # destination the leg arrives AT, since that's what determines
    # which day it lands on.
    leg_by_arrival_destination: dict[str, RouteLeg] = {
        leg.to_stop.destination_id: leg for leg in route_analysis.legs
    }

    destination_types: dict[str, str | None] = {
        stop.destination_id: stop.destination_type for stop in route_analysis.stops
    }

    records: list[dict[str, Any]] = []
    day_number = 0

    for destination_index, destination_id in enumerate(destination_order):
        nights_here = nights_per_destination[destination_index]

        for night_index in range(nights_here):
            day_number += 1

            is_first_day = day_number == 1
            is_last_day = day_number == total_days
            is_arrival_day = night_index == 0 and destination_index > 0

            leg = (
                leg_by_arrival_destination.get(destination_id)
                if is_arrival_day
                else None
            )

            records.append(
                day_record_from_route_leg(
                    day_number=day_number,
                    total_days=total_days,
                    is_first_day=is_first_day,
                    is_last_day=is_last_day,
                    is_arrival_day=is_arrival_day,
                    leg=leg,
                    activity_count=activity_counts.get(day_number, 0),
                    destination_type=destination_types.get(destination_id),
                )
            )

    return records


# ============================================================================
# DAY ARCHETYPE OUTPUT -> VALIDATION ENGINE INPUT
# ============================================================================

# Archetypes for which an overnight Headboard is NOT expected. Kept as
# an explicit allowlist (rather than "everything except NORMAL") so
# that adding a new DayArchetype value in the future does not silently
# change validation behavior -- a new archetype defaults to requiring
# accommodation until someone deliberately adds it here.
_ARCHETYPES_WITHOUT_OVERNIGHT_REQUIREMENT = frozenset({"departure"})


def overnight_required_from_day_plan(day_plan) -> dict[int, bool]:
    """
    Build the ``overnight_required`` mapping ValidationEngine.validate()
    accepts, from a day_archetype.DayArchetypePlan.

    day_plan.days is a tuple of DayArchetypeResult, each with
    .day_number and .archetype (a DayArchetype enum whose .value is a
    lowercase string, e.g. "departure").
    """

    return {
        day.day_number: (
            day.archetype.value not in _ARCHETYPES_WITHOUT_OVERNIGHT_REQUIREMENT
        )
        for day in day_plan.days
    }


# ============================================================================
# DAY ARCHETYPE OUTPUT -> ITINERARY PLANNING ENGINE INPUT (transit days)
# ============================================================================

# Archetypes that mean "this day's primary content is a long-haul
# transfer" -- ItineraryPlanningEngine uses this mapping to decide
# whether to build the normal activity template for a day or the
# TRANSIT-day template (no forced activity slot; see
# itineraryPlanningEngine.py's _populate_drawers()). Sourced from
# day_archetype.py's own DayArchetype enum -- LONG_TRANSFER is the
# archetype classify_day() assigns when travel_hours crosses that
# module's LONG_TRAVEL_HOURS/VERY_LONG_TRAVEL_HOURS thresholds. No new
# thresholds are introduced here; this is purely a lookup of an
# already-computed classification.
_ARCHETYPES_REQUIRING_TRANSIT_DAY = frozenset({"long_transfer"})


def transit_days_from_day_plan(day_plan) -> dict[int, bool]:
    """
    Build a day_number -> is_transit_day mapping from a
    day_archetype.DayArchetypePlan, for ItineraryPlanningEngine.build()
    to consult when constructing each Shelf's drawers.

    A day not present in day_plan (should not normally happen -- every
    day the orchestrator allocates is classified) is treated as NOT a
    transit day by the caller's default handling (see
    itineraryPlanningEngine.py: `.get(day_number, False)`), matching
    the same "unknown defaults to the conservative/existing behavior"
    convention used by overnight_required_from_day_plan above.
    """

    return {
        day.day_number: (day.archetype.value in _ARCHETYPES_REQUIRING_TRANSIT_DAY)
        for day in day_plan.days
    }


# ============================================================================
# CABINET / SHELF / DRAWER ORM -> SCHEDULE_REPAIR / ACTIVITY_CONSTRAINTS INPUT
# ============================================================================

def activity_record_from_drawer(drawer: Any) -> dict[str, Any]:
    """
    Build one activity_constraints.py-compatible activity record from a
    persisted Drawer ORM row.

    Only EXPERIENCE-type drawers represent bookable/schedulable
    activities in the sense activity_constraints.py models (duration,
    intensity, opening hours, etc.); MEAL/TRANSFER/ARRIVAL/DEPARTURE
    drawers are structural itinerary entries, not activities with
    constraints, so callers should filter to activity_type ==
    "EXPERIENCE" before calling this (see schedule_record_from_shelf
    below, which does this filtering).

    activity_constraints.normalize_activity() already tolerates missing
    fields via safe_* helpers and falls back to
    DEFAULT_ACTIVITY_DURATION_HOURS -- we do not need to fabricate
    values here, only pass through what the Drawer actually has.
    """

    record: dict[str, Any] = {
        "id": drawer.activity_id or drawer.id,
        "name": drawer.name,
    }

    if drawer.duration_minutes is not None:
        record["duration_minutes"] = drawer.duration_minutes

    # Doc 6's Drawer schema does not currently carry earliest_start,
    # opening_hours, min_age, incompatible_with, booking_required, or
    # any of the other richer fields activity_constraints.py can
    # consume (see the audit note: "activity schema not yet
    # confirmed"). We deliberately do NOT populate those keys with
    # guessed values -- normalize_activity() already defaults them to
    # None/unknown when absent, which is the correct behavior until
    # the underlying activities table is confirmed to carry that data.

    record["fixed_time"] = drawer.activity_type in {
        "ARRIVAL", "DEPARTURE", "TRANSFER", "MEAL",
    }

    if drawer.start_time is not None:
        record["start_time"] = drawer.start_time.strftime("%H:%M")

    record["_drawer_id"] = drawer.id
    record["_is_fallback"] = bool(getattr(drawer, "is_fallback", False))

    return record


def schedule_record_from_shelf(shelf: Any) -> dict[str, Any]:
    """
    Build one schedule_repair.py-compatible day record (the ``days``
    parameter to ScheduleRepairEngine.repair()) from a persisted Shelf
    ORM row, including only its EXPERIENCE-type drawers as schedulable
    activities.
    """

    activities = [
        activity_record_from_drawer(drawer)
        for drawer in shelf.drawers
        if drawer.activity_type == "EXPERIENCE"
    ]

    return {
        "day_number": shelf.day_number,
        "activities": activities,
    }


def schedule_input_from_cabinet(cabinet: Any) -> list[dict[str, Any]]:
    """
    Build the full ``days`` list schedule_repair.py's
    ScheduleRepairEngine.repair() expects, from a persisted Cabinet.

    Shelves are already stored in day_number order (see
    Cabinet.shelves relationship's order_by="Shelf.day_number" in
    models_furniture.py), so no re-sorting is performed here -- doing
    so would risk silently masking a persistence bug where shelves were
    written out of order.
    """

    return [schedule_record_from_shelf(shelf) for shelf in cabinet.shelves]


def archetypes_by_day_number(day_plan) -> dict[int, Any]:
    """
    Build the ``archetypes: Mapping[int, DayArchetype]`` parameter
    schedule_repair.py's repair()/validate() accept, from a
    day_archetype.DayArchetypePlan.
    """

    return {day.day_number: day.archetype for day in day_plan.days}


__all__ = [
    "minutes_to_hours",
    "hours_to_minutes",
    "allocate_days_for_route",
    "day_record_from_route_leg",
    "day_records_from_route_analysis",
    "overnight_required_from_day_plan",
    "transit_days_from_day_plan",
    "activity_record_from_drawer",
    "schedule_record_from_shelf",
    "schedule_input_from_cabinet",
    "archetypes_by_day_number",
]
