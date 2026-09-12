"""
Pipeline Adapters
=================

Translation layer between persistence-facing engines and the pure,
DB-free planning engines.

Important architecture rule
----------------------------
A route transition and a route duration are two different facts.

For example:

    Ngorongoro -> Pyramids
        transition exists = TRUE
        crosses country = TRUE
        duration = UNKNOWN

The adapter must NOT convert UNKNOWN into a fabricated duration.

Instead it carries both facts independently:

    is_destination_transition = True
    route_duration_available = False
    travel_hours = 0.0 # only because the downstream
                                      # DayArchetype contract is numeric

The raw RouteLeg remains attached so downstream code can distinguish
"zero travel" from "unknown travel".

Transit-day semantics
----------------------
A calendar day is a TRANSIT day when the itinerary actually enters a
different destination as part of the route.

This is independent of whether the route duration is known.

Therefore:

    measured 6h30m inter-destination route
        -> TRANSIT

    measured 2h inter-destination route
        -> TRANSIT

    unavailable inter-destination route
        -> TRANSIT

The DayArchetypeEngine may additionally classify the day as
LONG_TRANSFER / TRANSFER based on known duration, but the persistence
layer's Shelf.day_kind decision must not depend on duration being known.

No ORM records are created here.
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
    Convert a known duration from minutes to hours.

    None remains semantically unavailable.

    The downstream DayArchetype contract currently expects a numeric
    travel_hours field, so None is represented as 0.0 there. The adapter
    ALWAYS carries the original RouteLeg and an explicit
    route_duration_available flag alongside it so 0.0 is never treated
    as evidence that the route actually takes zero hours.
    """
    if minutes is None:
        return 0.0

    return round(minutes / 60.0, 3)


def hours_to_minutes(hours: float | None) -> int | None:
    if hours is None:
        return None

    return int(round(hours * 60))


# ============================================================================
# DAY ALLOCATION
# ============================================================================

BORDER_BUFFER_NIGHTS = 1


def allocate_days_for_route(
    *,
    destination_ids: list[str],
    meta: dict[str, dict[str, Any]],
    total_days: int,
    travel_style: list[str],
) -> tuple[list[int], list[str]]:
    """
    Decide how many nights each destination receives.

    This is the extracted allocation logic formerly owned by
    ItineraryPlanningEngine._allocate_days().
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

        if not (
            previous_country
            and current_country
            and previous_country != current_country
        ):
            continue

        if BORDER_BUFFER_NIGHTS <= 0:
            continue

        donor_candidates: list[tuple[int, int]] = []

        for donor_index in range(n):
            minimum = meta.get(
                destination_ids[donor_index],
                {},
            ).get("min_nights", 1)

            slack = allocation[donor_index] - minimum

            if slack >= BORDER_BUFFER_NIGHTS:
                donor_candidates.append(
                    (slack, donor_index)
                )

        if not donor_candidates:
            label = meta.get(
                current_destination,
                {},
            ).get(
                "headline_label",
                current_destination,
            )

            warnings.append(
                f"Could not add a border-buffer night before entering "
                f"{label} without shortening another destination below "
                f"its recommended minimum stay."
            )
            continue

        _, donor_index = max(
            donor_candidates,
            key=lambda item: item[0],
        )

        if donor_index == i:
            continue

        allocation[donor_index] -= BORDER_BUFFER_NIGHTS
        allocation[i] += BORDER_BUFFER_NIGHTS

    if any(value < 0 for value in allocation):
        logger.error(
            "Negative day allocation detected: %s",
            allocation,
        )

        allocation = [
            max(0, value)
            for value in allocation
        ]

        while sum(allocation) < total_days:
            allocation[-1] += 1

    if sum(allocation) != total_days:
        logger.error(
            "Day allocation invariant violated: %s != %s",
            sum(allocation),
            total_days,
        )

        allocation[-1] += total_days - sum(allocation)

    return allocation, warnings


# ============================================================================
# ROUTE LEG -> DAY RECORD
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
    Build one DayArchetype-compatible record.

    Critical distinction
    --------------------
    `is_destination_transition` means:

        "A route leg actually moves the traveler from one requested
         destination to another."

    It does NOT mean:

        "We know how long that movement takes."

    Therefore an unavailable RouteLeg is still a valid transition and
    must still be capable of producing a TRANSIT Shelf.
    """

    record: dict[str, Any] = {
        "day_number": day_number,
        "arrival": is_first_day,
        "departure": is_last_day,
        "destination_type": destination_type,
        "activity_count": activity_count,

        # Explicit semantic flags used by the pipeline adapter.
        "is_destination_transition": False,
        "route_duration_available": False,
        "route_unavailable": False,
        "requires_transit_day": False,
    }

    if leg is not None:
        duration_available = (
            leg.duration_minutes is not None
        )

        is_destination_transition = True

        record.update(
            {
                "transfer": True,
                "travel_hours": minutes_to_hours(
                    leg.duration_minutes
                ),
                "travel_distance_km": (
                    leg.distance_km
                    if leg.distance_km is not None
                    else 0.0
                ),
                "crosses_country": bool(
                    leg.is_inter_country
                ),
                "border_crossing": bool(
                    leg.requires_border_crossing
                ),

                # These are intentionally independent of duration.
                "is_destination_transition": is_destination_transition,
                "route_duration_available": duration_available,
                "route_unavailable": not duration_available,

                # A destination-to-destination route transition consumes
                # the arrival/transfer day regardless of whether the
                # duration is known.
                "requires_transit_day": True,

                # Preserve the authoritative source object.
                "_route_leg": leg,
            }
        )

        return record

    # ------------------------------------------------------------------
    # No destination-to-destination route leg.
    # ------------------------------------------------------------------

    record.update(
        {
            "transfer": bool(is_arrival_day),
            "travel_hours": 0.0,
            "travel_distance_km": 0.0,
            "crosses_country": False,
            "border_crossing": False,
            "is_destination_transition": False,
            "route_duration_available": False,
            "route_unavailable": False,
            "requires_transit_day": False,
            "_route_leg": None,
        }
    )

    return record


# ============================================================================
# ROUTE ANALYSIS -> DAY RECORDS
# ============================================================================

def day_records_from_route_analysis(
    *,
    route_analysis: RouteAnalysis,
    destination_order: list[str],
    nights_per_destination: list[int],
    total_days: int,
    activity_counts_by_day: Mapping[int, int] | None = None,
) -> list[dict[str, Any]]:
    """
    Build one ordered day record for every itinerary day.

    The route leg between destination A and destination B is attached
    to the FIRST calendar day allocated to destination B.

    That day is explicitly marked:

        is_destination_transition = True
        requires_transit_day = True

    even when:

        leg.duration_minutes is None

    This is the critical fix for unavailable inter-destination routes.
    """

    if len(destination_order) != len(nights_per_destination):
        raise ValueError(
            "destination_order and nights_per_destination must be "
            "the same length "
            f"({len(destination_order)} != {len(nights_per_destination)})."
        )

    activity_counts = activity_counts_by_day or {}

    # One leg per consecutive destination transition.
    leg_by_arrival_destination: dict[str, RouteLeg] = {
        leg.to_stop.destination_id: leg
        for leg in route_analysis.legs
    }

    destination_types: dict[str, str | None] = {
        stop.destination_id: stop.destination_type
        for stop in route_analysis.stops
    }

    records: list[dict[str, Any]] = []

    day_number = 0

    for destination_index, destination_id in enumerate(
        destination_order
    ):
        nights_here = nights_per_destination[
            destination_index
        ]

        for night_index in range(nights_here):
            day_number += 1

            is_first_day = day_number == 1
            is_last_day = day_number == total_days

            is_arrival_day = (
                night_index == 0
                and destination_index > 0
            )

            leg = (
                leg_by_arrival_destination.get(
                    destination_id
                )
                if is_arrival_day
                else None
            )

            record = day_record_from_route_leg(
                day_number=day_number,
                total_days=total_days,
                is_first_day=is_first_day,
                is_last_day=is_last_day,
                is_arrival_day=is_arrival_day,
                leg=leg,
                activity_count=activity_counts.get(
                    day_number,
                    0,
                ),
                destination_type=destination_types.get(
                    destination_id
                ),
            )

            records.append(record)

    # Defensive invariant:
    # the number of generated records must match the requested trip days.
    if len(records) != total_days:
        raise ValueError(
            "Generated day records do not match total_days: "
            f"{len(records)} != {total_days}"
        )

    return records


# ============================================================================
# EXPLICIT TRANSIT-DAY EXTRACTION
# ============================================================================

def transit_days_from_day_records(
    day_records: list[Mapping[str, Any]],
) -> dict[int, bool]:
    """
    Extract the authoritative transit-day decision directly from the
    route-aware day records.

    This is intentionally NOT based on DayArchetype.

    Why?

    DayArchetype answers:

        "What kind of day is this?"

    Transit-day persistence answers:

        "Does this day contain a destination-to-destination transition?"

    Those are related but not identical questions.

    Most importantly, an unavailable route duration must not prevent a
    real destination transition from becoming a TRANSIT Shelf.
    """

    transit_days: dict[int, bool] = {}

    for record in day_records:
        day_number = record.get("day_number")

        if day_number is None:
            continue

        transit_days[int(day_number)] = bool(
            record.get("requires_transit_day", False)
        )

    return transit_days


# ============================================================================
# DAY ARCHETYPE OUTPUT -> VALIDATION ENGINE INPUT
# ============================================================================

_ARCHETYPES_WITHOUT_OVERNIGHT_REQUIREMENT = frozenset(
    {"departure"}
)


def overnight_required_from_day_plan(day_plan) -> dict[int, bool]:
    """
    Build the overnight_required mapping consumed by ValidationEngine.
    """

    return {
        day.day_number: (
            day.archetype.value
            not in _ARCHETYPES_WITHOUT_OVERNIGHT_REQUIREMENT
        )
        for day in day_plan.days
    }


# ============================================================================
# DAY ARCHETYPE OUTPUT -> TRANSIT FALLBACK
# ============================================================================

_ARCHETYPES_REQUIRING_TRANSIT_DAY = frozenset(
    {
        "long_transfer",
        "transfer",
        "overnight_transition",
    }
)


def transit_days_from_day_plan(
    day_plan,
    day_records: list[Mapping[str, Any]] | None = None,
) -> dict[int, bool]:
    """
    Build the transit-day mapping.

    Primary source
    --------------
    Route-aware day records.

    Fallback source
    --------------
    DayArchetype classification.

    The route-aware source is authoritative because a route can be
    unavailable while still being a genuine destination transition.

    Example:

        Ngorongoro -> Pyramids
        duration = None

    DayArchetype may classify this as SAFARI because it cannot infer
    travel hours from None.

    That must NOT erase the route transition.

    Therefore:

        route record says TRANSIT -> True
        archetype says SAFARI -> irrelevant

    The fallback to archetype exists only for compatibility with
    callers that do not provide day_records.
    """

    result: dict[int, bool] = {}

    # ---------------------------------------------------------------
    # Authoritative route-aware decision.
    # ---------------------------------------------------------------
    if day_records is not None:
        result.update(
            transit_days_from_day_records(
                day_records
            )
        )

    # ---------------------------------------------------------------
    # Archetype fallback / compatibility.
    # ---------------------------------------------------------------
    for day in day_plan.days:
        day_number = day.day_number

        archetype_requires_transit = (
            day.archetype.value
            in _ARCHETYPES_REQUIRING_TRANSIT_DAY
        )

        # Never downgrade an explicit route transition.
        result[day_number] = bool(
            result.get(day_number, False)
            or archetype_requires_transit
        )

    return result


# ============================================================================
# ACTIVITY CONSTRAINTS ADAPTER
# ============================================================================

def activity_record_from_drawer(
    drawer: Any,
) -> dict[str, Any]:
    """
    Build an activity_constraints-compatible record from a Drawer ORM row.
    """

    record: dict[str, Any] = {
        "id": drawer.activity_id or drawer.id,
        "name": drawer.name,
    }

    if drawer.duration_minutes is not None:
        record["duration_minutes"] = (
            drawer.duration_minutes
        )

    record["fixed_time"] = drawer.activity_type in {
        "ARRIVAL",
        "DEPARTURE",
        "TRANSFER",
        "MEAL",
    }

    if drawer.start_time is not None:
        record["start_time"] = (
            drawer.start_time.strftime("%H:%M")
        )

    record["_drawer_id"] = drawer.id
    record["_is_fallback"] = bool(
        getattr(drawer, "is_fallback", False)
    )

    return record


def schedule_record_from_shelf(
    shelf: Any,
) -> dict[str, Any]:
    """
    Build one ScheduleRepair-compatible day record.
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


def schedule_input_from_cabinet(
    cabinet: Any,
) -> list[dict[str, Any]]:
    """
    Build the complete ScheduleRepair input from a Cabinet.
    """

    return [
        schedule_record_from_shelf(shelf)
        for shelf in cabinet.shelves
    ]


# ============================================================================
# ARCHETYPE LOOKUP
# ============================================================================

def archetypes_by_day_number(
    day_plan,
) -> dict[int, Any]:
    """
    Build day_number -> DayArchetype mapping.
    """

    return {
        day.day_number: day.archetype
        for day in day_plan.days
    }


__all__ = [
    "minutes_to_hours",
    "hours_to_minutes",
    "allocate_days_for_route",
    "day_record_from_route_leg",
    "day_records_from_route_analysis",
    "transit_days_from_day_records",
    "overnight_required_from_day_plan",
    "transit_days_from_day_plan",
    "activity_record_from_drawer",
    "schedule_record_from_shelf",
    "schedule_input_from_cabinet",
    "archetypes_by_day_number",
]
