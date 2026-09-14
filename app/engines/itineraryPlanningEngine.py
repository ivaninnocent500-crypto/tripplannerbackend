"""
ItineraryPlanningEngine
-----------------------

Production itinerary builder for the persisted furniture schema.

Pipeline contract
-----------------
The orchestrator is responsible for:

    RulesEngine
        ↓
    RouteGeographyEngine
        ↓
    route feasibility
        ↓
    allocate_days_for_route()
        ↓
    DayArchetypeEngine
        ↓
    ItineraryPlanningEngine
        ↓
    ScheduleRepairEngine
        ↓
    ValidationEngine

This engine does NOT calculate day allocation.

It receives:
    - day_allocation
    - transit_days
    - day_archetypes
    - route_facts

RouteGeographyEngine is authoritative for:
    - route order
    - route duration
    - route transport mode
    - inter-country status
    - border-crossing facts
    - route source

No-fabrication rules
--------------------
- Preserve the route order supplied by the orchestrator.
- Never globally deduplicate destination IDs.
- Real route duration is preferred.
- Unknown route duration remains unknown internally.
- Internal fallback durations are for scheduling only.
- Never invent airport names, airport transfers, attraction names,
  flight modes, drive modes, lodge names, or route durations.
- Only game-drive drawers receive absolute clock times.
- Other drawers use duration_minutes + sort_order.
- Destination transitions and transit days are distinct concepts.
- Transit classification comes from the orchestrator.

Presentation rule
-----------------
Missing backend facts must not be exposed as database/system failures
to the traveler.

Therefore:
- no "duration unavailable"
- no "transport details unavailable"
- no "confirm before booking" caused by missing planner data
- no fabricated transport mode
- no fabricated transport duration

If a transport fact is unknown, the planner either:
1. presents a neutral transfer label, or
2. omits the Armrest record when the persisted schema requires a
   non-null transport mode.

The latter is important because armrests.mode is NOT NULL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta, time as dt_time
from typing import Any, Mapping

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.db.models_furniture import (
    Armrest,
    Cabinet,
    Drawer,
    Headboard,
    Hinge,
    Shelf,
    Tray,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------

DEFAULT_GAME_DRIVE_START = dt_time(6, 0)
EVENING_GAME_DRIVE_START = dt_time(16, 0)

# These values are scheduling fallbacks only.
# They are never treated as factual route durations.
FALLBACK_ARRIVAL_TRANSFER_MINUTES = 60
FALLBACK_DEPARTURE_TRANSFER_MINUTES = 60
FALLBACK_TRANSIT_LEG_MINUTES = 60
FALLBACK_ACTIVITY_DURATION_MINUTES = 120
FALLBACK_MORNING_ACTIVITY_MINUTES = 240
FALLBACK_AFTERNOON_ACTIVITY_MINUTES = 150


try:
    from app.engine.activity_constraints import (
        MAX_NORMAL_ACTIVITY_HOURS_PER_DAY,
    )
except ImportError:
    try:
        from app.engines.activity_constraints import (
            MAX_NORMAL_ACTIVITY_HOURS_PER_DAY,
        )
    except ImportError:
        MAX_NORMAL_ACTIVITY_HOURS_PER_DAY = 8


# ---------------------------------------------------------------------
# CATEGORY RANKING
# ---------------------------------------------------------------------

DESTINATION_TYPE_CATEGORY_RANKS: dict[str, list[str]] = {
    "national_park": [
        "game_drive",
        "walking_safari",
        "birding",
        "night_drive",
        "photography",
    ],
    "game_reserve": [
        "game_drive",
        "walking_safari",
        "birding",
        "night_drive",
        "horseback_safari",
    ],
    "island": [
        "beach_leisure",
        "diving",
        "snorkeling",
        "boat_safari",
        "fishing",
    ],
    "beach": [
        "beach_leisure",
        "diving",
        "snorkeling",
        "boat_safari",
        "fishing",
    ],
    "marine_park": [
        "diving",
        "snorkeling",
        "boat_safari",
        "fishing",
    ],
    "mountain": [
        "mountain_climbing",
        "hiking",
    ],
    "desert": [
        "hiking",
        "camping",
        "photography",
    ],
    "city": [
        "cultural_visit",
        "shopping",
        "photography",
    ],
    "cultural_site": [
        "cultural_visit",
        "photography",
    ],
    "unesco_site": [
        "cultural_visit",
        "photography",
    ],
    "lake": [
        "boat_safari",
        "canoeing",
        "fishing",
        "birding",
    ],
    "waterfall": [
        "hiking",
        "photography",
    ],
    "forest_reserve": [
        "walking_safari",
        "birding",
        "hiking",
    ],
    "wetland": [
        "birding",
        "boat_safari",
        "canoeing",
    ],
}


TRAVEL_STYLE_CATEGORY_RANKS: dict[str, list[str]] = {
    "wildlife": [
        "game_drive",
        "walking_safari",
        "birding",
        "night_drive",
        "horseback_safari",
    ],
    "adventure": [
        "hiking",
        "mountain_climbing",
        "diving",
        "canoeing",
        "cycling",
    ],
    "beach": [
        "beach_leisure",
        "diving",
        "snorkeling",
        "boat_safari",
        "fishing",
    ],
    "cultural": [
        "cultural_visit",
        "shopping",
        "photography",
    ],
    "culture": [
        "cultural_visit",
        "shopping",
        "photography",
    ],
    "photography": [
        "photography",
        "birding",
        "game_drive",
        "walking_safari",
    ],
    "birding": [
        "birding",
    ],
    "luxury": [
        "spa_wellness",
        "photography",
        "boat_safari",
    ],
    "relaxed_pace": [
        "beach_leisure",
        "spa_wellness",
        "boat_safari",
        "photography",
    ],
    "walking": [
        "walking_safari",
        "hiking",
    ],
}


FALLBACK_VARIANTS: dict[str, list[tuple[str, str]]] = {
    "island": [
        (
            "Beach & relaxation",
            "Free time at the destination. No specific excursion "
            "is booked for this slot.",
        ),
        (
            "Shoreline time",
            "Open beach time. No guided excursion is booked "
            "for this slot.",
        ),
    ],
    "beach": [
        (
            "Beach & relaxation",
            "Free time at the destination. No specific excursion "
            "is booked for this slot.",
        ),
        (
            "Shoreline time",
            "Unstructured time along the beach. No guided activity "
            "is booked for this slot.",
        ),
    ],
    "mountain": [
        (
            "Acclimatisation time",
            "Time to acclimatise at a comfortable pace. No summit "
            "attempt is scheduled.",
        ),
        (
            "Rest & recovery",
            "Open time to rest and recover at the accommodation.",
        ),
    ],
    "desert": [
        (
            "Desert nature time",
            "Open time suited to the surrounding terrain. No specific "
            "site is booked for this slot.",
        ),
        (
            "Open camp time",
            "Unstructured time at the accommodation. No specific "
            "excursion is booked.",
        ),
    ],
    "marine_park": [
        (
            "Shore time",
            "Open time at the destination. Optional water-based "
            "activities may depend on conditions.",
        ),
    ],
    "city": [
        (
            "Local exploration time",
            "Open time for local exploration. No specific venue "
            "is booked in advance.",
        ),
        (
            "Free time to explore",
            "Unstructured time to explore independently. No guided "
            "activity is booked.",
        ),
    ],
    "cultural_site": [
        (
            "Cultural exploration time",
            "Open time around the destination. No additional venue "
            "is booked in advance.",
        ),
    ],
    "unesco_site": [
        (
            "Heritage exploration time",
            "Open time around the destination. No additional venue "
            "is booked in advance.",
        ),
    ],
    "national_park": [
        (
            "Guided wilderness drive",
            "Game drive on available lodge circuits. No specific "
            "route is booked in advance.",
        ),
        (
            "Photographic drive",
            "A slower-paced drive focused on photography opportunities. "
            "No specific route is booked in advance.",
        ),
        (
            "Bush walk near camp",
            "Short guided walk near the accommodation, conditions "
            "permitting. No specific route is booked in advance.",
        ),
    ],
    "game_reserve": [
        (
            "Guided wilderness drive",
            "Game drive on available lodge circuits. No specific "
            "route is booked in advance.",
        ),
        (
            "Photographic drive",
            "A slower-paced drive focused on photography opportunities. "
            "No specific route is booked in advance.",
        ),
    ],
}


_DEFAULT_FALLBACK = [
    (
        "Time at the destination",
        "Open time at the destination. No specific excursion "
        "is booked for this slot.",
    ),
]


# ---------------------------------------------------------------------
# RESULT
# ---------------------------------------------------------------------

@dataclass
class BuildResult:
    cabinet: Cabinet
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------
# GENERAL HELPERS
# ---------------------------------------------------------------------

def _is_game_drive_category(
    category: str | None,
) -> bool:
    """
    Absolute clock times are intentionally restricted to game drives.
    """
    return str(category or "").strip().lower() == "game_drive"


def _fallback_drawer_text(
    destination_type: str | None,
    variant_index: int,
) -> tuple[str, str]:
    variants = (
        FALLBACK_VARIANTS.get(
            str(destination_type or "").lower(),
        )
        or _DEFAULT_FALLBACK
    )

    return variants[
        variant_index % len(variants)
    ]


def _merged_ranked_categories(
    destination_type: str | None,
    travel_style: list[str],
    focus: str | None = None,
) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []

    tags: list[str] = []

    if focus:
        tags.append(
            str(focus).strip().lower()
        )

    tags.extend(
        str(style).strip().lower()
        for style in travel_style
        if style
    )

    for tag in tags:
        for category in TRAVEL_STYLE_CATEGORY_RANKS.get(
            tag,
            [],
        ):
            if category not in seen:
                seen.add(category)
                result.append(category)

    if destination_type:
        for category in DESTINATION_TYPE_CATEGORY_RANKS.get(
            str(destination_type).strip().lower(),
            [],
        ):
            if category not in seen:
                seen.add(category)
                result.append(category)

    return result


def _format_transfer_description(
    mode: str | None,
    minutes: int | None,
) -> str:
    """
    Create neutral traveler-facing transport text.

    Important:
    Unknown duration is NOT exposed as a backend/data-quality
    failure. Unknown mode is also never fabricated.

    Examples:
        known mode + known duration:
            "Road transfer · approximately 90 min"

        known mode + unknown duration:
            "Road transfer"

        unknown mode + known duration:
            "Transfer · approximately 90 min"

        unknown mode + unknown duration:
            "Transfer"
    """
    normalized_mode = (
        str(mode).strip().lower()
        if mode
        else None
    )

    mode_label = {
        "scheduled_flight": "Scheduled flight",
        "charter_flight": "Charter flight",
        "private_4x4": "Private 4x4",
        "road_transfer": "Road transfer",
        "drive": "Road transfer",
        "road": "Road transfer",
        "flight": "Flight",
        "ferry": "Ferry",
    }.get(
        normalized_mode,
        "Transfer",
    )

    if minutes is None:
        return mode_label

    return (
        f"{mode_label} · approximately "
        f"{minutes} min"
    )


def _normalise_archetype(
    value: Any,
) -> str | None:
    if value is None:
        return None

    if isinstance(value, Mapping):
        for key in (
            "archetype",
            "day_archetype",
            "kind",
            "type",
        ):
            if value.get(key) is not None:
                value = value[key]
                break

    if hasattr(value, "value"):
        value = value.value

    if value is None:
        return None

    return (
        str(value)
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )


def _is_cultural_archetype(
    value: Any,
) -> bool:
    return _normalise_archetype(value) in {
        "cultural",
        "culture",
        "cultural_day",
        "city_exploration",
        "city",
        "heritage",
    }


# ---------------------------------------------------------------------
# ENGINE
# ---------------------------------------------------------------------

class ItineraryPlanningEngine:

    def __init__(
        self,
        db: Session,
    ):
        self.db = db

    # -----------------------------------------------------------------
    # PUBLIC METADATA CONTRACT
    # -----------------------------------------------------------------

    def fetch_destination_meta(
        self,
        destination_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        return self._fetch_destination_meta(
            destination_ids
        )

    # -----------------------------------------------------------------
    # BUILD
    # -----------------------------------------------------------------

    def build(
        self,
        request: dict[str, Any],
        destination_ids: list[str],
        *,
        day_allocation: list[int],
        transit_days: Mapping[int, bool] | None = None,
        day_archetypes: Mapping[int, Any] | None = None,
        route_facts: list[dict[str, Any]] | None = None,
    ) -> BuildResult:

        days = self._safe_int(
            request.get("days"),
            0,
        )

        if days < 1:
            raise ValueError(
                "Trip must contain at least one day."
            )

        travelers = max(
            1,
            self._safe_int(
                request.get("travelers"),
                1,
            ),
        )

        travel_style = (
            request.get("travel_style")
            or self._infer_style(request)
        )

        if isinstance(
            travel_style,
            str,
        ):
            travel_style = [
                travel_style
            ]

        travel_style = list(
            dict.fromkeys(
                str(style).strip().lower()
                for style in travel_style
                if style
            )
        )

        # IMPORTANT:
        # Do not silently force wildlife when the caller did not
        # specify a focus. The Rules/UI/orchestrator layer owns
        # request defaults.

        focus_raw = request.get("focus")

        focus = (
            str(focus_raw).strip().lower()
            if focus_raw
            else None
        )

        budget_tier = str(
            request.get(
                "budget_tier",
                "mid",
            )
        ).strip().lower()

        start_date_raw = request.get(
            "start_date"
        )

        start_date: date | None = None

        if start_date_raw:
            try:
                start_date = (
                    start_date_raw
                    if isinstance(
                        start_date_raw,
                        date,
                    )
                    else date.fromisoformat(
                        str(start_date_raw)
                    )
                )
            except ValueError as exc:
                raise ValueError(
                    f"Invalid start_date: {start_date_raw}"
                ) from exc

        if not destination_ids:
            raise ValueError(
                "Itinerary generation requires at least "
                "one destination."
            )

        destination_ids = (
            self._normalize_destination_ids(
                destination_ids
            )
        )

        if not destination_ids:
            raise ValueError(
                "No valid destination IDs were supplied."
            )

        if len(destination_ids) > days:
            raise ValueError(
                "Effective destination count exceeds "
                "available trip days. Route feasibility must "
                "reduce the destination list before "
                "ItineraryPlanningEngine.build()."
            )

        if len(day_allocation) != len(destination_ids):
            raise ValueError(
                "day_allocation length "
                f"({len(day_allocation)}) does not match "
                "destination_ids length "
                f"({len(destination_ids)})."
            )

        normalized_allocation = [
            max(
                0,
                self._safe_int(
                    value,
                    0,
                ),
            )
            for value in day_allocation
        ]

        if sum(normalized_allocation) != days:
            raise ValueError(
                "day_allocation must account for the full "
                f"requested trip duration. Expected {days} "
                f"days but received "
                f"{sum(normalized_allocation)}."
            )

        if any(
            value <= 0
            for value in normalized_allocation
        ):
            raise ValueError(
                "Every effective destination must receive "
                "at least one allocated day."
            )

        transit_days = transit_days or {}
        day_archetypes = day_archetypes or {}
        route_facts = route_facts or []

        cabinet = Cabinet(
            request_json=request,
            title=(
                request.get("title")
                or self._default_title(request)
            ),
            duration_days=days,
            travelers_adults=travelers,
            travelers_children=self._safe_int(
                request.get(
                    "travelers_children"
                ),
                0,
            ),
            travel_style=travel_style,
            budget_tier=budget_tier,
            status="draft",
            start_date=start_date,
            end_date=(
                start_date
                + timedelta(days=days - 1)
                if start_date
                else None
            ),
            primary_destination_id=destination_ids[0],
            route_destination_ids=destination_ids,
        )

        self.db.add(cabinet)
        self.db.flush()

        warnings: list[str] = []

        meta = self._fetch_destination_meta(
            destination_ids
        )

        missing_meta = [
            destination_id
            for destination_id in destination_ids
            if destination_id not in meta
        ]

        if missing_meta:
            warnings.append(
                "Some requested destinations could not "
                "be resolved in travel_places: "
                + ", ".join(missing_meta)
            )

        countries = [
            meta[destination_id].get("country")
            for destination_id in destination_ids
            if meta.get(destination_id, {}).get("country")
        ]

        if hasattr(cabinet, "route_countries"):
            cabinet.route_countries = list(
                dict.fromkeys(countries)
            )

        if hasattr(cabinet, "primary_country"):
            cabinet.primary_country = meta.get(
                destination_ids[0],
                {},
            ).get("country")

        # -------------------------------------------------------------
        # ROUTE FACTS
        # -------------------------------------------------------------

        legs = self._build_hinges(
            cabinet=cabinet,
            destination_ids=destination_ids,
            meta=meta,
            route_facts=route_facts,
        )

        destination_types = {
            destination_id: meta.get(
                destination_id,
                {},
            ).get("destination_type")
            for destination_id in destination_ids
        }

        # -------------------------------------------------------------
        # ACTIVITY POOLS
        # -------------------------------------------------------------

        per_destination_pool: dict[
            str,
            list[dict[str, Any]],
        ] = {}

        for destination_id in destination_ids:
            pool = self._fetch_ranked_activity_pool(
                dest_id=destination_id,
                destination_type=destination_types.get(
                    destination_id
                ),
                travel_style=travel_style,
                focus=focus,
                start_date=start_date,
                cabinet_id=str(cabinet.id),
            )

            per_destination_pool[
                destination_id
            ] = pool

            if not pool:
                warnings.append(
                    f"Destination {destination_id} has no "
                    "seeded activities. Explicit fallback "
                    "time will be used."
                )
            elif len(pool) < 2:
                warnings.append(
                    f"Destination {destination_id} has only "
                    f"{len(pool)} seeded activity."
                )

        cursors = {
            destination_id: 0
            for destination_id in destination_ids
        }

        fallback_counters = {
            destination_id: 0
            for destination_id in destination_ids
        }

        # -------------------------------------------------------------
        # CALENDAR BUILD
        # -------------------------------------------------------------

        day_number = 1
        current_date = start_date

        for destination_index, destination_id in enumerate(
            destination_ids
        ):
            allocated_days = normalized_allocation[
                destination_index
            ]

            destination_type = destination_types.get(
                destination_id
            )

            for night_index in range(allocated_days):

                if day_number > days:
                    raise RuntimeError(
                        "Planner attempted to create more "
                        "calendar days than requested."
                    )

                is_first_day = day_number == 1
                is_last_day = day_number == days

                is_arrival_day = (
                    night_index == 0
                    and destination_index > 0
                )

                is_transit_day = bool(
                    transit_days.get(
                        day_number,
                        False,
                    )
                )

                archetype = day_archetypes.get(
                    day_number
                )

                # A final-day transition is NOT automatically a
                # departure day. Transit classification supplied by
                # the orchestrator wins over the generic calendar
                # endpoint meaning.

                effective_departure_day = (
                    is_last_day
                    and not is_transit_day
                )

                shelf = Shelf(
                    cabinet_id=cabinet.id,
                    day_number=day_number,
                    date=current_date,
                    destination_id=destination_id,
                    theme=self._theme_for(
                        idx=destination_index,
                        night_idx=night_index,
                        is_first=is_first_day,
                        is_last=effective_departure_day,
                        destination_type=destination_type,
                        is_transit_day=is_transit_day,
                        archetype=archetype,
                    ),
                    day_kind=(
                        "TRANSIT"
                        if is_transit_day
                        else "STANDARD"
                    ),
                )

                self.db.add(shelf)

                if hasattr(shelf, "cabinet"):
                    shelf.cabinet = cabinet
                elif shelf not in cabinet.shelves:
                    cabinet.shelves.append(shelf)

                self.db.flush()

                origin_dest_id = (
                    destination_ids[
                        destination_index - 1
                    ]
                    if is_arrival_day
                    else None
                )

                first_activity_id = (
                    self._populate_drawers(
                        shelf=shelf,
                        pool=per_destination_pool[
                            destination_id
                        ],
                        cursor=cursors,
                        fallback_counters=fallback_counters,
                        dest_id=destination_id,
                        dest_type=destination_type,
                        travel_style=travel_style,
                        focus=focus,
                        day_number=day_number,
                        is_first_day=is_first_day,
                        is_last_day=effective_departure_day,
                        is_arrival_day=is_arrival_day,
                        is_transit_day=is_transit_day,
                        day_archetype=archetype,
                        legs=legs,
                        destination_index=destination_index,
                        origin_dest_id=origin_dest_id,
                    )
                )

                self._populate_headboard(
                    shelf=shelf,
                    dest_id=destination_id,
                    budget_tier=budget_tier,
                    remaining_nights_here=(
                        allocated_days - night_index
                    ),
                    warnings=warnings,
                )

                self._populate_armrest(
                    shelf=shelf,
                    legs=legs,
                    destination_index=destination_index,
                    is_arrival_day=is_arrival_day,
                )

                self._populate_trays(
                    shelf=shelf,
                    is_first_day=is_first_day,
                    is_last_day=effective_departure_day,
                    is_transit_day=is_transit_day,
                )

                if first_activity_id:
                    self._populate_day_photo(
                        shelf=shelf,
                        activity_id=first_activity_id,
                        destination_id=destination_id,
                    )

                day_number += 1

                if current_date:
                    current_date += timedelta(days=1)

        if day_number != days + 1:
            raise RuntimeError(
                "Planner did not produce exactly the "
                f"requested {days} calendar days. "
                f"Stopped at day {day_number - 1}."
            )

        self.db.flush()

        return BuildResult(
            cabinet=cabinet,
            warnings=warnings,
        )

    # -----------------------------------------------------------------
    # BASIC HELPERS
    # -----------------------------------------------------------------

    @staticmethod
    def _safe_int(
        value: Any,
        default: int = 0,
    ) -> int:
        try:
            return int(value)
        except (
            TypeError,
            ValueError,
        ):
            return default

    @staticmethod
    def _normalize_destination_ids(
        destination_ids: list[str],
    ) -> list[str]:
        """
        Preserve route order.

        Removes:
            - null/empty values
            - consecutive duplicates

        Does NOT remove non-consecutive repeats.

        Therefore:
            A -> B -> A

        remains:
            A -> B -> A
        """
        result: list[str] = []
        previous: str | None = None

        for raw_id in destination_ids:
            if raw_id is None:
                continue

            destination_id = str(
                raw_id
            ).strip()

            if not destination_id:
                continue

            if destination_id == previous:
                continue

            result.append(destination_id)
            previous = destination_id

        return result

    # -----------------------------------------------------------------
    # DESTINATION META
    # -----------------------------------------------------------------

    def _fetch_destination_meta(
        self,
        destination_ids: list[str],
    ) -> dict[str, dict[str, Any]]:

        if not destination_ids:
            return {}

        rows = self.db.execute(
            text(
                """
                SELECT
                    CAST(id AS text) AS id,
                    name,
                    CAST(country AS text) AS country,
                    CAST(destination_type AS text)
                        AS destination_type
                FROM travel_places
                WHERE id = ANY(
                    CAST(:ids AS uuid[])
                )
                """
            ),
            {
                "ids": destination_ids
            },
        ).fetchall()

        meta: dict[
            str,
            dict[str, Any],
        ] = {}

        for row in rows:
            destination_id = str(
                row[0]
            )

            meta[destination_id] = {
                "country": row[2],
                "headline_label": row[1],
                "destination_type": row[3],
                "min_nights": 1,
            }

        # Minimum-night information belongs to
        # feasibility/allocation. This planner only reads it.

        try:
            table_exists = self.db.execute(
                text(
                    """
                    SELECT to_regclass(
                        'estimated_visit_durations'
                    )
                    """
                )
            ).scalar()

            if table_exists:
                min_rows = self.db.execute(
                    text(
                        """
                        SELECT
                            CAST(destination_id AS text),
                            MIN(
                                recommended_nights_min
                            )
                        FROM estimated_visit_durations
                        WHERE destination_id =
                            ANY(
                                CAST(:ids AS uuid[])
                            )
                          AND scope =
                              'full_destination'
                          AND recommended_nights_min
                              IS NOT NULL
                        GROUP BY destination_id
                        """
                    ),
                    {
                        "ids": destination_ids
                    },
                ).fetchall()

                for (
                    destination_id,
                    minimum,
                ) in min_rows:

                    destination_id = str(
                        destination_id
                    )

                    if (
                        destination_id in meta
                        and minimum is not None
                    ):
                        meta[
                            destination_id
                        ]["min_nights"] = max(
                            1,
                            int(minimum),
                        )

        except Exception as exc:
            logger.warning(
                "Could not read estimated_visit_durations "
                "minimum nights: %s",
                exc,
            )

        return meta

    # -----------------------------------------------------------------
    # ACTIVITY DURATION COLUMN
    # -----------------------------------------------------------------

    def _get_activity_duration_column(
        self,
    ) -> str | None:

        try:
            inspector = inspect(
                self.db.bind
            )

            columns = {
                column["name"]
                for column in inspector.get_columns(
                    "estimated_visit_durations"
                )
            }

        except Exception:
            return None

        candidates = (
            "duration_minutes",
            "estimated_duration_minutes",
            "visit_duration_minutes",
            "recommended_minutes",
            "estimated_minutes",
        )

        for candidate in candidates:
            if candidate in columns:
                return candidate

        return None

    # -----------------------------------------------------------------
    # ACTIVITY POOL
    # -----------------------------------------------------------------

    def _fetch_ranked_activity_pool(
        self,
        dest_id: str,
        destination_type: str | None,
        travel_style: list[str],
        focus: str | None,
        start_date: date | None,
        cabinet_id: str,
    ) -> list[dict[str, Any]]:

        ranked_categories = (
            _merged_ranked_categories(
                destination_type=destination_type,
                travel_style=travel_style,
                focus=focus,
            )
        )

        month_token = (
            start_date.strftime("%B").lower()
            if start_date
            else None
        )

        duration_column = (
            self._get_activity_duration_column()
        )

        if duration_column:
            duration_expression = (
                f"evd.{duration_column}"
            )

            duration_join = """
                LEFT JOIN estimated_visit_durations evd
                    ON evd.activity_id = a.id
                   AND evd.scope = 'single_activity'
            """
        else:
            duration_expression = "NULL"
            duration_join = ""

        sql = text(
            f"""
            WITH ranked AS (
                SELECT
                    a.id,
                    a.name,
                    a.description,
                    CAST(a.category AS text)
                        AS category,
                    a.difficulty,
                    a.available_months,
                    {duration_expression}
                        AS estimated_visit_duration_minutes,

                    CASE
                        WHEN cardinality(
                            CAST(:ranked AS text[])
                        ) = 0
                        THEN 999
                        ELSE COALESCE(
                            array_position(
                                CAST(:ranked AS text[]),
                                CAST(
                                    a.category AS text
                                )
                            ),
                            998
                        )
                    END AS style_position,

                    CASE
                        WHEN CAST(:month AS text) IS NULL
                        THEN 0

                        WHEN a.available_months IS NULL
                        THEN 0

                        WHEN CAST(:month AS month_enum)
                            = ANY(a.available_months)
                        THEN 0

                        ELSE 1
                    END AS month_mismatch,

                    md5(
                        CAST(:cab_id AS text)
                        || '|'
                        || CAST(a.id AS text)
                    ) AS deterministic_order

                FROM activities a

                {duration_join}

                WHERE a.destination_id =
                    CAST(:dest_id AS uuid)
            )

            SELECT
                id,
                name,
                description,
                category,
                difficulty,
                estimated_visit_duration_minutes,
                style_position,
                month_mismatch

            FROM ranked

            ORDER BY
                style_position ASC,
                month_mismatch ASC,
                deterministic_order ASC,
                id ASC
            """
        )

        rows = self.db.execute(
            sql,
            {
                "ranked": ranked_categories,
                "month": month_token,
                "cab_id": cabinet_id,
                "dest_id": dest_id,
            },
        ).fetchall()

        result: list[dict[str, Any]] = []

        for row in rows:
            duration = row[5]

            if duration is not None:
                try:
                    duration = max(
                        1,
                        int(duration),
                    )
                except (
                    TypeError,
                    ValueError,
                ):
                    duration = None

            result.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "description": row[2],
                    "category": row[3],
                    "difficulty": row[4],
                    "estimated_visit_duration_minutes": (
                        duration
                    ),
                }
            )

        return result

    # -----------------------------------------------------------------
    # DRAWER DISPATCH
    # -----------------------------------------------------------------

    def _populate_drawers(
        self,
        shelf: Shelf,
        pool: list[dict[str, Any]],
        cursor: dict[str, int],
        fallback_counters: dict[str, int],
        dest_id: str,
        dest_type: str | None,
        travel_style: list[str],
        focus: str | None,
        day_number: int,
        is_first_day: bool,
        is_last_day: bool,
        is_arrival_day: bool,
        is_transit_day: bool,
        day_archetype: Any,
        legs: list[dict[str, Any]],
        destination_index: int,
        origin_dest_id: str | None = None,
    ) -> str | None:

        # Structural semantics take precedence.
        if is_first_day:
            return self._populate_first_day_drawers(
                shelf=shelf,
            )

        if is_transit_day:
            return self._populate_transit_day_drawers(
                shelf=shelf,
                legs=legs,
                destination_index=destination_index,
                is_arrival_day=is_arrival_day,
            )

        if is_last_day:
            return self._populate_last_day_drawers(
                shelf=shelf,
            )

        if _is_cultural_archetype(
            day_archetype
        ):
            return self._populate_cultural_day_drawers(
                shelf=shelf,
                pool=pool,
                cursor=cursor,
                fallback_counters=fallback_counters,
                dest_id=dest_id,
                dest_type=dest_type,
                travel_style=travel_style,
                day_number=day_number,
                is_arrival_day=is_arrival_day,
                origin_dest_id=origin_dest_id,
                legs=legs,
                destination_index=destination_index,
            )

        return self._populate_standard_day_drawers(
            shelf=shelf,
            pool=pool,
            cursor=cursor,
            fallback_counters=fallback_counters,
            dest_id=dest_id,
            dest_type=dest_type,
            travel_style=travel_style,
            day_number=day_number,
            is_arrival_day=is_arrival_day,
            origin_dest_id=origin_dest_id,
            legs=legs,
            destination_index=destination_index,
        )

    # -----------------------------------------------------------------
    # FIRST DAY
    # -----------------------------------------------------------------

    def _populate_first_day_drawers(
        self,
        shelf: Shelf,
    ) -> None:

        order = 1

        self._add_drawer(
            shelf=shelf,
            name="Arrival & settle in",
            description=(
                "Arrival day. Settle into the destination and "
                "accommodation at a comfortable pace."
            ),
            start_time=None,
            duration_minutes=60,
            sort_order=order,
            activity_type="ARRIVAL",
            source="planner_arrival_semantics",
            is_fallback=True,
        )

        order += 1

        self._add_drawer(
            shelf=shelf,
            name="Free time",
            description=(
                "Unstructured time to rest and settle in."
            ),
            start_time=None,
            duration_minutes=240,
            sort_order=order,
            activity_type="FREE_TIME",
            source="planner_arrival_semantics",
            is_fallback=True,
        )

        order += 1

        self._add_drawer(
            shelf=shelf,
            name="Dinner",
            description=None,
            start_time=None,
            duration_minutes=90,
            sort_order=order,
            activity_type="MEAL",
            source="hardcoded_meal",
        )

    # -----------------------------------------------------------------
    # LAST DAY
    # -----------------------------------------------------------------

    def _populate_last_day_drawers(
        self,
        shelf: Shelf,
    ) -> None:

        order = 1

        self._add_drawer(
            shelf=shelf,
            name="Breakfast",
            description=None,
            start_time=None,
            duration_minutes=45,
            sort_order=order,
            activity_type="MEAL",
            source="hardcoded_meal",
        )

        order += 1

        self._add_drawer(
            shelf=shelf,
            name="Departure",
            description=(
                "Trip departure."
            ),
            start_time=None,
            duration_minutes=30,
            sort_order=order,
            activity_type="DEPARTURE",
            source="planner_departure_semantics",
            is_fallback=True,
        )

    # -----------------------------------------------------------------
    # TRANSIT DAY
    # -----------------------------------------------------------------

    def _populate_transit_day_drawers(
        self,
        shelf: Shelf,
        legs: list[dict[str, Any]],
        destination_index: int,
        is_arrival_day: bool,
    ) -> None:

        order = 1

        leg: dict[str, Any] | None = None

        if (
            is_arrival_day
            and destination_index > 0
        ):
            leg = self._get_arrival_leg(
                legs=legs,
                destination_index=destination_index,
            )

        if leg is None:
            logger.warning(
                "Transit day %s has no matching incoming "
                "RouteGeography leg.",
                shelf.day_number,
            )

            # Do not expose the missing route fact.
            # Keep the drawer neutral.
            self._add_drawer(
                shelf=shelf,
                name="Destination transfer",
                description="Transfer to the destination.",
                start_time=None,
                duration_minutes=(
                    FALLBACK_TRANSIT_LEG_MINUTES
                ),
                sort_order=order,
                activity_type="TRANSFER",
                source="unavailable",
                is_fallback=True,
                destination_id=shelf.destination_id,
            )

        else:
            duration = self._coerce_optional_int(
                leg.get("duration_minutes")
            )

            mode = (
                str(leg.get("mode")).strip()
                if leg.get("mode")
                else None
            )

            source = (
                str(leg.get("source")).strip()
                if leg.get("source")
                else "unavailable"
            )

            description = _format_transfer_description(
                mode=mode,
                minutes=duration,
            )

            effective_duration = (
                duration
                if duration is not None
                else FALLBACK_TRANSIT_LEG_MINUTES
            )

            self._add_drawer(
                shelf=shelf,
                name="Long-distance transfer",
                description=description,
                start_time=None,
                duration_minutes=effective_duration,
                sort_order=order,
                activity_type="TRANSFER",
                source=source,
                is_fallback=duration is None,
                destination_id=shelf.destination_id,
            )

        order += 1

        self._add_drawer(
            shelf=shelf,
            name="Settle in",
            description=(
                "Time to rest and settle into the destination "
                "after the journey."
            ),
            start_time=None,
            duration_minutes=180,
            sort_order=order,
            activity_type="FREE_TIME",
            source="planner_transit_semantics",
            is_fallback=True,
            destination_id=shelf.destination_id,
        )

        order += 1

        self._add_drawer(
            shelf=shelf,
            name="Dinner",
            description=None,
            start_time=None,
            duration_minutes=90,
            sort_order=order,
            activity_type="MEAL",
            source="hardcoded_meal",
            destination_id=shelf.destination_id,
        )

    # -----------------------------------------------------------------
    # CULTURAL DAYS
    # -----------------------------------------------------------------

    def _populate_cultural_day_drawers(
        self,
        shelf: Shelf,
        pool: list[dict[str, Any]],
        cursor: dict[str, int],
        fallback_counters: dict[str, int],
        dest_id: str,
        dest_type: str | None,
        travel_style: list[str],
        day_number: int,
        is_arrival_day: bool,
        origin_dest_id: str | None,
        legs: list[dict[str, Any]],
        destination_index: int,
    ) -> str | None:

        order = 1
        first_activity_id: str | None = None

        if is_arrival_day:
            order, _ = self._destination_arrival_transfer(
                shelf=shelf,
                order=order,
                leg=self._get_arrival_leg(
                    legs=legs,
                    destination_index=destination_index,
                ),
            )
            order += 1

        max_activity_minutes = max(
            60,
            int(
                MAX_NORMAL_ACTIVITY_HOURS_PER_DAY * 60
            ),
        )

        packed_minutes = 0
        activities_added = 0

        while cursor.get(dest_id, 0) < len(pool):

            position = cursor.get(
                dest_id,
                0,
            )

            activity = pool[position]

            duration = activity.get(
                "estimated_visit_duration_minutes"
            )

            if duration is None:
                planning_duration = (
                    FALLBACK_ACTIVITY_DURATION_MINUTES
                )
                duration_is_estimated = True
            else:
                planning_duration = max(
                    1,
                    int(duration),
                )
                duration_is_estimated = False

            remaining = (
                max_activity_minutes
                - packed_minutes
            )

            if (
                activities_added > 0
                and planning_duration > remaining
            ):
                break

            cursor[dest_id] = position + 1

            source = (
                "estimated_visit_durations"
                if not duration_is_estimated
                else "planning_fallback_duration"
            )

            self._add_drawer(
                shelf=shelf,
                name=activity["name"],
                description=activity.get(
                    "description"
                ),
                start_time=None,
                duration_minutes=planning_duration,
                sort_order=order,
                activity_type="EXPERIENCE",
                activity_id=activity["id"],
                source=source,
                is_fallback=duration_is_estimated,
                destination_id=dest_id,
            )

            if first_activity_id is None:
                first_activity_id = str(
                    activity["id"]
                )

            packed_minutes += planning_duration
            activities_added += 1
            order += 1

            if packed_minutes >= max_activity_minutes:
                break

        if activities_added == 0:

            title, description = _fallback_drawer_text(
                dest_type,
                fallback_counters[dest_id],
            )

            fallback_counters[dest_id] += 1

            self._add_drawer(
                shelf=shelf,
                name=title,
                description=description,
                start_time=None,
                duration_minutes=120,
                sort_order=order,
                activity_type="FREE_TIME",
                source="fallback_activity",
                is_fallback=True,
                destination_id=dest_id,
            )

            logger.warning(
                "Day %s at %s: no cultural activities "
                "available; explicit fallback used.",
                day_number,
                dest_id,
            )

            order += 1

        self._add_drawer(
            shelf=shelf,
            name="Lunch",
            description=None,
            start_time=None,
            duration_minutes=60,
            sort_order=order,
            activity_type="MEAL",
            source="hardcoded_meal",
            destination_id=dest_id,
        )

        order += 1

        remaining_after_lunch = (
            max_activity_minutes
            - packed_minutes
            - 60
        )

        if (
            remaining_after_lunch >= 60
            and cursor.get(dest_id, 0) < len(pool)
        ):

            position = cursor.get(
                dest_id,
                0,
            )

            activity = pool[position]

            duration = activity.get(
                "estimated_visit_duration_minutes"
            )

            if duration is None:
                planning_duration = (
                    FALLBACK_ACTIVITY_DURATION_MINUTES
                )
                duration_is_estimated = True
            else:
                planning_duration = max(
                    1,
                    int(duration),
                )
                duration_is_estimated = False

            if planning_duration <= remaining_after_lunch:

                cursor[dest_id] = position + 1

                self._add_drawer(
                    shelf=shelf,
                    name=activity["name"],
                    description=activity.get(
                        "description"
                    ),
                    start_time=None,
                    duration_minutes=planning_duration,
                    sort_order=order,
                    activity_type="EXPERIENCE",
                    activity_id=activity["id"],
                    source=(
                        "estimated_visit_durations"
                        if not duration_is_estimated
                        else "planning_fallback_duration"
                    ),
                    is_fallback=duration_is_estimated,
                    destination_id=dest_id,
                )

                if first_activity_id is None:
                    first_activity_id = str(
                        activity["id"]
                    )

                order += 1

        if "relaxed_pace" in travel_style:

            self._add_drawer(
                shelf=shelf,
                name="Free evening",
                description=(
                    "Unstructured evening time after "
                    "the day's exploration."
                ),
                start_time=None,
                duration_minutes=90,
                sort_order=order,
                activity_type="FREE_TIME",
                source="hardcoded_relaxed_pace",
                is_fallback=True,
                destination_id=dest_id,
            )

        return first_activity_id

    # -----------------------------------------------------------------
    # STANDARD DAYS
    # -----------------------------------------------------------------

    def _populate_standard_day_drawers(
        self,
        shelf: Shelf,
        pool: list[dict[str, Any]],
        cursor: dict[str, int],
        fallback_counters: dict[str, int],
        dest_id: str,
        dest_type: str | None,
        travel_style: list[str],
        day_number: int,
        is_arrival_day: bool,
        origin_dest_id: str | None,
        legs: list[dict[str, Any]],
        destination_index: int,
    ) -> str | None:

        order = 1
        first_activity_id: str | None = None

        if is_arrival_day:
            order, _ = self._destination_arrival_transfer(
                shelf=shelf,
                order=order,
                leg=self._get_arrival_leg(
                    legs=legs,
                    destination_index=destination_index,
                ),
            )
            order += 1

        if is_arrival_day:
            morning = None
        else:
            morning = self._consume_next_activity(
                pool=pool,
                cursor=cursor,
                dest_id=dest_id,
            )

        if morning:

            first_activity_id = str(
                morning["id"]
            )

            morning_is_game_drive = (
                _is_game_drive_category(
                    morning.get("category")
                )
            )

            duration = morning.get(
                "estimated_visit_duration_minutes"
            )

            duration_is_fallback = duration is None

            if duration is None:
                duration = (
                    FALLBACK_MORNING_ACTIVITY_MINUTES
                )

            self._add_drawer(
                shelf=shelf,
                name=morning["name"],
                description=morning.get(
                    "description"
                ),
                start_time=(
                    DEFAULT_GAME_DRIVE_START
                    if morning_is_game_drive
                    else None
                ),
                duration_minutes=int(duration),
                sort_order=order,
                activity_type="EXPERIENCE",
                activity_id=morning["id"],
                source=(
                    "estimated_visit_durations"
                    if not duration_is_fallback
                    else "planning_fallback_duration"
                ),
                is_fallback=duration_is_fallback,
                destination_id=dest_id,
            )

            order += 1

        elif not is_arrival_day:

            title, description = _fallback_drawer_text(
                dest_type,
                fallback_counters[dest_id],
            )

            fallback_counters[dest_id] += 1

            self._add_drawer(
                shelf=shelf,
                name=title,
                description=description,
                start_time=None,
                duration_minutes=180,
                sort_order=order,
                activity_type="EXPERIENCE",
                source="fallback_activity",
                is_fallback=True,
                destination_id=dest_id,
            )

            logger.warning(
                "Day %s at %s: activity pool exhausted "
                "(morning slot); fallback used.",
                day_number,
                dest_id,
            )

            order += 1

        self._add_drawer(
            shelf=shelf,
            name="Lunch at the lodge",
            description=None,
            start_time=None,
            duration_minutes=60,
            sort_order=order,
            activity_type="MEAL",
            source="hardcoded_meal",
            destination_id=dest_id,
        )

        order += 1

        afternoon = self._consume_next_activity(
            pool=pool,
            cursor=cursor,
            dest_id=dest_id,
        )

        if afternoon:

            if first_activity_id is None:
                first_activity_id = str(
                    afternoon["id"]
                )

            afternoon_is_game_drive = (
                _is_game_drive_category(
                    afternoon.get("category")
                )
            )

            duration = afternoon.get(
                "estimated_visit_duration_minutes"
            )

            duration_is_fallback = duration is None

            if duration is None:
                duration = (
                    FALLBACK_AFTERNOON_ACTIVITY_MINUTES
                )

            self._add_drawer(
                shelf=shelf,
                name=afternoon["name"],
                description=afternoon.get(
                    "description"
                ),
                start_time=(
                    EVENING_GAME_DRIVE_START
                    if afternoon_is_game_drive
                    else None
                ),
                duration_minutes=int(duration),
                sort_order=order,
                activity_type="EXPERIENCE",
                activity_id=afternoon["id"],
                source=(
                    "estimated_visit_durations"
                    if not duration_is_fallback
                    else "planning_fallback_duration"
                ),
                is_fallback=duration_is_fallback,
                destination_id=dest_id,
            )

        else:

            title, description = _fallback_drawer_text(
                dest_type,
                fallback_counters[dest_id],
            )

            fallback_counters[dest_id] += 1

            self._add_drawer(
                shelf=shelf,
                name=title,
                description=description,
                start_time=None,
                duration_minutes=150,
                sort_order=order,
                activity_type="EXPERIENCE",
                source="fallback_activity",
                is_fallback=True,
                destination_id=dest_id,
            )

            logger.warning(
                "Day %s at %s: activity pool exhausted "
                "(afternoon slot); fallback used.",
                day_number,
                dest_id,
            )

        order += 1

        if "relaxed_pace" in travel_style:

            self._add_drawer(
                shelf=shelf,
                name="Sundowner at the lodge",
                description=(
                    "Relaxed evening time at the lodge."
                ),
                start_time=None,
                duration_minutes=60,
                sort_order=order,
                activity_type="EXPERIENCE",
                source="hardcoded_relaxed_pace",
                is_fallback=True,
                destination_id=dest_id,
            )

        return first_activity_id

    # -----------------------------------------------------------------
    # ARRIVAL TRANSFER
    # -----------------------------------------------------------------

    def _get_arrival_leg(
        self,
        legs: list[dict[str, Any]],
        destination_index: int,
    ) -> dict[str, Any] | None:

        if destination_index <= 0:
            return None

        expected_from_index = destination_index - 1

        if expected_from_index >= len(legs):
            return None

        leg = legs[expected_from_index]

        if not isinstance(
            leg,
            Mapping,
        ):
            return None

        return dict(leg)

    def _destination_arrival_transfer(
        self,
        shelf: Shelf,
        order: int,
        leg: dict[str, Any] | None,
    ) -> tuple[int, bool]:

        if leg is None:

            logger.warning(
                "Arrival day %s has no RouteGeography "
                "transition fact.",
                shelf.day_number,
            )

            # Neutral traveler-facing presentation.
            # The missing fact remains internal.
            self._add_drawer(
                shelf=shelf,
                name="Arrival transfer",
                description="Transfer into the destination.",
                start_time=None,
                duration_minutes=(
                    FALLBACK_ARRIVAL_TRANSFER_MINUTES
                ),
                sort_order=order,
                activity_type="TRANSFER",
                source="unavailable",
                is_fallback=True,
                destination_id=shelf.destination_id,
            )

            return (
                order,
                False,
            )

        duration = self._coerce_optional_int(
            leg.get("duration_minutes")
        )

        mode = (
            str(leg.get("mode")).strip()
            if leg.get("mode")
            else None
        )

        source = (
            str(leg.get("source")).strip()
            if leg.get("source")
            else "unavailable"
        )

        description = (
            "Transfer into the destination. "
            + _format_transfer_description(
                mode=mode,
                minutes=duration,
            )
        )

        effective_duration = (
            duration
            if duration is not None
            else FALLBACK_ARRIVAL_TRANSFER_MINUTES
        )

        self._add_drawer(
            shelf=shelf,
            name="Arrival transfer",
            description=description,
            start_time=None,
            duration_minutes=effective_duration,
            sort_order=order,
            activity_type="TRANSFER",
            source=source,
            is_fallback=duration is None,
            destination_id=shelf.destination_id,
        )

        return (
            order,
            duration is not None,
        )

    # -----------------------------------------------------------------
    # ACTIVITY CONSUMPTION
    # -----------------------------------------------------------------

    @staticmethod
    def _consume_next_activity(
        pool: list[dict[str, Any]],
        cursor: dict[str, int],
        dest_id: str,
    ) -> dict[str, Any] | None:

        position = cursor.get(
            dest_id,
            0,
        )

        if position >= len(pool):
            return None

        activity = pool[position]

        cursor[dest_id] = position + 1

        return activity

    # -----------------------------------------------------------------
    # DRAWER CREATION
    # -----------------------------------------------------------------

    @staticmethod
    def _add_drawer(
        shelf: Shelf,
        name: str,
        description: str | None,
        start_time: dt_time | None,
        duration_minutes: int,
        sort_order: int,
        activity_type: str,
        activity_id: Any = None,
        source: str = "activities_table",
        is_fallback: bool = False,
        destination_id: str | None = None,
    ) -> Drawer:

        drawer = Drawer(
            shelf_id=shelf.id,
            activity_id=activity_id,
            name=name,
            description=description,
            start_time=start_time,
            duration_minutes=max(
                0,
                int(duration_minutes),
            ),
            sort_order=sort_order,
            activity_type=activity_type,
            source=source,
            is_fallback=is_fallback,
            destination_id=(
                destination_id
                if destination_id is not None
                else getattr(
                    shelf,
                    "destination_id",
                    None,
                )
            ),
        )

        shelf.drawers.append(drawer)

        return drawer

    # -----------------------------------------------------------------
    # ROUTE / HINGES
    # -----------------------------------------------------------------

    def _build_hinges(
        self,
        cabinet: Any,
        destination_ids: list[str],
        meta: dict[str, dict[str, Any]],
        route_facts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:

        cabinet_id = cabinet.id

        legs: list[dict[str, Any]] = []

        sequence = 0

        for index in range(
            len(destination_ids) - 1
        ):

            frm = destination_ids[index]
            to = destination_ids[index + 1]

            if frm == to:
                continue

            route_fact = self._find_route_fact(
                route_facts=route_facts,
                from_destination=frm,
                to_destination=to,
            )

            from_country = meta.get(
                frm,
                {},
            ).get("country")

            to_country = meta.get(
                to,
                {},
            ).get("country")

            if route_fact is not None:
                is_inter_country = bool(
                    route_fact.get(
                        "is_inter_country",
                        False,
                    )
                )
            else:
                is_inter_country = bool(
                    from_country
                    and to_country
                    and from_country != to_country
                )

            duration_minutes: int | None = None
            distance_km: float | None = None
            source = "unavailable"
            mode: str | None = None
            border_crossing_id = None
            requires_border_crossing = False

            if route_fact:

                duration_minutes = (
                    self._coerce_optional_int(
                        route_fact.get(
                            "duration_minutes"
                        )
                    )
                )

                distance_km = (
                    self._coerce_optional_float(
                        route_fact.get(
                            "distance_km"
                        )
                    )
                )

                source = (
                    str(
                        route_fact.get("source")
                    ).strip()
                    if route_fact.get("source")
                    else "unavailable"
                )

                mode = (
                    str(
                        route_fact.get("mode")
                    ).strip()
                    if route_fact.get("mode")
                    else None
                )

                border_crossing_id = (
                    route_fact.get(
                        "border_crossing_id"
                    )
                )

                # Only claim a land-border fact when the authoritative
                # route fact actually says that one is required.

                requires_border_crossing = bool(
                    route_fact.get(
                        "requires_border_crossing",
                        False,
                    )
                )

            if (
                duration_minutes is None
                and source != "unavailable"
            ):
                source = (
                    f"{source}:duration_unavailable"
                )

            if duration_minutes is None:
                logger.warning(
                    "Route duration unavailable for "
                    "%s -> %s. No duration is invented.",
                    frm,
                    to,
                )

            if mode is None:
                logger.warning(
                    "Route transport mode unavailable for "
                    "%s -> %s. No transport mode is invented.",
                    frm,
                    to,
                )

            sequence += 1

            hinge = Hinge(
                cabinet_id=cabinet_id,
                from_destination_id=frm,
                to_destination_id=to,
                sequence_order=sequence,
                distance_km=distance_km,
                duration_minutes=duration_minutes,
                mode=mode,
                source=source,
                is_inter_country=is_inter_country,
                requires_border_crossing=(
                    requires_border_crossing
                ),
                border_crossing_id=border_crossing_id,
            )

            self.db.add(hinge)

            if hasattr(
                hinge,
                "cabinet",
            ):
                hinge.cabinet = cabinet

            elif hinge not in cabinet.hinges:
                cabinet.hinges.append(hinge)

            legs.append(
                {
                    "from": frm,
                    "to": to,
                    "duration_minutes": duration_minutes,
                    "distance_km": distance_km,
                    "source": source,
                    "mode": mode,
                    "is_inter_country": is_inter_country,
                    "requires_border_crossing": (
                        requires_border_crossing
                    ),
                    "border_crossing_id": (
                        border_crossing_id
                    ),
                }
            )

        return legs

    @staticmethod
    def _find_route_fact(
        route_facts: list[dict[str, Any]],
        from_destination: str,
        to_destination: str,
    ) -> dict[str, Any] | None:
        """
        Exact transition matching.

        This deliberately does not rely on a dictionary keyed only by
        destination ID because routes such as A -> B -> A contain
        multiple occurrences of the same destination.
        """

        for fact in route_facts:

            if not isinstance(
                fact,
                Mapping,
            ):
                continue

            fact_from = (
                fact.get("from")
                or fact.get(
                    "from_destination_id"
                )
                or fact.get(
                    "origin_destination_id"
                )
            )

            fact_to = (
                fact.get("to")
                or fact.get(
                    "to_destination_id"
                )
                or fact.get(
                    "destination_id"
                )
            )

            if (
                str(fact_from)
                == str(from_destination)
                and
                str(fact_to)
                == str(to_destination)
            ):
                return dict(fact)

            nested = (
                fact.get("route")
                or fact.get("leg")
            )

            if isinstance(
                nested,
                Mapping,
            ):

                nested_from = (
                    nested.get("from")
                    or nested.get(
                        "from_destination_id"
                    )
                    or nested.get(
                        "origin_destination_id"
                    )
                )

                nested_to = (
                    nested.get("to")
                    or nested.get(
                        "to_destination_id"
                    )
                    or nested.get(
                        "destination_id"
                    )
                )

                if (
                    str(nested_from)
                    == str(from_destination)
                    and
                    str(nested_to)
                    == str(to_destination)
                ):
                    return dict(nested)

        return None

    @staticmethod
    def _coerce_optional_int(
        value: Any,
    ) -> int | None:

        if value is None:
            return None

        try:
            return max(
                0,
                int(value),
            )
        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _coerce_optional_float(
        value: Any,
    ) -> float | None:

        if value is None:
            return None

        try:
            return float(value)
        except (
            TypeError,
            ValueError,
        ):
            return None

    # -----------------------------------------------------------------
    # ACCOMMODATION
    # -----------------------------------------------------------------

    def _populate_headboard(
        self,
        shelf: Shelf,
        dest_id: str,
        budget_tier: str,
        remaining_nights_here: int,
        warnings: list[str],
    ) -> None:
        """
        Accommodation is only persisted when a real lodge record
        exists.

        A fabricated "Luxury lodge" / "Mid lodge" label is deliberately
        not created because that would present an invented property
        as factual accommodation.
        """

        tier_map = {
            "budget": (
                "budget",
                "camping",
            ),
            "mid": (
                "mid_range",
            ),
            "luxury": (
                "luxury",
                "ultra_luxury",
            ),
        }

        tiers = tier_map.get(
            budget_tier,
            ("mid_range",),
        )

        row = self.db.execute(
            text(
                """
                SELECT
                    id,
                    name,
                    tier
                FROM lodges
                WHERE destination_id =
                    CAST(:dest_id AS uuid)
                  AND tier::text =
                    ANY(
                        CAST(:tiers AS text[])
                    )
                ORDER BY
                    star_rating DESC NULLS LAST
                LIMIT 1
                """
            ),
            {
                "dest_id": dest_id,
                "tiers": list(tiers),
            },
        ).fetchone()

        if not row:

            warnings.append(
                f"No seeded lodge matched destination "
                f"{dest_id} for budget tier "
                f"'{budget_tier}'. Accommodation was "
                "left unassigned rather than fabricated."
            )

            logger.warning(
                "No lodge found for destination %s and tier %s. "
                "No generic accommodation name will be created.",
                dest_id,
                budget_tier,
            )

            return

        check_out = None

        if (
            shelf.date
            and remaining_nights_here > 0
        ):
            check_out = (
                shelf.date
                + timedelta(
                    days=remaining_nights_here
                )
            )

        headboard = Headboard(
            shelf_id=shelf.id,
            lodge_id=row[0],
            name=row[1],
            tier=row[2],
            check_in=shelf.date,
            check_out=check_out,
            nights=remaining_nights_here,
        )

        self.db.add(headboard)

    # -----------------------------------------------------------------
    # ARMREST / TRANSPORT
    # -----------------------------------------------------------------

    def _populate_armrest(
        self,
        shelf: Shelf,
        legs: list[dict[str, Any]],
        destination_index: int,
        is_arrival_day: bool,
    ) -> None:
        """
        Armrest represents inter-destination transport only when the
        current day actually corresponds to a route transition.

        Important schema rule:
            armrests.mode is NOT NULL.

        Therefore an Armrest row cannot represent an unknown transport
        mode with mode=None.

        We do NOT invent a sentinel such as:
            "unknown"
            "transfer"
            "road_transfer"
            "private_4x4"

        Instead, when the authoritative route fact does not provide a
        mode, the Armrest record is omitted. The route fact remains
        represented by the Hinge and the neutral Drawer presentation.

        This prevents the confirmed PostgreSQL error:

            null value in column "mode" of relation "armrests"
            violates not-null constraint
        """

        if not is_arrival_day:
            return

        leg = self._get_arrival_leg(
            legs=legs,
            destination_index=destination_index,
        )

        if leg is None:

            logger.warning(
                "Arrival day %s has no RouteGeography "
                "transport leg. Armrest omitted because "
                "armrests.mode is NOT NULL.",
                shelf.day_number,
            )

            return

        minutes = self._coerce_optional_int(
            leg.get("duration_minutes")
        )

        raw_mode = leg.get("mode")

        mode = (
            str(raw_mode).strip()
            if raw_mode
            else None
        )

        # Confirmed production constraint:
        # armrests.mode cannot be NULL.
        #
        # Do not fabricate a transport mode merely to satisfy
        # persistence. The Hinge remains the authoritative route fact.

        if not mode:
            logger.warning(
                "Arrival transport mode is unknown for "
                "day %s. Armrest omitted rather than "
                "fabricating mode.",
                shelf.day_number,
            )
            return

        description = _format_transfer_description(
            mode=mode,
            minutes=minutes,
        )

        normalized_mode = mode.strip().lower()

        is_private = (
            normalized_mode == "private_4x4"
        )

        armrest = Armrest(
            shelf_id=shelf.id,
            mode=mode,
            description=description,
            duration_minutes=minutes,
            is_private=is_private,
        )

        self.db.add(armrest)

    # -----------------------------------------------------------------
    # MEALS
    # -----------------------------------------------------------------

    def _populate_trays(
        self,
        shelf: Shelf,
        is_first_day: bool,
        is_last_day: bool,
        is_transit_day: bool,
    ) -> None:

        if is_first_day:
            meals = ["dinner"]

        elif is_transit_day:
            meals = ["dinner"]

        elif is_last_day:
            meals = ["breakfast"]

        else:
            meals = [
                "breakfast",
                "lunch",
                "dinner",
            ]

        for meal in meals:

            self.db.add(
                Tray(
                    shelf_id=shelf.id,
                    meal_type=meal,
                    included=True,
                )
            )

    # -----------------------------------------------------------------
    # DAY PHOTO
    # -----------------------------------------------------------------

    def _populate_day_photo(
        self,
        shelf: Shelf,
        activity_id: Any,
        destination_id: str,
    ) -> None:

        try:
            exists = self.db.execute(
                text(
                    """
                    SELECT to_regclass(
                        'photo_states'
                    )
                    """
                )
            ).scalar()

        except Exception as exc:
            logger.warning(
                "Could not check photo_states table: %s",
                exc,
            )
            return

        if not exists:
            return

        row = None

        try:
            row = self.db.execute(
                text(
                    """
                    SELECT url
                    FROM photo_states
                    WHERE activity_id =
                        CAST(:activity_id AS uuid)
                      AND url IS NOT NULL
                    ORDER BY id
                    LIMIT 1
                    """
                ),
                {
                    "activity_id": activity_id
                },
            ).fetchone()

        except Exception as exc:
            logger.debug(
                "Activity-specific photo lookup unavailable: %s",
                exc,
            )

        if not row:

            try:
                row = self.db.execute(
                    text(
                        """
                        SELECT url
                        FROM photo_states
                        WHERE destination_id =
                            CAST(:destination_id AS uuid)
                          AND url IS NOT NULL
                        ORDER BY id
                        LIMIT 1
                        """
                    ),
                    {
                        "destination_id": destination_id
                    },
                ).fetchone()

            except Exception as exc:
                logger.debug(
                    "Destination photo lookup unavailable: %s",
                    exc,
                )

        if not row or not row[0]:
            return

        image_url = row[0]

        for field_name in (
            "hero_image_url",
            "image_url",
            "photo_url",
            "cover_image_url",
        ):

            if hasattr(
                shelf,
                field_name,
            ):

                setattr(
                    shelf,
                    field_name,
                    image_url,
                )

                return

    # -----------------------------------------------------------------
    # THEMES
    # -----------------------------------------------------------------

    @staticmethod
    def _theme_for(
        idx: int,
        night_idx: int,
        is_first: bool,
        is_last: bool,
        destination_type: str | None,
        is_transit_day: bool = False,
        archetype: Any = None,
    ) -> str:

        if is_transit_day:
            return "Travel day"

        if is_first:
            return "Arrival & slow start"

        if is_last:
            return "Departure"

        archetype_normalized = (
            _normalise_archetype(archetype)
        )

        if archetype_normalized in {
            "cultural",
            "culture",
            "cultural_day",
            "city_exploration",
            "city",
            "heritage",
        }:
            return "Culture & discovery"

        normalized_type = (
            str(destination_type).strip().lower()
            if destination_type
            else None
        )

        if normalized_type in {
            "national_park",
            "game_reserve",
        }:
            return (
                "Wildlife & wide horizons"
                if night_idx == 0
                else "Deeper into the park"
            )

        if normalized_type in {
            "island",
            "beach",
            "marine_park",
        }:
            return "Coast, water & open horizons"

        if normalized_type in {
            "mountain",
            "waterfall",
            "forest_reserve",
        }:
            return "Nature & exploration"

        if normalized_type in {
            "city",
            "cultural_site",
            "unesco_site",
        }:
            return "Culture & discovery"

        return "Explore the destination"

    # -----------------------------------------------------------------
    # STYLE / TITLE
    # -----------------------------------------------------------------

    @staticmethod
    def _infer_style(
        request: dict[str, Any],
    ) -> list[str]:

        styles: list[str] = []

        focus = request.get("focus")

        if focus:

            normalized_focus = (
                str(focus).strip().lower()
            )

            if normalized_focus in {
                "wildlife",
                "beach",
                "adventure",
                "culture",
                "cultural",
                "photography",
                "birding",
                "walking",
            }:

                styles.append(
                    normalized_focus
                )

        if (
            str(
                request.get(
                    "budget_tier",
                    "",
                )
            ).strip().lower()
            == "luxury"
        ):
            styles.append("luxury")

        if (
            ItineraryPlanningEngine._safe_int(
                request.get("travelers"),
                2,
            )
            <= 2
        ):
            styles.append("private")

        # Relaxed pace remains an inferred product style.
        # It does not override explicit focus and does not create
        # factual transport or attraction data.

        styles.append("relaxed_pace")

        return list(
            dict.fromkeys(styles)
        )

    @staticmethod
    def _default_title(
        request: dict[str, Any],
    ) -> str:

        country = request.get(
            "country_name",
            "Africa",
        )

        return (
            f"{country}, Wild & Unhurried"
        )
