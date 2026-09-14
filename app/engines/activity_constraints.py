"""
Activity Constraints Engine
===========================

Deterministic constraint extraction and validation for itinerary activities.

Design principles
-----------------
- Activity facts come from authoritative input records.
- Missing duration is NOT silently converted into a fabricated duration.
- Zero values are preserved.
- Time parsing distinguishes hour values from minute values.
- Constraints describe facts; they do not invent scheduling facts.
- Actual schedule overlap is validated only when actual schedule times exist.
- Capacity calculations distinguish known activity duration from unknown duration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

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
            return (
                self.end_minutes
                - self.start_minutes
            )

        return (
            (24 * 60 - self.start_minutes)
            + self.end_minutes
        )

    def contains(self, minutes: int) -> bool:
        if self.start_minutes <= self.end_minutes:
            return (
                self.start_minutes
                <= minutes
                <= self.end_minutes
            )

        return (
            minutes >= self.start_minutes
            or minutes <= self.end_minutes
        )


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

    # Kept as float for compatibility with existing callers.
    # duration_known tells consumers whether this is an actual fact.
    duration_hours: float
    duration_known: bool = True

    earliest_start_minutes: int | None = None
    latest_start_minutes: int | None = None
    latest_finish_minutes: int | None = None
    fixed_start_minutes: int | None = None

    opening_windows: tuple[TimeWindow, ...] = ()

    preparation_minutes: int = 0
    buffer_minutes: int = DEFAULT_BUFFER_MINUTES
    recovery_minutes: int = 0

    intensity: ActivityIntensity = ActivityIntensity.UNKNOWN

    booking_required: bool = False
    daylight_required: bool = False
    weather_dependent: bool = False
    seasonal: bool = False

    min_age: int | None = None
    max_age: int | None = None

    fitness_level: str | None = None
    accessibility: str | None = None

    geographic_area: str | None = None
    location_id: str | None = None

    prerequisites: tuple[str, ...] = ()
    incompatible_with: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    flexible: bool = True

    raw: Mapping[str, Any] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class ActivityValidation:
    activity_id: str
    valid: bool
    hard_constraints: tuple[
        ActivityConstraint, ...
    ]
    soft_constraints: tuple[
        ActivityConstraint, ...
    ]
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


def _safe_float(
    value: Any,
    default: float | None = None,
) -> float | None:

    if value is None:
        return default

    try:
        result = float(value)
    except (TypeError, ValueError):
        return default

    if result < 0:
        return default

    return result


def _safe_int(
    value: Any,
    default: int | None = None,
) -> int | None:

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
        return value.strip().lower() in {
            "true",
            "1",
            "yes",
            "y",
            "required",
            "on",
        }

    if isinstance(value, (int, float)):
        return value != 0

    return False


def _normalize(value: Any) -> str:
    value = _safe_str(value)

    if not value:
        return ""

    return (
        value.lower()
        .replace("-", "_")
        .replace(" ", "_")
    )


def _first_present(
    record: Mapping[str, Any],
    keys: Sequence[str],
) -> Any:

    for key in keys:
        if key in record and record[key] is not None:
            return record[key]

    return None


def _parse_time(value: Any) -> int | None:
    """
    Parse time deterministically.

    Numeric values:
      - 0..24 are interpreted as hours.
      - >24..1440 are interpreted as minutes.

    Strings:
      - HH:MM
      - H:MM
      - numeric strings follow the same numeric rule.
    """

    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        numeric = float(value)

        if numeric < 0:
            return None

        if numeric <= 24:
            return int(numeric * 60)

        if numeric <= 24 * 60:
            return int(numeric)

        return None

    text = str(value).strip()

    if not text:
        return None

    if ":" not in text:
        try:
            numeric = float(text)
        except ValueError:
            return None

        if numeric < 0:
            return None

        if numeric <= 24:
            return int(numeric * 60)

        if numeric <= 24 * 60:
            return int(numeric)

        return None

    parts = text.split(":")

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
        start = _parse_time(
            _first_present(
                value,
                (
                    "start",
                    "open",
                    "from",
                ),
            )
        )

        end = _parse_time(
            _first_present(
                value,
                (
                    "end",
                    "close",
                    "to",
                ),
            )
        )

        if start is None or end is None:
            return None

        return TimeWindow(
            start_minutes=start,
            end_minutes=end,
        )

    if isinstance(value, str):
        parts = value.split("-")

        if len(parts) != 2:
            return None

        start = _parse_time(parts[0].strip())
        end = _parse_time(parts[1].strip())

        if start is None or end is None:
            return None

        return TimeWindow(
            start_minutes=start,
            end_minutes=end,
        )

    return None


def _parse_windows(
    value: Any,
) -> tuple[TimeWindow, ...]:

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

    windows: list[TimeWindow] = []

    for item in value:
        parsed = _parse_window(item)

        if parsed:
            windows.append(parsed)

    return tuple(windows)


def _parse_string_list(
    value: Any,
) -> tuple[str, ...]:

    if value is None:
        return ()

    if isinstance(value, str):
        value = value.strip()
        return (value,) if value else ()

    if isinstance(value, Sequence):
        result: list[str] = []

        for item in value:
            text = str(item).strip()

            if text:
                result.append(text)

        return tuple(result)

    return ()


def _extract_intensity(
    record: Mapping[str, Any],
) -> ActivityIntensity:

    value = _normalize(
        _first_present(
            record,
            (
                "intensity",
                "difficulty",
                "fitness_level",
            ),
        )
    )

    mapping = {
        "low": ActivityIntensity.LOW,
        "easy": ActivityIntensity.LOW,
        "relaxed": ActivityIntensity.LOW,

        "moderate": ActivityIntensity.MODERATE,
        "medium": ActivityIntensity.MODERATE,

        "high": ActivityIntensity.HIGH,
        "hard": ActivityIntensity.HIGH,
        "difficult": ActivityIntensity.HIGH,

        "extreme": ActivityIntensity.EXTREME,
        "very_high": ActivityIntensity.EXTREME,
    }

    return mapping.get(
        value,
        ActivityIntensity.UNKNOWN,
    )


def normalize_activity(
    record: Mapping[str, Any],
    *,
    index: int = 0,
) -> ActivityProfile:

    if not isinstance(record, Mapping):
        raise TypeError(
            "Activity record must be a mapping."
        )

    activity_id = _safe_str(
        _first_present(
            record,
            (
                "id",
                "activity_id",
                "slug",
                "name",
            ),
        )
    )

    if not activity_id:
        activity_id = f"activity_{index + 1}"

    name = (
        _safe_str(
            _first_present(
                record,
                (
                    "name",
                    "title",
                    "activity_name",
                ),
            )
        )
        or activity_id
    )

    # ---------------------------------------------------------
    # Duration
    # ---------------------------------------------------------

    duration_value = _first_present(
        record,
        (
            "duration_hours",
            "duration",
        ),
    )

    duration = _safe_float(
        duration_value,
        None,
    )

    if duration is not None:
        duration_known = True
    else:
        duration_minutes = _safe_float(
            record.get("duration_minutes"),
            None,
        )

        if duration_minutes is not None:
            duration = duration_minutes / 60.0
            duration_known = True
        else:
            # No fabricated duration.
            duration = 0.0
            duration_known = False

    earliest_start = _parse_time(
        _first_present(
            record,
            (
                "earliest_start",
                "earliest_start_time",
                "start_after",
            ),
        )
    )

    latest_start = _parse_time(
        _first_present(
            record,
            (
                "latest_start",
                "latest_start_time",
                "start_before",
            ),
        )
    )

    latest_finish = _parse_time(
        _first_present(
            record,
            (
                "latest_finish",
                "latest_finish_time",
            ),
        )
    )

    fixed_start = _parse_time(
        _first_present(
            record,
            (
                "fixed_start",
                "fixed_start_time",
                "start_time",
            ),
        )
    )

    preparation_minutes = (
        _safe_int(
            _first_present(
                record,
                (
                    "preparation_minutes",
                    "prep_minutes",
                ),
            ),
            0,
        )
        or 0
    )

    buffer_value = _first_present(
        record,
        (
            "buffer_minutes",
            "arrival_buffer_minutes",
        ),
    )

    buffer_minutes = (
        _safe_int(
            buffer_value,
            DEFAULT_BUFFER_MINUTES,
        )
        if buffer_value is not None
        else DEFAULT_BUFFER_MINUTES
    )

    if buffer_minutes is None:
        buffer_minutes = DEFAULT_BUFFER_MINUTES

    recovery_minutes = (
        _safe_int(
            _first_present(
                record,
                (
                    "recovery_minutes",
                    "recovery_buffer_minutes",
                ),
            ),
            0,
        )
        or 0
    )

    booking_required = _safe_bool(
        _first_present(
            record,
            (
                "booking_required",
                "reservation_required",
            ),
        )
    )

    daylight_required = _safe_bool(
        _first_present(
            record,
            (
                "daylight_required",
                "requires_daylight",
            ),
        )
    )

    weather_dependent = _safe_bool(
        _first_present(
            record,
            (
                "weather_dependent",
                "weather_sensitive",
            ),
        )
    )

    seasonal = _safe_bool(
        _first_present(
            record,
            (
                "seasonal",
                "seasonality",
            ),
        )
    )

    min_age = _safe_int(
        _first_present(
            record,
            (
                "min_age",
                "minimum_age",
            ),
        )
    )

    max_age = _safe_int(
        _first_present(
            record,
            (
                "max_age",
                "maximum_age",
            ),
        )
    )

    fitness_level = _safe_str(
        _first_present(
            record,
            (
                "fitness_level",
                "fitness_requirement",
            ),
        )
    )

    accessibility = _safe_str(
        _first_present(
            record,
            (
                "accessibility",
                "accessibility_notes",
            ),
        )
    )

    geographic_area = _safe_str(
        _first_present(
            record,
            (
                "geographic_area",
                "area",
                "neighborhood",
            ),
        )
    )

    location_id = _safe_str(
        _first_present(
            record,
            (
                "location_id",
                "destination_id",
                "place_id",
            ),
        )
    )

    tags = _parse_string_list(
        _first_present(
            record,
            (
                "tags",
                "activity_types",
                "categories",
            ),
        )
    )

    prerequisites = _parse_string_list(
        _first_present(
            record,
            (
                "prerequisites",
                "requires",
            ),
        )
    )

    incompatible_with = _parse_string_list(
        _first_present(
            record,
            (
                "incompatible_with",
                "incompatible_activities",
            ),
        )
    )

    flexible = not _safe_bool(
        record.get("fixed_time")
    )

    if fixed_start is not None:
        flexible = False

    return ActivityProfile(
        activity_id=activity_id,
        name=name,
        duration_hours=duration,
        duration_known=duration_known,
        earliest_start_minutes=earliest_start,
        latest_start_minutes=latest_start,
        latest_finish_minutes=latest_finish,
        fixed_start_minutes=fixed_start,
        opening_windows=_parse_windows(
            _first_present(
                record,
                (
                    "opening_hours",
                    "opening_windows",
                    "hours",
                ),
            )
        ),
        preparation_minutes=preparation_minutes,
        buffer_minutes=buffer_minutes,
        recovery_minutes=recovery_minutes,
        intensity=_extract_intensity(record),
        booking_required=booking_required,
        daylight_required=daylight_required,
        weather_dependent=weather_dependent,
        seasonal=seasonal,
        min_age=min_age,
        max_age=max_age,
        fitness_level=fitness_level,
        accessibility=accessibility,
        geographic_area=geographic_area,
        location_id=location_id,
        prerequisites=prerequisites,
        incompatible_with=incompatible_with,
        tags=tags,
        flexible=flexible,
        raw=dict(record),
    )


def build_constraints(
    activity: ActivityProfile,
) -> tuple[ActivityConstraint, ...]:

    constraints: list[ActivityConstraint] = []

    if activity.duration_known:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.DURATION,
                severity=ConstraintSeverity.HARD,
                value=activity.duration_hours,
                description=(
                    f"{activity.name} requires approximately "
                    f"{activity.duration_hours:.2f} hours."
                ),
                source="activity.duration",
            )
        )

    if activity.earliest_start_minutes is not None:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.EARLIEST_START,
                severity=ConstraintSeverity.HARD,
                value=activity.earliest_start_minutes,
                description=(
                    f"{activity.name} cannot start before "
                    f"{activity.earliest_start_minutes} minutes after midnight."
                ),
                source="activity.earliest_start",
            )
        )

    if activity.latest_start_minutes is not None:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.LATEST_START,
                severity=ConstraintSeverity.HARD,
                value=activity.latest_start_minutes,
                description=(
                    f"{activity.name} should start no later than "
                    f"{activity.latest_start_minutes} minutes after midnight."
                ),
                source="activity.latest_start",
            )
        )

    if activity.latest_finish_minutes is not None:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.LATEST_FINISH,
                severity=ConstraintSeverity.HARD,
                value=activity.latest_finish_minutes,
                description=(
                    f"{activity.name} must finish by "
                    f"{activity.latest_finish_minutes} minutes after midnight."
                ),
                source="activity.latest_finish",
            )
        )

    if activity.fixed_start_minutes is not None:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.FIXED_TIME,
                severity=ConstraintSeverity.HARD,
                value=activity.fixed_start_minutes,
                description=(
                    f"{activity.name} has a fixed start time."
                ),
                source="activity.fixed_start",
            )
        )

    if activity.opening_windows:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.OPENING_HOURS,
                severity=ConstraintSeverity.HARD,
                value=activity.opening_windows,
                description=(
                    f"{activity.name} must occur within "
                    "its operating hours."
                ),
                source="activity.opening_hours",
            )
        )

    if activity.preparation_minutes > 0:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.PREPARATION_BUFFER,
                severity=ConstraintSeverity.HARD,
                value=activity.preparation_minutes,
                description=(
                    f"{activity.name} requires "
                    f"{activity.preparation_minutes} minutes of preparation."
                ),
                source="activity.preparation",
            )
        )

    if activity.buffer_minutes > 0:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.TRAVEL_BUFFER,
                severity=ConstraintSeverity.SOFT,
                value=activity.buffer_minutes,
                description=(
                    f"{activity.name} should have "
                    f"{activity.buffer_minutes} minutes of buffer."
                ),
                source="activity.buffer",
            )
        )

    if activity.recovery_minutes > 0:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.RECOVERY_BUFFER,
                severity=ConstraintSeverity.HARD,
                value=activity.recovery_minutes,
                description=(
                    f"{activity.name} requires "
                    f"{activity.recovery_minutes} minutes of recovery."
                ),
                source="activity.recovery",
            )
        )

    if activity.booking_required:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.BOOKING_REQUIRED,
                severity=ConstraintSeverity.HARD,
                value=True,
                description=(
                    f"{activity.name} requires advance booking."
                ),
                source="activity.booking",
            )
        )

    if (
        activity.min_age is not None
        or activity.max_age is not None
    ):
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.AGE_REQUIREMENT,
                severity=ConstraintSeverity.HARD,
                value={
                    "min_age": activity.min_age,
                    "max_age": activity.max_age,
                },
                description=(
                    f"{activity.name} has age requirements."
                ),
                source="activity.age",
            )
        )

    if activity.fitness_level:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.FITNESS_REQUIREMENT,
                severity=ConstraintSeverity.HARD,
                value=activity.fitness_level,
                description=(
                    f"{activity.name} has a fitness requirement."
                ),
                source="activity.fitness",
            )
        )

    if activity.accessibility:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.ACCESSIBILITY,
                severity=ConstraintSeverity.HARD,
                value=activity.accessibility,
                description=(
                    f"{activity.name} has accessibility requirements."
                ),
                source="activity.accessibility",
            )
        )

    if activity.weather_dependent:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.WEATHER_DEPENDENCY,
                severity=ConstraintSeverity.SOFT,
                value=True,
                description=(
                    f"{activity.name} depends on suitable weather."
                ),
                source="activity.weather",
            )
        )

    if activity.daylight_required:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.DAYLIGHT_DEPENDENCY,
                severity=ConstraintSeverity.HARD,
                value=True,
                description=(
                    f"{activity.name} requires daylight."
                ),
                source="activity.daylight",
            )
        )

    if activity.seasonal:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.SEASONALITY,
                severity=ConstraintSeverity.SOFT,
                value=True,
                description=(
                    f"{activity.name} has seasonal availability considerations."
                ),
                source="activity.seasonality",
            )
        )

    if activity.geographic_area or activity.location_id:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.GEOGRAPHIC,
                severity=ConstraintSeverity.SOFT,
                value={
                    "area": activity.geographic_area,
                    "location_id": activity.location_id,
                },
                description=(
                    f"{activity.name} has a specific geographic location."
                ),
                source="activity.location",
            )
        )

    if activity.intensity != ActivityIntensity.UNKNOWN:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.INTENSITY,
                severity=ConstraintSeverity.SOFT,
                value=activity.intensity.value,
                description=(
                    f"{activity.name} has "
                    f"{activity.intensity.value} intensity."
                ),
                source="activity.intensity",
            )
        )

    if activity.incompatible_with:
        constraints.append(
            ActivityConstraint(
                constraint_type=ConstraintType.CONFLICT,
                severity=ConstraintSeverity.HARD,
                value=activity.incompatible_with,
                description=(
                    f"{activity.name} should not be combined "
                    "with specified incompatible activities."
                ),
                source="activity.incompatible_with",
            )
        )

    return tuple(constraints)


def validate_activity(
    activity: ActivityProfile,
) -> ActivityValidation:

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

    if not activity.duration_known:
        warnings.append(
            "Activity duration is unavailable; no duration was invented."
        )

    elif activity.duration_hours <= 0:
        conflicts.append(
            "Activity duration must be greater than zero."
        )

    elif activity.duration_hours > VERY_LONG_ACTIVITY_HOURS:
        warnings.append(
            "Activity duration is unusually long."
        )

    elif activity.duration_hours > LONG_ACTIVITY_HOURS:
        warnings.append(
            "Activity duration is long and may dominate the day."
        )

    if (
        activity.earliest_start_minutes is not None
        and activity.latest_start_minutes is not None
        and activity.earliest_start_minutes
        > activity.latest_start_minutes
    ):
        conflicts.append(
            "Earliest start is later than latest start."
        )

    if (
        activity.fixed_start_minutes is not None
        and activity.latest_start_minutes is not None
        and activity.fixed_start_minutes
        > activity.latest_start_minutes
    ):
        conflicts.append(
            "Fixed start occurs after latest allowed start."
        )

    if (
        activity.fixed_start_minutes is not None
        and activity.earliest_start_minutes is not None
        and activity.fixed_start_minutes
        < activity.earliest_start_minutes
    ):
        conflicts.append(
            "Fixed start occurs before earliest allowed start."
        )

    if (
        activity.min_age is not None
        and activity.max_age is not None
        and activity.min_age > activity.max_age
    ):
        conflicts.append(
            "Minimum age exceeds maximum age."
        )

    for window in activity.opening_windows:
        if window.duration_minutes <= 0:
            conflicts.append(
                "Activity contains an invalid opening-hours window."
            )

    return ActivityValidation(
        activity_id=activity.activity_id,
        valid=not conflicts,
        hard_constraints=tuple(hard),
        soft_constraints=tuple(soft),
        warnings=tuple(warnings),
        conflicts=tuple(conflicts),
    )


def _activity_names_match(
    value: str,
    activity: ActivityProfile,
) -> bool:

    normalized = _normalize(value)

    if not normalized:
        return False

    if normalized == _normalize(activity.name):
        return True

    if normalized == _normalize(activity.activity_id):
        return True

    return normalized in {
        _normalize(tag)
        for tag in activity.tags
    }


def activities_conflict(
    first: ActivityProfile,
    second: ActivityProfile,
) -> bool:

    first_name = _normalize(first.name)
    second_name = _normalize(second.name)

    first_id = _normalize(first.activity_id)
    second_id = _normalize(second.activity_id)

    first_tags = {
        _normalize(tag)
        for tag in first.tags
    }

    second_tags = {
        _normalize(tag)
        for tag in second.tags
    }

    first_incompatible = {
        _normalize(value)
        for value in first.incompatible_with
    }

    second_incompatible = {
        _normalize(value)
        for value in second.incompatible_with
    }

    if (
        second_name in first_incompatible
        or second_id in first_incompatible
    ):
        return True

    if (
        first_name in second_incompatible
        or first_id in second_incompatible
    ):
        return True

    if first_tags & second_incompatible:
        return True

    if second_tags & first_incompatible:
        return True

    if (
        first.fixed_start_minutes is not None
        and second.fixed_start_minutes is not None
        and first.fixed_start_minutes
        == second.fixed_start_minutes
    ):
        return True

    return False


def _activity_interval(
    activity: ActivityProfile,
) -> tuple[int, int] | None:

    if activity.fixed_start_minutes is None:
        return None

    if not activity.duration_known:
        return None

    duration_minutes = int(
        round(
            activity.duration_hours * 60
        )
    )

    if duration_minutes <= 0:
        return None

    start = activity.fixed_start_minutes

    end = start + duration_minutes

    return start, end


def _fixed_time_overlap(
    first: ActivityProfile,
    second: ActivityProfile,
) -> bool:

    first_interval = _activity_interval(first)
    second_interval = _activity_interval(second)

    if first_interval is None or second_interval is None:
        return False

    first_start, first_end = first_interval
    second_start, second_end = second_interval

    # Adjacent activities do not overlap.
    return (
        first_start < second_end
        and second_start < first_end
    )


def calculate_daily_capacity(
    activities: Sequence[ActivityProfile],
) -> tuple[float, float]:

    normal_hours = MAX_NORMAL_ACTIVITY_HOURS_PER_DAY
    intense_hours = MAX_INTENSE_ACTIVITY_HOURS_PER_DAY

    for activity in activities:

        if not activity.duration_known:
            continue

        if activity.intensity in {
            ActivityIntensity.EXTREME,
            ActivityIntensity.HIGH,
        }:
            intense_hours -= activity.duration_hours

    intense_hours = max(
        0.0,
        intense_hours,
    )

    return normal_hours, intense_hours


def _known_duration_hours(
    activities: Sequence[ActivityProfile],
) -> float:

    return sum(
        activity.duration_hours
        for activity in activities
        if activity.duration_known
    )


class ActivityConstraintsEngine:
    name = "ActivityConstraintsEngine"
    version = "2.0"

    def normalize(
        self,
        activity: Mapping[str, Any],
        *,
        index: int = 0,
    ) -> ActivityProfile:

        return normalize_activity(
            activity,
            index=index,
        )

    def constraints(
        self,
        activity,
    ) -> tuple[ActivityConstraint, ...]:

        profile = (
            activity
            if isinstance(activity, ActivityProfile)
            else normalize_activity(activity)
        )

        return build_constraints(profile)

    def validate(
        self,
        activity,
    ) -> ActivityValidation:

        profile = (
            activity
            if isinstance(activity, ActivityProfile)
            else normalize_activity(activity)
        )

        return validate_activity(profile)

    def analyze(
        self,
        activities: Sequence,
    ) -> ActivityConstraintPlan:

        if activities is None:
            raise ValueError(
                "activities cannot be None"
            )

        profiles: list[ActivityProfile] = []

        for index, activity in enumerate(
            activities
        ):
            if isinstance(
                activity,
                ActivityProfile,
            ):
                profiles.append(activity)

            elif isinstance(
                activity,
                Mapping,
            ):
                profiles.append(
                    normalize_activity(
                        activity,
                        index=index,
                    )
                )

            else:
                raise TypeError(
                    f"Activity {index + 1} must be "
                    "a mapping or ActivityProfile."
                )

        validations = [
            validate_activity(activity)
            for activity in profiles
        ]

        warnings: list[str] = []

        for validation in validations:
            warnings.extend(
                f"{validation.activity_id}: {warning}"
                for warning in validation.warnings
            )

            warnings.extend(
                f"{validation.activity_id}: {conflict}"
                for conflict in validation.conflicts
            )

        # Explicit incompatibilities.
        for index, first in enumerate(profiles):
            for second in profiles[index + 1:]:
                if activities_conflict(
                    first,
                    second,
                ):
                    warnings.append(
                        f"{first.name} conflicts with "
                        f"{second.name}."
                    )

        # Actual fixed-time overlap.
        for index, first in enumerate(profiles):
            for second in profiles[index + 1:]:
                if _fixed_time_overlap(
                    first,
                    second,
                ):
                    warnings.append(
                        f"{first.name} overlaps with "
                        f"{second.name} at their scheduled times."
                    )

        daily_capacity, intense_capacity = (
            calculate_daily_capacity(profiles)
        )

        known_hours = _known_duration_hours(
            profiles
        )

        if known_hours > daily_capacity:
            warnings.append(
                "Known activity duration exceeds the "
                "normal daily activity capacity."
            )

        unknown_count = sum(
            1
            for activity in profiles
            if not activity.duration_known
        )

        if unknown_count:
            warnings.append(
                f"{unknown_count} activity duration(s) "
                "are unavailable and were excluded from "
                "capacity arithmetic."
            )

        return ActivityConstraintPlan(
            activities=tuple(profiles),
            validations=tuple(validations),
            daily_capacity_hours=daily_capacity,
            intense_capacity_hours=intense_capacity,
            warnings=tuple(warnings),
        )

    def analyze_activities(
        self,
        activities: Sequence,
    ) -> ActivityConstraintPlan:

        return self.analyze(activities)


def activity_profile_to_dict(
    activity: ActivityProfile,
) -> dict[str, Any]:

    return {
        "activity_id": activity.activity_id,
        "name": activity.name,
        "duration_hours": activity.duration_hours,
        "duration_known": activity.duration_known,
        "earliest_start_minutes": activity.earliest_start_minutes,
        "latest_start_minutes": activity.latest_start_minutes,
        "latest_finish_minutes": activity.latest_finish_minutes,
        "fixed_start_minutes": activity.fixed_start_minutes,
        "opening_windows": [
            {
                "start_minutes": window.start_minutes,
                "end_minutes": window.end_minutes,
            }
            for window in activity.opening_windows
        ],
        "preparation_minutes": activity.preparation_minutes,
        "buffer_minutes": activity.buffer_minutes,
        "recovery_minutes": activity.recovery_minutes,
        "intensity": activity.intensity.value,
        "booking_required": activity.booking_required,
        "daylight_required": activity.daylight_required,
        "weather_dependent": activity.weather_dependent,
        "seasonal": activity.seasonal,
        "min_age": activity.min_age,
        "max_age": activity.max_age,
        "fitness_level": activity.fitness_level,
        "accessibility": activity.accessibility,
        "geographic_area": activity.geographic_area,
        "location_id": activity.location_id,
        "prerequisites": list(
            activity.prerequisites
        ),
        "incompatible_with": list(
            activity.incompatible_with
        ),
        "tags": list(activity.tags),
        "flexible": activity.flexible,
    }


def activity_constraint_plan_to_dict(
    plan: ActivityConstraintPlan,
) -> dict[str, Any]:

    return {
        "engine": ActivityConstraintsEngine.name,
        "version": ActivityConstraintsEngine.version,
        "activities": [
            activity_profile_to_dict(activity)
            for activity in plan.activities
        ],
        "validations": [
            {
                "activity_id": validation.activity_id,
                "valid": validation.valid,
                "hard_constraints": [
                    {
                        "type": constraint.constraint_type.value,
                        "severity": constraint.severity.value,
                        "value": constraint.value,
                        "description": constraint.description,
                        "source": constraint.source,
                    }
                    for constraint
                    in validation.hard_constraints
                ],
                "soft_constraints": [
                    {
                        "type": constraint.constraint_type.value,
                        "severity": constraint.severity.value,
                        "value": constraint.value,
                        "description": constraint.description,
                        "source": constraint.source,
                    }
                    for constraint
                    in validation.soft_constraints
                ],
                "warnings": list(
                    validation.warnings
                ),
                "conflicts": list(
                    validation.conflicts
                ),
            }
            for validation in plan.validations
        ],
        "daily_capacity_hours": (
            plan.daily_capacity_hours
        ),
        "intense_capacity_hours": (
            plan.intense_capacity_hours
        ),
        "warnings": list(plan.warnings),
    }


def analyze_activity_constraints(
    activities: Sequence,
) -> ActivityConstraintPlan:

    return ActivityConstraintsEngine().analyze(
        activities
    )


__all__ = [
    "ConstraintSeverity",
    "ActivityIntensity",
    "ConstraintType",
    "TimeWindow",
    "ActivityConstraint",
    "ActivityProfile",
    "ActivityValidation",
    "ActivityConstraintPlan",
    "ActivityConstraintsEngine",
    "normalize_activity",
    "build_constraints",
    "validate_activity",
    "activities_conflict",
    "calculate_daily_capacity",
    "activity_profile_to_dict",
    "activity_constraint_plan_to_dict",
    "analyze_activity_constraints",
]
