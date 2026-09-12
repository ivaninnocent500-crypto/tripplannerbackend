"""
Pipeline Adapters
=================

Translation layer between persistence-facing engines and the pure,
DB-free planning engines.

Responsibilities
----------------
1. Convert route facts into planning records.
2. Allocate the requested calendar days across destinations.
3. Determine whether a requested destination sequence can coherently
   fit inside the requested trip duration.
4. Determine TRANSIT days without confusing every destination change
   with a travel day.

Important architecture rules
----------------------------

A destination transition and a TRANSIT day are different concepts.

Example:

    Serengeti -> Ngorongoro
        same country
        normal safari movement
        short/unknown duration
        -> NOT automatically TRANSIT

    Ngorongoro -> Pyramids
        different country
        international movement
        duration may be unknown
        -> TRANSIT

    Same-country route taking 7 hours
        measured long travel
        -> TRANSIT

A route duration must never be fabricated.

Feasibility is separate from transit classification.

A TRANSIT day is a calendar day already contained inside the requested
trip duration. It is NOT an additional day added on top of the destination
allocation.

Therefore:

    7 requested days
        -> allocation must sum to 7

and never:

    7 requested days
        -> 7 destination days + 1 transit day
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from app.engines.route_geography import RouteAnalysis, RouteLeg

logger = logging.getLogger(__name__)


# ============================================================================
# CONSTANTS
# ============================================================================

TRANSIT_TRAVEL_THRESHOLD_HOURS = 6.0

# Used only when destination minimum-stay metadata is missing or invalid.
DEFAULT_MIN_NIGHTS = 2

BORDER_BUFFER_NIGHTS = 1


# ============================================================================
# UNIT CONVERSION
# ============================================================================

def minutes_to_hours(
    minutes: int | None,
) -> float:
    """
    Convert known minutes to hours.

    None remains semantically unavailable.

    DayArchetype currently expects a numeric travel_hours value, so
    unavailable duration is represented there as 0.0 while the explicit
    route_duration_available flag preserves the real meaning.
    """

    if minutes is None:
        return 0.0

    return round(
        minutes / 60.0,
        3,
    )


def hours_to_minutes(
    hours: float | None,
) -> int | None:

    if hours is None:
        return None

    return int(
        round(
            hours * 60
        )
    )


# ============================================================================
# DESTINATION FEASIBILITY
# ============================================================================

def destination_min_nights(
    destination_id: str,
    meta: dict[str, dict[str, Any]],
) -> int:
    """
    Return the minimum meaningful stay for a destination.

    Database metadata is authoritative when available.

    Missing/invalid metadata receives the conservative default.
    """

    raw_value = (
        meta
        .get(
            destination_id,
            {},
        )
        .get("min_nights")
    )

    try:
        value = int(raw_value)
    except (
        TypeError,
        ValueError,
    ):
        value = DEFAULT_MIN_NIGHTS

    return max(
        1,
        value,
    )


def route_transition_consumes_day(
    leg: RouteLeg | None,
) -> bool:
    """
    Determine whether a route transition should be classified as a
    dedicated TRANSIT calendar day.

    Rules:

        cross-country
            -> TRANSIT

        measured duration >= 6 hours
            -> TRANSIT

        unknown same-country route
            -> NOT automatically TRANSIT

        short same-country route
            -> NOT TRANSIT
    """

    if leg is None:
        return False

    if bool(
        leg.is_inter_country
    ):
        return True

    if leg.duration_minutes is None:
        return False

    return (
        minutes_to_hours(
            leg.duration_minutes
        )
        >= TRANSIT_TRAVEL_THRESHOLD_HOURS
    )


def route_transition_days(
    *,
    route_analysis: RouteAnalysis,
    destination_order: list[str],
) -> int:
    """
    Count transitions that should be classified as TRANSIT.

    This function is informational/classification-only.

    It does NOT add calendar days to the trip.
    """

    if len(destination_order) <= 1:
        return 0

    legs_by_destination: dict[
        str,
        RouteLeg,
    ] = {
        leg.to_stop.destination_id: leg
        for leg in route_analysis.legs
    }

    transition_days = 0

    for destination_id in destination_order[1:]:
        leg = legs_by_destination.get(
            destination_id
        )

        if route_transition_consumes_day(
            leg
        ):
            transition_days += 1

    return transition_days


def minimum_days_required_for_route(
    *,
    destination_order: list[str],
    meta: dict[str, dict[str, Any]],
    route_analysis: RouteAnalysis | None = None,
) -> int:
    """
    Calculate the minimum calendar days required for the requested
    destination sequence.

    IMPORTANT:

    Transit days are NOT added here.

    A transit day occupies one of the destination's allocated calendar
    days. It does not create an additional day beyond the user's
    requested trip duration.

    Example:

        7-day trip

        Destination minimums:
            Tarangire = 2
            Zanzibar = 2
            Lalibela = 2

        minimum = 6

    The remaining seventh day can then be distributed by the allocator.

    Route geography is used only for transit classification, not to
    increase the requested calendar duration.
    """

    if not destination_order:
        return 0

    return sum(
        destination_min_nights(
            destination_id,
            meta,
        )
        for destination_id in destination_order
    )


def find_feasible_destination_order(
    *,
    destination_ids: list[str],
    meta: dict[str, dict[str, Any]],
    total_days: int,
    route_analysis: RouteAnalysis | None = None,
) -> tuple[list[str], list[str]]:
    """
    Find the largest feasible destination PREFIX.

    The user's requested destination order is authoritative.

    We intentionally do NOT search arbitrary combinations.

    Example:

        Requested:

            A -> B -> C -> D

        If A+B+C fit but A+B+C+D does not:

            A -> B -> C

        is selected.

    We do NOT produce:

            A -> C -> D

    because that silently removes a middle destination and changes
    the user's requested route.

    Transit classification does not add calendar days here.
    """

    cleaned = list(
        dict.fromkeys(
            str(destination_id)
            for destination_id in destination_ids
            if destination_id
        )
    )

    if not cleaned:
        return [], []

    if total_days <= 0:
        return cleaned, [
            "Trip duration is not valid; destination feasibility "
            "cannot be evaluated."
        ]

    full_required = minimum_days_required_for_route(
        destination_order=cleaned,
        meta=meta,
        route_analysis=route_analysis,
    )

    if full_required <= total_days:
        return cleaned, []

    warnings: list[str] = [
        (
            f"Requested destination sequence requires at least "
            f"{full_required} destination days, but the trip contains "
            f"only {total_days} calendar days. The route will be reduced "
            "without reordering or skipping middle destinations."
        )
    ]

    feasible_prefix: list[str] = []

    for destination_id in cleaned:

        candidate = (
            feasible_prefix
            + [destination_id]
        )

        required_days = (
            minimum_days_required_for_route(
                destination_order=candidate,
                meta=meta,
                route_analysis=None,
            )
        )

        if required_days <= total_days:
            feasible_prefix = candidate
            continue

        break

    if not feasible_prefix:
        first_destination = cleaned[0]

        first_minimum = destination_min_nights(
            first_destination,
            meta,
        )

        if first_minimum > total_days:
            label = (
                meta
                .get(
                    first_destination,
                    {},
                )
                .get(
                    "headline_label",
                    first_destination,
                )
            )

            raise ValueError(
                f"Destination '{label}' requires at least "
                f"{first_minimum} days but the requested trip contains "
                f"only {total_days} days."
            )

        feasible_prefix = [
            first_destination
        ]

    removed = [
        destination_id
        for destination_id in cleaned
        if destination_id not in feasible_prefix
    ]

    if removed:
        warnings.append(
            "The following destinations were removed because the "
            f"requested {total_days}-day trip could not accommodate "
            f"their minimum stays: {', '.join(removed)}."
        )

    return feasible_prefix, warnings


# ============================================================================
# DAY ALLOCATION
# ============================================================================

def allocate_days_for_route(
    *,
    destination_ids: list[str],
    meta: dict[str, dict[str, Any]],
    total_days: int,
    travel_style: list[str],
    route_analysis: RouteAnalysis | None = None,
) -> tuple[list[int], list[str]]:
    """
    Allocate EXACTLY total_days across the selected destinations.

    Invariant:

        sum(allocation) == total_days

    Transit days are contained inside this allocation.

    They are NOT subtracted from total_days.

    Example:

        7-day trip
        destinations = [A, B, C]
        minimums = [2, 2, 2]

        initial = [2, 2, 2]
        remaining = 1

        final = [3, 2, 2]

    If the B arrival is a measured 7-hour route, one of B's allocated
    days may later be marked TRANSIT. The trip still has exactly 7 days.
    """

    destination_ids = list(
        destination_ids
    )

    n = len(
        destination_ids
    )

    if n == 0:
        return [], []

    if total_days <= 0:
        raise ValueError(
            "total_days must be greater than zero."
        )

    minimums = [
        destination_min_nights(
            destination_id,
            meta,
        )
        for destination_id in destination_ids
    ]

    minimum_required = sum(
        minimums
    )

    warnings: list[str] = []

    if minimum_required > total_days:
        raise ValueError(
            "Destination allocation is infeasible: minimum destination "
            f"stay requires {minimum_required} days but only "
            f"{total_days} days were requested."
        )

    allocation = list(
        minimums
    )

    remaining = (
        total_days
        - minimum_required
    )

    # ------------------------------------------------------------
    # Distribute extra days deterministically.
    #
    # The current policy is balanced round-robin allocation.
    # This preserves route order and avoids arbitrary destination
    # preference.
    # ------------------------------------------------------------

    index = 0

    while remaining > 0:
        allocation[
            index % n
        ] += 1

        remaining -= 1
        index += 1

    # ------------------------------------------------------------
    # Optional border-buffer redistribution.
    #
    # This moves an already-allocated day from a destination with
    # available slack to the destination after an international
    # boundary.
    #
    # It never changes the total calendar days.
    # ------------------------------------------------------------

    if BORDER_BUFFER_NIGHTS > 0:

        for i in range(
            1,
            n,
        ):
            previous_destination = (
                destination_ids[i - 1]
            )

            current_destination = (
                destination_ids[i]
            )

            previous_country = (
                meta
                .get(
                    previous_destination,
                    {},
                )
                .get("country")
            )

            current_country = (
                meta
                .get(
                    current_destination,
                    {},
                )
                .get("country")
            )

            if not (
                previous_country
                and current_country
                and previous_country
                != current_country
            ):
                continue

            donor_candidates: list[
                tuple[int, int]
            ] = []

            for donor_index in range(n):

                slack = (
                    allocation[donor_index]
                    - minimums[donor_index]
                )

                if slack >= BORDER_BUFFER_NIGHTS:
                    donor_candidates.append(
                        (
                            slack,
                            donor_index,
                        )
                    )

            if not donor_candidates:
                label = (
                    meta
                    .get(
                        current_destination,
                        {},
                    )
                    .get(
                        "headline_label",
                        current_destination,
                    )
                )

                warnings.append(
                    f"Could not move a border-buffer day toward "
                    f"{label} without reducing another destination "
                    "below its minimum stay."
                )

                continue

            _, donor_index = max(
                donor_candidates,
                key=lambda item: item[0],
            )

            # Never take a day from the same destination.
            if donor_index == i:
                continue

            allocation[
                donor_index
            ] -= BORDER_BUFFER_NIGHTS

            allocation[
                i
            ] += BORDER_BUFFER_NIGHTS

    # ------------------------------------------------------------
    # HARD INVARIANTS
    # ------------------------------------------------------------

    if len(allocation) != n:
        raise ValueError(
            "Destination allocation length does not match destination "
            f"count: {len(allocation)} != {n}"
        )

    if any(
        value < 1
        for value in allocation
    ):
        raise ValueError(
            "Destination allocation contains a value below one day: "
            f"{allocation}"
        )

    if sum(allocation) != total_days:
        raise ValueError(
            "Destination allocation invariant violated: "
            f"{sum(allocation)} != requested {total_days} days."
        )

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
        "transfer": False,
        "travel_hours": 0.0,
        "travel_distance_km": 0.0,
        "crosses_country": False,
        "border_crossing": False,
        "_route_leg": None,
    }

    if leg is None:
        record["transfer"] = bool(
            is_arrival_day
        )

        return record

    duration_available = (
        leg.duration_minutes is not None
    )

    travel_hours = minutes_to_hours(
        leg.duration_minutes
    )

    crosses_country = bool(
        leg.is_inter_country
    )

    requires_transit_day = (
        route_transition_consumes_day(
            leg
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
            "is_destination_transition": True,
            "route_duration_available": duration_available,
            "route_unavailable": not duration_available,
            "requires_transit_day": requires_transit_day,
            "_route_leg": leg,
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
    activity_counts_by_day: Mapping[
        int,
        int,
    ] | None = None,
) -> list[dict[str, Any]]:
    """
    Expand destination allocation into exactly one record per calendar day.

    Example:

        destination_order = [A, B, C]
        allocation = [3, 2, 2]
        total_days = 7

    Produces:

        Day 1 A
        Day 2 A
        Day 3 A
        Day 4 B
        Day 5 B
        Day 6 C
        Day 7 C

    Route information is attached to the first day of arrival at B/C.

    A TRANSIT classification therefore lives on an existing calendar day;
    it never creates an eighth day.
    """

    if len(
        destination_order
    ) != len(
        nights_per_destination
    ):
        raise ValueError(
            "destination_order and nights_per_destination must be "
            "the same length "
            f"({len(destination_order)} != "
            f"{len(nights_per_destination)})."
        )

    if total_days <= 0:
        raise ValueError(
            "total_days must be greater than zero."
        )

    if any(
        value < 1
        for value in nights_per_destination
    ):
        raise ValueError(
            "Every selected destination must receive at least one "
            f"calendar day: {nights_per_destination}"
        )

    allocation_total = sum(
        nights_per_destination
    )

    if allocation_total != total_days:
        raise ValueError(
            "Destination allocation does not equal requested trip "
            f"duration: {allocation_total} != {total_days}"
        )

    activity_counts = (
        activity_counts_by_day
        or {}
    )

    leg_by_arrival_destination: dict[
        str,
        RouteLeg,
    ] = {
        leg.to_stop.destination_id: leg
        for leg in route_analysis.legs
    }

    destination_types: dict[
        str,
        str | None,
    ] = {
        stop.destination_id: stop.destination_type
        for stop in route_analysis.stops
    }

    records: list[
        dict[str, Any]
    ] = []

    day_number = 0

    for destination_index, destination_id in enumerate(
        destination_order
    ):

        nights_here = (
            nights_per_destination[
                destination_index
            ]
        )

        for night_index in range(
            nights_here
        ):

            day_number += 1

            is_first_day = (
                day_number == 1
            )

            is_last_day = (
                day_number == total_days
            )

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

            records.append(
                day_record_from_route_leg(
                    day_number=day_number,
                    total_days=total_days,
                    is_first_day=is_first_day,
                    is_last_day=is_last_day,
                    is_arrival_day=is_arrival_day,
                    leg=leg,
                    activity_count=(
                        activity_counts.get(
                            day_number,
                            0,
                        )
                    ),
                    destination_type=(
                        destination_types.get(
                            destination_id
                        )
                    ),
                )
            )

    if len(records) != total_days:
        raise ValueError(
            "Generated day records do not match total_days: "
            f"{len(records)} != {total_days}"
        )

    return records


# ============================================================================
# TRANSIT-DAY EXTRACTION
# ============================================================================

def transit_days_from_day_records(
    day_records: list[
        Mapping[str, Any]
    ],
) -> dict[int, bool]:

    return {
        int(
            record["day_number"]
        ): bool(
            record.get(
                "requires_transit_day",
                False,
            )
        )
        for record in day_records
        if record.get(
            "day_number"
        ) is not None
    }


_ARCHETYPES_WITHOUT_OVERNIGHT_REQUIREMENT = frozenset(
    {
        "departure",
    }
)


def overnight_required_from_day_plan(
    day_plan,
) -> dict[int, bool]:

    return {
        day.day_number: (
            day.archetype.value
            not in _ARCHETYPES_WITHOUT_OVERNIGHT_REQUIREMENT
        )
        for day in day_plan.days
    }


_ARCHETYPES_REQUIRING_TRANSIT_DAY = frozenset(
    {
        "long_transfer",
        "transfer",
        "overnight_transition",
    }
)


def transit_days_from_day_plan(
    day_plan,
    day_records: list[
        Mapping[str, Any]
    ] | None = None,
) -> dict[int, bool]:
    """
    Route-aware transit decisions are authoritative whenever day_records
    are available.

    Archetypes are only a compatibility fallback.
    """

    result: dict[
        int,
        bool,
    ] = {}

    if day_records is not None:
        result.update(
            transit_days_from_day_records(
                day_records
            )
        )

    for day in day_plan.days:

        day_number = (
            day.day_number
        )

        if (
            day_records is not None
            and day_number in result
        ):
            continue

        result[
            day_number
        ] = (
            day.archetype.value
            in _ARCHETYPES_REQUIRING_TRANSIT_DAY
        )

    return result


# ============================================================================
# ACTIVITY CONSTRAINTS ADAPTER
# ============================================================================

def activity_record_from_drawer(
    drawer: Any,
) -> dict[str, Any]:

    record: dict[
        str,
        Any,
    ] = {
        "id": (
            drawer.activity_id
            or drawer.id
        ),
        "name": drawer.name,
    }

    if drawer.duration_minutes is not None:
        record[
            "duration_minutes"
        ] = drawer.duration_minutes

    record[
        "fixed_time"
    ] = drawer.activity_type in {
        "ARRIVAL",
        "DEPARTURE",
        "TRANSFER",
        "MEAL",
    }

    if drawer.start_time is not None:
        record[
            "start_time"
        ] = drawer.start_time.strftime(
            "%H:%M"
        )

    record[
        "_drawer_id"
    ] = drawer.id

    record[
        "_is_fallback"
    ] = bool(
        getattr(
            drawer,
            "is_fallback",
            False,
        )
    )

    return record


def schedule_record_from_shelf(
    shelf: Any,
) -> dict[str, Any]:

    activities = [
        activity_record_from_drawer(
            drawer
        )
        for drawer in shelf.drawers
        if drawer.activity_type
        == "EXPERIENCE"
    ]

    return {
        "day_number": shelf.day_number,
        "activities": activities,
    }


def schedule_input_from_cabinet(
    cabinet: Any,
) -> list[
    dict[str, Any]
]:

    return [
        schedule_record_from_shelf(
            shelf
        )
        for shelf in cabinet.shelves
    ]


# ============================================================================
# ARCHETYPE LOOKUP
# ============================================================================

def archetypes_by_day_number(
    day_plan,
) -> dict[int, Any]:

    return {
        day.day_number: day.archetype
        for day in day_plan.days
    }


__all__ = [
    "minutes_to_hours",
    "hours_to_minutes",
    "destination_min_nights",
    "route_transition_consumes_day",
    "route_transition_days",
    "minimum_days_required_for_route",
    "find_feasible_destination_order",
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
