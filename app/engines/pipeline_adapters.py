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
"""

from __future__ import annotations

from typing import Any, Mapping

from route_geography import RouteAnalysis, RouteLeg


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
    - how many nights ItineraryPlanningEngine._allocate_days() assigned
      to each destination in order,
    - and, optionally, per-day activity counts (day_number -> count) if
      already known; days not present default to 0.

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
    "day_record_from_route_leg",
    "day_records_from_route_analysis",
    "overnight_required_from_day_plan",
    "activity_record_from_drawer",
    "schedule_record_from_shelf",
    "schedule_input_from_cabinet",
    "archetypes_by_day_number",
]

