"""
Schedule Repair Engine
======================

Deterministic itinerary schedule validation and conservative repair.

Design principles
-----------------
1. Repair timing before moving activities.
2. Never move an activity between different destinations.
3. Never move route-transfer activities between days.
4. Never move fixed-time activities.
5. Preserve factual fixed game-drive times.
6. Never invent transport, destinations, or activities.
7. Never use schedule repair to compensate for a broken route.
8. If a conflict cannot be safely repaired, leave it in place and report it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Sequence

from app.engines.activity_constraints import (
    ActivityConstraintsEngine,
    ActivityIntensity,
    ActivityProfile,
    TimeWindow,
)
from app.engines.day_archetype import (
    DayArchetype,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scheduling constants
# ---------------------------------------------------------------------------

DEFAULT_DAY_START_MINUTES = 7 * 60
DEFAULT_DAY_END_MINUTES = 21 * 60

MIN_ACTIVITY_GAP_MINUTES = 15

ARRIVAL_DAY_MAX_ACTIVITY_HOURS = 5.0
DEPARTURE_DAY_MAX_ACTIVITY_HOURS = 4.0
TRANSFER_DAY_MAX_ACTIVITY_HOURS = 5.0
LONG_TRANSFER_MAX_ACTIVITY_HOURS = 3.0
RECOVERY_DAY_MAX_ACTIVITY_HOURS = 4.0
NORMAL_DAY_MAX_ACTIVITY_HOURS = 8.0
INTENSE_DAY_MAX_ACTIVITY_HOURS = 6.0

MAX_REPAIR_ITERATIONS = 100
MAX_SEARCH_ITERATIONS = 100


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RepairActionType(str, Enum):
    MOVE = "move"
    DELAY = "delay"
    ADVANCE = "advance"
    REMOVE = "remove"
    FLAG = "flag"
    RESIZE = "resize"


class ConflictType(str, Enum):
    OVERLAP = "overlap"
    OUTSIDE_OPENING_HOURS = "outside_opening_hours"
    TOO_EARLY = "too_early"
    TOO_LATE = "too_late"
    LATEST_FINISH = "latest_finish"
    DAY_OVERLOAD = "day_overload"
    INTENSITY_OVERLOAD = "intensity_overload"
    FIXED_TIME_CONFLICT = "fixed_time_conflict"
    TRAVEL_CONFLICT = "travel_conflict"
    INVALID_DURATION = "invalid_duration"
    UNSATISFIABLE = "unsatisfiable"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduledActivity:
    activity: ActivityProfile
    start_minutes: int
    end_minutes: int
    day_number: int
    fixed: bool = False
    priority: int = 50
    travel_before_minutes: int = 0
    travel_after_minutes: int = 0
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def duration_minutes(self) -> int:
        return self.end_minutes - self.start_minutes

    @property
    def occupied_start_minutes(self) -> int:
        return (
            self.start_minutes
            - self.travel_before_minutes
            - self.activity.preparation_minutes
        )

    @property
    def occupied_end_minutes(self) -> int:
        return (
            self.end_minutes
            + self.travel_after_minutes
            + self.activity.recovery_minutes
        )

    @property
    def destination_id(self) -> str | None:
        """
        Destination associated with this activity.

        Planner-generated records should always populate this.
        Missing destination identity is deliberately treated as unknown,
        not guessed.
        """
        value = self.raw.get("destination_id")

        if value is None:
            return None

        text = str(value).strip()

        return text or None

    @property
    def activity_type(self) -> str:
        value = (
            self.raw.get("activity_type")
            or self.raw.get("type")
            or self.raw.get("category")
            or ""
        )

        return str(value).strip().lower()

    @property
    def is_transfer_activity(self) -> bool:
        """
        Identify activities representing inter-destination movement.

        This is intentionally conservative. An activity named "transfer"
        alone is not enough unless its record also carries transfer-like
        semantic information.
        """

        activity_type = self.activity_type

        if activity_type in {
            "transfer",
            "transit",
            "transport",
            "inter_destination_transfer",
            "route_transfer",
            "long_transfer",
            "overnight_transition",
        }:
            return True

        text = " ".join(
            str(
                self.raw.get(key, "")
            ).strip().lower()
            for key in (
                "name",
                "description",
                "category",
                "activity_type",
            )
        )

        transfer_terms = (
            "transfer to",
            "transfer from",
            "inter-destination",
            "inter destination",
            "route transfer",
            "travel day",
            "travel to",
            "travel from",
            "drive to",
            "drive from",
            "flight to",
            "flight from",
        )

        return any(
            term in text
            for term in transfer_terms
        )


@dataclass(frozen=True)
class ScheduleConflict:
    conflict_type: ConflictType
    day_number: int
    activity_id: str
    severity: str
    message: str
    related_activity_id: str | None = None


@dataclass(frozen=True)
class RepairAction:
    action_type: RepairActionType
    activity_id: str
    from_day: int
    to_day: int
    from_start_minutes: int | None
    to_start_minutes: int | None
    reason: str
    confidence: float


@dataclass(frozen=True)
class RepairedDay:
    day_number: int
    activities: tuple[ScheduledActivity, ...]
    archetype: DayArchetype | None
    total_activity_hours: float
    total_intense_hours: float
    overloaded: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ScheduleRepairResult:
    days: tuple[RepairedDay, ...]
    conflicts_found: tuple[ScheduleConflict, ...]
    conflicts_remaining: tuple[ScheduleConflict, ...]
    actions: tuple[RepairAction, ...]
    repaired: bool
    fully_repaired: bool
    warnings: tuple[str, ...]
    iterations: int


# ---------------------------------------------------------------------------
# Safe conversion helpers
# ---------------------------------------------------------------------------


def _safe_int(
    value: Any,
    default: int = 0,
) -> int:
    if value is None:
        return default

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(
    value: Any,
) -> bool:
    if isinstance(value, bool):
        return value

    if value is None:
        return False

    if isinstance(value, str):
        return value.lower().strip() in {
            "true",
            "1",
            "yes",
            "fixed",
        }

    if isinstance(value, (int, float)):
        return value != 0

    return False


def _parse_time(
    value: Any,
) -> int | None:
    """
    Parse a time value into minutes after midnight.

    Unlike the previous implementation, this does not use `value or ...`,
    so valid zero values are preserved.
    """

    if value is None:
        return None

    if isinstance(value, (int, float)):
        numeric = int(value)

        if 0 <= numeric <= 24 * 60:
            return numeric

        if 0 <= numeric <= 24:
            return numeric * 60

        return None

    text = str(value).strip()

    if not text:
        return None

    # Support basic AM/PM values as well as HH:MM.
    upper = text.upper()

    is_pm = upper.endswith("PM")
    is_am = upper.endswith("AM")

    if is_pm or is_am:
        upper = upper[:-2].strip()

    parts = upper.split(":")

    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        return None

    if is_pm:
        if hour == 12:
            hour = 12
        else:
            hour += 12

    elif is_am and hour == 12:
        hour = 0

    if not 0 <= hour <= 23:
        return None

    if not 0 <= minute <= 59:
        return None

    return hour * 60 + minute


def _windows_allow(
    windows: Sequence[TimeWindow],
    start: int,
    end: int,
) -> bool:
    if not windows:
        return True

    return any(
        window.contains(start)
        and window.contains(end)
        for window in windows
    )


def _next_window_start(
    windows: Sequence[TimeWindow],
    start: int,
) -> int | None:
    if not windows:
        return start

    candidates: list[int] = []

    for window in windows:
        if start <= window.start_minutes:
            candidates.append(
                window.start_minutes
            )
        elif window.contains(start):
            candidates.append(start)

    if not candidates:
        return None

    return min(candidates)


def _activity_end(
    activity: ActivityProfile,
    start_minutes: int,
) -> int:
    return (
        start_minutes
        + int(
            round(
                activity.duration_hours * 60
            )
        )
    )


def _activity_capacity_for_archetype(
    archetype: DayArchetype | None,
) -> float:
    if archetype == DayArchetype.ARRIVAL:
        return ARRIVAL_DAY_MAX_ACTIVITY_HOURS

    if archetype == DayArchetype.DEPARTURE:
        return DEPARTURE_DAY_MAX_ACTIVITY_HOURS

    if archetype == DayArchetype.TRANSFER:
        return TRANSFER_DAY_MAX_ACTIVITY_HOURS

    if archetype == DayArchetype.LONG_TRANSFER:
        return LONG_TRANSFER_MAX_ACTIVITY_HOURS

    if archetype == DayArchetype.RECOVERY:
        return RECOVERY_DAY_MAX_ACTIVITY_HOURS

    return NORMAL_DAY_MAX_ACTIVITY_HOURS


def _activity_from_record(
    record: Mapping[str, Any],
) -> ActivityProfile:
    engine = ActivityConstraintsEngine()

    return engine.normalize(record)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _scheduled_from_record(
    record: Mapping[str, Any],
    *,
    day_number: int,
) -> ScheduledActivity:
    activity_record = record.get("activity")

    activity = (
        _activity_from_record(activity_record)
        if isinstance(
            activity_record,
            Mapping,
        )
        else _activity_from_record(record)
    )

    start_value = record.get("start_minutes")

    if start_value is None:
        start_value = record.get("start_time")

    start = _parse_time(start_value)

    if start is None:
        start = activity.fixed_start_minutes

    if start is None:
        start = DEFAULT_DAY_START_MINUTES

    end_value = record.get("end_minutes")

    if end_value is None:
        end_value = record.get("end_time")

    end = _parse_time(end_value)

    if end is None:
        end = _activity_end(
            activity,
            start,
        )

    fixed_value = record.get("fixed")

    if fixed_value is None:
        fixed_value = record.get("fixed_time")

    fixed = _safe_bool(fixed_value)

    if activity.fixed_start_minutes is not None:
        fixed = True

    priority = _safe_int(
        record.get("priority"),
        50,
    )

    travel_before = record.get(
        "travel_before_minutes"
    )

    if travel_before is None:
        travel_before = record.get(
            "transfer_before_minutes"
        )

    travel_after = record.get(
        "travel_after_minutes"
    )

    if travel_after is None:
        travel_after = record.get(
            "transfer_after_minutes"
        )

    return ScheduledActivity(
        activity=activity,
        start_minutes=start,
        end_minutes=end,
        day_number=day_number,
        fixed=fixed,
        priority=priority,
        travel_before_minutes=_safe_int(
            travel_before
        ),
        travel_after_minutes=_safe_int(
            travel_after
        ),
        raw=dict(record),
    )


def parse_schedule(
    days: Sequence[Mapping[str, Any]],
) -> list[list[ScheduledActivity]]:
    result: list[list[ScheduledActivity]] = []

    for day_index, day in enumerate(
        days,
        start=1,
    ):
        raw_activities = (
            day.get("activities")
            or day.get("schedule")
            or []
        )

        if not isinstance(
            raw_activities,
            Sequence,
        ) or isinstance(
            raw_activities,
            (str, bytes),
        ):
            raw_activities = []

        parsed = [
            _scheduled_from_record(
                activity,
                day_number=day_index,
            )
            for activity in raw_activities
            if isinstance(
                activity,
                Mapping,
            )
        ]

        parsed.sort(
            key=lambda item: (
                item.start_minutes,
                not item.fixed,
                -item.priority,
            )
        )

        result.append(parsed)

    return result


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class ScheduleValidator:
    def validate_day(
        self,
        activities: Sequence[ScheduledActivity],
        *,
        day_number: int,
        archetype: DayArchetype | None = None,
    ) -> list[ScheduleConflict]:
        conflicts: list[ScheduleConflict] = []

        ordered = sorted(
            activities,
            key=lambda item: (
                item.start_minutes,
                not item.fixed,
                -item.priority,
            ),
        )

        capacity = _activity_capacity_for_archetype(
            archetype
        )

        total_hours = sum(
            item.activity.duration_hours
            for item in ordered
        )

        if total_hours > capacity:
            conflicts.append(
                ScheduleConflict(
                    conflict_type=ConflictType.DAY_OVERLOAD,
                    day_number=day_number,
                    activity_id=(
                        ordered[0].activity.activity_id
                        if ordered
                        else ""
                    ),
                    severity="hard",
                    message=(
                        f"Day contains {total_hours:.2f} activity hours "
                        f"but its archetype capacity is {capacity:.2f} hours."
                    ),
                )
            )

        intense_hours = sum(
            item.activity.duration_hours
            for item in ordered
            if item.activity.intensity
            in {
                ActivityIntensity.HIGH,
                ActivityIntensity.EXTREME,
            }
        )

        if intense_hours > INTENSE_DAY_MAX_ACTIVITY_HOURS:
            conflicts.append(
                ScheduleConflict(
                    conflict_type=ConflictType.INTENSITY_OVERLOAD,
                    day_number=day_number,
                    activity_id=(
                        ordered[0].activity.activity_id
                        if ordered
                        else ""
                    ),
                    severity="hard",
                    message=(
                        f"Day contains {intense_hours:.2f} hours "
                        f"of high-intensity activity."
                    ),
                )
            )

        for activity in ordered:
            conflicts.extend(
                self.validate_activity(
                    activity
                )
            )

        for first, second in zip(
            ordered,
            ordered[1:],
        ):
            first_end = (
                first.end_minutes
                + first.activity.recovery_minutes
                + first.travel_after_minutes
            )

            second_start = (
                second.start_minutes
                - second.activity.preparation_minutes
                - second.travel_before_minutes
            )

            if (
                second_start
                < first_end
                + MIN_ACTIVITY_GAP_MINUTES
            ):
                conflicts.append(
                    ScheduleConflict(
                        conflict_type=ConflictType.OVERLAP,
                        day_number=day_number,
                        activity_id=(
                            second.activity.activity_id
                        ),
                        related_activity_id=(
                            first.activity.activity_id
                        ),
                        severity="hard",
                        message=(
                            f"{second.activity.name} overlaps or has "
                            f"insufficient buffer after "
                            f"{first.activity.name}."
                        ),
                    )
                )

        return conflicts

    def validate_activity(
        self,
        scheduled: ScheduledActivity,
    ) -> list[ScheduleConflict]:
        activity = scheduled.activity
        day = scheduled.day_number
        conflicts: list[ScheduleConflict] = []

        if (
            scheduled.end_minutes
            <= scheduled.start_minutes
        ):
            conflicts.append(
                ScheduleConflict(
                    conflict_type=ConflictType.INVALID_DURATION,
                    day_number=day,
                    activity_id=activity.activity_id,
                    severity="hard",
                    message=(
                        f"{activity.name} has an invalid "
                        f"scheduled duration."
                    ),
                )
            )

            return conflicts

        if (
            activity.fixed_start_minutes is not None
            and scheduled.start_minutes
            != activity.fixed_start_minutes
        ):
            conflicts.append(
                ScheduleConflict(
                    conflict_type=ConflictType.FIXED_TIME_CONFLICT,
                    day_number=day,
                    activity_id=activity.activity_id,
                    severity="hard",
                    message=(
                        f"{activity.name} is not scheduled "
                        f"at its fixed start."
                    ),
                )
            )

        if (
            activity.earliest_start_minutes is not None
            and scheduled.start_minutes
            < activity.earliest_start_minutes
        ):
            conflicts.append(
                ScheduleConflict(
                    conflict_type=ConflictType.TOO_EARLY,
                    day_number=day,
                    activity_id=activity.activity_id,
                    severity="hard",
                    message=(
                        f"{activity.name} starts before "
                        f"its allowed window."
                    ),
                )
            )

        if (
            activity.latest_start_minutes is not None
            and scheduled.start_minutes
            > activity.latest_start_minutes
        ):
            conflicts.append(
                ScheduleConflict(
                    conflict_type=ConflictType.TOO_LATE,
                    day_number=day,
                    activity_id=activity.activity_id,
                    severity="hard",
                    message=(
                        f"{activity.name} starts after "
                        f"its latest allowed start."
                    ),
                )
            )

        if (
            activity.latest_finish_minutes is not None
            and scheduled.end_minutes
            > activity.latest_finish_minutes
        ):
            conflicts.append(
                ScheduleConflict(
                    conflict_type=ConflictType.LATEST_FINISH,
                    day_number=day,
                    activity_id=activity.activity_id,
                    severity="hard",
                    message=(
                        f"{activity.name} finishes after "
                        f"its latest allowed time."
                    ),
                )
            )

        if not _windows_allow(
            activity.opening_windows,
            scheduled.start_minutes,
            scheduled.end_minutes,
        ):
            conflicts.append(
                ScheduleConflict(
                    conflict_type=ConflictType.OUTSIDE_OPENING_HOURS,
                    day_number=day,
                    activity_id=activity.activity_id,
                    severity="hard",
                    message=(
                        f"{activity.name} is scheduled outside "
                        f"its operating hours."
                    ),
                )
            )

        # A factual fixed-time activity may legitimately begin before
        # the normal 07:00 itinerary window. This is important for
        # 06:00 game drives.
        if (
            not scheduled.fixed
            and scheduled.start_minutes
            < DEFAULT_DAY_START_MINUTES
        ):
            conflicts.append(
                ScheduleConflict(
                    conflict_type=ConflictType.TOO_EARLY,
                    day_number=day,
                    activity_id=activity.activity_id,
                    severity="soft",
                    message=(
                        f"{activity.name} starts before "
                        f"the normal itinerary day."
                    ),
                )
            )

        # Do not automatically call a fixed activity invalid merely
        # because it extends the normal soft day boundary.
        if (
            scheduled.end_minutes
            > DEFAULT_DAY_END_MINUTES
        ):
            conflicts.append(
                ScheduleConflict(
                    conflict_type=ConflictType.TOO_LATE,
                    day_number=day,
                    activity_id=activity.activity_id,
                    severity="soft",
                    message=(
                        f"{activity.name} finishes after "
                        f"the normal itinerary day."
                    ),
                )
            )

        return conflicts

    def validate(
        self,
        schedule: Sequence[
            Sequence[ScheduledActivity]
        ],
        *,
        archetypes: Mapping[
            int,
            DayArchetype,
        ]
        | None = None,
    ) -> list[ScheduleConflict]:
        conflicts: list[ScheduleConflict] = []

        for day_number, activities in enumerate(
            schedule,
            start=1,
        ):
            archetype = (
                archetypes.get(day_number)
                if archetypes
                else None
            )

            conflicts.extend(
                self.validate_day(
                    activities,
                    day_number=day_number,
                    archetype=archetype,
                )
            )

        return conflicts


# ---------------------------------------------------------------------------
# Repair engine
# ---------------------------------------------------------------------------


class ScheduleRepairEngine:
    name = "ScheduleRepairEngine"
    version = "2.0"

    def __init__(self) -> None:
        self.validator = ScheduleValidator()

    # ------------------------------------------------------------------
    # Activity semantic helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_non_movable(
        activity: ScheduledActivity,
    ) -> bool:
        if activity.fixed:
            return True

        if activity.activity.fixed_start_minutes is not None:
            return True

        if activity.is_transfer_activity:
            return True

        return False

    @staticmethod
    def _day_destination_id(
        day: Mapping[str, Any],
    ) -> str | None:
        """
        Extract the authoritative destination identity for a calendar day.

        Planner-generated days should provide destination_id directly.
        """

        value = day.get("destination_id")

        if value is None:
            destination = day.get("destination")

            if isinstance(
                destination,
                Mapping,
            ):
                value = (
                    destination.get("id")
                    or destination.get("destination_id")
                )

        if value is None:
            return None

        text = str(value).strip()

        return text or None

    @staticmethod
    def _day_destination_ids_from_input(
        days: Sequence[Mapping[str, Any]],
    ) -> dict[int, str | None]:
        return {
            day_number: ScheduleRepairEngine._day_destination_id(
                day
            )
            for day_number, day in enumerate(
                days,
                start=1,
            )
        }

    @staticmethod
    def _same_destination(
        activity: ScheduledActivity,
        target_destination_id: str | None,
    ) -> bool:
        """
        Cross-day movement is allowed only when both sides identify the
        same destination.

        Unknown destination identity is NOT treated as equal.
        This deliberately prevents unsafe historical records from being
        silently moved between destinations.
        """

        activity_destination = activity.destination_id

        if (
            activity_destination is None
            or target_destination_id is None
        ):
            return False

        return (
            activity_destination
            == target_destination_id
        )

    # ------------------------------------------------------------------
    # Time placement
    # ------------------------------------------------------------------

    def _earliest_legal_start(
        self,
        activity: ActivityProfile,
        existing: Sequence[ScheduledActivity],
        *,
        preferred_start: int,
        travel_before_minutes: int = 0,
        travel_after_minutes: int = 0,
    ) -> int | None:
        """
        Find the earliest legal start without changing activity identity.

        This method considers:
        - activity preparation
        - activity recovery
        - travel before/after
        - minimum gap
        - opening windows
        - earliest/latest start
        - latest finish
        - existing scheduled activities
        """

        candidate = max(
            preferred_start,
            (
                activity.earliest_start_minutes
                if activity.earliest_start_minutes
                is not None
                else DEFAULT_DAY_START_MINUTES
            ),
        )

        candidate = _next_window_start(
            activity.opening_windows,
            candidate,
        )

        if candidate is None:
            return None

        for _ in range(
            MAX_SEARCH_ITERATIONS
        ):
            end = _activity_end(
                activity,
                candidate,
            )

            if (
                activity.latest_start_minutes
                is not None
                and candidate
                > activity.latest_start_minutes
            ):
                return None

            if (
                activity.latest_finish_minutes
                is not None
                and end
                > activity.latest_finish_minutes
            ):
                return None

            if not _windows_allow(
                activity.opening_windows,
                candidate,
                end,
            ):
                next_start = _next_window_start(
                    activity.opening_windows,
                    candidate + 1,
                )

                if next_start is None:
                    return None

                candidate = next_start
                continue

            moved = False

            ordered = sorted(
                existing,
                key=lambda item: (
                    item.start_minutes,
                    item.end_minutes,
                ),
            )

            for other in ordered:
                candidate_occupied_start = (
                    candidate
                    - activity.preparation_minutes
                    - travel_before_minutes
                )

                candidate_occupied_end = (
                    end
                    + activity.recovery_minutes
                    + travel_after_minutes
                )

                other_occupied_start = (
                    other.start_minutes
                    - other.activity.preparation_minutes
                    - other.travel_before_minutes
                )

                other_occupied_end = (
                    other.end_minutes
                    + other.activity.recovery_minutes
                    + other.travel_after_minutes
                )

                if (
                    candidate_occupied_end
                    + MIN_ACTIVITY_GAP_MINUTES
                    <= other_occupied_start
                ):
                    continue

                if (
                    other_occupied_end
                    + MIN_ACTIVITY_GAP_MINUTES
                    <= candidate_occupied_start
                ):
                    continue

                # Conflict. Push the candidate after this activity.
                candidate = (
                    other.end_minutes
                    + other.activity.recovery_minutes
                    + other.travel_after_minutes
                    + activity.preparation_minutes
                    + travel_before_minutes
                    + MIN_ACTIVITY_GAP_MINUTES
                )

                next_start = _next_window_start(
                    activity.opening_windows,
                    candidate,
                )

                if next_start is None:
                    return None

                candidate = next_start
                moved = True
                break

            if moved:
                continue

            return candidate

        return None

    # ------------------------------------------------------------------
    # Cross-day candidates
    # ------------------------------------------------------------------

    def _candidate_days(
        self,
        activity: ScheduledActivity,
        schedule: Sequence[
            Sequence[ScheduledActivity]
        ],
        archetypes: Mapping[
            int,
            DayArchetype,
        ]
        | None,
        *,
        day_destinations: Mapping[
            int,
            str | None,
        ]
        | None = None,
    ) -> list[int]:
        """
        Return only semantically safe target days.

        IMPORTANT:
        A flexible activity may only move to another day belonging to the
        SAME destination.

        If destination identity is unavailable, cross-day repair is disabled.
        """

        if self._is_non_movable(activity):
            return []

        current_day = activity.day_number

        if (
            day_destinations is None
            or activity.destination_id is None
        ):
            return []

        candidates = list(
            range(
                1,
                len(schedule) + 1,
            )
        )

        candidates.sort(
            key=lambda day: (
                abs(day - current_day),
                day,
            )
        )

        result: list[int] = []

        for day in candidates:
            if day == current_day:
                continue

            target_destination_id = (
                day_destinations.get(day)
            )

            if not self._same_destination(
                activity,
                target_destination_id,
            ):
                continue

            archetype = (
                archetypes.get(day)
                if archetypes
                else None
            )

            blocked_archetypes = {
                DayArchetype.DEPARTURE,
                DayArchetype.ARRIVAL,
                DayArchetype.TRANSFER,
                DayArchetype.LONG_TRANSFER,
                DayArchetype.RECOVERY,
            }

            overnight_transition = getattr(
                DayArchetype,
                "OVERNIGHT_TRANSITION",
                None,
            )

            if overnight_transition is not None:
                blocked_archetypes.add(
                    overnight_transition
                )

            if archetype in blocked_archetypes:
                continue

            result.append(day)

        return result

    # ------------------------------------------------------------------
    # Cross-day movement
    # ------------------------------------------------------------------

    def _move_activity(
        self,
        schedule: list[
            list[ScheduledActivity]
        ],
        activity: ScheduledActivity,
        *,
        to_day: int,
        archetypes: Mapping[
            int,
            DayArchetype,
        ]
        | None,
        day_destinations: Mapping[
            int,
            str | None,
        ]
        | None = None,
    ) -> RepairAction | None:
        """
        Move an activity only when destination semantics remain identical.

        Route-transfer activities are never moved.
        """

        if self._is_non_movable(activity):
            return None

        if (
            day_destinations is None
            or not self._same_destination(
                activity,
                day_destinations.get(to_day),
            )
        ):
            return None

        destination_index = to_day - 1

        if not (
            0 <= destination_index
            < len(schedule)
        ):
            return None

        destination_day = schedule[
            destination_index
        ]

        archetype = (
            archetypes.get(to_day)
            if archetypes
            else None
        )

        capacity = _activity_capacity_for_archetype(
            archetype
        )

        current_hours = sum(
            item.activity.duration_hours
            for item in destination_day
        )

        if (
            current_hours
            + activity.activity.duration_hours
            > capacity
        ):
            return None

        preferred_start = max(
            DEFAULT_DAY_START_MINUTES,
            activity.start_minutes,
        )

        start = self._earliest_legal_start(
            activity.activity,
            destination_day,
            preferred_start=preferred_start,
            travel_before_minutes=(
                activity.travel_before_minutes
            ),
            travel_after_minutes=(
                activity.travel_after_minutes
            ),
        )

        if start is None:
            return None

        end = _activity_end(
            activity.activity,
            start,
        )

        if (
            not activity.fixed
            and end > DEFAULT_DAY_END_MINUTES
        ):
            return None

        repaired = replace(
            activity,
            start_minutes=start,
            end_minutes=end,
            day_number=to_day,
        )

        source_index = activity.day_number - 1

        if not (
            0 <= source_index
            < len(schedule)
        ):
            return None

        try:
            schedule[source_index].remove(
                activity
            )
        except ValueError:
            return None

        destination_day.append(
            repaired
        )

        destination_day.sort(
            key=lambda item: (
                item.start_minutes,
                not item.fixed,
                -item.priority,
            )
        )

        return RepairAction(
            action_type=RepairActionType.MOVE,
            activity_id=(
                activity.activity.activity_id
            ),
            from_day=activity.day_number,
            to_day=to_day,
            from_start_minutes=(
                activity.start_minutes
            ),
            to_start_minutes=start,
            reason=(
                "Moved flexible activity to another "
                "calendar day within the same destination "
                "to reduce a schedule conflict."
            ),
            confidence=0.94,
        )

    # ------------------------------------------------------------------
    # Same-day shifting
    # ------------------------------------------------------------------

    def _shift_activity(
        self,
        schedule: list[
            list[ScheduledActivity]
        ],
        activity: ScheduledActivity,
    ) -> RepairAction | None:
        """
        Shift an activity within its existing day.

        This is the preferred repair mechanism because it cannot alter
        destination sequence.
        """

        if self._is_non_movable(activity):
            return None

        day_index = activity.day_number - 1

        if not (
            0 <= day_index
            < len(schedule)
        ):
            return None

        day = schedule[day_index]

        others = [
            item
            for item in day
            if item is not activity
        ]

        start = self._earliest_legal_start(
            activity.activity,
            others,
            preferred_start=activity.start_minutes,
            travel_before_minutes=(
                activity.travel_before_minutes
            ),
            travel_after_minutes=(
                activity.travel_after_minutes
            ),
        )

        if (
            start is None
            or start == activity.start_minutes
        ):
            return None

        end = _activity_end(
            activity.activity,
            start,
        )

        # Never push a normal flexible activity beyond the normal
        # itinerary boundary merely to claim it was repaired.
        if end > DEFAULT_DAY_END_MINUTES:
            return None

        index = day.index(activity)

        day[index] = replace(
            activity,
            start_minutes=start,
            end_minutes=end,
        )

        day.sort(
            key=lambda item: (
                item.start_minutes,
                not item.fixed,
                -item.priority,
            )
        )

        action_type = (
            RepairActionType.DELAY
            if start > activity.start_minutes
            else RepairActionType.ADVANCE
        )

        return RepairAction(
            action_type=action_type,
            activity_id=(
                activity.activity.activity_id
            ),
            from_day=activity.day_number,
            to_day=activity.day_number,
            from_start_minutes=(
                activity.start_minutes
            ),
            to_start_minutes=start,
            reason=(
                "Shifted flexible activity within the "
                "same destination day to a legal time window."
            ),
            confidence=0.96,
        )

    # ------------------------------------------------------------------
    # Conflict handling
    # ------------------------------------------------------------------

    @staticmethod
    def _repairable_conflict(
        conflict: ScheduleConflict,
    ) -> bool:
        return conflict.conflict_type in {
            ConflictType.OVERLAP,
            ConflictType.OUTSIDE_OPENING_HOURS,
            ConflictType.TOO_EARLY,
            ConflictType.TOO_LATE,
            ConflictType.LATEST_FINISH,
        }

    # ------------------------------------------------------------------
    # Main repair
    # ------------------------------------------------------------------

    def repair(
        self,
        days: Sequence[
            Mapping[str, Any]
        ],
        *,
        archetypes: Mapping[
            int,
            DayArchetype,
        ]
        | None = None,
    ) -> ScheduleRepairResult:
        """
        Validate and conservatively repair an itinerary schedule.

        The engine does NOT:
        - invent activities,
        - invent transport,
        - change destination order,
        - move activities between different destinations,
        - move route transfers,
        - move fixed-time activities,
        - fabricate missing destination identity.
        """

        schedule = parse_schedule(days)

        day_destinations = (
            self._day_destination_ids_from_input(
                days
            )
        )

        initial_conflicts = self.validator.validate(
            schedule,
            archetypes=archetypes,
        )

        actions: list[RepairAction] = []
        iterations = 0

        while (
            iterations
            < MAX_REPAIR_ITERATIONS
        ):
            iterations += 1

            conflicts = self.validator.validate(
                schedule,
                archetypes=archetypes,
            )

            if not conflicts:
                break

            progress = False

            # ----------------------------------------------------------
            # Phase 1:
            # Repair conflicts inside the same day.
            # ----------------------------------------------------------

            for conflict in conflicts:
                if not self._repairable_conflict(
                    conflict
                ):
                    continue

                activity = self._find_activity(
                    schedule,
                    conflict.activity_id,
                )

                if activity is None:
                    continue

                if self._is_non_movable(
                    activity
                ):
                    continue

                action = self._shift_activity(
                    schedule,
                    activity,
                )

                if action is not None:
                    actions.append(action)
                    progress = True
                    break

            if progress:
                continue

            # ----------------------------------------------------------
            # Phase 2:
            # Cross-day repair is deliberately conservative.
            #
            # Only same-destination activities may move.
            # Transfer activities and fixed activities are excluded.
            # ----------------------------------------------------------

            for conflict in conflicts:
                if not self._repairable_conflict(
                    conflict
                ):
                    continue

                activity = self._find_activity(
                    schedule,
                    conflict.activity_id,
                )

                if activity is None:
                    continue

                if self._is_non_movable(
                    activity
                ):
                    continue

                candidates = self._candidate_days(
                    activity,
                    schedule,
                    archetypes,
                    day_destinations=day_destinations,
                )

                for target_day in candidates:
                    action = self._move_activity(
                        schedule,
                        activity,
                        to_day=target_day,
                        archetypes=archetypes,
                        day_destinations=day_destinations,
                    )

                    if action is not None:
                        actions.append(action)
                        progress = True
                        break

                if progress:
                    break

            if progress:
                continue

            # ----------------------------------------------------------
            # No safe deterministic repair exists.
            #
            # Stop instead of making an unsafe mutation.
            # ----------------------------------------------------------

            break

        remaining = self.validator.validate(
            schedule,
            archetypes=archetypes,
        )

        repaired = bool(actions)

        warnings: list[str] = []

        if remaining:
            warnings.append(
                "One or more schedule conflicts could not "
                "be repaired automatically without risking "
                "destination, route, or fixed-time integrity."
            )

        for conflict in remaining:
            warnings.append(
                f"Day {conflict.day_number}: "
                f"{conflict.message}"
            )

        repaired_days = self._build_repaired_days(
            schedule,
            archetypes=archetypes,
        )

        return ScheduleRepairResult(
            days=tuple(
                repaired_days
            ),
            conflicts_found=tuple(
                initial_conflicts
            ),
            conflicts_remaining=tuple(
                remaining
            ),
            actions=tuple(actions),
            repaired=repaired,
            fully_repaired=not remaining,
            warnings=tuple(warnings),
            iterations=iterations,
        )

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    @staticmethod
    def _find_activity(
        schedule: Sequence[
            Sequence[ScheduledActivity]
        ],
        activity_id: str,
    ) -> ScheduledActivity | None:
        for day in schedule:
            for activity in day:
                if (
                    activity.activity.activity_id
                    == activity_id
                ):
                    return activity

        return None

    # ------------------------------------------------------------------
    # Result construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_repaired_days(
        schedule: Sequence[
            Sequence[ScheduledActivity]
        ],
        *,
        archetypes: Mapping[
            int,
            DayArchetype,
        ]
        | None,
    ) -> list[RepairedDay]:
        result: list[RepairedDay] = []

        for day_number, activities in enumerate(
            schedule,
            start=1,
        ):
            ordered = sorted(
                activities,
                key=lambda item: (
                    item.start_minutes,
                    not item.fixed,
                    -item.priority,
                ),
            )

            archetype = (
                archetypes.get(day_number)
                if archetypes
                else None
            )

            total_activity_hours = sum(
                item.activity.duration_hours
                for item in ordered
            )

            total_intense_hours = sum(
                item.activity.duration_hours
                for item in ordered
                if item.activity.intensity
                in {
                    ActivityIntensity.HIGH,
                    ActivityIntensity.EXTREME,
                }
            )

            capacity = (
                _activity_capacity_for_archetype(
                    archetype
                )
            )

            overloaded = (
                total_activity_hours
                > capacity
                or total_intense_hours
                > INTENSE_DAY_MAX_ACTIVITY_HOURS
            )

            warnings: list[str] = []

            if overloaded:
                warnings.append(
                    "Day remains above its activity capacity."
                )

            result.append(
                RepairedDay(
                    day_number=day_number,
                    activities=tuple(
                        ordered
                    ),
                    archetype=archetype,
                    total_activity_hours=round(
                        total_activity_hours,
                        2,
                    ),
                    total_intense_hours=round(
                        total_intense_hours,
                        2,
                    ),
                    overloaded=overloaded,
                    warnings=tuple(
                        warnings
                    ),
                )
            )

        return result

    # ------------------------------------------------------------------
    # Public compatibility methods
    # ------------------------------------------------------------------

    def repair_schedule(
        self,
        days: Sequence[
            Mapping[str, Any]
        ],
        *,
        archetypes: Mapping[
            int,
            DayArchetype,
        ]
        | None = None,
    ) -> ScheduleRepairResult:
        return self.repair(
            days,
            archetypes=archetypes,
        )

    def validate(
        self,
        days: Sequence[
            Mapping[str, Any]
        ],
        *,
        archetypes: Mapping[
            int,
            DayArchetype,
        ]
        | None = None,
    ) -> tuple[ScheduleConflict, ...]:
        schedule = parse_schedule(days)

        return tuple(
            self.validator.validate(
                schedule,
                archetypes=archetypes,
            )
        )


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _format_minutes(
    minutes: int,
) -> str:
    return (
        f"{minutes // 60:02d}:"
        f"{minutes % 60:02d}"
    )


def scheduled_activity_to_dict(
    activity: ScheduledActivity,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "activity_id": (
            activity.activity.activity_id
        ),
        "name": activity.activity.name,
        "day_number": activity.day_number,
        "start_time": _format_minutes(
            activity.start_minutes
        ),
        "end_time": _format_minutes(
            activity.end_minutes
        ),
        "start_minutes": activity.start_minutes,
        "end_minutes": activity.end_minutes,
        "duration_hours": (
            activity.activity.duration_hours
        ),
        "fixed": activity.fixed,
        "priority": activity.priority,
        "travel_before_minutes": (
            activity.travel_before_minutes
        ),
        "travel_after_minutes": (
            activity.travel_after_minutes
        ),
        "intensity": (
            activity.activity.intensity.value
        ),
    }

    if activity.destination_id is not None:
        result["destination_id"] = (
            activity.destination_id
        )

    return result


def schedule_repair_result_to_dict(
    result: ScheduleRepairResult,
) -> dict[str, Any]:
    return {
        "engine": ScheduleRepairEngine.name,
        "version": ScheduleRepairEngine.version,
        "days": [
            {
                "day_number": day.day_number,
                "archetype": (
                    day.archetype.value
                    if day.archetype
                    else None
                ),
                "activities": [
                    scheduled_activity_to_dict(
                        activity
                    )
                    for activity in day.activities
                ],
                "total_activity_hours": (
                    day.total_activity_hours
                ),
                "total_intense_hours": (
                    day.total_intense_hours
                ),
                "overloaded": day.overloaded,
                "warnings": list(
                    day.warnings
                ),
            }
            for day in result.days
        ],
        "conflicts_found": [
            {
                "type": conflict.conflict_type.value,
                "day_number": conflict.day_number,
                "activity_id": conflict.activity_id,
                "related_activity_id": (
                    conflict.related_activity_id
                ),
                "severity": conflict.severity,
                "message": conflict.message,
            }
            for conflict in result.conflicts_found
        ],
        "conflicts_remaining": [
            {
                "type": conflict.conflict_type.value,
                "day_number": conflict.day_number,
                "activity_id": conflict.activity_id,
                "related_activity_id": (
                    conflict.related_activity_id
                ),
                "severity": conflict.severity,
                "message": conflict.message,
            }
            for conflict in result.conflicts_remaining
        ],
        "actions": [
            {
                "type": action.action_type.value,
                "activity_id": action.activity_id,
                "from_day": action.from_day,
                "to_day": action.to_day,
                "from_start_minutes": (
                    action.from_start_minutes
                ),
                "to_start_minutes": (
                    action.to_start_minutes
                ),
                "reason": action.reason,
                "confidence": action.confidence,
            }
            for action in result.actions
        ],
        "repaired": result.repaired,
        "fully_repaired": result.fully_repaired,
        "warnings": list(
            result.warnings
        ),
        "iterations": result.iterations,
    }


def repair_schedule(
    days: Sequence[
        Mapping[str, Any]
    ],
    *,
    archetypes: Mapping[
        int,
        DayArchetype,
    ]
    | None = None,
) -> ScheduleRepairResult:
    return ScheduleRepairEngine().repair(
        days,
        archetypes=archetypes,
    )


__all__ = [
    "RepairActionType",
    "ConflictType",
    "ScheduledActivity",
    "ScheduleConflict",
    "RepairAction",
    "RepairedDay",
    "ScheduleRepairResult",
    "ScheduleValidator",
    "ScheduleRepairEngine",
    "parse_schedule",
    "scheduled_activity_to_dict",
    "schedule_repair_result_to_dict",
    "repair_schedule",
]
