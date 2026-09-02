"""
Activity Constraints Engine
===========================

Deterministic constraint extraction and validation for itinerary activities.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

DEFAULT_ACTIVITY_DURATION_HOURS = 1.5
DEFAULT_BUFFER_MINUTES = 15
MAX_NORMAL_ACTIVITY_HOURS_PER_DAY = 8.0
MAX_INTENSE_ACTIVITY_HOURS_PER_DAY = 6.0
DEFAULT_ACTIVITY_GAP_MINUTES = 15
LONG_ACTIVITY_HOURS = 4.0
VERY_LONG_ACTIVITY_HOURS = 6.0
EARLY_MORNING_HOUR = 6
EVENING_HOUR = 18


class ConstraintSeverity(str, Enum):
    HARD = "hard"
    SOFT = "soft"
    INFORMATIONAL = "informational"


class ActivityIntensity(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    EXTREME = "extreme"
    UNKNOWN = "unknown"


class ConstraintType(str, Enum):
    DURATION = "duration"
    TIME_WINDOW = "time_window"
    OPENING_HOURS = "opening_hours"
    EARLIEST_START = "earliest_start"
    LATEST_START = "latest_start"
    LATEST_FINISH = "latest_finish"
    TRAVEL_BUFFER = "travel_buffer"
    PREPARATION_BUFFER = "preparation_buffer"
    RECOVERY_BUFFER = "recovery_buffer"
    BOOKING_REQUIRED = "booking_required"
    FIXED_TIME = "fixed_time"
    AGE_REQUIREMENT = "age_requirement"
    FITNESS_REQUIREMENT = "fitness_requirement"
    ACCESSIBILITY = "accessibility"
    WEATHER_DEPENDENCY = "weather_dependency"
    DAYLIGHT_DEPENDENCY = "daylight_dependency"
    SEASONALITY = "seasonality"
    GEOGRAPHIC = "geographic"
    TRANSPORT = "transport"
    INTENSITY = "intensity"
    ACTIVITY_STACKING = "activity_stacking"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class TimeWindow:
    start_minutes: int
    end_minutes: int

    @property
    def duration_minutes(self) -> int:
        if self.end_minutes >= self.start_minutes:
            return self.end_minutes - self.start_minutes
        return (24 * 60 - self.start_minutes) + self.end_minutes

    def contains(self, minutes: int) -> bool:
        if self.start_minutes <= self.end_minutes:
            return self.start_minutes <= minutes <= self.end_minutes
        return minutes >= self.start_minutes or minutes <= self.end_minutes


@dataclass(frozen=True)
class ActivityConstraint:
    constraint_type: ConstraintType
    severity: ConstraintSeverity
    value: Any
    description: str
    source: str | None = None


@dataclass(frozen=True)
class ActivityProfile:
    activity_id: str
    name: str
    duration_hours: float
    earliest_start_minutes: int | None
    latest_start_minutes: int | None
    latest_finish_minutes: int | None
    fixed_start_minutes: int | None
    opening_windows: tuple[TimeWindow, ...]
    preparation_minutes: int
    buffer_minutes: int
    recovery_minutes: int
    intensity: ActivityIntensity
    booking_required: bool
    daylight_required: bool
    weather_dependent: bool
    seasonal: bool
    min_age: int | None
    max_age: int | None
    fitness_level: str | None
    accessibility: str | None
    geographic_area: str | None
    location_id: str | None
    prerequisites: tuple[str, ...]
    incompatible_with: tuple[str, ...]
    tags: tuple[str, ...]
    flexible: bool
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActivityValidation:
    activity_id: str
    valid: bool
    hard_constraints: tuple[ActivityConstraint, ...]
    soft_constraints: tuple[ActivityConstraint, ...]
    warnings: tuple[str, ...]
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class ActivityConstraintPlan:
    activities: tuple[ActivityProfile, ...]
    validations: tuple[ActivityValidation, ...]
    daily_capacity_hours: float
    intense_capacity_hours: float
    warnings: tuple[str, ...]


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result < 0:
        return default
    return result


def _safe_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "required"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _normalize(value: Any) -> str:
    value = _safe_str(value)
    if not value:
        return ""
    return value.lower().replace("-", "_").replace(" ", "_")


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
    value = str(value).strip()
    if not value:
        return None
    parts = value.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return None
    if hour == 24 and minute == 0:
        return 24 * 60
    if not 0 <= hour <= 23:
        return None
    if not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def _parse_window(value: Any) -> TimeWindow | None:
    if isinstance(value, TimeWindow):
        return value
    if isinstance(value, Mapping):
        start = _parse_time(value.get("start") or value.get("open") or value.get("from"))
        end = _parse_time(value.get("end") or value.get("close") or value.get("to"))
        if start is None or end is None:
            return None
        return TimeWindow(start_minutes=start, end_minutes=end)
    if isinstance(value, str):
        parts = value.split("-")
        if len(parts) != 2:
            return None
        start = _parse_time(parts[0].strip())
        end = _parse_time(parts[1].strip())
        if start is None or end is None:
            return None
        return TimeWindow(start_minutes=start, end_minutes=end)
    return None


def _parse_windows(value: Any) -> tuple[TimeWindow, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        parsed = _parse_window(value)
        return (parsed,) if parsed else ()
    if isinstance(value, str):
        parsed = _parse_window(value)
        return (parsed,) if parsed else ()
    if not isinstance(value, Sequence):
        return ()
    windows = []
    for item in value:
        parsed = _parse_window(item)
        if parsed:
            windows.append(parsed)
    return tuple(windows)


def _parse_string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Sequence):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _extract_intensity(record: Mapping[str, Any]) -> ActivityIntensity:
    value = _normalize(record.get("intensity") or record.get("difficulty") or record.get("fitness_level"))
    mapping = {
        "low": ActivityIntensity.LOW, "easy": ActivityIntensity.LOW, "relaxed": ActivityIntensity.LOW,
        "moderate": ActivityIntensity.MODERATE, "medium": ActivityIntensity.MODERATE,
        "high": ActivityIntensity.HIGH, "hard": ActivityIntensity.HIGH, "difficult": ActivityIntensity.HIGH,
        "extreme": ActivityIntensity.EXTREME, "very_high": ActivityIntensity.EXTREME,
    }
    return mapping.get(value, ActivityIntensity.UNKNOWN)


def normalize_activity(record: Mapping[str, Any], *, index: int = 0) -> ActivityProfile:
    activity_id = _safe_str(
        record.get("id") or record.get("activity_id") or record.get("slug")
        or record.get("name") or f"activity_{index + 1}"
    )
    name = _safe_str(record.get("name") or record.get("title") or record.get("activity_name")) or activity_id

    duration = _safe_float(record.get("duration_hours") or record.get("duration"))
    if duration is None:
        duration_minutes = _safe_float(record.get("duration_minutes"))
        if duration_minutes is not None:
            duration = duration_minutes / 60.0
    if duration is None:
        duration = DEFAULT_ACTIVITY_DURATION_HOURS

    earliest_start = _parse_time(record.get("earliest_start") or record.get("earliest_start_time") or record.get("start_after"))
    latest_start = _parse_time(record.get("latest_start") or record.get("latest_start_time") or record.get("start_before"))
    latest_finish = _parse_time(record.get("latest_finish") or record.get("latest_finish_time"))
    fixed_start = _parse_time(record.get("fixed_start") or record.get("fixed_start_time") or record.get("start_time"))

    preparation_minutes = _safe_int(record.get("preparation_minutes") or record.get("prep_minutes"), 0) or 0
    buffer_minutes = _safe_int(record.get("buffer_minutes") or record.get("arrival_buffer_minutes"), DEFAULT_BUFFER_MINUTES) or DEFAULT_BUFFER_MINUTES
    recovery_minutes = _safe_int(record.get("recovery_minutes") or record.get("recovery_buffer_minutes"), 0) or 0

    booking_required = _safe_bool(record.get("booking_required") or record.get("reservation_required"))
    daylight_required = _safe_bool(record.get("daylight_required") or record.get("requires_daylight"))
    weather_dependent = _safe_bool(record.get("weather_dependent") or record.get("weather_sensitive"))
    seasonal = _safe_bool(record.get("seasonal") or record.get("seasonality"))

    min_age = _safe_int(record.get("min_age") or record.get("minimum_age"))
    max_age = _safe_int(record.get("max_age") or record.get("maximum_age"))
    fitness_level = _safe_str(record.get("fitness_level") or record.get("fitness_requirement"))
    accessibility = _safe_str(record.get("accessibility") or record.get("accessibility_notes"))
    geographic_area = _safe_str(record.get("geographic_area") or record.get("area") or record.get("neighborhood"))
    location_id = _safe_str(record.get("location_id") or record.get("destination_id") or record.get("place_id"))

    tags = _parse_string_list(record.get("tags") or record.get("activity_types") or record.get("categories"))
    prerequisites = _parse_string_list(record.get("prerequisites") or record.get("requires"))
    incompatible_with = _parse_string_list(record.get("incompatible_with") or record.get("incompatible_activities"))

    flexible = not _safe_bool(record.get("fixed_time"))
    if fixed_start is not None:
        flexible = False

    return ActivityProfile(
        activity_id=activity_id, name=name, duration_hours=duration,
        earliest_start_minutes=earliest_start, latest_start_minutes=latest_start,
        latest_finish_minutes=latest_finish, fixed_start_minutes=fixed_start,
        opening_windows=_parse_windows(record.get("opening_hours") or record.get("opening_windows") or record.get("hours")),
        preparation_minutes=preparation_minutes, buffer_minutes=buffer_minutes, recovery_minutes=recovery_minutes,
        intensity=_extract_intensity(record), booking_required=booking_required,
        daylight_required=daylight_required, weather_dependent=weather_dependent, seasonal=seasonal,
        min_age=min_age, max_age=max_age, fitness_level=fitness_level, accessibility=accessibility,
        geographic_area=geographic_area, location_id=location_id, prerequisites=prerequisites,
        incompatible_with=incompatible_with, tags=tags, flexible=flexible, raw=dict(record),
    )


def build_constraints(activity: ActivityProfile) -> tuple[ActivityConstraint, ...]:
    constraints: list[ActivityConstraint] = []

    constraints.append(ActivityConstraint(
        constraint_type=ConstraintType.DURATION, severity=ConstraintSeverity.HARD, value=activity.duration_hours,
        description=f"{activity.name} requires approximately {activity.duration_hours:.2f} hours.",
        source="activity.duration",
    ))

    if activity.earliest_start_minutes is not None:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.EARLIEST_START, severity=ConstraintSeverity.HARD,
            value=activity.earliest_start_minutes,
            description=f"{activity.name} cannot start before {activity.earliest_start_minutes} minutes after midnight.",
            source="activity.earliest_start",
        ))
    if activity.latest_start_minutes is not None:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.LATEST_START, severity=ConstraintSeverity.HARD,
            value=activity.latest_start_minutes,
            description=f"{activity.name} should start no later than {activity.latest_start_minutes} minutes after midnight.",
            source="activity.latest_start",
        ))
    if activity.latest_finish_minutes is not None:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.LATEST_FINISH, severity=ConstraintSeverity.HARD,
            value=activity.latest_finish_minutes,
            description=f"{activity.name} must finish by {activity.latest_finish_minutes} minutes after midnight.",
            source="activity.latest_finish",
        ))
    if activity.fixed_start_minutes is not None:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.FIXED_TIME, severity=ConstraintSeverity.HARD,
            value=activity.fixed_start_minutes, description=f"{activity.name} has a fixed start time.",
            source="activity.fixed_start",
        ))
    if activity.opening_windows:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.OPENING_HOURS, severity=ConstraintSeverity.HARD,
            value=activity.opening_windows, description=f"{activity.name} must occur within its operating hours.",
            source="activity.opening_hours",
        ))
    if activity.preparation_minutes > 0:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.PREPARATION_BUFFER, severity=ConstraintSeverity.HARD,
            value=activity.preparation_minutes,
            description=f"{activity.name} requires {activity.preparation_minutes} minutes of preparation.",
            source="activity.preparation",
        ))
    if activity.buffer_minutes > 0:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.TRAVEL_BUFFER, severity=ConstraintSeverity.SOFT,
            value=activity.buffer_minutes,
            description=f"{activity.name} should have {activity.buffer_minutes} minutes of buffer.",
            source="activity.buffer",
        ))
    if activity.recovery_minutes > 0:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.RECOVERY_BUFFER, severity=ConstraintSeverity.HARD,
            value=activity.recovery_minutes,
            description=f"{activity.name} requires {activity.recovery_minutes} minutes of recovery.",
            source="activity.recovery",
        ))
    if activity.booking_required:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.BOOKING_REQUIRED, severity=ConstraintSeverity.HARD, value=True,
            description=f"{activity.name} requires advance booking.", source="activity.booking",
        ))
    if activity.min_age is not None:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.AGE_REQUIREMENT, severity=ConstraintSeverity.HARD,
            value={"min_age": activity.min_age, "max_age": activity.max_age},
            description=f"{activity.name} has age requirements.", source="activity.age",
        ))
    if activity.fitness_level:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.FITNESS_REQUIREMENT, severity=ConstraintSeverity.HARD,
            value=activity.fitness_level, description=f"{activity.name} has a fitness requirement.",
            source="activity.fitness",
        ))
    if activity.accessibility:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.ACCESSIBILITY, severity=ConstraintSeverity.HARD,
            value=activity.accessibility, description=f"{activity.name} has accessibility requirements.",
            source="activity.accessibility",
        ))
    if activity.weather_dependent:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.WEATHER_DEPENDENCY, severity=ConstraintSeverity.SOFT, value=True,
            description=f"{activity.name} depends on suitable weather.", source="activity.weather",
        ))
    if activity.daylight_required:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.DAYLIGHT_DEPENDENCY, severity=ConstraintSeverity.HARD, value=True,
            description=f"{activity.name} requires daylight.", source="activity.daylight",
        ))
    if activity.seasonal:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.SEASONALITY, severity=ConstraintSeverity.SOFT, value=True,
            description=f"{activity.name} has seasonal availability considerations.", source="activity.seasonality",
        ))
    if activity.geographic_area or activity.location_id:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.GEOGRAPHIC, severity=ConstraintSeverity.SOFT,
            value={"area": activity.geographic_area, "location_id": activity.location_id},
            description=f"{activity.name} has a specific geographic location.", source="activity.location",
        ))
    if activity.intensity != ActivityIntensity.UNKNOWN:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.INTENSITY, severity=ConstraintSeverity.SOFT, value=activity.intensity.value,
            description=f"{activity.name} has {activity.intensity.value} intensity.", source="activity.intensity",
        ))
    if activity.incompatible_with:
        constraints.append(ActivityConstraint(
            constraint_type=ConstraintType.CONFLICT, severity=ConstraintSeverity.HARD, value=activity.incompatible_with,
            description=f"{activity.name} should not be combined with specified incompatible activities.",
            source="activity.incompatible_with",
        ))

    return tuple(constraints)


def validate_activity(activity: ActivityProfile) -> ActivityValidation:
    hard: list[ActivityConstraint] = []
    soft: list[ActivityConstraint] = []
    warnings: list[str] = []
    conflicts: list[str] = []

    constraints = build_constraints(activity)
    for constraint in constraints:
        if constraint.severity == ConstraintSeverity.HARD:
            hard.append(constraint)
        elif constraint.severity == ConstraintSeverity.SOFT:
            soft.append(constraint)

    if activity.duration_hours <= 0:
        conflicts.append("Activity duration must be greater than zero.")
    if activity.duration_hours > VERY_LONG_ACTIVITY_HOURS:
        warnings.append("Activity duration is unusually long.")
    elif activity.duration_hours > LONG_ACTIVITY_HOURS:
        warnings.append("Activity duration is long and may dominate the day.")

    if (activity.earliest_start_minutes is not None and activity.latest_start_minutes is not None
            and activity.earliest_start_minutes > activity.latest_start_minutes):
        conflicts.append("Earliest start is later than latest start.")
    if (activity.fixed_start_minutes is not None and activity.latest_start_minutes is not None
            and activity.fixed_start_minutes > activity.latest_start_minutes):
        conflicts.append("Fixed start occurs after latest allowed start.")
    if (activity.fixed_start_minutes is not None and activity.earliest_start_minutes is not None
            and activity.fixed_start_minutes < activity.earliest_start_minutes):
        conflicts.append("Fixed start occurs before earliest allowed start.")
    if (activity.min_age is not None and activity.max_age is not None and activity.min_age > activity.max_age):
        conflicts.append("Minimum age exceeds maximum age.")

    for window in activity.opening_windows:
        if window.duration_minutes <= 0:
            conflicts.append("Activity contains an invalid opening-hours window.")

    return ActivityValidation(
        activity_id=activity.activity_id, valid=not conflicts, hard_constraints=tuple(hard),
        soft_constraints=tuple(soft), warnings=tuple(warnings), conflicts=tuple(conflicts),
    )


def activities_conflict(first: ActivityProfile, second: ActivityProfile) -> bool:
    first_name = _normalize(first.name)
    second_name = _normalize(second.name)
    first_tags = {_normalize(tag) for tag in first.tags}
    second_tags = {_normalize(tag) for tag in second.tags}
    first_incompatible = {_normalize(value) for value in first.incompatible_with}
    second_incompatible = {_normalize(value) for value in second.incompatible_with}

    if second_name in first_incompatible:
        return True
    if first_name in second_incompatible:
        return True
    if first_tags & second_incompatible:
        return True
    if second_tags & first_incompatible:
        return True
    if (first.fixed_start_minutes is not None and second.fixed_start_minutes is not None
            and first.fixed_start_minutes == second.fixed_start_minutes):
        return True
    return False


def calculate_daily_capacity(activities: Sequence[ActivityProfile]) -> tuple[float, float]:
    normal_hours = MAX_NORMAL_ACTIVITY_HOURS_PER_DAY
    intense_hours = MAX_INTENSE_ACTIVITY_HOURS_PER_DAY
    for activity in activities:
        if activity.intensity in {ActivityIntensity.EXTREME, ActivityIntensity.HIGH}:
            intense_hours -= activity.duration_hours
    intense_hours = max(0.0, intense_hours)
    return normal_hours, intense_hours


class ActivityConstraintsEngine:
    name = "ActivityConstraintsEngine"
    version = "1.0"

    def normalize(self, activity: Mapping[str, Any], *, index: int = 0) -> ActivityProfile:
        return normalize_activity(activity, index=index)

    def constraints(self, activity) -> tuple[ActivityConstraint, ...]:
        profile = activity if isinstance(activity, ActivityProfile) else normalize_activity(activity)
        return build_constraints(profile)

    def validate(self, activity) -> ActivityValidation:
        profile = activity if isinstance(activity, ActivityProfile) else normalize_activity(activity)
        return validate_activity(profile)

    def analyze(self, activities: Sequence) -> ActivityConstraintPlan:
        profiles: list[ActivityProfile] = []
        for index, activity in enumerate(activities):
            if isinstance(activity, ActivityProfile):
                profiles.append(activity)
            elif isinstance(activity, Mapping):
                profiles.append(normalize_activity(activity, index=index))
            else:
                raise TypeError(f"Activity {index + 1} must be a mapping or ActivityProfile.")

        validations = [validate_activity(activity) for activity in profiles]

        warnings: list[str] = []
        for validation in validations:
            warnings.extend(f"{validation.activity_id}: {warning}" for warning in validation.warnings)
            warnings.extend(f"{validation.activity_id}: {conflict}" for conflict in validation.conflicts)

        for index, first in enumerate(profiles):
            for second in profiles[index + 1:]:
                if activities_conflict(first, second):
                    warnings.append(f"{first.name} conflicts with {second.name}.")

        daily_capacity, intense_capacity = calculate_daily_capacity(profiles)
        total_hours = sum(activity.duration_hours for activity in profiles)
        if total_hours > daily_capacity:
            warnings.append("Activity collection exceeds the normal daily activity capacity.")

        return ActivityConstraintPlan(
            activities=tuple(profiles), validations=tuple(validations),
            daily_capacity_hours=daily_capacity, intense_capacity_hours=intense_capacity,
            warnings=tuple(warnings),
        )

    def analyze_activities(self, activities: Sequence) -> ActivityConstraintPlan:
        return self.analyze(activities)


def activity_profile_to_dict(activity: ActivityProfile) -> dict[str, Any]:
    return {
        "activity_id": activity.activity_id, "name": activity.name, "duration_hours": activity.duration_hours,
        "earliest_start_minutes": activity.earliest_start_minutes, "latest_start_minutes": activity.latest_start_minutes,
        "latest_finish_minutes": activity.latest_finish_minutes, "fixed_start_minutes": activity.fixed_start_minutes,
        "opening_windows": [{"start_minutes": w.start_minutes, "end_minutes": w.end_minutes} for w in activity.opening_windows],
        "preparation_minutes": activity.preparation_minutes, "buffer_minutes": activity.buffer_minutes,
        "recovery_minutes": activity.recovery_minutes, "intensity": activity.intensity.value,
        "booking_required": activity.booking_required, "daylight_required": activity.daylight_required,
        "weather_dependent": activity.weather_dependent, "seasonal": activity.seasonal,
        "min_age": activity.min_age, "max_age": activity.max_age, "fitness_level": activity.fitness_level,
        "accessibility": activity.accessibility, "geographic_area": activity.geographic_area,
        "location_id": activity.location_id, "prerequisites": list(activity.prerequisites),
        "incompatible_with": list(activity.incompatible_with), "tags": list(activity.tags), "flexible": activity.flexible,
    }


def activity_constraint_plan_to_dict(plan: ActivityConstraintPlan) -> dict[str, Any]:
    return {
        "engine": ActivityConstraintsEngine.name, "version": ActivityConstraintsEngine.version,
        "activities": [activity_profile_to_dict(a) for a in plan.activities],
        "validations": [
            {
                "activity_id": v.activity_id, "valid": v.valid,
                "hard_constraints": [{"type": c.constraint_type.value, "severity": c.severity.value, "value": c.value, "description": c.description, "source": c.source} for c in v.hard_constraints],
                "soft_constraints": [{"type": c.constraint_type.value, "severity": c.severity.value, "value": c.value, "description": c.description, "source": c.source} for c in v.soft_constraints],
                "warnings": list(v.warnings), "conflicts": list(v.conflicts),
            }
            for v in plan.validations
        ],
        "daily_capacity_hours": plan.daily_capacity_hours, "intense_capacity_hours": plan.intense_capacity_hours,
        "warnings": list(plan.warnings),
    }


def analyze_activity_constraints(activities: Sequence) -> ActivityConstraintPlan:
    return ActivityConstraintsEngine().analyze(activities)


__all__ = [
    "ConstraintSeverity", "ActivityIntensity", "ConstraintType", "TimeWindow", "ActivityConstraint",
    "ActivityProfile", "ActivityValidation", "ActivityConstraintPlan", "ActivityConstraintsEngine",
    "normalize_activity", "build_constraints", "validate_activity", "activities_conflict",
    "calculate_daily_capacity", "activity_profile_to_dict", "activity_constraint_plan_to_dict",
    "analyze_activity_constraints",
]

