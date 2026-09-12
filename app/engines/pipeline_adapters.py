"""
Pipeline Adapters
=================

Translation layer between persistence-facing engines and the pure,
DB-free planning engines.

Important architecture rule
----------------------------
A route transition and a transit-day decision are two different facts.

A destination can change without consuming the calendar day as a
dedicated TRANSIT day.

Examples:

    Serengeti -> Ngorongoro
        same country
        normal safari routing
        duration may be known or unknown
        -> NOT automatically TRANSIT

    Ngorongoro -> Pyramids
        different country
        international transition
        duration may be unknown
        -> TRANSIT

    Same-country route taking 7 hours
        measured long travel
        -> TRANSIT

The adapter must NOT fabricate a duration when the route duration
is unavailable.

The raw RouteLeg remains attached so downstream code can distinguish
known travel from unknown travel.

No ORM records are created here.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from app.engines.route_geography import RouteAnalysis, RouteLeg

logger = logging.getLogger(__name__)


# ============================================================================
# TRANSIT RULES
# ============================================================================

# A measured route consuming approximately half a day or more is treated
# as a dedicated transit day.
TRANSIT_TRAVEL_THRESHOLD_HOURS = 6.0


# ============================================================================
# UNIT CONVERSION
# ============================================================================

def minutes_to_hours(minutes: int | None) -> float:
    """
    Convert a known duration from minutes to hours.

    None remains semantically unavailable.

    The downstream DayArchetype contract currently expects a numeric
    travel_hours field, so None is represented as 0.0 there.

    The original RouteLeg and route_duration_available flag are preserved
    so 0.0 is never interpreted as evidence that the route actually takes
    zero hours.
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

    IMPORTANT
    ---------
    A destination transition does NOT automatically mean the calendar
    day is a TRANSIT day.

    The route facts are kept separately:

        is_destination_transition
        route_duration_available
        travel_hours
        crosses_country
        requires_transit_day

    Transit-day semantics:

        1. Cross-country/international transition
           -> TRANSIT

        2. Measured travel >= 6 hours
           -> TRANSIT

        3. Same-country destination transition below 6 hours
           -> NOT automatically TRANSIT

        4. Unknown-duration same-country transition
           -> NOT automatically TRANSIT

    This prevents normal safari routing such as:

        Serengeti -> Ngorongoro

    from becoming a dedicated travel day merely because the destination
    changed.
    """

    record: dict[str, Any] = {
        "day_number": day_number,
        "arrival": is_first_day,
        "departure": is_last_day,
        "destination_type": destination_type,
        "activity_count": activity_count,

        "is_destination_transition": False,
        "route_duration_available": False,
        "route_unavailable": False,
        "requires_transit_day": False,
    }

    if leg is not None:
        duration_available = (
            leg.duration_minutes is not None
        )

        travel_hours = minutes_to_hours(
            leg.duration_minutes
        )

        crosses_country = bool(
            leg.is_inter_country
        )

        # ---------------------------------------------------------------
        # Transit decision
        # ---------------------------------------------------------------
        #
        # DO NOT use:
        #
        # is_destination_transition = True
        #
        # as the transit criterion.
        #
        # A destination change is only a route fact.
        #
        # A TRANSIT day requires either:
        #
        # - an international/cross-country transition, OR
        # - measured travel that consumes >= 6 hours.
        #
        # Therefore:
        #
        # Serengeti -> Ngorongoro
        # crosses_country = False
        # travel < 6h OR unavailable
        # -> STANDARD
        #
        # Ngorongoro -> Pyramids
        # crosses_country = True
        # -> TRANSIT
        #
        requires_transit_day = (
            crosses_country
            or (
                duration_available
                and travel_hours >= TRANSIT_TRAVEL_THRESHOLD_HOURS
            )
        )

        record.update(
            {
                "transfer": True,

                "travel_hours": travel_hours,

                "travel_distance_km": (
                    leg.distance_km
                    if leg.distance_km is not None
                    else 0.0
                ),

                "crosses_country": crosses_country,

                "border_crossing": bool(
                    leg.requires_border_crossing
                ),

                # A route transition exists independently from whether
                # it consumes the day as TRANSIT.
                "is_destination_transition": True,

                "route_duration_available": duration_available,

                "route_unavailable": not duration_available,

                # This is the actual Shelf.day_kind decision.
                "requires_transit_day": requires_transit_day,

                # Preserve authoritative route information.
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

    Important:

        A route leg means a destination transition exists.

        It does NOT automatically mean that day is TRANSIT.

    The final transit decision is made by day_record_from_route_leg()
    using cross-country status and measured travel duration.
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

    # Defensive invariant.
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
    Extract the authoritative transit-day decision from route-aware
    day records.

    This is intentionally independent of DayArchetype.

    DayArchetype answers:

        "What kind of day is this?"

    Transit persistence answers:

        "Should this Shelf be persisted as TRANSIT?"

    The route-aware decision is:

        cross-country -> TRANSIT

        OR

        measured travel >= 6h -> TRANSIT

        otherwise -> STANDARD

    A destination transition alone is NOT enough.
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

    When day_records are supplied, they are authoritative.

    This is important because the DayArchetypeEngine currently receives
    travel_hours as a numeric field. An unavailable route duration is
    represented there as 0.0, so DayArchetype cannot reliably determine
    whether an unknown route is an international transition.

    Therefore:

        route-aware record -> authoritative Shelf.day_kind

    while DayArchetype is only a compatibility fallback.
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
    # Archetype fallback.
    #
    # IMPORTANT:
    # If a day already exists in day_records, do NOT allow the generic
    # archetype fallback to override the route-aware decision.
    #
    # Otherwise a same-country safari transfer classified as "transfer"
    # could incorrectly become a TRANSIT Shelf.
    # ---------------------------------------------------------------
    for day in day_plan.days:
        day_number = day.day_number

        if (
            day_records is not None
            and day_number in result
        ):
            continue

        archetype_requires_transit = (
            day.archetype.value
            in _ARCHETYPES_REQUIRING_TRANSIT_DAY
        )

        result[day_number] = bool(
            archetype_requires_transit
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
