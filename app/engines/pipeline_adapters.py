"""
Pipeline Adapters
=================

Translation layer between persistence-facing engines and the pure,
DB-free planning engines.

This module is intentionally deterministic.

Responsibilities
----------------
1. Translate RouteGeographyEngine facts into planning records.
2. Validate that the requested route and analyzed route remain identical
   in order.
3. Determine whether the requested destination sequence can fit the
   requested calendar duration using authoritative stay metadata.
4. Allocate EXACTLY the requested number of calendar days.
5. Classify route transitions as TRANSIT without creating additional
   calendar days.
6. Preserve factual route-leg information for downstream engines.
7. Translate persisted furniture into schedule-engine records.

Architecture rules
------------------

A destination transition and a TRANSIT day are different concepts.

Examples:

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

Unknown duration is NEVER converted into a guessed route duration.

A TRANSIT day is an existing calendar day inside the requested trip
duration. It is never added on top of the requested duration.

Therefore:

    requested_days == sum(destination_day_allocation)

must always hold.

This module does NOT:
- invent route durations;
- reorder destinations;
- optimize the user's route;
- silently replace destinations;
- create additional transit days;
- treat country changes as an excuse to fabricate travel times;
- decide which activities belong to a destination;
- repair schedules.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from app.engines.route_geography import RouteAnalysis, RouteLeg

logger = logging.getLogger(__name__)


# ============================================================================
# CONSTANTS
# ============================================================================

# A measured same-country transfer at or above this duration is treated as
# a dedicated transit day.
TRANSIT_TRAVEL_THRESHOLD_HOURS = 6.0

# Used only when destination minimum-stay metadata is absent or invalid.
#
# This is a stay-planning default, NOT a transport-duration default.
DEFAULT_MIN_NIGHTS = 2


# ============================================================================
# UNIT CONVERSION
# ============================================================================

def minutes_to_hours(
    minutes: int | None,
) -> float:
    """
    Convert known minutes to hours.

    None remains semantically unavailable.

    DayArchetypeEngine currently accepts a numeric travel_hours value.
    Therefore unavailable duration is represented as 0.0 here while
    route_duration_available remains available to preserve the actual
    meaning.
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
    """
    Convert hours to whole minutes.

    None remains unavailable.
    """

    if hours is None:
        return None

    return int(
        round(
            hours * 60.0
        )
    )


# ============================================================================
# DESTINATION SEQUENCE NORMALIZATION
# ============================================================================

def normalize_destination_order(
    destination_ids: Sequence[str | None],
) -> list[str]:
    """
    Normalize destination IDs without changing intentional route order.

    Only:
        - null/empty values are removed;
        - immediately repeated destinations are collapsed.

    IMPORTANT:

    We do NOT use dict.fromkeys() here.

    A route such as:

        A -> B -> A

    is a meaningful user route and must remain:

        A -> B -> A

    until a separate route-integrity policy explicitly evaluates it.

    This function therefore performs normalization, not route optimization.
    """

    normalized: list[str] = []

    for raw_id in destination_ids:

        if raw_id is None:
            continue

        destination_id = str(
            raw_id
        ).strip()

        if not destination_id:
            continue

        if (
            normalized
            and normalized[-1] == destination_id
        ):
            continue

        normalized.append(
            destination_id
        )

    return normalized


# ============================================================================
# DESTINATION METADATA
# ============================================================================

def destination_min_nights(
    destination_id: str,
    meta: Mapping[str, Mapping[str, Any]],
) -> int:
    """
    Return the minimum meaningful stay for a destination.

    Database metadata is authoritative when available.

    Missing or invalid metadata receives the conservative planning
    default.

    This is a stay-duration fallback only. It must never be interpreted
    as a transport or route-duration fallback.
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
        value = int(
            raw_value
        )
    except (
        TypeError,
        ValueError,
    ):
        value = DEFAULT_MIN_NIGHTS

    return max(
        1,
        value,
    )


# ============================================================================
# ROUTE-LEG LOOKUP
# ============================================================================

def _leg_for_transition(
    *,
    route_analysis: RouteAnalysis,
    from_destination_id: str,
    to_destination_id: str,
    transition_index: int,
) -> RouteLeg | None:
    """
    Resolve the RouteLeg representing one specific ordered transition.

    The preferred lookup is by the exact from/to pair.

    Positional fallback is allowed only when the RouteGeographyEngine
    produced the expected ordered leg list.

    We intentionally do not index solely by destination ID because a
    legitimate route may revisit a destination:

        A -> B -> A

    and destination-ID-only lookup would overwrite one of those legs.
    """

    for leg in route_analysis.legs:

        try:
            from_id = (
                leg.from_stop.destination_id
            )
            to_id = (
                leg.to_stop.destination_id
            )
        except AttributeError:
            continue

        if (
            from_id == from_destination_id
            and to_id == to_destination_id
        ):
            return leg

    # Safe positional fallback only when the route-analysis leg count
    # corresponds to the number of transitions.
    if (
        len(route_analysis.legs)
        == max(
            0,
            len(route_analysis.stops) - 1,
        )
        and 0 <= transition_index < len(route_analysis.legs)
    ):
        candidate = route_analysis.legs[
            transition_index
        ]

        try:
            if (
                candidate.from_stop.destination_id
                == from_destination_id
                and candidate.to_stop.destination_id
                == to_destination_id
            ):
                return candidate
        except AttributeError:
            return None

    return None


# ============================================================================
# ROUTE ANALYSIS INTEGRITY
# ============================================================================

def validate_route_analysis_order(
    *,
    route_analysis: RouteAnalysis,
    destination_order: Sequence[str],
) -> list[str]:
    """
    Validate that RouteGeographyEngine analyzed the same ordered route
    supplied to it.

    Returns warnings instead of silently repairing the route.

    The caller/orchestrator decides whether a warning is acceptable or
    whether generation should fail.
    """

    expected = normalize_destination_order(
        destination_order
    )

    actual = [
        str(
            stop.destination_id
        ).strip()
        for stop in route_analysis.stops
        if getattr(
            stop,
            "destination_id",
            None,
        )
    ]

    warnings: list[str] = []

    if actual != expected:
        warnings.append(
            "Route analysis does not match the requested destination "
            f"order. Requested={expected}; analyzed={actual}."
        )

    expected_leg_count = max(
        0,
        len(expected) - 1,
    )

    if len(route_analysis.legs) != expected_leg_count:
        warnings.append(
            "Route analysis contains an unexpected number of route legs: "
            f"expected {expected_leg_count}, "
            f"received {len(route_analysis.legs)}."
        )

    for index in range(
        expected_leg_count
    ):

        from_id = expected[index]
        to_id = expected[index + 1]

        leg = _leg_for_transition(
            route_analysis=route_analysis,
            from_destination_id=from_id,
            to_destination_id=to_id,
            transition_index=index,
        )

        if leg is None:
            warnings.append(
                "No route leg was found for ordered transition "
                f"{from_id} -> {to_id}."
            )

    return warnings


# ============================================================================
# TRANSIT CLASSIFICATION
# ============================================================================

def route_transition_consumes_day(
    leg: RouteLeg | None,
) -> bool:
    """
    Determine whether a route transition qualifies as a TRANSIT day.

    Rules:

        cross-country
            -> TRANSIT

        measured same-country duration >= 6 hours
            -> TRANSIT

        unknown same-country duration
            -> NOT automatically TRANSIT

        measured same-country duration < 6 hours
            -> NOT TRANSIT

    No duration is fabricated here.
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
    Count route transitions that qualify as TRANSIT.

    This is classification only.

    It NEVER adds days to the user's requested duration.
    """

    ordered = normalize_destination_order(
        destination_order
    )

    if len(ordered) <= 1:
        return 0

    transition_days = 0

    for index in range(
        1,
        len(ordered),
    ):

        leg = _leg_for_transition(
            route_analysis=route_analysis,
            from_destination_id=ordered[index - 1],
            to_destination_id=ordered[index],
            transition_index=index - 1,
        )

        if route_transition_consumes_day(
            leg
        ):
            transition_days += 1

    return transition_days


# ============================================================================
# ROUTE FEASIBILITY
# ============================================================================

def minimum_days_required_for_route(
    *,
    destination_order: list[str],
    meta: Mapping[str, Mapping[str, Any]],
    route_analysis: RouteAnalysis | None = None,
) -> int:
    """
    Calculate the minimum calendar days required by destination stay
    metadata.

    Route analysis is deliberately NOT converted into extra days.

    A route transition is performed on an existing calendar day assigned
    to the arriving destination.

    Therefore:

        A = 2 days
        B = 2 days
        A -> B = TRANSIT

    still requires:

        4 calendar days

    rather than 5.

    IMPORTANT:

    This function does not claim that every geographically complex route
    is logistically feasible. It only answers the narrow question:

        "Do the destination minimum stays fit inside the requested
         calendar duration?"

    Geographic feasibility remains a separate responsibility.
    """

    del route_analysis

    if not destination_order:
        return 0

    return sum(
        destination_min_nights(
            destination_id,
            meta,
        )
        for destination_id in destination_order
    )


def _route_has_known_hard_problem(
    *,
    route_analysis: RouteAnalysis | None,
    destination_order: Sequence[str],
) -> bool:
    """
    Detect only hard route-analysis failures explicitly represented by
    the geography layer.

    Unknown transport duration is NOT a hard failure.

    Missing road data is NOT a hard failure.

    Coordinate estimates being disabled is NOT a hard failure.

    The geography layer must be allowed to say "unknown" honestly.
    """

    if route_analysis is None:
        return False

    ordered = normalize_destination_order(
        destination_order
    )

    for index in range(
        1,
        len(ordered),
    ):

        leg = _leg_for_transition(
            route_analysis=route_analysis,
            from_destination_id=ordered[index - 1],
            to_destination_id=ordered[index],
            transition_index=index - 1,
        )

        if leg is None:
            continue

        if bool(
            getattr(
                leg,
                "route_unavailable",
                False,
            )
        ):
            return True

    return False


def find_feasible_destination_order(
    *,
    destination_ids: list[str],
    meta: Mapping[str, Mapping[str, Any]],
    total_days: int,
    route_analysis: RouteAnalysis | None = None,
) -> tuple[list[str], list[str]]:
    """
    Determine the largest feasible PREFIX while preserving the user's
    requested order.

    Example:

        A -> B -> C -> D

    If A+B+C fit but A+B+C+D does not:

        A -> B -> C

    is returned.

    We never silently construct:

        A -> C -> D

    because that changes the user's route.

    This function does not solve route optimization.

    It evaluates:
        1. destination stay requirements;
        2. destination order;
        3. explicit route-analysis hard failures.

    Unknown transport duration remains unknown.
    """

    cleaned = normalize_destination_order(
        destination_ids
    )

    if not cleaned:
        return [], []

    if total_days <= 0:
        return cleaned, [
            "Trip duration is not valid; destination feasibility "
            "cannot be evaluated."
        ]

    warnings = (
        validate_route_analysis_order(
            route_analysis=route_analysis,
            destination_order=cleaned,
        )
        if route_analysis is not None
        else []
    )

    full_required = minimum_days_required_for_route(
        destination_order=cleaned,
        meta=meta,
        route_analysis=route_analysis,
    )

    if full_required <= total_days:

        if _route_has_known_hard_problem(
            route_analysis=route_analysis,
            destination_order=cleaned,
        ):
            warnings.append(
                "The requested destination sequence contains an "
                "explicitly unavailable route transition."
            )

        return cleaned, warnings

    warnings.append(
        f"Requested destination sequence requires at least "
        f"{full_required} destination days, but the trip contains "
        f"only {total_days} calendar days. The route will be reduced "
        "without reordering or skipping middle destinations."
    )

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
    meta: Mapping[str, Mapping[str, Any]],
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

    Allocation policy:

        1. Start at each destination's minimum stay.
        2. Distribute remaining days deterministically in route order.
        3. Never reduce a destination below its minimum.
        4. Never reorder destinations.
        5. Never move days because of a heuristic border buffer.

    `travel_style` and `route_analysis` remain in the signature for
    compatibility with the orchestrator and future policies. Neither is
    allowed to override the deterministic stay allocation.
    """

    del travel_style
    del route_analysis

    destination_ids = normalize_destination_order(
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

    warnings: list[str] = []

    index = 0

    while remaining > 0:

        allocation[
            index % n
        ] += 1

        remaining -= 1
        index += 1

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

    if any(
        allocation[index] < minimums[index]
        for index in range(n)
    ):
        raise ValueError(
            "Destination allocation reduced a destination below its "
            f"minimum stay: allocation={allocation}, "
            f"minimums={minimums}"
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
    destination_id: str,
    leg: RouteLeg | None,
    activity_count: int,
    destination_type: str | None,
) -> dict[str, Any]:
    """
    Convert one destination-arrival route leg into a planning day record.

    IMPORTANT CONTRACT:

    Every day record contains `destination_id`.

    `itinerary_v2.py` uses destination identity to verify that the
    day-level route remains identical to the requested route.

    The route leg is attached only to the calendar day on which the
    traveler arrives at the new destination.

    Unknown route duration remains explicitly unavailable.
    """

    record: dict[str, Any] = {
        "day_number": day_number,

        # AUTHORITATIVE DAY DESTINATION.
        "destination_id": str(
            destination_id
        ),

        "arrival": is_first_day,
        "departure": is_last_day,
        "destination_type": destination_type,
        "activity_count": activity_count,

        # Transition identity.
        "is_destination_transition": False,
        "requires_transit_day": False,

        # Duration truth.
        "route_duration_available": False,
        "route_unavailable": False,
        "travel_hours": 0.0,

        # Distance truth.
        "travel_distance_km": None,

        # Country/border truth.
        "crosses_country": False,
        "border_crossing": False,

        # Planning compatibility.
        "transfer": bool(
            is_arrival_day
        ),

        # Internal authoritative fact.
        "_route_leg": None,
    }

    if leg is None:
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

    distance_km = getattr(
        leg,
        "distance_km",
        None,
    )

    # IMPORTANT:
    #
    # route_duration_available answers:
    # "Do we know the route duration?"
    #
    # route_unavailable answers:
    # "Did the geography engine explicitly mark this route as
    # unavailable?"
    #
    # These are NOT equivalent.
    #
    # duration_minutes=None means UNKNOWN, not unavailable.
    explicit_route_unavailable = bool(
        getattr(
            leg,
            "route_unavailable",
            False,
        )
    )

    record.update(
        {
            "transfer": True,
            "travel_hours": travel_hours,
            "travel_distance_km": (
                float(distance_km)
                if distance_km is not None
                else None
            ),
            "crosses_country": crosses_country,
            "border_crossing": bool(
                getattr(
                    leg,
                    "requires_border_crossing",
                    False,
                )
            ),
            "is_destination_transition": True,
            "route_duration_available": duration_available,
            "route_unavailable": explicit_route_unavailable,
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
    Expand the destination allocation into exactly one record per
    calendar day.

    Example:

        destination_order = [A, B, C]
        allocation = [3, 2, 2]
        total_days = 7

        Day 1 A
        Day 2 A
        Day 3 A
        Day 4 B <- A -> B route leg attached here
        Day 5 B
        Day 6 C <- B -> C route leg attached here
        Day 7 C

    TRANSIT is therefore a property of an existing day.

    No eighth day is ever created.

    IMPORTANT CONTRACT:

    Every generated record contains `destination_id`, including
    non-transition days.

    This is required because itinerary_v2.py validates destination
    sequence from the generated day records.
    """

    ordered = normalize_destination_order(
        destination_order
    )

    if len(ordered) != len(
        nights_per_destination
    ):
        raise ValueError(
            "destination_order and nights_per_destination must be "
            "the same length "
            f"({len(ordered)} != "
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

    route_warnings = validate_route_analysis_order(
        route_analysis=route_analysis,
        destination_order=ordered,
    )

    if route_warnings:
        raise ValueError(
            "Route analysis is inconsistent with the destination order: "
            + " | ".join(route_warnings)
        )

    activity_counts = (
        activity_counts_by_day
        or {}
    )

    destination_types: dict[
        str,
        str | None,
    ] = {
        str(
            stop.destination_id
        ): getattr(
            stop,
            "destination_type",
            None,
        )
        for stop in route_analysis.stops
    }

    records: list[
        dict[str, Any]
    ] = []

    day_number = 0

    for destination_index, destination_id in enumerate(
        ordered
    ):

        days_here = (
            nights_per_destination[
                destination_index
            ]
        )

        for local_day_index in range(
            days_here
        ):

            day_number += 1

            is_first_day = (
                day_number == 1
            )

            is_last_day = (
                day_number == total_days
            )

            is_arrival_day = (
                local_day_index == 0
                and destination_index > 0
            )

            leg: RouteLeg | None = None

            if is_arrival_day:

                previous_destination_id = (
                    ordered[
                        destination_index - 1
                    ]
                )

                leg = _leg_for_transition(
                    route_analysis=route_analysis,
                    from_destination_id=(
                        previous_destination_id
                    ),
                    to_destination_id=destination_id,
                    transition_index=(
                        destination_index - 1
                    ),
                )

                if leg is None:
                    raise ValueError(
                        "Missing route leg for destination transition "
                        f"{previous_destination_id} -> "
                        f"{destination_id}."
                    )

            records.append(
                day_record_from_route_leg(
                    day_number=day_number,
                    total_days=total_days,
                    is_first_day=is_first_day,
                    is_last_day=is_last_day,
                    is_arrival_day=is_arrival_day,
                    destination_id=destination_id,
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

    if (
        records[0]["day_number"] != 1
        or records[-1]["day_number"] != total_days
    ):
        raise ValueError(
            "Generated day records do not form a continuous calendar "
            f"sequence from 1 to {total_days}."
        )

    # Final destination-sequence invariant.
    #
    # This catches adapter corruption before itinerary_v2.py receives
    # the records.
    actual_destination_sequence = (
        normalize_destination_order(
            [
                record.get(
                    "destination_id"
                )
                for record in records
            ]
        )
    )

    if actual_destination_sequence != ordered:
        raise ValueError(
            "Generated day records changed destination order: "
            f"expected={ordered} "
            f"actual={actual_destination_sequence}"
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
    """
    Extract route-authoritative transit flags.

    Only explicit `requires_transit_day=True` values are considered
    transit days here.
    """

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


# ============================================================================
# OVERNIGHT REQUIREMENT
# ============================================================================

_ARCHETYPES_WITHOUT_OVERNIGHT_REQUIREMENT = frozenset(
    {
        "departure",
    }
)


def overnight_required_from_day_plan(
    day_plan,
) -> dict[int, bool]:
    """
    Determine whether accommodation is required overnight for each
    generated day.

    Departure is the current explicit exception.
    """

    return {
        day.day_number: (
            day.archetype.value
            not in _ARCHETYPES_WITHOUT_OVERNIGHT_REQUIREMENT
        )
        for day in day_plan.days
    }


# ============================================================================
# TRANSIT FALLBACK FROM ARCHETYPE
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
    day_records: list[
        Mapping[str, Any]
    ] | None = None,
) -> dict[int, bool]:
    """
    Return transit-day decisions.

    Route facts are authoritative whenever day_records are available.

    Archetype-based classification is only a compatibility fallback for
    days for which no route record exists.

    This prevents DayArchetypeEngine from overriding factual geography.
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
    """
    Convert a persisted Drawer into ScheduleRepairEngine input.

    Activity identity and fallback provenance are preserved.
    """

    activity_id = getattr(
        drawer,
        "activity_id",
        None,
    )

    drawer_id = getattr(
        drawer,
        "id",
        None,
    )

    record: dict[
        str,
        Any,
    ] = {
        "id": (
            activity_id
            or drawer_id
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
    ] = drawer_id

    record[
        "_is_fallback"
    ] = bool(
        getattr(
            drawer,
            "is_fallback",
            False,
        )
    )

    drawer_destination_id = getattr(
        drawer,
        "destination_id",
        None,
    )

    if drawer_destination_id is not None:
        record[
            "destination_id"
        ] = str(
            drawer_destination_id
        )

    return record


def schedule_record_from_shelf(
    shelf: Any,
) -> dict[str, Any]:
    """
    Convert one Shelf into schedule-repair input.

    Only EXPERIENCE activities enter the schedule repair layer.
    """

    activities = [
        activity_record_from_drawer(
            drawer
        )
        for drawer in shelf.drawers
        if drawer.activity_type
        == "EXPERIENCE"
    ]

    destination_id = getattr(
        shelf,
        "destination_id",
        None,
    )

    return {
        "day_number": shelf.day_number,
        "destination_id": (
            str(destination_id)
            if destination_id is not None
            else None
        ),
        "activities": activities,
    }


def schedule_input_from_cabinet(
    cabinet: Any,
) -> list[
    dict[str, Any]
]:
    """
    Convert the complete Cabinet into schedule-repair input.
    """

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
    """
    Build a deterministic day-number -> archetype mapping.
    """

    return {
        day.day_number: day.archetype
        for day in day_plan.days
    }


# ============================================================================
# PUBLIC API
# ============================================================================

__all__ = [
    "minutes_to_hours",
    "hours_to_minutes",
    "normalize_destination_order",
    "destination_min_nights",
    "validate_route_analysis_order",
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
