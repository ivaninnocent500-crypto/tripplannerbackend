"""
Pipeline Adapters
=================

Translation layer between persistence-facing engines and the pure,
DB-free planning engines.

Responsibilities
----------------
1. Convert route facts into planning records.
2. Allocate days across destinations.
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

Feasibility is also separate from transit classification.

A trip can contain a destination transition without that transition
consuming an entire calendar day.

However, a destination should not receive an artificially tiny stay
just to make the arithmetic equal the requested number of days.

No ORM records are created here.
"""

from __future__ import annotations

import logging
from itertools import combinations
from typing import Any, Mapping

from app.engines.route_geography import RouteAnalysis, RouteLeg

logger = logging.getLogger(__name__)


# ============================================================================
# CONSTANTS
# ============================================================================

# A measured route consuming approximately half a day or more becomes
# a dedicated TRANSIT day.
TRANSIT_TRAVEL_THRESHOLD_HOURS = 6.0

# Used only when a destination has no meaningful minimum-stay metadata.
#
# This is intentionally conservative. It prevents a destination from
# receiving a meaningless one-day visit simply because its database
# minimum is missing.
DEFAULT_MIN_NIGHTS = 2

BORDER_BUFFER_NIGHTS = 1


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
# DESTINATION FEASIBILITY
# ============================================================================

def destination_min_nights(
    destination_id: str,
    meta: dict[str, dict[str, Any]],
) -> int:
    """
    Return the minimum meaningful stay for a destination.

    The database value is authoritative when present.

    If the metadata does not contain a valid minimum, use a conservative
    default rather than allowing the destination to receive a meaningless
    one-night allocation.
    """

    raw_value = meta.get(
        destination_id,
        {},
    ).get("min_nights")

    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = DEFAULT_MIN_NIGHTS

    return max(1, value)


def route_transition_consumes_day(
    leg: RouteLeg | None,
) -> bool:
    """
    Decide whether a route leg itself consumes a dedicated calendar day.

    This is deliberately stricter than merely checking whether a
    destination changes.

    Rules:

        cross-country -> yes

        measured travel >= 6h -> yes

        unknown same-country route -> no

        short same-country route -> no
    """

    if leg is None:
        return False

    if bool(leg.is_inter_country):
        return True

    if leg.duration_minutes is None:
        return False

    return (
        minutes_to_hours(leg.duration_minutes)
        >= TRANSIT_TRAVEL_THRESHOLD_HOURS
    )


def route_transition_days(
    *,
    route_analysis: RouteAnalysis,
    destination_order: list[str],
) -> int:
    """
    Count route transitions that consume dedicated calendar days.

    A normal short same-country movement does not consume a dedicated
    calendar day.

    This prevents:

        Tarangire -> Zanzibar

    from automatically consuming a full travel day merely because
    the destination changes.

    Conversely:

        Ngorongoro -> Pyramids

    is counted when RouteGeographyEngine identifies it as an
    inter-country transition.
    """

    if len(destination_order) <= 1:
        return 0

    legs_by_destination: dict[str, RouteLeg] = {
        leg.to_stop.destination_id: leg
        for leg in route_analysis.legs
    }

    transition_days = 0

    for destination_id in destination_order[1:]:
        leg = legs_by_destination.get(destination_id)

        if route_transition_consumes_day(leg):
            transition_days += 1

    return transition_days


def minimum_days_required_for_route(
    *,
    destination_order: list[str],
    meta: dict[str, dict[str, Any]],
    route_analysis: RouteAnalysis | None = None,
) -> int:
    """
    Calculate the minimum coherent trip duration for a destination route.

    The calculation is:

        destination minimum stays
        +
        dedicated long/international transition days

    A normal short destination transfer does NOT automatically add
    a full day.

    Example:

        Tarangire min 2
        Zanzibar min 2
        Lalibela min 2
        Tsingy min 2

        = 8 minimum destination days

    If an additional route transition consumes a dedicated day, that
    day is added separately.

    The function never invents a route duration.
    """

    if not destination_order:
        return 0

    destination_days = sum(
        destination_min_nights(
            destination_id,
            meta,
        )
        for destination_id in destination_order
    )

    transition_days = 0

    if route_analysis is not None:
        transition_days = route_transition_days(
            route_analysis=route_analysis,
            destination_order=destination_order,
        )

    return destination_days + transition_days


def _route_subset_score(
    subset: tuple[str, ...],
    *,
    original_order: list[str],
    meta: dict[str, dict[str, Any]],
) -> tuple[int, int, int]:
    """
    Score a feasible destination subset.

    Higher is better.

    Priority:

        1. More destinations retained.
        2. More total minimum-stay nights retained.
        3. Preserve earlier destinations in the user's requested order.

    This is intentionally deterministic.
    """

    retained_count = len(subset)

    retained_min_nights = sum(
        destination_min_nights(
            destination_id,
            meta,
        )
        for destination_id in subset
    )

    original_positions = [
        original_order.index(destination_id)
        for destination_id in subset
    ]

    # Earlier requested destinations receive a slightly stronger score.
    position_score = sum(
        len(original_order) - position
        for position in original_positions
    )

    return (
        retained_count,
        retained_min_nights,
        position_score,
    )


def find_feasible_destination_order(
    *,
    destination_ids: list[str],
    meta: dict[str, dict[str, Any]],
    total_days: int,
    route_analysis: RouteAnalysis | None = None,
) -> tuple[list[str], list[str]]:
    """
    Find the largest coherent destination subset that fits the requested
    number of days.

    IMPORTANT
    ---------
    This function does NOT blindly squeeze every requested destination
    into the trip.

    If four destinations cannot coherently fit into seven days, it keeps
    the largest feasible subset instead of generating meaningless
    one-day stays everywhere.

    The original destination order is preserved.

    Returns:

        feasible_destination_order
        warnings
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

    minimum_required = minimum_days_required_for_route(
        destination_order=cleaned,
        meta=meta,
        route_analysis=route_analysis,
    )

    if minimum_required <= total_days:
        return cleaned, []

    warnings: list[str] = [
        (
            f"Requested route requires at least {minimum_required} "
            f"coherent days, but the trip contains only {total_days} days. "
            "The destination sequence must be reduced."
        )
    ]

    # Search largest feasible subset.
    #
    # The number of requested destinations is normally small, so this
    # deterministic subset search is preferable to arbitrary removal.
    best_subset: tuple[str, ...] | None = None
    best_score: tuple[int, int, int] | None = None

    for subset_size in range(
        min(len(cleaned), total_days),
        0,
        -1,
    ):
        feasible_subsets: list[
            tuple[str, ...]
        ] = []

        for indexes in combinations(
            range(len(cleaned)),
            subset_size,
        ):
            subset = tuple(
                cleaned[index]
                for index in indexes
            )

            required_days = minimum_days_required_for_route(
                destination_order=list(subset),
                meta=meta,
                route_analysis=route_analysis,
            )

            if required_days <= total_days:
                feasible_subsets.append(subset)

        if feasible_subsets:
            best_subset = max(
                feasible_subsets,
                key=lambda subset: _route_subset_score(
                    subset,
                    original_order=cleaned,
                    meta=meta,
                ),
            )

            break

    if best_subset is None:
        # At least one destination should always fit because its minimum
        # stay is clamped to >= 1.
        best_subset = (
            cleaned[0],
        )

    removed = [
        destination_id
        for destination_id in cleaned
        if destination_id not in best_subset
    ]

    if removed:
        warnings.append(
            "The following destinations were removed from the generated "
            f"route because they could not fit coherently into "
            f"{total_days} days: {', '.join(removed)}."
        )

    return list(best_subset), warnings


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
    Allocate calendar days across a destination route.

    The allocation starts from each destination's meaningful minimum stay.

    Remaining days are distributed across destinations.

    Dedicated transition days are reserved where RouteGeographyEngine
    provides an actual cross-country or >=6-hour route.

    This function assumes destination feasibility has already been checked.
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
            "destinations."
        ]

    minimums = [
        destination_min_nights(
            destination_id,
            meta,
        )
        for destination_id in destination_ids
    ]

    transition_days_by_index = [0] * n

    if route_analysis is not None:
        legs_by_destination: dict[str, RouteLeg] = {
            leg.to_stop.destination_id: leg
            for leg in route_analysis.legs
        }

        for index in range(1, n):
            destination_id = destination_ids[index]

            leg = legs_by_destination.get(
                destination_id
            )

            if route_transition_consumes_day(leg):
                transition_days_by_index[index] = 1

    warnings: list[str] = []

    reserved_transition_days = sum(
        transition_days_by_index
    )

    destination_day_budget = (
        total_days - reserved_transition_days
    )

    required_destination_days = sum(
        minimums
    )

    if destination_day_budget < required_destination_days:
        warnings.append(
            "Destination minimum stays plus required route transition "
            "days exceed the requested trip duration."
        )

        # Defensive fallback. Feasibility should normally have removed
        # excess destinations before this point.
        allocation = minimums[:]

        while (
            sum(allocation)
            + reserved_transition_days
            < total_days
        ):
            allocation[-1] += 1

        return allocation, warnings

    allocation = minimums[:]

    remaining = (
        destination_day_budget
        - required_destination_days
    )

    # Distribute remaining days according to a relaxed travel style.
    # For now the allocation remains deterministic and balanced.
    index = 0

    while remaining > 0:
        allocation[index % n] += 1
        remaining -= 1
        index += 1

    # Border-buffer logic.
    for i in range(1, n):
        previous_destination = destination_ids[i - 1]
        current_destination = destination_ids[i]

        previous_country = meta.get(
            previous_destination,
            {},
        ).get("country")

        current_country = meta.get(
            current_destination,
            {},
        ).get("country")

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
            minimum = minimums[donor_index]

            slack = (
                allocation[donor_index]
                - minimum
            )

            if slack >= BORDER_BUFFER_NIGHTS:
                donor_candidates.append(
                    (
                        slack,
                        donor_index,
                    )
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

    # Final invariant.
    expected_destination_days = (
        total_days - reserved_transition_days
    )

    if sum(allocation) != expected_destination_days:
        logger.error(
            "Destination allocation invariant violated: "
            "%s != %s",
            sum(allocation),
            expected_destination_days,
        )

        difference = (
            expected_destination_days
            - sum(allocation)
        )

        allocation[-1] += difference

    if any(value < 1 for value in allocation):
        logger.error(
            "Invalid destination allocation generated: %s",
            allocation,
        )

        allocation = [
            max(1, value)
            for value in allocation
        ]

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

        requires_transit_day = (
            crosses_country
            or (
                duration_available
                and travel_hours
                >= TRANSIT_TRAVEL_THRESHOLD_HOURS
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

    if len(destination_order) != len(nights_per_destination):
        raise ValueError(
            "destination_order and nights_per_destination must be "
            "the same length "
            f"({len(destination_order)} != {len(nights_per_destination)})."
        )

    activity_counts = activity_counts_by_day or {}

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

            records.append(
                day_record_from_route_leg(
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
    day_records: list[Mapping[str, Any]],
) -> dict[int, bool]:

    return {
        int(record["day_number"]): bool(
            record.get(
                "requires_transit_day",
                False,
            )
        )
        for record in day_records
        if record.get("day_number") is not None
    }


_ARCHETYPES_WITHOUT_OVERNIGHT_REQUIREMENT = frozenset(
    {"departure"}
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
    day_records: list[Mapping[str, Any]] | None = None,
) -> dict[int, bool]:
    """
    Route-aware transit decisions are authoritative whenever day_records
    are available.

    DayArchetype is only a compatibility fallback when route-aware
    records are unavailable.
    """

    result: dict[int, bool] = {}

    if day_records is not None:
        result.update(
            transit_days_from_day_records(
                day_records
            )
        )

    for day in day_plan.days:
        day_number = day.day_number

        if (
            day_records is not None
            and day_number in result
        ):
            continue

        result[day_number] = (
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
