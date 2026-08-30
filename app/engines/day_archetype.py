"""
Day Archetype Engine
====================

Deterministic classification of itinerary days.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

DEFAULT_DAY_START_HOUR = 7
DEFAULT_DAY_END_HOUR = 21
MIN_FULL_DAY_ACTIVITY_HOURS = 4.0
MAX_NORMAL_TRAVEL_HOURS = 4.0
LONG_TRAVEL_HOURS = 6.0
VERY_LONG_TRAVEL_HOURS = 9.0
MIN_RECOVERY_DAY_AFTER_LONG_TRANSFER_HOURS = 6.0
EARLY_START_HOUR = 6
LATE_START_HOUR = 10


class DayArchetype(str, Enum):
    ARRIVAL = "arrival"
    DEPARTURE = "departure"
    TRANSFER = "transfer"
    LONG_TRANSFER = "long_transfer"
    SAFARI = "safari"
    WILDLIFE = "wildlife"
    BEACH = "beach"
    RELAXATION = "relaxation"
    EXPLORATION = "exploration"
    CITY_EXPLORATION = "city_exploration"
    CULTURAL = "cultural"
    ADVENTURE = "adventure"
    NATURE = "nature"
    RECOVERY = "recovery"
    FREE = "free"
    MIXED = "mixed"
    OVERNIGHT_TRANSITION = "overnight_transition"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DaySignals:
    day_number: int
    is_first_day: bool = False
    is_last_day: bool = False
    has_arrival: bool = False
    has_departure: bool = False
    has_transfer: bool = False
    travel_hours: float = 0.0
    travel_distance_km: float = 0.0
    crosses_country: bool = False
    border_crossing: bool = False
    destination_type: str | None = None
    has_safari: bool = False
    has_wildlife: bool = False
    has_beach: bool = False
    has_city: bool = False
    has_cultural: bool = False
    has_adventure: bool = False
    has_nature: bool = False
    has_relaxation: bool = False
    activity_count: int = 0
    early_start_required: bool = False
    late_finish_expected: bool = False
    previous_day_travel_hours: float = 0.0
    explicit_archetype: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DayArchetypeResult:
    day_number: int
    archetype: DayArchetype
    confidence: float
    travel_hours: float
    travel_distance_km: float
    activity_capacity: str
    recommended_pace: str
    early_start: bool
    late_finish: bool
    recovery_recommended: bool
    dominant_signals: tuple[str, ...]
    warnings: tuple[str, ...]
    planning_notes: tuple[str, ...]
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DayArchetypePlan:
    days: tuple[DayArchetypeResult, ...]
    archetype_counts: Mapping[str, int]
    warnings: tuple[str, ...]
    generated_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def requires_repair(self) -> bool:
        return any(
            day.recovery_recommended
            or day.archetype in {DayArchetype.LONG_TRANSFER, DayArchetype.OVERNIGHT_TRANSITION}
            for day in self.days
        )


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "on"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result >= 0 else default


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _normalize(value: Any) -> str:
    value = _safe_str(value)
    if not value:
        return ""
    return value.lower().replace("-", "_").replace(" ", "_")


def _contains_any(value: str, terms: Sequence[str]) -> bool:
    return any(term in value for term in terms)


def _extract_destination_type(record: Mapping[str, Any]) -> str | None:
    return _safe_str(
        record.get("destination_type") or record.get("type")
        or record.get("category") or record.get("experience_type")
    )


def _extract_activity_flags(record: Mapping[str, Any]) -> dict[str, bool]:
    flags = {
        "has_safari": _safe_bool(record.get("has_safari")),
        "has_wildlife": _safe_bool(record.get("has_wildlife")),
        "has_beach": _safe_bool(record.get("has_beach")),
        "has_city": _safe_bool(record.get("has_city")),
        "has_cultural": _safe_bool(record.get("has_cultural")),
        "has_adventure": _safe_bool(record.get("has_adventure")),
        "has_nature": _safe_bool(record.get("has_nature")),
        "has_relaxation": _safe_bool(record.get("has_relaxation")),
    }

    text_parts = []
    for key in ("destination_type", "type", "category", "theme", "description", "activities", "activity_types"):
        value = record.get(key)
        if isinstance(value, (list, tuple)):
            text_parts.extend(str(item) for item in value)
        elif value is not None:
            text_parts.append(str(value))

    text = " ".join(text_parts).lower()

    if _contains_any(text, ("safari", "game_drive", "wildlife")):
        flags["has_safari"] = True
        flags["has_wildlife"] = True
    if _contains_any(text, ("beach", "coast", "island", "sea")):
        flags["has_beach"] = True
    if _contains_any(text, ("city", "urban")):
        flags["has_city"] = True
    if _contains_any(text, ("culture", "cultural", "heritage", "historical")):
        flags["has_cultural"] = True
    if _contains_any(text, ("hiking", "trekking", "climbing", "adventure")):
        flags["has_adventure"] = True
    if _contains_any(text, ("nature", "forest", "waterfall", "landscape")):
        flags["has_nature"] = True
    if _contains_any(text, ("relax", "wellness", "spa", "leisure")):
        flags["has_relaxation"] = True

    return flags


def _signals_from_record(
    record: Mapping[str, Any], day_number: int, total_days: int, previous_travel_hours: float,
) -> DaySignals:
    travel_hours = _safe_float(
        record.get("travel_hours") or record.get("total_travel_hours") or record.get("transfer_hours")
    )
    travel_distance_km = _safe_float(
        record.get("travel_distance_km") or record.get("distance_km") or record.get("transfer_distance_km")
    )

    activities = record.get("activities")
    if isinstance(activities, Sequence) and not isinstance(activities, (str, bytes)):
        activity_count = len(activities)
    else:
        activity_count = _safe_int(record.get("activity_count"), 0)

    flags = _extract_activity_flags(record)

    arrival = _safe_bool(record.get("arrival") or record.get("is_arrival") or record.get("arrival_day"))
    departure = _safe_bool(record.get("departure") or record.get("is_departure") or record.get("departure_day"))
    transfer = _safe_bool(record.get("transfer") or record.get("is_transfer") or record.get("transfer_day"))

    if travel_hours > 0:
        transfer = True

    return DaySignals(
        day_number=day_number,
        is_first_day=day_number == 1,
        is_last_day=day_number == total_days,
        has_arrival=arrival,
        has_departure=departure,
        has_transfer=transfer,
        travel_hours=travel_hours,
        travel_distance_km=travel_distance_km,
        crosses_country=_safe_bool(record.get("crosses_country") or record.get("country_change")),
        border_crossing=_safe_bool(record.get("border_crossing") or record.get("crosses_border")),
        destination_type=_extract_destination_type(record),
        has_safari=flags["has_safari"], has_wildlife=flags["has_wildlife"], has_beach=flags["has_beach"],
        has_city=flags["has_city"], has_cultural=flags["has_cultural"], has_adventure=flags["has_adventure"],
        has_nature=flags["has_nature"], has_relaxation=flags["has_relaxation"],
        activity_count=activity_count,
        early_start_required=_safe_bool(record.get("early_start") or record.get("early_start_required")),
        late_finish_expected=_safe_bool(record.get("late_finish") or record.get("late_finish_expected")),
        previous_day_travel_hours=previous_travel_hours,
        explicit_archetype=_safe_str(record.get("archetype") or record.get("day_archetype")),
        raw=dict(record),
    )


def _explicit_archetype(value: str | None) -> DayArchetype | None:
    if not value:
        return None
    normalized = _normalize(value)
    for archetype in DayArchetype:
        if normalized == archetype.value:
            return archetype
    aliases = {
        "arrival_day": DayArchetype.ARRIVAL, "departure_day": DayArchetype.DEPARTURE,
        "transfer_day": DayArchetype.TRANSFER, "travel_day": DayArchetype.TRANSFER,
        "long_drive": DayArchetype.LONG_TRANSFER, "game_drive": DayArchetype.SAFARI,
        "safari_day": DayArchetype.SAFARI, "beach_day": DayArchetype.BEACH,
        "rest_day": DayArchetype.RECOVERY, "relaxation_day": DayArchetype.RELAXATION,
        "city_day": DayArchetype.CITY_EXPLORATION, "culture_day": DayArchetype.CULTURAL,
    }
    return aliases.get(normalized)


def classify_day(signals: DaySignals) -> tuple[DayArchetype, float, list[str]]:
    explicit = _explicit_archetype(signals.explicit_archetype)
    if explicit is not None:
        return explicit, 1.0, ["explicit_archetype"]

    if signals.has_arrival and signals.is_first_day:
        return DayArchetype.ARRIVAL, 0.98, ["arrival", "first_day"]
    if signals.has_departure and signals.is_last_day:
        return DayArchetype.DEPARTURE, 0.98, ["departure", "last_day"]
    if signals.travel_hours >= VERY_LONG_TRAVEL_HOURS:
        return DayArchetype.LONG_TRANSFER, 0.98, ["very_long_transfer"]
    if signals.has_transfer and signals.travel_hours >= LONG_TRAVEL_HOURS:
        return DayArchetype.LONG_TRANSFER, 0.96, ["long_transfer"]
    if signals.has_safari or signals.has_wildlife:
        return DayArchetype.SAFARI, 0.93, ["wildlife", "safari"]
    if signals.has_beach:
        return DayArchetype.BEACH, 0.92, ["beach"]
    if signals.has_relaxation:
        return DayArchetype.RELAXATION, 0.90, ["relaxation"]
    if signals.has_cultural:
        return DayArchetype.CULTURAL, 0.88, ["cultural"]
    if signals.has_city:
        return DayArchetype.CITY_EXPLORATION, 0.88, ["city"]
    if signals.has_adventure:
        return DayArchetype.ADVENTURE, 0.88, ["adventure"]
    if signals.has_nature:
        return DayArchetype.NATURE, 0.86, ["nature"]
    if signals.has_transfer and signals.travel_hours > 0:
        return DayArchetype.TRANSFER, 0.90, ["transfer"]
    if signals.activity_count == 0 and signals.previous_day_travel_hours >= MIN_RECOVERY_DAY_AFTER_LONG_TRANSFER_HOURS:
        return DayArchetype.RECOVERY, 0.82, ["post_transfer_recovery"]
    if signals.activity_count == 0:
        return DayArchetype.FREE, 0.80, ["no_scheduled_activities"]
    if signals.activity_count > 1:
        return DayArchetype.MIXED, 0.70, ["multiple_activity_types"]
    return DayArchetype.UNKNOWN, 0.40, ["insufficient_signals"]


def _capacity_for(archetype: DayArchetype, travel_hours: float) -> str:
    if archetype in {DayArchetype.ARRIVAL, DayArchetype.DEPARTURE, DayArchetype.LONG_TRANSFER, DayArchetype.OVERNIGHT_TRANSITION}:
        return "low"
    if archetype in {DayArchetype.TRANSFER, DayArchetype.RECOVERY, DayArchetype.RELAXATION, DayArchetype.BEACH}:
        return "light"
    if travel_hours >= LONG_TRAVEL_HOURS:
        return "light"
    if archetype in {DayArchetype.SAFARI, DayArchetype.WILDLIFE, DayArchetype.ADVENTURE}:
        return "focused"
    if archetype in {DayArchetype.EXPLORATION, DayArchetype.CITY_EXPLORATION, DayArchetype.CULTURAL, DayArchetype.NATURE}:
        return "normal"
    if archetype == DayArchetype.FREE:
        return "optional"
    if archetype == DayArchetype.MIXED:
        return "normal"
    return "light"


def _pace_for(archetype: DayArchetype) -> str:
    if archetype in {DayArchetype.LONG_TRANSFER, DayArchetype.OVERNIGHT_TRANSITION}:
        return "slow"
    if archetype in {DayArchetype.ARRIVAL, DayArchetype.DEPARTURE, DayArchetype.TRANSFER, DayArchetype.RECOVERY}:
        return "light"
    if archetype in {DayArchetype.BEACH, DayArchetype.RELAXATION}:
        return "relaxed"
    if archetype in {DayArchetype.SAFARI, DayArchetype.WILDLIFE, DayArchetype.ADVENTURE}:
        return "focused"
    return "moderate"


def _early_start_for(archetype: DayArchetype, signals: DaySignals) -> bool:
    if signals.early_start_required:
        return True
    return archetype in {DayArchetype.SAFARI, DayArchetype.WILDLIFE, DayArchetype.ADVENTURE}


def _late_finish_for(archetype: DayArchetype, signals: DaySignals) -> bool:
    if signals.late_finish_expected:
        return True
    return archetype in {DayArchetype.SAFARI, DayArchetype.WILDLIFE}


def _build_day_result(signals: DaySignals) -> DayArchetypeResult:
    archetype, confidence, dominant_signals = classify_day(signals)

    recovery_recommended = (
        signals.travel_hours >= LONG_TRAVEL_HOURS
        or (signals.previous_day_travel_hours >= MIN_RECOVERY_DAY_AFTER_LONG_TRANSFER_HOURS and signals.activity_count == 0)
    )

    warnings: list[str] = []
    notes: list[str] = []

    if signals.travel_hours >= VERY_LONG_TRAVEL_HOURS:
        warnings.append("Travel load is extremely high for a normal activity day.")
    elif signals.travel_hours >= LONG_TRAVEL_HOURS:
        warnings.append("Long travel time should reduce activity density.")

    if signals.crosses_country:
        warnings.append("Country transition may introduce border or operational uncertainty.")
    if signals.border_crossing:
        warnings.append("Border crossing should be treated as a schedule constraint.")
    if archetype == DayArchetype.SAFARI and signals.travel_hours >= MAX_NORMAL_TRAVEL_HOURS:
        warnings.append("Safari activities should not be stacked aggressively with long transfers.")

    if archetype == DayArchetype.ARRIVAL:
        notes.extend(["Protect arrival and check-in time.", "Prefer flexible activities near accommodation.",
                       "Avoid stacking fixed-time activities immediately after arrival."])
    elif archetype == DayArchetype.DEPARTURE:
        notes.extend(["Protect checkout and departure transfer.", "Keep final activities optional or close to departure point."])
    elif archetype == DayArchetype.LONG_TRANSFER:
        notes.extend(["Treat transportation as the dominant component of the day.",
                       "Use light or optional activities only.", "Consider recovery time after arrival."])
    elif archetype == DayArchetype.TRANSFER:
        notes.extend(["Keep activity density moderate.", "Place activities around confirmed transport windows."])
    elif archetype == DayArchetype.SAFARI:
        notes.extend(["Protect early wildlife windows.", "Allow flexible meal timing.", "Avoid unnecessary fixed-time commitments."])
    elif archetype == DayArchetype.BEACH:
        notes.extend(["Prefer flexible pacing.", "Avoid over-scheduling."])
    elif archetype == DayArchetype.RECOVERY:
        notes.extend(["Prioritize rest and low-friction activities.", "Avoid consecutive high-intensity commitments."])
    elif archetype == DayArchetype.CULTURAL:
        notes.append("Group geographically compatible cultural activities.")
    elif archetype == DayArchetype.CITY_EXPLORATION:
        notes.append("Keep activities geographically clustered.")
    elif archetype == DayArchetype.ADVENTURE:
        notes.append("Protect preparation, safety briefing, and recovery time.")

    capacity = _capacity_for(archetype, signals.travel_hours)
    pace = _pace_for(archetype)

    return DayArchetypeResult(
        day_number=signals.day_number, archetype=archetype, confidence=confidence,
        travel_hours=round(signals.travel_hours, 2), travel_distance_km=round(signals.travel_distance_km, 2),
        activity_capacity=capacity, recommended_pace=pace,
        early_start=_early_start_for(archetype, signals), late_finish=_late_finish_for(archetype, signals),
        recovery_recommended=recovery_recommended, dominant_signals=tuple(dominant_signals),
        warnings=tuple(warnings), planning_notes=tuple(notes), raw=dict(signals.raw),
    )


class DayArchetypeEngine:
    name = "DayArchetypeEngine"
    version = "1.0"

    def classify_day(
        self, day: Mapping[str, Any], *, day_number: int = 1, total_days: int = 1, previous_travel_hours: float = 0.0,
    ) -> DayArchetypeResult:
        signals = _signals_from_record(day, day_number, total_days, previous_travel_hours)
        return _build_day_result(signals)

    def analyze(self, days: Sequence[Mapping[str, Any]]) -> DayArchetypePlan:
        if days is None:
            raise ValueError("days cannot be None")

        total_days = len(days)
        if total_days == 0:
            return DayArchetypePlan(days=(), archetype_counts={}, warnings=("No itinerary days were supplied.",))

        results: list[DayArchetypeResult] = []
        previous_travel_hours = 0.0

        for index, day in enumerate(days, start=1):
            if not isinstance(day, Mapping):
                raise TypeError(f"Day {index} must be a mapping.")
            result = self.classify_day(day, day_number=index, total_days=total_days, previous_travel_hours=previous_travel_hours)
            results.append(result)
            previous_travel_hours = result.travel_hours

        counts: dict[str, int] = {}
        for result in results:
            key = result.archetype.value
            counts[key] = counts.get(key, 0) + 1

        warnings: list[str] = []
        for result in results:
            warnings.extend(f"Day {result.day_number}: {warning}" for warning in result.warnings)

        for previous, current in zip(results, results[1:]):
            if previous.travel_hours >= LONG_TRAVEL_HOURS and current.travel_hours >= LONG_TRAVEL_HOURS:
                warnings.append(f"Days {previous.day_number}-{current.day_number}: consecutive long-travel days detected.")

        return DayArchetypePlan(days=tuple(results), archetype_counts=counts, warnings=tuple(warnings))

    def classify_days(self, days: Sequence[Mapping[str, Any]]) -> DayArchetypePlan:
        return self.analyze(days)


def day_archetype_plan_to_dict(plan: DayArchetypePlan) -> dict[str, Any]:
    return {
        "engine": DayArchetypeEngine.name, "version": DayArchetypeEngine.version,
        "days": [
            {
                "day_number": day.day_number, "archetype": day.archetype.value, "confidence": day.confidence,
                "travel_hours": day.travel_hours, "travel_distance_km": day.travel_distance_km,
                "activity_capacity": day.activity_capacity, "recommended_pace": day.recommended_pace,
                "early_start": day.early_start, "late_finish": day.late_finish,
                "recovery_recommended": day.recovery_recommended, "dominant_signals": list(day.dominant_signals),
                "warnings": list(day.warnings), "planning_notes": list(day.planning_notes),
            }
            for day in plan.days
        ],
        "archetype_counts": dict(plan.archetype_counts), "requires_repair": plan.requires_repair,
        "warnings": list(plan.warnings), "generated_at": plan.generated_at.isoformat(),
    }


def analyze_day_archetypes(days: Sequence[Mapping[str, Any]]) -> DayArchetypePlan:
    return DayArchetypeEngine().analyze(days)


__all__ = [
    "DayArchetype", "DaySignals", "DayArchetypeResult", "DayArchetypePlan", "DayArchetypeEngine",
    "classify_day", "day_archetype_plan_to_dict", "analyze_day_archetypes",
]

