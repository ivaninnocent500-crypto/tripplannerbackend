"""
Schedule Repair Engine
======================

Deterministic itinerary schedule validation and repair.
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
from app.engines.day_archetype import DayArchetypeEngine (
    DayArchetype,
    DayArchetypeResult,
)

logger = logging.getLogger(__name__)

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
        return self.start_minutes - self.travel_before_minutes - self.activity.preparation_minutes

    @property
    def occupied_end_minutes(self) -> int:
        return self.end_minutes + self.travel_after_minutes + self.activity.recovery_minutes


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


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.lower().strip() in {"true", "1", "yes", "fixed"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _parse_time(value: Any) -> int | None:
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
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return None
    if not 0 <= hour <= 23:
        return None
    if not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def _windows_allow(windows: Sequence[TimeWindow], start: int, end: int) -> bool:
    if not windows:
        return True
    return any(w.contains(start) and w.contains(end) for w in windows)


def _next_window_start(windows: Sequence[TimeWindow], start: int) -> int | None:
    if not windows:
        return start
    candidates = []
    for window in windows:
        if start <= window.start_minutes:
            candidates.append(window.start_minutes)
        elif window.contains(start):
            candidates.append(start)
    if not candidates:
        return None
    return min(candidates)


def _activity_end(activity: ActivityProfile, start_minutes: int) -> int:
    return start_minutes + int(round(activity.duration_hours * 60))


def _activity_capacity_for_archetype(archetype: DayArchetype | None) -> float:
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


def _activity_from_record(record: Mapping[str, Any]) -> ActivityProfile:
    engine = ActivityConstraintsEngine()
    return engine.normalize(record)


def _scheduled_from_record(record: Mapping[str, Any], *, day_number: int) -> ScheduledActivity:
    activity_record = record.get("activity")
    activity = _activity_from_record(activity_record) if isinstance(activity_record, Mapping) else _activity_from_record(record)

    start = _parse_time(record.get("start_minutes") or record.get("start_time"))
    if start is None:
        start = activity.fixed_start_minutes
    if start is None:
        start = DEFAULT_DAY_START_MINUTES

    end = _parse_time(record.get("end_minutes") or record.get("end_time"))
    if end is None:
        end = _activity_end(activity, start)

    fixed = _safe_bool(record.get("fixed") or record.get("fixed_time"))
    if activity.fixed_start_minutes is not None:
        fixed = True

    priority = _safe_int(record.get("priority"), 50)

    return ScheduledActivity(
        activity=activity, start_minutes=start, end_minutes=end, day_number=day_number, fixed=fixed,
        priority=priority,
        travel_before_minutes=_safe_int(record.get("travel_before_minutes") or record.get("transfer_before_minutes")),
        travel_after_minutes=_safe_int(record.get("travel_after_minutes") or record.get("transfer_after_minutes")),
        raw=dict(record),
    )


def parse_schedule(days: Sequence[Mapping[str, Any]]) -> list[list[ScheduledActivity]]:
    result: list[list[ScheduledActivity]] = []
    for day_index, day in enumerate(days, start=1):
        raw_activities = day.get("activities") or day.get("schedule") or []
        if not isinstance(raw_activities, Sequence):
            raw_activities = []
        parsed = [
            _scheduled_from_record(activity, day_number=day_index)
            for activity in raw_activities if isinstance(activity, Mapping)
        ]
        parsed.sort(key=lambda item: (item.start_minutes, item.fixed is False, -item.priority))
        result.append(parsed)
    return result


class ScheduleValidator:
    def validate_day(
        self, activities: Sequence[ScheduledActivity], *, day_number: int, archetype: DayArchetype | None = None,
    ) -> list[ScheduleConflict]:
        conflicts: list[ScheduleConflict] = []
        ordered = sorted(activities, key=lambda item: item.start_minutes)
        capacity = _activity_capacity_for_archetype(archetype)
        total_hours = sum(item.activity.duration_hours for item in ordered)

        if total_hours > capacity:
            conflicts.append(ScheduleConflict(
                conflict_type=ConflictType.DAY_OVERLOAD, day_number=day_number,
                activity_id=(ordered[0].activity.activity_id if ordered else ""), severity="hard",
                message=f"Day contains {total_hours:.2f} activity hours but its archetype capacity is {capacity:.2f} hours.",
            ))

        intense_hours = sum(
            item.activity.duration_hours for item in ordered
            if item.activity.intensity in {ActivityIntensity.HIGH, ActivityIntensity.EXTREME}
        )
        if intense_hours > INTENSE_DAY_MAX_ACTIVITY_HOURS:
            conflicts.append(ScheduleConflict(
                conflict_type=ConflictType.INTENSITY_OVERLOAD, day_number=day_number,
                activity_id=(ordered[0].activity.activity_id if ordered else ""), severity="hard",
                message=f"Day contains {intense_hours:.2f} hours of high-intensity activity.",
            ))

        for activity in ordered:
            conflicts.extend(self.validate_activity(activity))

        for first, second in zip(ordered, ordered[1:]):
            first_end = first.end_minutes + first.activity.recovery_minutes + first.travel_after_minutes
            second_start = second.start_minutes - second.activity.preparation_minutes - second.travel_before_minutes
            if second_start < first_end + MIN_ACTIVITY_GAP_MINUTES:
                conflicts.append(ScheduleConflict(
                    conflict_type=ConflictType.OVERLAP, day_number=day_number,
                    activity_id=second.activity.activity_id, related_activity_id=first.activity.activity_id,
                    severity="hard",
                    message=f"{second.activity.name} overlaps or has insufficient buffer after {first.activity.name}.",
                ))

        return conflicts

    def validate_activity(self, scheduled: ScheduledActivity) -> list[ScheduleConflict]:
        activity = scheduled.activity
        day = scheduled.day_number
        conflicts: list[ScheduleConflict] = []

        if scheduled.end_minutes <= scheduled.start_minutes:
            conflicts.append(ScheduleConflict(
                conflict_type=ConflictType.INVALID_DURATION, day_number=day, activity_id=activity.activity_id,
                severity="hard", message=f"{activity.name} has an invalid scheduled duration.",
            ))
            return conflicts

        if activity.fixed_start_minutes is not None and scheduled.start_minutes != activity.fixed_start_minutes:
            conflicts.append(ScheduleConflict(
                conflict_type=ConflictType.FIXED_TIME_CONFLICT, day_number=day, activity_id=activity.activity_id,
                severity="hard", message=f"{activity.name} is not scheduled at its fixed start.",
            ))
        if activity.earliest_start_minutes is not None and scheduled.start_minutes < activity.earliest_start_minutes:
            conflicts.append(ScheduleConflict(
                conflict_type=ConflictType.TOO_EARLY, day_number=day, activity_id=activity.activity_id,
                severity="hard", message=f"{activity.name} starts before its allowed window.",
            ))
        if activity.latest_start_minutes is not None and scheduled.start_minutes > activity.latest_start_minutes:
            conflicts.append(ScheduleConflict(
                conflict_type=ConflictType.TOO_LATE, day_number=day, activity_id=activity.activity_id,
                severity="hard", message=f"{activity.name} starts after its latest allowed start.",
            ))
        if activity.latest_finish_minutes is not None and scheduled.end_minutes > activity.latest_finish_minutes:
            conflicts.append(ScheduleConflict(
                conflict_type=ConflictType.LATEST_FINISH, day_number=day, activity_id=activity.activity_id,
                severity="hard", message=f"{activity.name} finishes after its latest allowed time.",
            ))
        if not _windows_allow(activity.opening_windows, scheduled.start_minutes, scheduled.end_minutes):
            conflicts.append(ScheduleConflict(
                conflict_type=ConflictType.OUTSIDE_OPENING_HOURS, day_number=day, activity_id=activity.activity_id,
                severity="hard", message=f"{activity.name} is scheduled outside its operating hours.",
            ))
        if scheduled.start_minutes < DEFAULT_DAY_START_MINUTES:
            conflicts.append(ScheduleConflict(
                conflict_type=ConflictType.TOO_EARLY, day_number=day, activity_id=activity.activity_id,
                severity="soft", message=f"{activity.name} starts before the normal itinerary day.",
            ))
        if scheduled.end_minutes > DEFAULT_DAY_END_MINUTES:
            conflicts.append(ScheduleConflict(
                conflict_type=ConflictType.TOO_LATE, day_number=day, activity_id=activity.activity_id,
                severity="soft", message=f"{activity.name} finishes after the normal itinerary day.",
            ))

        return conflicts

    def validate(
        self, schedule: Sequence[Sequence[ScheduledActivity]], *, archetypes: Mapping[int, DayArchetype] | None = None,
    ) -> list[ScheduleConflict]:
        conflicts: list[ScheduleConflict] = []
        for day_number, activities in enumerate(schedule, start=1):
            archetype = archetypes.get(day_number) if archetypes else None
            conflicts.extend(self.validate_day(activities, day_number=day_number, archetype=archetype))
        return conflicts


class ScheduleRepairEngine:
    name = "ScheduleRepairEngine"
    version = "1.0"

    def __init__(self) -> None:
        self.validator = ScheduleValidator()

    def _earliest_legal_start(
        self, activity: ActivityProfile, existing: Sequence[ScheduledActivity], *, preferred_start: int,
    ) -> int | None:
        candidate = max(
            preferred_start,
            activity.earliest_start_minutes if activity.earliest_start_minutes is not None else DEFAULT_DAY_START_MINUTES,
        )
        candidate = _next_window_start(activity.opening_windows, candidate)
        if candidate is None:
            return None

        for _ in range(100):
            end = _activity_end(activity, candidate)

            if activity.latest_start_minutes is not None and candidate > activity.latest_start_minutes:
                return None
            if activity.latest_finish_minutes is not None and end > activity.latest_finish_minutes:
                return None

            if not _windows_allow(activity.opening_windows, candidate, end):
                candidate = _next_window_start(activity.opening_windows, candidate + 1)
                if candidate is None:
                    return None
                continue

            conflict_found = False
            for other in sorted(existing, key=lambda item: item.start_minutes):
                required_start = (
                    other.end_minutes + other.activity.recovery_minutes + other.travel_after_minutes
                    + activity.preparation_minutes + activity.travel_before_minutes + MIN_ACTIVITY_GAP_MINUTES
                )
                if candidate < required_start:
                    candidate = required_start
                    conflict_found = True
                    break

                candidate_end = end
                required_other_start = (
                    candidate_end + activity.recovery_minutes + activity.travel_after_minutes
                    + other.activity.preparation_minutes + other.travel_before_minutes + MIN_ACTIVITY_GAP_MINUTES
                )
                if required_other_start > other.start_minutes:
                    candidate = (
                        other.end_minutes + other.activity.recovery_minutes + other.travel_after_minutes
                        + activity.preparation_minutes + activity.travel_before_minutes + MIN_ACTIVITY_GAP_MINUTES
                    )
                    conflict_found = True
                    break

            if conflict_found:
                continue

            return candidate

        return None

    def _candidate_days(
        self, activity: ScheduledActivity, schedule: Sequence[Sequence[ScheduledActivity]],
        archetypes: Mapping[int, DayArchetype] | None,
    ) -> list[int]:
        current_day = activity.day_number
        candidates = list(range(1, len(schedule) + 1))
        candidates.sort(key=lambda day: (abs(day - current_day), day))

        result: list[int] = []
        for day in candidates:
            if day == current_day:
                continue
            archetype = archetypes.get(day) if archetypes else None
            if archetype in {DayArchetype.DEPARTURE, DayArchetype.ARRIVAL, DayArchetype.LONG_TRANSFER}:
                continue
            result.append(day)
        return result

    def _move_activity(
        self, schedule: list[list[ScheduledActivity]], activity: ScheduledActivity, *,
        to_day: int, archetypes: Mapping[int, DayArchetype] | None,
    ) -> RepairAction | None:
        if activity.fixed:
            return None
        if activity.activity.fixed_start_minutes is not None:
            return None

        destination_index = to_day - 1
        if not 0 <= destination_index < len(schedule):
            return None

        destination_day = schedule[destination_index]
        archetype = archetypes.get(to_day) if archetypes else None
        capacity = _activity_capacity_for_archetype(archetype)
        current_hours = sum(item.activity.duration_hours for item in destination_day)

        if current_hours + activity.activity.duration_hours > capacity:
            return None

        preferred_start = max(DEFAULT_DAY_START_MINUTES, activity.start_minutes)
        start = self._earliest_legal_start(activity.activity, destination_day, preferred_start=preferred_start)
        if start is None:
            return None

        end = _activity_end(activity.activity, start)
        repaired = replace(activity, start_minutes=start, end_minutes=end, day_number=to_day)

        schedule[activity.day_number - 1].remove(activity)
        destination_day.append(repaired)
        destination_day.sort(key=lambda item: (item.start_minutes, item.fixed is False, -item.priority))

        return RepairAction(
            action_type=RepairActionType.MOVE, activity_id=activity.activity.activity_id,
            from_day=activity.day_number, to_day=to_day, from_start_minutes=activity.start_minutes,
            to_start_minutes=start, reason="Moved flexible activity to reduce schedule conflict.", confidence=0.92,
        )

    def _shift_activity(self, schedule: list[list[ScheduledActivity]], activity: ScheduledActivity) -> RepairAction | None:
        if activity.fixed:
            return None

        day = schedule[activity.day_number - 1]
        others = [item for item in day if item.activity.activity_id != activity.activity.activity_id]
        start = self._earliest_legal_start(activity.activity, others, preferred_start=activity.start_minutes)

        if start is None or start == activity.start_minutes:
            return None

        end = _activity_end(activity.activity, start)
        index = day.index(activity)
        day[index] = replace(activity, start_minutes=start, end_minutes=end)
        day.sort(key=lambda item: (item.start_minutes, item.fixed is False, -item.priority))

        action_type = RepairActionType.DELAY if start > activity.start_minutes else RepairActionType.ADVANCE
        return RepairAction(
            action_type=action_type, activity_id=activity.activity.activity_id, from_day=activity.day_number,
            to_day=activity.day_number, from_start_minutes=activity.start_minutes, to_start_minutes=start,
            reason="Shifted flexible activity to a legal time window.", confidence=0.95,
        )

    def repair(
        self, days: Sequence[Mapping[str, Any]], *, archetypes: Mapping[int, DayArchetype] | None = None,
    ) -> ScheduleRepairResult:
        schedule = parse_schedule(days)
        all_conflicts = self.validator.validate(schedule, archetypes=archetypes)
        actions: list[RepairAction] = []
        iterations = 0

        while iterations < MAX_REPAIR_ITERATIONS:
            iterations += 1
            conflicts = self.validator.validate(schedule, archetypes=archetypes)
            if not conflicts:
                break

            progress = False

            for conflict in conflicts:
                if conflict.conflict_type not in {
                    ConflictType.OVERLAP, ConflictType.OUTSIDE_OPENING_HOURS,
                    ConflictType.TOO_EARLY, ConflictType.TOO_LATE, ConflictType.LATEST_FINISH,
                }:
                    continue
                activity = self._find_activity(schedule, conflict.activity_id)
                if activity is None or activity.fixed:
                    continue
                action = self._shift_activity(schedule, activity)
                if action:
                    actions.append(action)
                    progress = True
                    break

            if progress:
                continue

            for conflict in conflicts:
                activity = self._find_activity(schedule, conflict.activity_id)
                if activity is None or activity.fixed:
                    continue
                for target_day in self._candidate_days(activity, schedule, archetypes):
                    action = self._move_activity(schedule, activity, to_day=target_day, archetypes=archetypes)
                    if action:
                        actions.append(action)
                        progress = True
                        break
                if progress:
                    break

            if progress:
                continue

            break

        remaining = self.validator.validate(schedule, archetypes=archetypes)
        repaired = bool(actions)

        warnings: list[str] = []
        if remaining:
            warnings.append("One or more schedule conflicts could not be repaired automatically.")
        for conflict in remaining:
            warnings.append(f"Day {conflict.day_number}: {conflict.message}")

        repaired_days = self._build_repaired_days(schedule, archetypes=archetypes)

        return ScheduleRepairResult(
            days=tuple(repaired_days), conflicts_found=tuple(all_conflicts), conflicts_remaining=tuple(remaining),
            actions=tuple(actions), repaired=repaired, fully_repaired=not remaining, warnings=tuple(warnings),
            iterations=iterations,
        )

    @staticmethod
    def _find_activity(schedule: Sequence[Sequence[ScheduledActivity]], activity_id: str) -> ScheduledActivity | None:
        for day in schedule:
            for activity in day:
                if activity.activity.activity_id == activity_id:
                    return activity
        return None

    @staticmethod
    def _build_repaired_days(
        schedule: Sequence[Sequence[ScheduledActivity]], *, archetypes: Mapping[int, DayArchetype] | None,
    ) -> list[RepairedDay]:
        result: list[RepairedDay] = []
        for day_number, activities in enumerate(schedule, start=1):
            ordered = sorted(activities, key=lambda item: (item.start_minutes, item.fixed is False, -item.priority))
            archetype = archetypes.get(day_number) if archetypes else None

            total_activity_hours = sum(item.activity.duration_hours for item in ordered)
            total_intense_hours = sum(
                item.activity.duration_hours for item in ordered
                if item.activity.intensity in {ActivityIntensity.HIGH, ActivityIntensity.EXTREME}
            )
            capacity = _activity_capacity_for_archetype(archetype)
            overloaded = total_activity_hours > capacity or total_intense_hours > INTENSE_DAY_MAX_ACTIVITY_HOURS

            warnings: list[str] = []
            if overloaded:
                warnings.append("Day remains above its activity capacity.")

            result.append(RepairedDay(
                day_number=day_number, activities=tuple(ordered), archetype=archetype,
                total_activity_hours=round(total_activity_hours, 2), total_intense_hours=round(total_intense_hours, 2),
                overloaded=overloaded, warnings=tuple(warnings),
            ))
        return result

    def repair_schedule(
        self, days: Sequence[Mapping[str, Any]], *, archetypes: Mapping[int, DayArchetype] | None = None,
    ) -> ScheduleRepairResult:
        return self.repair(days, archetypes=archetypes)

    def validate(
        self, days: Sequence[Mapping[str, Any]], *, archetypes: Mapping[int, DayArchetype] | None = None,
    ) -> tuple[ScheduleConflict, ...]:
        schedule = parse_schedule(days)
        return tuple(self.validator.validate(schedule, archetypes=archetypes))


def _format_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def scheduled_activity_to_dict(activity: ScheduledActivity) -> dict[str, Any]:
    return {
        "activity_id": activity.activity.activity_id, "name": activity.activity.name,
        "day_number": activity.day_number, "start_time": _format_minutes(activity.start_minutes),
        "end_time": _format_minutes(activity.end_minutes), "start_minutes": activity.start_minutes,
        "end_minutes": activity.end_minutes, "duration_hours": activity.activity.duration_hours,
        "fixed": activity.fixed, "priority": activity.priority,
        "travel_before_minutes": activity.travel_before_minutes, "travel_after_minutes": activity.travel_after_minutes,
        "intensity": activity.activity.intensity.value,
    }


def schedule_repair_result_to_dict(result: ScheduleRepairResult) -> dict[str, Any]:
    return {
        "engine": ScheduleRepairEngine.name, "version": ScheduleRepairEngine.version,
        "days": [
            {
                "day_number": day.day_number, "archetype": (day.archetype.value if day.archetype else None),
                "activities": [scheduled_activity_to_dict(a) for a in day.activities],
                "total_activity_hours": day.total_activity_hours, "total_intense_hours": day.total_intense_hours,
                "overloaded": day.overloaded, "warnings": list(day.warnings),
            }
            for day in result.days
        ],
        "conflicts_found": [
            {"type": c.conflict_type.value, "day_number": c.day_number, "activity_id": c.activity_id,
             "related_activity_id": c.related_activity_id, "severity": c.severity, "message": c.message}
            for c in result.conflicts_found
        ],
        "conflicts_remaining": [
            {"type": c.conflict_type.value, "day_number": c.day_number, "activity_id": c.activity_id,
             "related_activity_id": c.related_activity_id, "severity": c.severity, "message": c.message}
            for c in result.conflicts_remaining
        ],
        "actions": [
            {"type": a.action_type.value, "activity_id": a.activity_id, "from_day": a.from_day, "to_day": a.to_day,
             "from_start_minutes": a.from_start_minutes, "to_start_minutes": a.to_start_minutes,
             "reason": a.reason, "confidence": a.confidence}
            for a in result.actions
        ],
        "repaired": result.repaired, "fully_repaired": result.fully_repaired,
        "warnings": list(result.warnings), "iterations": result.iterations,
    }


def repair_schedule(
    days: Sequence[Mapping[str, Any]], *, archetypes: Mapping[int, DayArchetype] | None = None,
) -> ScheduleRepairResult:
    return ScheduleRepairEngine().repair(days, archetypes=archetypes)


__all__ = [
    "RepairActionType", "ConflictType", "ScheduledActivity", "ScheduleConflict", "RepairAction", "RepairedDay",
    "ScheduleRepairResult", "ScheduleValidator", "ScheduleRepairEngine", "parse_schedule",
    "scheduled_activity_to_dict", "schedule_repair_result_to_dict", "repair_schedule",
]

