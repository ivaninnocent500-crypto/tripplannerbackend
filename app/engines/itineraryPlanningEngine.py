"""
ItineraryPlanningEngine v4
---------------------------

Production itinerary builder for the persisted furniture schema.

Pipeline contract
-----------------
The orchestrator is responsible for:

    RulesEngine
        ↓
    RouteGeographyEngine
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

The engine remains destination-aware but does not create separate
engines for safari/city/cultural destinations.

No-fabrication rules
--------------------
- Route order is authoritative.
- Real drive/flight duration is preferred.
- Unknown route duration remains unknown in the displayed description.
- Internal fallback duration may be used for scheduling only and is
  marked as fallback.
- No invented attraction names are generated from database-free facts.
- Only game-drive drawers receive absolute clock times.
- All other drawers use duration_minutes + sort_order.
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

DRIVE_TO_FLIGHT_THRESHOLD_MINUTES = 6 * 60

FALLBACK_ARRIVAL_TRANSFER_MINUTES = 60
FALLBACK_DEPARTURE_TRANSFER_MINUTES = 60
FALLBACK_TRANSIT_LEG_MINUTES = 60

# Reused from the existing activity-constraints contract.
# The import is intentionally guarded because deployments may have
# slightly different module layouts during migration.
try:
    from app.engine.activity_constraints import MAX_NORMAL_ACTIVITY_HOURS_PER_DAY
except ImportError:
    try:
        from app.engines.activity_constraints import MAX_NORMAL_ACTIVITY_HOURS_PER_DAY
    except ImportError:
        # Conservative compatibility value only if the existing module
        # is temporarily unavailable. This is NOT used as a factual
        # activity duration; it is only a packing ceiling.
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
            "Free time at the lodge's beach area — no specific excursion "
            "booked; the operator will offer what suits sea conditions.",
        ),
        (
            "Shoreline time",
            "Open beach time at the lodge — no guided excursion booked "
            "for this slot.",
        ),
    ],
    "beach": [
        (
            "Beach & relaxation",
            "Free time at the lodge's beach area — no specific excursion "
            "booked; the operator will offer what suits sea conditions.",
        ),
        (
            "Shoreline time",
            "Unstructured time along the beach — no guided activity "
            "booked for this slot.",
        ),
    ],
    "mountain": [
        (
            "Acclimatisation time",
            "Time to acclimatise at a comfortable pace — no summit attempt "
            "is scheduled for this slot.",
        ),
        (
            "Rest & recovery",
            "Open time at camp to rest and recover.",
        ),
    ],
    "desert": [
        (
            "Desert nature time",
            "Open time suited to the surrounding terrain — no specific "
            "site is booked for this slot.",
        ),
        (
            "Open camp time",
            "Unstructured time at camp — no specific excursion booked.",
        ),
    ],
    "marine_park": [
        (
            "Shore time",
            "Open time at the lodge — optional water-based excursions may "
            "be offered if conditions allow.",
        ),
    ],
    "city": [
        (
            "Local exploration time",
            "Open time for local exploration — no specific venue is "
            "booked in advance.",
        ),
        (
            "Free time to explore",
            "Unstructured time to explore independently — no guided "
            "activity booked for this slot.",
        ),
    ],
    "cultural_site": [
        (
            "Cultural exploration time",
            "Open time around the destination — no specific additional "
            "venue is booked in advance.",
        ),
    ],
    "unesco_site": [
        (
            "Heritage exploration time",
            "Open time around the destination — no specific additional "
            "venue is booked in advance.",
        ),
    ],
    "national_park": [
        (
            "Guided wilderness drive",
            "Game drive on lodge circuits — no specific route booked in "
            "advance. Times may shift with conditions.",
        ),
        (
            "Photographic drive",
            "A slower-paced drive focused on photography opportunities — "
            "no specific route booked in advance.",
        ),
        (
            "Bush walk near camp",
            "Short guided walk near the lodge grounds, conditions "
            "permitting — no specific route booked in advance.",
        ),
    ],
    "game_reserve": [
        (
            "Guided wilderness drive",
            "Game drive on lodge circuits — no specific route booked in "
            "advance. Times may shift with conditions.",
        ),
        (
            "Photographic drive",
            "A slower-paced drive focused on photography opportunities — "
            "no specific route booked in advance.",
        ),
    ],
}


_DEFAULT_FALLBACK = [
    (
        "Time at the lodge",
        "Open time at the lodge — operator will offer what suits the "
        "day's conditions.",
    ),
]


# ---------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------

@dataclass
class BuildResult:
    cabinet: Cabinet
    warnings: list[str] = field(default_factory=list)


def _is_game_drive_category(category: str | None) -> bool:
    """
    Absolute clock times are intentionally restricted to game drives.

    Walking safaris, hiking and climbing do NOT receive fixed clock
    times because their exact start depends on local conditions,
    guide/operator planning and destination-specific constraints.
    """
    return str(category or "").lower() == "game_drive"


def _fallback_drawer_text(
    destination_type: str | None,
    variant_index: int,
) -> tuple[str, str]:
    variants = (
        FALLBACK_VARIANTS.get(destination_type or "", _DEFAULT_FALLBACK)
        or _DEFAULT_FALLBACK
    )
    return variants[variant_index % len(variants)]


def _merged_ranked_categories(
    destination_type: str | None,
    travel_style: list[str],
    focus: str | None = None,
) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    tags: list[str] = []

    if focus:
        tags.append(str(focus).lower())

    tags.extend(str(style).lower() for style in travel_style)

    for tag in tags:
        for category in TRAVEL_STYLE_CATEGORY_RANKS.get(tag, []):
            if category not in seen:
                seen.add(category)
                result.append(category)

    if destination_type:
        for category in DESTINATION_TYPE_CATEGORY_RANKS.get(
            str(destination_type).lower(),
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
    mode_label = {
        "scheduled_flight": "Scheduled flight",
        "charter_flight": "Charter flight",
        "private_4x4": "Private 4x4",
    }.get(mode or "private_4x4", "Transfer")

    if minutes is None:
        return (
            f"{mode_label} · duration unavailable — "
            "confirm before booking"
        )

    return f"{mode_label} · approximately {minutes} min"


def _normalise_archetype(value: Any) -> str | None:
    """
    DayArchetypeEngine implementations may expose either:

        "CULTURAL"

    or:

        {"archetype": "CULTURAL"}

    or an enum-like object.

    Keep this adapter local so the planner does not become coupled to
    one representation.
    """
    if value is None:
        return None

    if isinstance(value, Mapping):
        for key in ("archetype", "day_archetype", "kind", "type"):
            if value.get(key) is not None:
                value = value[key]
                break

    if hasattr(value, "value"):
        value = value.value

    if value is None:
        return None

    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _is_cultural_archetype(value: Any) -> bool:
    normalized = _normalise_archetype(value)

    return normalized in {
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
    def __init__(self, db: Session):
        self.db = db

    # -----------------------------------------------------------------
    # PUBLIC METADATA CONTRACT
    # -----------------------------------------------------------------

    def fetch_destination_meta(
        self,
        destination_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """
        Public adapter used by ItineraryOrchestrator.

        The actual query remains private so existing internal callers
        continue to use _fetch_destination_meta().
        """
        return self._fetch_destination_meta(destination_ids)

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
    ) -> BuildResult:
        """
        Build the persisted itinerary.

        day_allocation:
            One allocation value per effective destination.

        transit_days:
            day_number -> bool.

        day_archetypes:
            day_number -> DayArchetypeEngine classification.

        All three are supplied by the orchestrator so this engine does
        not independently invent route/day classification.
        """

        days = self._safe_int(request.get("days"), 0)

        if days < 1:
            raise ValueError("Trip must contain at least one day.")

        travelers = max(
            1,
            self._safe_int(request.get("travelers"), 1),
        )

        travel_style = request.get("travel_style") or self._infer_style(
            request
        )

        if isinstance(travel_style, str):
            travel_style = [travel_style]

        travel_style = list(
            dict.fromkeys(
                str(style).lower()
                for style in travel_style
                if style
            )
        )

        focus = request.get("focus", "wildlife")
        budget_tier = str(
            request.get("budget_tier", "mid")
        ).lower()

        start_date_raw = request.get("start_date")
        start_date = None

        if start_date_raw:
            try:
                start_date = (
                    start_date_raw
                    if isinstance(start_date_raw, date)
                    else date.fromisoformat(str(start_date_raw))
                )
            except ValueError as exc:
                raise ValueError(
                    f"Invalid start_date: {start_date_raw}"
                ) from exc

        if not destination_ids:
            raise ValueError(
                "Itinerary generation requires at least one destination."
            )

        # The orchestrator should already have cleaned this route.
        # We repeat the normalization defensively so the engine's
        # allocation contract remains deterministic.
        cleaned_destination_ids = list(
            dict.fromkeys(
                str(destination_id)
                for destination_id in destination_ids
                if destination_id
            )
        )

        if len(cleaned_destination_ids) > days:
            logger.warning(
                "Trip requests %s destinations but only %s days. "
                "Only the first %s destinations can receive an "
                "overnight.",
                len(cleaned_destination_ids),
                days,
                days,
            )
            cleaned_destination_ids = cleaned_destination_ids[:days]

        destination_ids = cleaned_destination_ids

        if len(day_allocation) != len(destination_ids):
            raise ValueError(
                "day_allocation length "
                f"({len(day_allocation)}) does not match "
                f"destination_ids length ({len(destination_ids)}) "
                "after cleaning."
            )

        transit_days = transit_days or {}
        day_archetypes = day_archetypes or {}

        cabinet = Cabinet(
            request_json=request,
            title=request.get("title")
            or self._default_title(request),
            duration_days=days,
            travelers_adults=travelers,
            travelers_children=self._safe_int(
                request.get("travelers_children"),
                0,
            ),
            travel_style=travel_style,
            budget_tier=budget_tier,
            status="draft",
            start_date=start_date,
            end_date=(
                start_date + timedelta(days=days - 1)
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
                "Some requested destinations could not be resolved "
                "in travel_places: "
                + ", ".join(missing_meta)
            )

        if hasattr(cabinet, "route_countries"):
            countries = [
                meta[d].get("country")
                for d in destination_ids
                if meta.get(d, {}).get("country")
            ]
            cabinet.route_countries = list(
                dict.fromkeys(countries)
            )

        if hasattr(cabinet, "primary_country"):
            cabinet.primary_country = meta.get(
                destination_ids[0],
                {},
            ).get("country")

        legs = self._build_hinges(
            cabinet=cabinet,
            destination_ids=destination_ids,
            meta=meta,
        )

        destination_types = {
            destination_id: meta.get(
                destination_id,
                {},
            ).get("destination_type")
            for destination_id in destination_ids
        }

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

            per_destination_pool[destination_id] = pool

            if not pool:
                warnings.append(
                    f"Destination {destination_id} has no seeded "
                    "activities. Explicit fallback time will be used."
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

        day_number = 1
        current_date = start_date

        for destination_index, destination_id in enumerate(
            destination_ids
        ):
            nights_here = day_allocation[destination_index]

            if nights_here <= 0:
                continue

            destination_type = destination_types.get(
                destination_id
            )

            for night_index in range(nights_here):
                is_first_day = day_number == 1
                is_last_day = day_number == days

                is_arrival_day = (
                    night_index == 0
                    and destination_index > 0
                )

                is_transit_day = bool(
                    transit_days.get(day_number, False)
                )

                archetype = day_archetypes.get(day_number)

                shelf = Shelf(
                    cabinet_id=cabinet.id,
                    day_number=day_number,
                    date=current_date,
                    destination_id=destination_id,
                    theme=self._theme_for(
                        destination_index,
                        night_index,
                        is_first_day,
                        is_last_day,
                        destination_type,
                        is_transit_day,
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
                    destination_ids[destination_index - 1]
                    if is_arrival_day
                    and destination_index > 0
                    else None
                )

                first_activity_id = self._populate_drawers(
                    shelf=shelf,
                    pool=per_destination_pool[
                        destination_id
                    ],
                    cursor=cursors,
                    fallback_counters=fallback_counters,
                    dest_id=destination_id,
                    origin_dest_id=origin_dest_id,
                    dest_type=destination_type,
                    travel_style=travel_style,
                    focus=focus,
                    day_number=day_number,
                    is_first_day=is_first_day,
                    is_last_day=is_last_day,
                    is_arrival_day=is_arrival_day,
                    is_transit_day=is_transit_day,
                    day_archetype=archetype,
                    legs=legs,
                    destination_index=destination_index,
                )

                self._populate_headboard(
                    shelf=shelf,
                    dest_id=destination_id,
                    budget_tier=budget_tier,
                    remaining_nights_here=(
                        nights_here - night_index
                    ),
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
                    is_last_day=is_last_day,
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
        except (TypeError, ValueError):
            return default

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
                    CAST(destination_type AS text) AS destination_type
                FROM travel_places
                WHERE id = ANY(CAST(:ids AS uuid[]))
                """
            ),
            {"ids": destination_ids},
        ).fetchall()

        meta: dict[str, dict[str, Any]] = {}

        for row in rows:
            destination_id = str(row[0])

            meta[destination_id] = {
                "country": row[2],
                "headline_label": row[1],
                "destination_type": row[3],
                "min_nights": 1,
            }

        try:
            table_exists = self.db.execute(
                text(
                    "SELECT to_regclass("
                    "'estimated_visit_durations'"
                    ")"
                )
            ).scalar()

            if table_exists:
                min_rows = self.db.execute(
                    text(
                        """
                        SELECT
                            CAST(destination_id AS text),
                            MIN(recommended_nights_min)
                        FROM estimated_visit_durations
                        WHERE destination_id =
                            ANY(CAST(:ids AS uuid[]))
                          AND scope = 'full_destination'
                          AND recommended_nights_min IS NOT NULL
                        GROUP BY destination_id
                        """
                    ),
                    {"ids": destination_ids},
                ).fetchall()

                for destination_id, minimum in min_rows:
                    destination_id = str(destination_id)

                    if (
                        destination_id in meta
                        and minimum is not None
                    ):
                        meta[destination_id][
                            "min_nights"
                        ] = max(1, int(minimum))

        except Exception as exc:
            logger.warning(
                "Could not read estimated_visit_durations: %s",
                exc,
            )

        return meta

    # -----------------------------------------------------------------
    # ACTIVITY POOL
    # -----------------------------------------------------------------

    def _get_activity_duration_column(self) -> str | None:
        """
        Resolve the duration column from the actual deployed table.

        This prevents the planner from hard-coding a column name that
        may differ between schema revisions.

        Only identifiers from the inspected database schema are returned
        and inserted into SQL.
        """

        try:
            inspector = inspect(self.db.bind)

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

    def _fetch_ranked_activity_pool(
        self,
        dest_id: str,
        destination_type: str | None,
        travel_style: list[str],
        focus: str | None,
        start_date: date | None,
        cabinet_id: str,
    ) -> list[dict[str, Any]]:

        ranked_categories = _merged_ranked_categories(
            destination_type=destination_type,
            travel_style=travel_style,
            focus=focus,
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
            duration_expression = f"""
                evd.{duration_column}
            """
            duration_join = f"""
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
                    CAST(a.category AS text) AS category,
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
                                CAST(a.category AS text)
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

        try:
            rows = self.db.execute(
                sql,
                {
                    "ranked": ranked_categories,
                    "month": month_token,
                    "cab_id": cabinet_id,
                    "dest_id": dest_id,
                },
            ).fetchall()

        except Exception:
            logger.exception(
                "Failed to fetch ranked activities for "
                "destination %s",
                dest_id,
            )
            raise

        result: list[dict[str, Any]] = []

        for row in rows:
            duration = row[5]

            if duration is not None:
                try:
                    duration = max(1, int(duration))
                except (TypeError, ValueError):
                    duration = None

            result.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "description": row[2],
                    "category": row[3],
                    "difficulty": row[4],
                    "estimated_visit_duration_minutes": duration,
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

        # Priority is intentional:
        #
        # 1. First day
        # 2. Last day
        # 3. Transit day
        # 4. Cultural/city archetype
        # 5. Standard destination-aware day
        #
        # This prevents an archetype from accidentally overriding
        # structural travel-day behavior.

        if is_first_day:
            return self._populate_first_day_drawers(shelf)

        if is_last_day:
            return self._populate_last_day_drawers(shelf)

        if is_transit_day:
            return self._populate_transit_day_drawers(
                shelf=shelf,
                legs=legs,
                destination_index=destination_index,
                is_arrival_day=is_arrival_day,
            )

        if _is_cultural_archetype(day_archetype):
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
        )

    # -----------------------------------------------------------------
    # FIRST / LAST / TRANSIT
    # -----------------------------------------------------------------

    def _populate_first_day_drawers(
        self,
        shelf: Shelf,
    ) -> None:

        order = 1

        self._add_drawer(
            shelf=shelf,
            name="Airport welcome",
            description=(
                "Met at the airport by your driver-guide."
            ),
            start_time=None,
            duration_minutes=20,
            sort_order=order,
            activity_type="ARRIVAL",
            source="hardcoded_arrival_departure",
        )

        order += 1

        self._add_drawer(
            shelf=shelf,
            name="Transfer to lodge",
            description=(
                "Transfer to the lodge. No excursion is assumed "
                "during this arrival transfer."
            ),
            start_time=None,
            duration_minutes=80,
            sort_order=order,
            activity_type="TRANSFER",
            source="hardcoded_arrival_departure",
        )

        order += 1

        self._add_drawer(
            shelf=shelf,
            name="Free time at the lodge",
            description=(
                "Settle in and rest ahead of dinner."
            ),
            start_time=None,
            duration_minutes=240,
            sort_order=order,
            activity_type="FREE_TIME",
            source="hardcoded_arrival_departure",
            is_fallback=True,
        )

        order += 1

        self._add_drawer(
            shelf=shelf,
            name="Dinner at the lodge",
            description=None,
            start_time=None,
            duration_minutes=90,
            sort_order=order,
            activity_type="MEAL",
            source="hardcoded_meal",
        )

        return None

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
            name="Transfer to airport",
            description=None,
            start_time=None,
            duration_minutes=120,
            sort_order=order,
            activity_type="TRANSFER",
            source="hardcoded_arrival_departure",
        )

        order += 1

        self._add_drawer(
            shelf=shelf,
            name="Departure",
            description=None,
            start_time=None,
            duration_minutes=30,
            sort_order=order,
            activity_type="DEPARTURE",
            source="hardcoded_arrival_departure",
        )

        return None

    def _populate_transit_day_drawers(
        self,
        shelf: Shelf,
        legs: list[dict[str, Any]],
        destination_index: int,
        is_arrival_day: bool,
    ) -> None:

        order = 1

        leg = None

        if (
            is_arrival_day
            and destination_index > 0
            and destination_index - 1 < len(legs)
        ):
            leg = legs[destination_index - 1]

        mode = (
            (leg or {}).get("mode")
            or "charter_flight"
        )

        duration_minutes = (
            (leg or {}).get("duration_minutes")
        )

        transfer_description = _format_transfer_description(
            mode,
            duration_minutes,
        )

        effective_duration = (
            duration_minutes
            if duration_minutes is not None
            else FALLBACK_TRANSIT_LEG_MINUTES
        )

        self._add_drawer(
            shelf=shelf,
            name="Long-distance transfer",
            description=transfer_description,
            start_time=None,
            duration_minutes=effective_duration,
            sort_order=order,
            activity_type="TRANSFER",
            source=(
                leg.get("source")
                if leg
                else "fallback_estimate"
            ) or "fallback_estimate",
            is_fallback=duration_minutes is None,
        )

        order += 1

        self._add_drawer(
            shelf=shelf,
            name="Free time at the lodge",
            description=(
                "Most of this day is spent travelling between "
                "destinations. No activity is scheduled so there "
                "is time to rest and settle in on arrival."
            ),
            start_time=None,
            duration_minutes=180,
            sort_order=order,
            activity_type="FREE_TIME",
            source="hardcoded_transit_day",
            is_fallback=True,
        )

        order += 1

        self._add_drawer(
            shelf=shelf,
            name="Dinner at the lodge",
            description=None,
            start_time=None,
            duration_minutes=90,
            sort_order=order,
            activity_type="MEAL",
            source="hardcoded_meal",
        )

        return None

    # -----------------------------------------------------------------
    # CULTURAL / CITY DAYS
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
    ) -> str | None:
        """
        Cultural/city day planner.

        Unlike the safari standard-day template, this branch does not
        blindly create:

            morning 240 min
            lunch 60 min
            afternoon 150 min

        Instead it uses the activity's seeded estimated visit duration
        where available and packs activities against the existing
        normal-day activity-hour ceiling.

        No opening-hours or walking-distance assumptions are made here.
        Those remain separate data-driven constraints for a later stage.
        """

        order = 1
        first_activity_id: str | None = None

        # Arrival transfer comes first.
        if is_arrival_day:
            order, _ = self._destination_arrival_transfer(
                shelf=shelf,
                order=order,
                dest_id=dest_id,
                origin_dest_id=origin_dest_id,
            )
            order += 1

        max_activity_minutes = max(
            60,
            int(MAX_NORMAL_ACTIVITY_HOURS_PER_DAY * 60),
        )

        packed_minutes = 0
        activities_added = 0

        # Keep the activity pool intact if an activity does not fit.
        # This is important because an activity rejected on one day
        # should remain available for the following day.
        while cursor.get(dest_id, 0) < len(pool):
            position = cursor.get(dest_id, 0)
            activity = pool[position]

            duration = activity.get(
                "estimated_visit_duration_minutes"
            )

            if duration is None:
                # No factual duration means we cannot claim that the
                # activity consumes a specific number of minutes.
                # Use a conservative planning duration only for packing,
                # and mark it internally as estimated.
                planning_duration = 120
                duration_is_estimated = True
            else:
                planning_duration = max(
                    1,
                    int(duration),
                )
                duration_is_estimated = False

            remaining = (
                max_activity_minutes - packed_minutes
            )

            if activities_added > 0 and (
                planning_duration > remaining
            ):
                break

            # Always allow one activity through. Otherwise a single
            # legitimate activity longer than the normal daily cap
            # could disappear indefinitely.
            cursor[dest_id] = position + 1

            description = activity.get("description")

            if duration_is_estimated:
                source = "activities_table_duration_estimate"
            else:
                source = "estimated_visit_durations"

            self._add_drawer(
                shelf=shelf,
                name=activity["name"],
                description=description,
                start_time=None,
                duration_minutes=planning_duration,
                sort_order=order,
                activity_type="EXPERIENCE",
                activity_id=activity["id"],
                source=source,
                is_fallback=duration_is_estimated,
            )

            if first_activity_id is None:
                first_activity_id = activity["id"]

            packed_minutes += planning_duration
            activities_added += 1
            order += 1

            if packed_minutes >= max_activity_minutes:
                break

        # If there are no seeded cultural activities, use an explicit
        # non-specific fallback rather than inventing a named attraction.
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
                source="fallback_estimate",
                is_fallback=True,
            )

            logger.warning(
                "Day %s at %s: no cultural activities available; "
                "explicit fallback used.",
                day_number,
                dest_id,
            )

            order += 1

        # Lunch is a sequence element, not a fixed clock event.
        # Put it after the first activity block.
        self._add_drawer(
            shelf=shelf,
            name="Lunch",
            description=None,
            start_time=None,
            duration_minutes=60,
            sort_order=order,
            activity_type="MEAL",
            source="hardcoded_meal",
        )

        order += 1

        # If the first packing pass stopped because an activity was too
        # large to fit, allow one additional activity after lunch only
        # when there is still meaningful remaining capacity.
        #
        # This keeps the day adaptive without turning the planner into
        # an arbitrary fixed two-slot system.
        remaining_after_lunch = (
            max_activity_minutes
            - packed_minutes
            - 60
        )

        if (
            remaining_after_lunch >= 60
            and cursor.get(dest_id, 0) < len(pool)
        ):
            position = cursor.get(dest_id, 0)
            activity = pool[position]

            duration = activity.get(
                "estimated_visit_duration_minutes"
            )

            if duration is None:
                planning_duration = 120
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
                    description=activity.get("description"),
                    start_time=None,
                    duration_minutes=planning_duration,
                    sort_order=order,
                    activity_type="EXPERIENCE",
                    activity_id=activity["id"],
                    source=(
                        "estimated_visit_durations"
                        if not duration_is_estimated
                        else "activities_table_duration_estimate"
                    ),
                    is_fallback=duration_is_estimated,
                )

                if first_activity_id is None:
                    first_activity_id = activity["id"]

                order += 1

        if "relaxed_pace" in travel_style:
            self._add_drawer(
                shelf=shelf,
                name="Free evening",
                description=(
                    "Unstructured evening time after the day's "
                    "exploration."
                ),
                start_time=None,
                duration_minutes=90,
                sort_order=order,
                activity_type="FREE_TIME",
                source="hardcoded_relaxed_pace",
                is_fallback=True,
            )

        return first_activity_id

    # -----------------------------------------------------------------
    # STANDARD SAFARI / DESTINATION DAY
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
    ) -> str | None:

        order = 1
        first_activity_id: str | None = None

        if is_arrival_day:
            order, _ = self._destination_arrival_transfer(
                shelf=shelf,
                order=order,
                dest_id=dest_id,
                origin_dest_id=origin_dest_id,
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
            first_activity_id = morning["id"]

            morning_is_game_drive = _is_game_drive_category(
                morning["category"]
            )

            duration = (
                morning.get(
                    "estimated_visit_duration_minutes"
                )
                or 240
            )

            self._add_drawer(
                shelf=shelf,
                name=morning["name"],
                description=morning["description"],
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
                    if morning.get(
                        "estimated_visit_duration_minutes"
                    )
                    else "activities_table"
                ),
                is_fallback=not bool(
                    morning.get(
                        "estimated_visit_duration_minutes"
                    )
                ),
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
                source="fallback_estimate",
                is_fallback=True,
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
        )

        order += 1

        afternoon = self._consume_next_activity(
            pool=pool,
            cursor=cursor,
            dest_id=dest_id,
        )

        if afternoon:
            if first_activity_id is None:
                first_activity_id = afternoon["id"]

            afternoon_is_game_drive = _is_game_drive_category(
                afternoon["category"]
            )

            duration = (
                afternoon.get(
                    "estimated_visit_duration_minutes"
                )
                or 150
            )

            self._add_drawer(
                shelf=shelf,
                name=afternoon["name"],
                description=afternoon["description"],
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
                    if afternoon.get(
                        "estimated_visit_duration_minutes"
                    )
                    else "activities_table"
                ),
                is_fallback=not bool(
                    afternoon.get(
                        "estimated_visit_duration_minutes"
                    )
                ),
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
                source="fallback_estimate",
                is_fallback=True,
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
            )

        return first_activity_id

    # -----------------------------------------------------------------
    # ARRIVAL TRANSFER
    # -----------------------------------------------------------------

    def _destination_arrival_transfer(
        self,
        shelf: Shelf,
        order: int,
        dest_id: str,
        origin_dest_id: str | None,
    ) -> tuple[int, bool]:

        row = None

        if origin_dest_id is not None:
            row = self.db.execute(
                text(
                    """
                    SELECT
                        distance_km,
                        duration_minutes_dry_season
                    FROM drive_times_between_destinations
                    WHERE from_destination_id =
                        CAST(:from_destination_id AS uuid)
                      AND to_destination_id =
                        CAST(:to_destination_id AS uuid)
                    """
                ),
                {
                    "from_destination_id": origin_dest_id,
                    "to_destination_id": dest_id,
                },
            ).fetchone()

        else:
            logger.warning(
                "_destination_arrival_transfer called for %s "
                "without origin_dest_id; directed route lookup "
                "cannot be performed.",
                dest_id,
            )

        duration = (
            int(row[1])
            if row and row[1] is not None
            else None
        )

        if duration is not None:
            description = (
                "Transfer into the destination. Approximately "
                f"{duration} minutes based on available route data."
            )
        else:
            description = (
                "Transfer into the destination. Duration is not "
                "yet confirmed from available route data; a "
                "conservative placeholder duration is used for "
                "scheduling purposes only."
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
            source=(
                "drive_times_between_destinations"
                if row and duration is not None
                else "fallback_estimate"
            ),
            is_fallback=not bool(
                row and duration is not None
            ),
        )

        return (
            order,
            bool(row and duration is not None),
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

        position = cursor.get(dest_id, 0)

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

            from_country = meta.get(
                frm,
                {},
            ).get("country")

            to_country = meta.get(
                to,
                {},
            ).get("country")

            is_inter_country = bool(
                from_country
                and to_country
                and from_country != to_country
            )

            drive_row = self.db.execute(
                text(
                    """
                    SELECT
                        distance_km,
                        duration_minutes_dry_season
                    FROM drive_times_between_destinations
                    WHERE from_destination_id =
                        CAST(:from_dest AS uuid)
                      AND to_destination_id =
                        CAST(:to_dest AS uuid)
                    """
                ),
                {
                    "from_dest": frm,
                    "to_dest": to,
                },
            ).fetchone()

            distance_km = None
            duration_minutes = None
            source = None
            mode = "private_4x4"

            if drive_row:
                if drive_row[0] is not None:
                    distance_km = float(
                        drive_row[0]
                    )

                if drive_row[1] is not None:
                    duration_minutes = int(
                        drive_row[1]
                    )

                source = (
                    "drive_times_between_destinations"
                )

            flight_row = None

            if (
                is_inter_country
                or (
                    duration_minutes is not None
                    and duration_minutes
                    > DRIVE_TO_FLIGHT_THRESHOLD_MINUTES
                )
            ):
                flight_row = self.db.execute(
                    text(
                        """
                        SELECT f.duration_minutes
                        FROM flights f
                        WHERE (
                            f.origin_airport_id IN (
                                SELECT airport_id
                                FROM destination_airports
                                WHERE destination_id =
                                    CAST(:frm AS uuid)
                                  AND is_primary_gateway
                            )
                            OR f.origin_airstrip_id IN (
                                SELECT id
                                FROM airstrips
                                WHERE destination_id =
                                    CAST(:frm AS uuid)
                            )
                        )
                        AND (
                            f.destination_airport_id IN (
                                SELECT airport_id
                                FROM destination_airports
                                WHERE destination_id =
                                    CAST(:to AS uuid)
                                  AND is_primary_gateway
                            )
                            OR f.destination_airstrip_id IN (
                                SELECT id
                                FROM airstrips
                                WHERE destination_id =
                                    CAST(:to AS uuid)
                            )
                        )
                        ORDER BY
                            f.duration_minutes ASC NULLS LAST
                        LIMIT 1
                        """
                    ),
                    {
                        "frm": frm,
                        "to": to,
                    },
                ).fetchone()

            if flight_row:
                duration_minutes = (
                    int(flight_row[0])
                    if flight_row[0] is not None
                    else None
                )

                mode = "scheduled_flight"
                source = "flights_table"
                distance_km = None

            if duration_minutes is None:
                mode = (
                    "charter_flight"
                    if is_inter_country
                    else "private_4x4"
                )

                source = "unavailable"
                distance_km = None

                logger.warning(
                    "No measured drive or flight route for "
                    "%s -> %s. Duration remains unavailable.",
                    frm,
                    to,
                )

            border_crossing_id = None

            if is_inter_country:
                border_row = self.db.execute(
                    text(
                        """
                        SELECT id
                        FROM border_crossings
                        WHERE (
                            country_a::text = :country_a
                            AND country_b::text = :country_b
                        )
                        OR (
                            country_a::text = :country_b
                            AND country_b::text = :country_a
                        )
                        ORDER BY
                            (visa_notes IS NOT NULL) DESC
                        LIMIT 1
                        """
                    ),
                    {
                        "country_a": from_country,
                        "country_b": to_country,
                    },
                ).fetchone()

                if border_row:
                    border_crossing_id = border_row[0]

                elif mode not in (
                    "scheduled_flight",
                    "charter_flight",
                ):
                    logger.warning(
                        "Inter-country overland leg %s -> %s "
                        "has no border_crossings record.",
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
                requires_border_crossing=is_inter_country,
                border_crossing_id=border_crossing_id,
            )

            self.db.add(hinge)

            if hasattr(hinge, "cabinet"):
                hinge.cabinet = cabinet
            elif hinge not in cabinet.hinges:
                cabinet.hinges.append(hinge)

            legs.append(
                {
                    "from": frm,
                    "to": to,
                    "duration_minutes": duration_minutes,
                    "source": source,
                    "mode": mode,
                    "is_inter_country": is_inter_country,
                    "border_crossing_id": border_crossing_id,
                }
            )

        return legs

    # -----------------------------------------------------------------
    # ACCOMMODATION
    # -----------------------------------------------------------------

    def _populate_headboard(
        self,
        shelf: Shelf,
        dest_id: str,
        budget_tier: str,
        remaining_nights_here: int,
    ) -> None:

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
                    ANY(CAST(:tiers AS text[]))
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

        check_out = None

        if shelf.date and remaining_nights_here > 0:
            check_out = (
                shelf.date
                + timedelta(
                    days=remaining_nights_here
                )
            )

        if row:
            headboard = Headboard(
                shelf_id=shelf.id,
                lodge_id=row[0],
                name=row[1],
                tier=row[2],
                check_in=shelf.date,
                check_out=check_out,
                nights=remaining_nights_here,
            )
        else:
            headboard = Headboard(
                shelf_id=shelf.id,
                name=(
                    f"{budget_tier.title()} lodge"
                ),
                tier=budget_tier,
                check_in=shelf.date,
                check_out=check_out,
                nights=remaining_nights_here,
            )

        self.db.add(headboard)

    # -----------------------------------------------------------------
    # ARMREST / TRANSPORT SUMMARY
    # -----------------------------------------------------------------

    def _populate_armrest(
        self,
        shelf: Shelf,
        legs: list[dict[str, Any]],
        destination_index: int,
        is_arrival_day: bool,
    ) -> None:

        if (
            is_arrival_day
            and destination_index > 0
            and destination_index - 1 < len(legs)
        ):
            leg = legs[destination_index - 1]

            minutes = leg.get(
                "duration_minutes"
            )

            mode = (
                leg.get("mode")
                or "private_4x4"
            )

            description = _format_transfer_description(
                mode,
                minutes,
            )

            armrest = Armrest(
                shelf_id=shelf.id,
                mode=mode,
                description=description,
                duration_minutes=minutes,
                is_private=(
                    mode == "private_4x4"
                ),
            )

        else:
            armrest = Armrest(
                shelf_id=shelf.id,
                mode="private_4x4",
                description=(
                    "Private 4x4 · local destination transport"
                ),
                duration_minutes=0,
                is_private=True,
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

        elif is_last_day:
            meals = ["breakfast"]

        elif is_transit_day:
            meals = ["dinner"]

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
                    "SELECT to_regclass('photo_states')"
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
            if hasattr(shelf, field_name):
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

        archetype_normalized = _normalise_archetype(
            archetype
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

        if destination_type in {
            "national_park",
            "game_reserve",
        }:
            return (
                "Wildlife & wide horizons"
                if night_idx == 0
                else "Deeper into the park"
            )

        if destination_type in {
            "island",
            "beach",
            "marine_park",
        }:
            return "Coast, water & open horizons"

        if destination_type in {
            "mountain",
            "waterfall",
            "forest_reserve",
        }:
            return "Nature & exploration"

        if destination_type in {
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

        if focus == "wildlife":
            styles.append("wildlife")

        elif focus in {
            "beach",
            "adventure",
            "culture",
            "cultural",
            "photography",
            "birding",
            "walking",
        }:
            styles.append(str(focus))

        if request.get("budget_tier") == "luxury":
            styles.append("luxury")

        if (
            ItineraryPlanningEngine._safe_int(
                request.get("travelers"),
                2,
            )
            <= 2
        ):
            styles.append("private")

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
