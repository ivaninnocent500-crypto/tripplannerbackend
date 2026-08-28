"""
ItineraryPlanningEngine v3
---------------------------

Complete replacement for app/engines/itinerary_v2.py.

Design goals
============

1. Build a complete persisted itinerary:
       Cabinet
       ├── Shelves (days)
       │ ├── Drawers (activities)
       │ ├── Headboard (lodge)
       │ ├── Armrest (transport)
       │ └── Trays (meals)
       └── Hinges (route legs)

2. Activity selection is destination-stay scoped, NOT day scoped.
3. Activity IDs are consumed exactly once per destination stay.
4. Activity ordering is deterministic per generated cabinet.
5. Sparse datasets do not produce fake "booked" activities.
6. Month/season preference is data-driven.
7. Multi-country routes detect border crossings.
8. Long/inter-country transfers prefer real flight data when available.
9. Optional Photo States integration.
10. SQLAlchemy/PostgreSQL-safe bind syntax using CAST() instead of :: inline casting.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta, time as dt_time
from typing import Any

from sqlalchemy import text
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


# ============================================================================
# CONSTANTS
# ============================================================================

DEFAULT_GAME_DRIVE_START = dt_time(6, 0)
DEFAULT_ARRIVAL_TIME = dt_time(14, 0)

DRIVE_TO_FLIGHT_THRESHOLD_MINUTES = 6 * 60
BORDER_BUFFER_NIGHTS = 1


# ============================================================================
# DESTINATION TYPE -> ACTIVITY PRIORITY
# ============================================================================

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


# ============================================================================
# TRAVEL STYLE -> ACTIVITY PRIORITY
# ============================================================================

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


# ============================================================================
# FALLBACKS
# ============================================================================

FALLBACK_VARIANTS: dict[str, list[tuple[str, str]]] = {
    "island": [
        (
            "Beach & relaxation",
            "Free time at the lodge's beach area — no specific excursion "
            "booked; the operator will offer whatever suits sea conditions.",
        ),
        (
            "Snorkel gear & shoreline time",
            "Open beach time with snorkel gear available at the lodge — "
            "no guided excursion booked for this slot.",
        ),
    ],
    "beach": [
        (
            "Beach & relaxation",
            "Free time at the lodge's beach area — no specific excursion "
            "booked; the operator will offer whatever suits sea conditions.",
        ),
        (
            "Sunset shoreline walk",
            "Unstructured time along the beach — no guided activity booked "
            "for this slot.",
        ),
    ],
    "mountain": [
        (
            "Acclimatisation walk",
            "Short lower-altitude walk to acclimatise — no summit attempt "
            "scheduled today.",
        ),
        (
            "Rest & recovery time",
            "Open time at camp to rest — no scheduled activity for this slot.",
        ),
    ],
    "desert": [
        (
            "Desert nature walk",
            "Light guided walk suited to the terrain — no specific site "
            "booked.",
        ),
        (
            "Open camp time",
            "Unstructured time at camp — no specific excursion booked for "
            "this slot.",
        ),
    ],
    "marine_park": [
        (
            "Shore time at the lodge",
            "Open time at the lodge — optional water-based excursions may "
            "be offered if conditions allow.",
        ),
    ],
    "city": [
        (
            "Guided cultural stop",
            "Short guided visit to a locally significant site — no specific "
            "venue booked in advance.",
        ),
        (
            "Free time to explore",
            "Unstructured time to explore independently — no guided activity "
            "booked for this slot.",
        ),
    ],
    "cultural_site": [
        (
            "Guided cultural stop",
            "Short guided visit to a locally significant site — no specific "
            "venue booked in advance.",
        ),
    ],
    "unesco_site": [
        (
            "Guided cultural stop",
            "Short guided visit to a locally significant site — no specific "
            "venue booked in advance.",
        ),
    ],
    "national_park": [
        (
            "Guided wilderness drive",
            "Game drive on lodge circuits — no specific route booked in "
            "advance. Times may shift with conditions.",
        ),
        (
            "Extended photographic drive",
            "A slower-paced drive focused on photography opportunities — "
            "no specific route booked in advance.",
        ),
        (
            "Bush walk near camp",
            "Short guided walk near the lodge grounds, conditions permitting "
            "— no specific route booked in advance.",
        ),
    ],
    "game_reserve": [
        (
            "Guided wilderness drive",
            "Game drive on lodge circuits — no specific route booked in "
            "advance. Times may shift with conditions.",
        ),
        (
            "Extended photographic drive",
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
    )
]


# ============================================================================
# RESULT
# ============================================================================

@dataclass
class BuildResult:
    cabinet: Cabinet
    warnings: list[str] = field(default_factory=list)


# ============================================================================
# HELPERS
# ============================================================================

def _default_start_for(category: str | None) -> dt_time:
    if category in (
        "game_drive",
        "walking_safari",
        "hiking",
        "mountain_climbing",
    ):
        return DEFAULT_GAME_DRIVE_START

    if category in (
        "diving",
        "snorkeling",
        "boat_safari",
        "fishing",
    ):
        return dt_time(9, 0)

    if category in (
        "cultural_visit",
        "shopping",
    ):
        return dt_time(10, 0)

    if category == "spa_wellness":
        return dt_time(11, 0)

    return dt_time(10, 0)


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
        tags.append(focus)

    tags.extend(travel_style)

    for tag in tags:
        for category in TRAVEL_STYLE_CATEGORY_RANKS.get(tag, []):
            if category not in seen:
                seen.add(category)
                result.append(category)

    if destination_type:
        for category in DESTINATION_TYPE_CATEGORY_RANKS.get(
            destination_type,
            [],
        ):
            if category not in seen:
                seen.add(category)
                result.append(category)

    return result


# ============================================================================
# ENGINE
# ============================================================================

class ItineraryPlanningEngine:

    def __init__(self, db: Session):
        self.db = db

    def build(
        self,
        request: dict[str, Any],
        destination_ids: list[str],
    ) -> BuildResult:

        days = int(request["days"])

        if days < 1:
            raise ValueError("Trip must contain at least one day.")

        travelers = int(request.get("travelers", 1))
        travel_style = request.get("travel_style", []) or self._infer_style(request)
        focus = request.get("focus", "wildlife")
        budget_tier = request.get("budget_tier", "mid")
        start_date_raw = request.get("start_date")
        start_date = date.fromisoformat(start_date_raw) if start_date_raw else None

        if not destination_ids:
            raise ValueError("Itinerary generation requires at least one destination.")

        cleaned_destination_ids: list[str] = []
        for destination_id in destination_ids:
            if not cleaned_destination_ids or cleaned_destination_ids[-1] != destination_id:
                cleaned_destination_ids.append(destination_id)

        destination_ids = cleaned_destination_ids

        if len(destination_ids) > days:
            logger.warning(
                "Trip requests %s destinations but only %s days. "
                "Only the first %s destinations can receive a day.",
                len(destination_ids),
                days,
                days,
            )
            destination_ids = destination_ids[:days]

        cabinet = Cabinet(
            request_json=request,
            title=request.get("title") or self._default_title(request),
            duration_days=days,
            travelers_adults=travelers,
            travel_style=travel_style,
            budget_tier=budget_tier,
            status="draft",
            start_date=start_date,
            end_date=(start_date + timedelta(days=days - 1) if start_date else None),
            primary_destination_id=(destination_ids[0] if destination_ids else None),
            route_destination_ids=destination_ids,
        )

        self.db.add(cabinet)
        self.db.flush()

        warnings: list[str] = []
        meta = self._fetch_destination_meta(destination_ids)

        if hasattr(cabinet, "route_countries"):
            cabinet.route_countries = list(
                dict.fromkeys(m["country"] for m in meta.values() if m.get("country"))
            )

        if hasattr(cabinet, "primary_country"):
            cabinet.primary_country = (
                meta.get(destination_ids[0], {}).get("country") if destination_ids else None
            )

        legs = self._build_hinges(
            cabinet_id=cabinet.id,
            destination_ids=destination_ids,
            meta=meta,
        )

        allocation, allocation_warnings = self._allocate_days(
            destination_ids=destination_ids,
            meta=meta,
            total_days=days,
            travel_style=travel_style,
        )

        warnings.extend(allocation_warnings)

        destination_types = {
            destination_id: meta.get(destination_id, {}).get("destination_type")
            for destination_id in destination_ids
        }

        per_destination_pool: dict[str, list[dict[str, Any]]] = {}

        for destination_id in destination_ids:
            pool = self._fetch_ranked_activity_pool(
                dest_id=destination_id,
                destination_type=destination_types.get(destination_id),
                travel_style=travel_style,
                focus=focus,
                start_date=start_date,
                cabinet_id=str(cabinet.id),
            )
            per_destination_pool[destination_id] = pool

            if not pool:
                warnings.append(
                    f"Destination {destination_id} has no seeded activities. "
                    "Generic fallback activities will be used."
                )
            elif len(pool) < 2:
                warnings.append(
                    f"Destination {destination_id} has only {len(pool)} seeded activity. "
                    "The itinerary may require fallback time."
                )
            elif len(pool) < 4:
                warnings.append(
                    f"Destination {destination_id} has only {len(pool)} seeded activities. "
                    "A longer stay may exhaust the activity pool."
                )

        cursors: dict[str, int] = {destination_id: 0 for destination_id in destination_ids}
        fallback_counters: dict[str, int] = {destination_id: 0 for destination_id in destination_ids}

        day_number = 1
        current_date = start_date

        for destination_index, destination_id in enumerate(destination_ids):
            nights_here = allocation[destination_index]
            destination_type = destination_types.get(destination_id)

            for night_index in range(nights_here):
                is_first_day_overall = day_number == 1
                is_last_day_overall = day_number == days
                is_arrival_day = night_index == 0 and destination_index > 0

                shelf = Shelf(
                    cabinet_id=cabinet.id,
                    day_number=day_number,
                    date=current_date,
                    destination_id=destination_id,
                    theme=self._theme_for(
                        destination_index,
                        night_index,
                        is_first_day_overall,
                        is_last_day_overall,
                        destination_type,
                    ),
                )

                self.db.add(shelf)
                self.db.flush()

                first_activity_id = self._populate_drawers(
                    shelf=shelf,
                    pool=per_destination_pool[destination_id],
                    cursor=cursors,
                    fallback_counters=fallback_counters,
                    dest_id=destination_id,
                    dest_type=destination_type,
                    travel_style=travel_style,
                    focus=focus,
                    day_number=day_number,
                    is_first_day=is_first_day_overall,
                    is_last_day=is_last_day_overall,
                )

                self._populate_headboard(
                    shelf=shelf,
                    dest_id=destination_id,
                    budget_tier=budget_tier,
                    remaining_nights_here=nights_here - night_index,
                )

                self._populate_armrest(
                    shelf=shelf,
                    legs=legs,
                    destination_index=destination_index,
                    is_arrival_day=is_arrival_day,
                )

                self._populate_trays(
                    shelf=shelf,
                    is_first_day=is_first_day_overall,
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

    # ========================================================================
    # DESTINATION META
    # ========================================================================

    def _fetch_destination_meta(
        self,
        destination_ids: list[str],
    ) -> dict[str, dict[str, Any]]:

        if not destination_ids:
            return {}

        meta: dict[str, dict[str, Any]] = {}

        rows = self.db.execute(
            text(
                """
                SELECT
                    CAST(id AS text),
                    name,
                    CAST(country AS text),
                    CAST(destination_type AS text)
                FROM travel_places
                WHERE id = ANY(:ids)
                """
            ),
            {"ids": destination_ids},
        ).fetchall()

        for row in rows:
            destination_id = row[0]
            meta[destination_id] = {
                "country": row[2],
                "headline_label": row[1],
                "destination_type": row[3],
                "min_nights": 1,
            }

        try:
            table_exists = self.db.execute(
                text("SELECT to_regclass('estimated_visit_durations')")
            ).scalar()

            if table_exists:
                min_rows = self.db.execute(
                    text(
                        """
                        SELECT
                            CAST(destination_id AS text),
                            MIN(recommended_nights_min)
                        FROM estimated_visit_durations
                        WHERE destination_id = ANY(:ids)
                          AND scope = 'full_destination'
                          AND recommended_nights_min IS NOT NULL
                        GROUP BY destination_id
                        """
                    ),
                    {"ids": destination_ids},
                ).fetchall()

                for destination_id, minimum in min_rows:
                    if destination_id in meta and minimum is not None:
                        meta[destination_id]["min_nights"] = max(1, int(minimum))

        except Exception as exc:
            logger.warning("Could not read estimated_visit_durations: %s", exc)

        return meta

    # ========================================================================
    # ACTIVITY POOL (FIXED FOR PSYCOPG3 / SQLALCHEMY :: CASTING)
    # ========================================================================

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

        ranked_array_literal = (
            "{" + ",".join(ranked_categories) + "}"
            if ranked_categories
            else "{}"
        )

        month_token = start_date.strftime("%B").lower() if start_date else None

        sql = text(
            """
            WITH ranked AS (
                SELECT
                    a.id,
                    a.name,
                    a.description,
                    a.category,
                    a.difficulty,
                    a.available_months,

                    CASE
                        WHEN cardinality(CAST(:ranked AS text[])) = 0 THEN 999
                        ELSE COALESCE(
                            array_position(
                                CAST(:ranked AS text[]),
                                CAST(a.category AS text)
                            ),
                            998
                        )
                    END AS style_position,

                    CASE
                        WHEN CAST(:month AS text) IS NULL THEN 0
                        WHEN a.available_months IS NULL THEN 0
                        WHEN CAST(:month AS month_enum) = ANY(a.available_months) THEN 0
                        ELSE 1
                    END AS month_mismatch,

                    hashtext(
                        CAST(:cab_id AS text)
                        || '|'
                        || CAST(a.id AS text)
                    ) AS seed_hash

                FROM activities a
                WHERE a.destination_id = :dest_id
            )

            SELECT
                id,
                name,
                description,
                category,
                difficulty,
                style_position,
                month_mismatch,
                seed_hash
            FROM ranked
            ORDER BY
                style_position ASC,
                month_mismatch ASC,
                seed_hash ASC
            """
        )

        try:
            rows = self.db.execute(
                sql,
                {
                    "ranked": ranked_array_literal,
                    "month": month_token,
                    "cab_id": cabinet_id,
                    "dest_id": dest_id,
                },
            ).fetchall()

        except Exception:
            logger.exception(
                "Failed to fetch ranked activities for destination %s",
                dest_id,
            )
            raise

        return [
            {
                "id": row[0],
                "name": row[1],
                "description": row[2],
                "category": row[3],
                "difficulty": row[4],
            }
            for row in rows
        ]

    # ========================================================================
    # DRAWERS
    # ========================================================================

    def _populate_drawers(
        self,
        shelf,
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
    ) -> str | None:

        order = 1

        if is_first_day:
            self._add_drawer(
                shelf=shelf,
                name="Airport welcome",
                description="Met at the airport by your driver-guide.",
                start_time=DEFAULT_ARRIVAL_TIME,
                duration_minutes=60,
                sort_order=order,
                activity_type="ARRIVAL",
                source="hardcoded_arrival_departure",
            )
            order += 1

            self._add_drawer(
                shelf=shelf,
                name="Transfer to lodge",
                description="Transfer to the lodge. No excursion is assumed during this arrival transfer.",
                start_time=dt_time(15, 30),
                duration_minutes=60,
                sort_order=order,
                activity_type="TRANSFER",
                source="hardcoded_arrival_departure",
            )
            order += 1

            self._add_drawer(
                shelf=shelf,
                name="Dinner at the lodge",
                description=None,
                start_time=dt_time(19, 30),
                duration_minutes=90,
                sort_order=order,
                activity_type="MEAL",
                source="hardcoded_meal",
            )
            return None

        if is_last_day:
            self._add_drawer(
                shelf=shelf,
                name="Breakfast",
                description=None,
                start_time=dt_time(7, 0),
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
                start_time=dt_time(9, 0),
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
                start_time=dt_time(12, 0),
                duration_minutes=30,
                sort_order=order,
                activity_type="DEPARTURE",
                source="hardcoded_arrival_departure",
            )
            return None

        morning = self._consume_next_activity(
            pool=pool,
            cursor=cursor,
            dest_id=dest_id,
        )

        first_activity_id: str | None = None

        if morning:
            first_activity_id = morning["id"]
            self._add_drawer(
                shelf=shelf,
                name=morning["name"],
                description=morning["description"],
                start_time=_default_start_for(morning["category"]),
                duration_minutes=240,
                sort_order=order,
                activity_type="EXPERIENCE",
                activity_id=morning["id"],
                source="activities_table",
            )
        else:
            title, description = _fallback_drawer_text(dest_type, fallback_counters[dest_id])
            fallback_counters[dest_id] += 1
            self._add_drawer(
                shelf=shelf,
                name=title,
                description=description,
                start_time=_default_start_for(None),
                duration_minutes=180,
                sort_order=order,
                activity_type="EXPERIENCE",
                source="fallback_estimate",
            )
            logger.warning(
                "Day %s at %s: activity pool exhausted (morning slot); fallback used.",
                day_number,
                dest_id,
            )

        order += 1

        self._add_drawer(
            shelf=shelf,
            name="Lunch at the lodge",
            description=None,
            start_time=dt_time(13, 0),
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
            self._add_drawer(
                shelf=shelf,
                name=afternoon["name"],
                description=afternoon["description"],
                start_time=dt_time(16, 0),
                duration_minutes=150,
                sort_order=order,
                activity_type="EXPERIENCE",
                activity_id=afternoon["id"],
                source="activities_table",
            )
        else:
            title, description = _fallback_drawer_text(dest_type, fallback_counters[dest_id])
            fallback_counters[dest_id] += 1
            self._add_drawer(
                shelf=shelf,
                name=title,
                description=description,
                start_time=dt_time(16, 0),
                duration_minutes=150,
                sort_order=order,
                activity_type="EXPERIENCE",
                source="fallback_estimate",
            )
            logger.warning(
                "Day %s at %s: activity pool exhausted (afternoon slot); fallback used.",
                day_number,
                dest_id,
            )

        order += 1

        if "relaxed_pace" in travel_style:
            self._add_drawer(
                shelf=shelf,
                name="Sundowner at the lodge",
                description="Relaxed evening time at the lodge.",
                start_time=dt_time(18, 30),
                duration_minutes=60,
                sort_order=order,
                activity_type="EXPERIENCE",
                source="hardcoded_relaxed_pace",
            )

        return first_activity_id

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

    @staticmethod
    def _add_drawer(
        shelf,
        name: str,
        description: str | None,
        start_time: dt_time,
        duration_minutes: int,
        sort_order: int,
        activity_type: str,
        activity_id: Any = None,
        source: str = "activities_table",
    ):
        drawer = Drawer(
            shelf_id=shelf.id,
            activity_id=activity_id,
            name=name,
            description=description,
            start_time=start_time,
            duration_minutes=duration_minutes,
            sort_order=sort_order,
            activity_type=activity_type,
        )

        if hasattr(drawer, "source"):
            drawer.source = source

        shelf.drawers.append(drawer)

    # ========================================================================
    # ROUTE HINGES
    # ========================================================================

    def _build_hinges(
        self,
        cabinet_id,
        destination_ids: list[str],
        meta: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:

        legs: list[dict[str, Any]] = []
        sequence = 0

        for index in range(len(destination_ids) - 1):
            frm = destination_ids[index]
            to = destination_ids[index + 1]

            if frm == to:
                logger.info("Skipping zero-distance hinge %s -> %s", frm, to)
                continue

            from_country = meta.get(frm, {}).get("country")
            to_country = meta.get(to, {}).get("country")
            is_inter_country = bool(from_country and to_country and from_country != to_country)

            drive_row = self.db.execute(
                text(
                    """
                    SELECT distance_km, duration_minutes_dry_season
                    FROM drive_times
                    WHERE destination_id = :to_dest
                    ORDER BY distance_km ASC
                    LIMIT 1
                    """
                ),
                {"to_dest": to},
            ).fetchone()

            distance_km: float | None = None
            duration_minutes: int | None = None
            source: str | None = None
            mode = "private_4x4"

            if drive_row:
                distance_km = float(drive_row[0])
                if drive_row[1] is not None:
                    duration_minutes = int(drive_row[1])
                source = "drive_times"

            flight_row = None
            if is_inter_country or (duration_minutes is not None and duration_minutes > DRIVE_TO_FLIGHT_THRESHOLD_MINUTES):
                flight_row = self.db.execute(
                    text(
                        """
                        SELECT f.duration_minutes
                        FROM flights f
                        WHERE f.origin_airport_id IN (
                            SELECT airport_id FROM destination_airports
                            WHERE destination_id = :frm AND is_primary_gateway
                        )
                        AND f.destination_airport_id IN (
                            SELECT airport_id FROM destination_airports
                            WHERE destination_id = :to AND is_primary_gateway
                        )
                        ORDER BY f.duration_minutes ASC NULLS LAST
                        LIMIT 1
                        """
                    ),
                    {"frm": frm, "to": to},
                ).fetchone()

            if flight_row:
                duration_minutes = int(flight_row[0]) if flight_row[0] is not None else 240
                mode = "scheduled_flight"
                source = "flights_table"
                distance_km = None

            if duration_minutes is None:
                if is_inter_country:
                    duration_minutes = 480
                    distance_km = None
                    mode = "charter_flight"
                    source = "fallback_inter_country_estimate"
                    logger.warning("No measured drive/flight route for %s -> %s. Using fallback inter-country estimate.", frm, to)
                else:
                    duration_minutes = 180
                    distance_km = 150.0
                    mode = "private_4x4"
                    source = "fallback_estimate"
                    logger.warning("No drive time for %s -> %s. Using fallback route estimate.", frm, to)

            border_crossing_id = None
            if is_inter_country:
                border_row = self.db.execute(
                    text(
                        """
                        SELECT id FROM border_crossings
                        WHERE (country_a = :country_a AND country_b = :country_b)
                           OR (country_a = :country_b AND country_b = :country_a)
                        ORDER BY (visa_notes IS NOT NULL) DESC
                        LIMIT 1
                        """
                    ),
                    {"country_a": from_country, "country_b": to_country},
                ).fetchone()

                if border_row:
                    border_crossing_id = border_row[0]
                elif mode not in ("scheduled_flight", "charter_flight"):
                    logger.warning("Inter-country overland leg %s -> %s has no border_crossings record.", frm, to)

            sequence += 1
            hinge_kwargs = {
                "cabinet_id": cabinet_id,
                "from_destination_id": frm,
                "to_destination_id": to,
                "sequence_order": sequence,
                "distance_km": distance_km,
                "duration_minutes": duration_minutes,
                "mode": mode,
                "source": source,
            }

            hinge = Hinge(**hinge_kwargs)

            if hasattr(hinge, "is_inter_country"):
                hinge.is_inter_country = is_inter_country
            if hasattr(hinge, "requires_border_crossing"):
                hinge.requires_border_crossing = is_inter_country
            if hasattr(hinge, "border_crossing_id"):
                hinge.border_crossing_id = border_crossing_id

            self.db.add(hinge)

            legs.append({
                "from": frm,
                "to": to,
                "duration_minutes": duration_minutes,
                "source": source,
                "mode": mode,
                "is_inter_country": is_inter_country,
                "border_crossing_id": border_crossing_id,
            })

        return legs

    # ========================================================================
    # DAY ALLOCATION
    # ========================================================================

    def _allocate_days(
        self,
        destination_ids: list[str],
        meta: dict[str, dict[str, Any]],
        total_days: int,
        travel_style: list[str],
    ) -> tuple[list[int], list[str]]:

        n = len(destination_ids)
        if n == 0:
            return [], []

        if total_days < n:
            allocation = [0] * n
            for index in range(total_days):
                allocation[index] = 1

            return allocation, [
                "Trip duration is shorter than the number of requested "
                "destinations; only the first destinations can receive a day."
            ]

        base = total_days // n
        remainder = total_days % n
        allocation = [base] * n

        index = n - 1
        while remainder > 0:
            allocation[index] += 1
            remainder -= 1
            index -= 1
            if index < 0:
                index = n - 1

        warnings: list[str] = []

        for i in range(1, n):
            previous_destination = destination_ids[i - 1]
            current_destination = destination_ids[i]
            previous_country = meta.get(previous_destination, {}).get("country")
            current_country = meta.get(current_destination, {}).get("country")

            if not (previous_country and current_country and previous_country != current_country):
                continue

            donor_candidates = []
            for donor_index in range(n):
                minimum = meta.get(destination_ids[donor_index], {}).get("min_nights", 1)
                slack = allocation[donor_index] - minimum
                if slack > 0:
                    donor_candidates.append((slack, donor_index))

            if not donor_candidates:
                label = meta.get(current_destination, {}).get("headline_label", current_destination)
                warnings.append(
                    f"Could not add a border-buffer night before entering {label} without "
                    "shortening another destination below its recommended minimum stay."
                )
                continue

            donor_index = max(donor_candidates, key=lambda item: item[0])[1]
            if donor_index == i:
                continue

            allocation[donor_index] -= BORDER_BUFFER_NIGHTS
            allocation[i] += BORDER_BUFFER_NIGHTS

        if sum(allocation) != total_days:
            logger.error(
                "Day allocation invariant violated. allocation=%s total_days=%s. Rebalancing.",
                allocation,
                total_days,
            )
            difference = total_days - sum(allocation)
            allocation[-1] += difference

        return allocation, warnings

    # ========================================================================
    # LODGE
    # ========================================================================

    def _populate_headboard(
        self,
        shelf,
        dest_id,
        budget_tier,
        remaining_nights_here,
    ):
        tier_map = {
            "budget": ("budget", "camping"),
            "mid": ("mid_range",),
            "luxury": ("luxury", "ultra_luxury"),
        }

        tiers = tier_map.get(budget_tier, ("mid_range",))

        row = self.db.execute(
            text(
                """
                SELECT id, name, tier
                FROM lodges
                WHERE destination_id = :dest_id AND tier = ANY(:tiers)
                ORDER BY star_rating DESC NULLS LAST
                LIMIT 1
                """
            ),
            {"dest_id": dest_id, "tiers": list(tiers)},
        ).fetchone()

        if row:
            headboard = Headboard(
                shelf_id=shelf.id,
                lodge_id=row[0],
                name=row[1],
                tier=row[2],
                check_in=shelf.date,
                nights=remaining_nights_here,
            )
        else:
            headboard = Headboard(
                shelf_id=shelf.id,
                name=f"{budget_tier.title()} lodge",
                tier=budget_tier,
                check_in=shelf.date,
                nights=remaining_nights_here,
            )

        self.db.add(headboard)

    # ========================================================================
    # TRANSPORT
    # ========================================================================

    def _populate_armrest(
        self,
        shelf,
        legs: list[dict[str, Any]],
        destination_index: int,
        is_arrival_day: bool,
    ):
        if is_arrival_day and destination_index > 0 and destination_index - 1 < len(legs):
            leg = legs[destination_index - 1]
            minutes = leg["duration_minutes"]
            mode = leg.get("mode") or "private_4x4"

            if mode == "scheduled_flight":
                description = f"Scheduled flight · approximately {minutes} min"
            elif mode == "charter_flight":
                description = f"Charter flight · approximately {minutes} min"
            else:
                description = f"Private 4x4 · approximately {minutes} min transfer"

            armrest = Armrest(
                shelf_id=shelf.id,
                mode=mode,
                description=description,
                duration_minutes=minutes,
                is_private=(mode == "private_4x4"),
            )
        else:
            armrest = Armrest(
                shelf_id=shelf.id,
                mode="private_4x4",
                description="Private 4x4 · local destination transport",
                duration_minutes=0,
                is_private=True,
            )

        self.db.add(armrest)

    # ========================================================================
    # MEALS
    # ========================================================================

    def _populate_trays(
        self,
        shelf,
        is_first_day: bool,
    ):
        meals = ["dinner"] if is_first_day else ["breakfast", "lunch", "dinner"]
        for meal in meals:
            self.db.add(
                Tray(
                    shelf_id=shelf.id,
                    meal_type=meal,
                    included=True,
                )
            )

    # ========================================================================
    # PHOTO STATES
    # ========================================================================

    def _populate_day_photo(
        self,
        shelf,
        activity_id,
        destination_id,
    ):
        try:
            exists = self.db.execute(text("SELECT to_regclass('photo_states')")).scalar()
        except Exception as exc:
            logger.warning("Could not check photo_states table: %s", exc)
            return

        if not exists:
            return

        row = None

        try:
            row = self.db.execute(
                text(
                    """
                    SELECT url FROM photo_states
                    WHERE activity_id = :activity_id AND url IS NOT NULL
                    ORDER BY id LIMIT 1
                    """
                ),
                {"activity_id": activity_id},
            ).fetchone()
        except Exception as exc:
            logger.debug("Activity-specific photo lookup unavailable: %s", exc)

        if not row:
            try:
                row = self.db.execute(
                    text(
                        """
                        SELECT url FROM photo_states
                        WHERE destination_id = :destination_id AND url IS NOT NULL
                        ORDER BY id LIMIT 1
                        """
                    ),
                    {"destination_id": destination_id},
                ).fetchone()
            except Exception as exc:
                logger.debug("Destination photo lookup unavailable: %s", exc)

        if not row or not row[0]:
            return

        image_url = row[0]
        possible_fields = ("image_url", "hero_image_url", "photo_url", "cover_image_url")

        for field_name in possible_fields:
            if hasattr(shelf, field_name):
                setattr(shelf, field_name, image_url)
                return

        logger.info("Photo found for shelf %s but Shelf has no image URL field.", shelf.id)

    # ========================================================================
    # THEME
    # ========================================================================

    @staticmethod
    def _theme_for(
        idx: int,
        night_idx: int,
        is_first: bool,
        is_last: bool,
        destination_type: str | None,
    ) -> str:
        if is_first:
            return "Arrival & slow start"
        if is_last:
            return "Departure"

        if destination_type in ("national_park", "game_reserve"):
            return "Wildlife & wide horizons" if night_idx == 0 else "Deeper into the park"
        if destination_type in ("island", "beach", "marine_park"):
            return "Coast, water & open horizons"
        if destination_type in ("mountain", "waterfall", "forest_reserve"):
            return "Nature & exploration"
        if destination_type in ("city", "cultural_site", "unesco_site"):
            return "Culture & discovery"

        return "Explore the destination"

    # ========================================================================
    # STYLE INFERENCE
    # ========================================================================

    @staticmethod
    def _infer_style(request: dict[str, Any]) -> list[str]:
        styles: list[str] = []
        focus = request.get("focus")

        if focus == "wildlife":
            styles.append("wildlife")
        elif focus in ("beach", "adventure", "culture", "cultural", "photography", "birding", "walking"):
            styles.append(focus)

        if request.get("budget_tier") == "luxury":
            styles.append("luxury")

        if request.get("travelers", 2) <= 2:
            styles.append("private")

        styles.append("relaxed_pace")
        return list(dict.fromkeys(styles))

    # ========================================================================
    # TITLE
    # ========================================================================

    @staticmethod
    def _default_title(request: dict[str, Any]) -> str:
        country = request.get("country_name", "Africa")
        return f"{country}, Wild & Unhurried"


