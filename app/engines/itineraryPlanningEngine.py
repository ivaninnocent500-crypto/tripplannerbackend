"""
ItineraryPlanningEngine v3
---------------------------

Production itinerary builder for the persisted furniture schema.

CHANGE LOG (audit-confirmed fixes)
------------------------------------
1. Native PostgreSQL array binding.

   _fetch_ranked_activity_pool() previously built the "ranked
   categories" array parameter by hand-concatenating strings:

       "{" + ",".join(ranked_categories) + "}"

   This bypasses SQLAlchemy's type system. It happens to be safe today
   only because ranked_categories always comes from hardcoded constant
   dicts (TRAVEL_STYLE_CATEGORY_RANKS / DESTINATION_TYPE_CATEGORY_RANKS)
   with no commas, quotes, or braces in any value -- but the query is
   one config change away from a syntax error or, if those categories
   are ever sourced dynamically, an injection vector. The list is now
   passed as a native Python list and bound with an explicit
   ``text[]`` cast, letting SQLAlchemy's own parameter binding do the
   escaping.

2. None-duration tolerance.

   route_geography.py's no-fabrication rule means a Hinge's
   duration_minutes can now be legitimately None when no measured or
   estimated route data exists for a leg -- this is intentional, not a
   bug, and must not be silently converted into a fake number. Every
   place that previously assumed a number (f-string formatting,
   arithmetic comparisons) now branches on None explicitly and
   produces an honest "duration unavailable" message instead of
   "approximately None min" or a TypeError.
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


DEFAULT_GAME_DRIVE_START = dt_time(6, 0)
DEFAULT_ARRIVAL_TIME = dt_time(14, 0)

DRIVE_TO_FLIGHT_THRESHOLD_MINUTES = 6 * 60
BORDER_BUFFER_NIGHTS = 1

# --- Overlap-scheduling decision (arrival-day transfer vs. fixed
# activity slots) ---
#
# _populate_drawers previously scheduled the afternoon activity at a
# fixed 16:00 regardless of how long the arrival transfer actually
# took. A measured 5-hour transfer starting at 14:00 does not end
# until 19:00, which silently overlapped the fixed 16:00 slot --
# caught downstream by ValidationEngine/ScheduleRepairEngine but never
# avoided at construction time, even though the transfer duration is
# already known before the afternoon slot is scheduled.
#
# Rule (see accompanying design note in _populate_drawers):
# - If the transfer's measured end time leaves at least
# MIN_USABLE_AFTERNOON_MINUTES before the evening slot, shift the
# afternoon activity to start (transfer end + buffer) instead of
# the fixed default.
# - If the transfer consumes the day (ends at/after the evening
# slot), drop the afternoon activity slot entirely and add an
# honest "settling in" entry instead of fabricating an activity
# that couldn't actually happen.
# - An unknown (None) transfer duration falls back to the
# conservative fixed default rather than being treated as zero.
ARRIVAL_TRANSFER_START = dt_time(14, 0)
AFTERNOON_SLOT_DEFAULT_START = dt_time(16, 0)
EVENING_SLOT_START = dt_time(18, 30)
MIN_USABLE_AFTERNOON_MINUTES = 90
POST_TRANSFER_BUFFER_MINUTES = 30
FALLBACK_ARRIVAL_TRANSFER_MINUTES = 60


DESTINATION_TYPE_CATEGORY_RANKS: dict[str, list[str]] = {
    "national_park": [
        "game_drive", "walking_safari", "birding", "night_drive", "photography",
    ],
    "game_reserve": [
        "game_drive", "walking_safari", "birding", "night_drive", "horseback_safari",
    ],
    "island": [
        "beach_leisure", "diving", "snorkeling", "boat_safari", "fishing",
    ],
    "beach": [
        "beach_leisure", "diving", "snorkeling", "boat_safari", "fishing",
    ],
    "marine_park": [
        "diving", "snorkeling", "boat_safari", "fishing",
    ],
    "mountain": ["mountain_climbing", "hiking"],
    "desert": ["hiking", "camping", "photography"],
    "city": ["cultural_visit", "shopping", "photography"],
    "cultural_site": ["cultural_visit", "photography"],
    "unesco_site": ["cultural_visit", "photography"],
    "lake": ["boat_safari", "canoeing", "fishing", "birding"],
    "waterfall": ["hiking", "photography"],
    "forest_reserve": ["walking_safari", "birding", "hiking"],
    "wetland": ["birding", "boat_safari", "canoeing"],
}


TRAVEL_STYLE_CATEGORY_RANKS: dict[str, list[str]] = {
    "wildlife": [
        "game_drive", "walking_safari", "birding", "night_drive", "horseback_safari",
    ],
    "adventure": [
        "hiking", "mountain_climbing", "diving", "canoeing", "cycling",
    ],
    "beach": [
        "beach_leisure", "diving", "snorkeling", "boat_safari", "fishing",
    ],
    "cultural": ["cultural_visit", "shopping", "photography"],
    "culture": ["cultural_visit", "shopping", "photography"],
    "photography": ["photography", "birding", "game_drive", "walking_safari"],
    "birding": ["birding"],
    "luxury": ["spa_wellness", "photography", "boat_safari"],
    "relaxed_pace": ["beach_leisure", "spa_wellness", "boat_safari", "photography"],
    "walking": ["walking_safari", "hiking"],
}


FALLBACK_VARIANTS: dict[str, list[tuple[str, str]]] = {
    "island": [
        ("Beach & relaxation",
         "Free time at the lodge's beach area — no specific excursion "
         "booked; the operator will offer whatever suits sea conditions."),
        ("Snorkel gear & shoreline time",
         "Open beach time with snorkel gear available at the lodge — "
         "no guided excursion booked for this slot."),
    ],
    "beach": [
        ("Beach & relaxation",
         "Free time at the lodge's beach area — no specific excursion "
         "booked; the operator will offer whatever suits sea conditions."),
        ("Sunset shoreline walk",
         "Unstructured time along the beach — no guided activity booked "
         "for this slot."),
    ],
    "mountain": [
        ("Acclimatisation walk",
         "Short lower-altitude walk to acclimatise — no summit attempt "
         "scheduled today."),
        ("Rest & recovery time",
         "Open time at camp to rest — no scheduled activity for this slot."),
    ],
    "desert": [
        ("Desert nature walk",
         "Light guided walk suited to the terrain — no specific site "
         "booked."),
        ("Open camp time",
         "Unstructured time at camp — no specific excursion booked for "
         "this slot."),
    ],
    "marine_park": [
        ("Shore time at the lodge",
         "Open time at the lodge — optional water-based excursions may "
         "be offered if conditions allow."),
    ],
    "city": [
        ("Guided cultural stop",
         "Short guided visit to a locally significant site — no specific "
         "venue booked in advance."),
        ("Free time to explore",
         "Unstructured time to explore independently — no guided activity "
         "booked for this slot."),
    ],
    "cultural_site": [
        ("Guided cultural stop",
         "Short guided visit to a locally significant site — no specific "
         "venue booked in advance."),
    ],
    "unesco_site": [
        ("Guided cultural stop",
         "Short guided visit to a locally significant site — no specific "
         "venue booked in advance."),
    ],
    "national_park": [
        ("Guided wilderness drive",
         "Game drive on lodge circuits — no specific route booked in "
         "advance. Times may shift with conditions."),
        ("Extended photographic drive",
         "A slower-paced drive focused on photography opportunities — "
         "no specific route booked in advance."),
        ("Bush walk near camp",
         "Short guided walk near the lodge grounds, conditions permitting "
         "— no specific route booked in advance."),
    ],
    "game_reserve": [
        ("Guided wilderness drive",
         "Game drive on lodge circuits — no specific route booked in "
         "advance. Times may shift with conditions."),
        ("Extended photographic drive",
         "A slower-paced drive focused on photography opportunities — "
         "no specific route booked in advance."),
    ],
}


_DEFAULT_FALLBACK = [
    ("Time at the lodge",
     "Open time at the lodge — operator will offer what suits the "
     "day's conditions."),
]


@dataclass
class BuildResult:
    cabinet: Cabinet
    warnings: list[str] = field(default_factory=list)


def _default_start_for(category: str | None) -> dt_time:
    if category in {"game_drive", "walking_safari", "hiking", "mountain_climbing"}:
        return DEFAULT_GAME_DRIVE_START
    if category in {"diving", "snorkeling", "boat_safari", "fishing"}:
        return dt_time(9, 0)
    if category in {"cultural_visit", "shopping"}:
        return dt_time(10, 0)
    if category == "spa_wellness":
        return dt_time(11, 0)
    return dt_time(10, 0)


def _fallback_drawer_text(destination_type: str | None, variant_index: int) -> tuple[str, str]:
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
        for category in DESTINATION_TYPE_CATEGORY_RANKS.get(str(destination_type).lower(), []):
            if category not in seen:
                seen.add(category)
                result.append(category)

    return result


def _format_transfer_description(mode: str | None, minutes: int | None) -> str:
    """
    Format a human-readable transfer description that is honest about
    an unavailable duration rather than ever rendering "None" or
    inventing a placeholder number.
    """

    mode_label = {
        "scheduled_flight": "Scheduled flight",
        "charter_flight": "Charter flight",
        "private_4x4": "Private 4x4",
    }.get(mode or "private_4x4", "Transfer")

    if minutes is None:
        return f"{mode_label} · duration unavailable — confirm before booking"

    return f"{mode_label} · approximately {minutes} min"


class ItineraryPlanningEngine:
    def __init__(self, db: Session):
        self.db = db

    def build(
        self,
        request: dict[str, Any],
        destination_ids: list[str],
    ) -> BuildResult:

        days = self._safe_int(request.get("days"), 0)

        if days < 1:
            raise ValueError("Trip must contain at least one day.")

        travelers = max(1, self._safe_int(request.get("travelers"), 1))

        travel_style = request.get("travel_style") or self._infer_style(request)
        if isinstance(travel_style, str):
            travel_style = [travel_style]
        travel_style = list(
            dict.fromkeys(str(style).lower() for style in travel_style if style)
        )

        focus = request.get("focus", "wildlife")
        budget_tier = str(request.get("budget_tier", "mid")).lower()

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
                raise ValueError(f"Invalid start_date: {start_date_raw}") from exc

        if not destination_ids:
            raise ValueError("Itinerary generation requires at least one destination.")

        cleaned_destination_ids = list(
            dict.fromkeys(str(destination_id) for destination_id in destination_ids)
        )

        if len(cleaned_destination_ids) > days:
            logger.warning(
                "Trip requests %s destinations but only %s days. "
                "Only the first %s destinations can receive an overnight.",
                len(cleaned_destination_ids), days, days,
            )
            cleaned_destination_ids = cleaned_destination_ids[:days]

        destination_ids = cleaned_destination_ids

        cabinet = Cabinet(
            request_json=request,
            title=request.get("title") or self._default_title(request),
            duration_days=days,
            travelers_adults=travelers,
            travelers_children=self._safe_int(request.get("travelers_children"), 0),
            travel_style=travel_style,
            budget_tier=budget_tier,
            status="draft",
            start_date=start_date,
            end_date=(start_date + timedelta(days=days - 1) if start_date else None),
            primary_destination_id=destination_ids[0],
            route_destination_ids=destination_ids,
        )

        self.db.add(cabinet)
        self.db.flush()

        warnings: list[str] = []

        meta = self._fetch_destination_meta(destination_ids)

        missing_meta = [d for d in destination_ids if d not in meta]
        if missing_meta:
            warnings.append(
                "Some requested destinations could not be resolved in "
                "travel_places: " + ", ".join(missing_meta)
            )

        if hasattr(cabinet, "route_countries"):
            countries = [
                meta[d].get("country") for d in destination_ids if meta.get(d, {}).get("country")
            ]
            cabinet.route_countries = list(dict.fromkeys(countries))

        if hasattr(cabinet, "primary_country"):
            cabinet.primary_country = meta.get(destination_ids[0], {}).get("country")

        legs = self._build_hinges(
            cabinet=cabinet,
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
            d: meta.get(d, {}).get("destination_type") for d in destination_ids
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
                    "Explicit fallback time will be used."
                )
            elif len(pool) < 2:
                warnings.append(
                    f"Destination {destination_id} has only {len(pool)} seeded activity."
                )

        cursors = {d: 0 for d in destination_ids}
        fallback_counters = {d: 0 for d in destination_ids}

        day_number = 1
        current_date = start_date

        for destination_index, destination_id in enumerate(destination_ids):
            nights_here = allocation[destination_index]
            if nights_here <= 0:
                continue

            destination_type = destination_types.get(destination_id)

            for night_index in range(nights_here):
                is_first_day = day_number == 1
                is_last_day = day_number == days
                is_arrival_day = night_index == 0 and destination_index > 0

                shelf = Shelf(
                    cabinet_id=cabinet.id,
                    day_number=day_number,
                    date=current_date,
                    destination_id=destination_id,
                    theme=self._theme_for(
                        destination_index, night_index, is_first_day, is_last_day, destination_type,
                    ),
                )
                self.db.add(shelf)

                # FIX: explicitly populate the in-memory relationship
                # collection rather than relying only on cabinet_id +
                # a later flush/lazy-reload to make this shelf visible
                # via cabinet.shelves. Setting the raw FK column alone
                # does not update the backref'd collection in memory --
                # doc 2's Cabinet.shelves relationship uses
                # backref="cabinet", so assigning shelf.cabinet here
                # keeps cabinet.shelves correct immediately for any
                # caller that reads it right after build() returns in
                # the same transaction (itinerary_v2.py's orchestrator
                # now does exactly this).
                if hasattr(shelf, "cabinet"):
                    shelf.cabinet = cabinet
                elif shelf not in cabinet.shelves:
                    cabinet.shelves.append(shelf)

                self.db.flush()

                origin_dest_id = (
                    destination_ids[destination_index - 1]
                    if is_arrival_day and destination_index > 0
                    else None
                )

                first_activity_id = self._populate_drawers(
                    shelf=shelf,
                    pool=per_destination_pool[destination_id],
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

                self._populate_trays(shelf=shelf, is_first_day=is_first_day, is_last_day=is_last_day)

                if first_activity_id:
                    self._populate_day_photo(
                        shelf=shelf, activity_id=first_activity_id, destination_id=destination_id,
                    )

                day_number += 1
                if current_date:
                    current_date += timedelta(days=1)

        self.db.flush()

        return BuildResult(cabinet=cabinet, warnings=warnings)

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _fetch_destination_meta(self, destination_ids: list[str]) -> dict[str, dict[str, Any]]:
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
                text("SELECT to_regclass('estimated_visit_durations')")
            ).scalar()

            if table_exists:
                min_rows = self.db.execute(
                    text(
                        """
                        SELECT CAST(destination_id AS text), MIN(recommended_nights_min)
                        FROM estimated_visit_durations
                        WHERE destination_id = ANY(CAST(:ids AS uuid[]))
                          AND scope = 'full_destination'
                          AND recommended_nights_min IS NOT NULL
                        GROUP BY destination_id
                        """
                    ),
                    {"ids": destination_ids},
                ).fetchall()

                for destination_id, minimum in min_rows:
                    destination_id = str(destination_id)
                    if destination_id in meta and minimum is not None:
                        meta[destination_id]["min_nights"] = max(1, int(minimum))

        except Exception as exc:
            logger.warning("Could not read estimated_visit_durations: %s", exc)

        return meta

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
            destination_type=destination_type, travel_style=travel_style, focus=focus,
        )

        month_token = start_date.strftime("%B").lower() if start_date else None

        # FIX: native list binding instead of a hand-built "{a,b,c}"
        # array literal string. SQLAlchemy/psycopg handles the
        # text[] cast and escaping; we never concatenate the category
        # values into the SQL string ourselves.
        sql = text(
            """
            WITH ranked AS (
                SELECT
                    a.id,
                    a.name,
                    a.description,
                    CAST(a.category AS text) AS category,
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

                    md5(CAST(:cab_id AS text) || '|' || CAST(a.id AS text)) AS deterministic_order

                FROM activities a
                WHERE a.destination_id = CAST(:dest_id AS uuid)
            )
            SELECT id, name, description, category, difficulty, style_position, month_mismatch
            FROM ranked
            ORDER BY style_position ASC, month_mismatch ASC, deterministic_order ASC, id ASC
            """
        )

        try:
            rows = self.db.execute(
                sql,
                {
                    # Native Python list -- SQLAlchemy binds this as a
                    # proper array parameter, not a string we built by
                    # hand.
                    "ranked": ranked_categories,
                    "month": month_token,
                    "cab_id": cabinet_id,
                    "dest_id": dest_id,
                },
            ).fetchall()
        except Exception:
            logger.exception("Failed to fetch ranked activities for destination %s", dest_id)
            raise

        return [
            {"id": row[0], "name": row[1], "description": row[2], "category": row[3], "difficulty": row[4]}
            for row in rows
        ]

    def _populate_drawers(
        self, shelf: Shelf, pool: list[dict[str, Any]], cursor: dict[str, int],
        fallback_counters: dict[str, int], dest_id: str, dest_type: str | None,
        travel_style: list[str], focus: str | None, day_number: int,
        is_first_day: bool, is_last_day: bool, is_arrival_day: bool,
        origin_dest_id: str | None = None,
    ) -> str | None:

        order = 1
        first_activity_id: str | None = None

        if is_first_day:
            self._add_drawer(
                shelf=shelf, name="Airport welcome",
                description="Met at the airport by your driver-guide.",
                start_time=DEFAULT_ARRIVAL_TIME, duration_minutes=60, sort_order=order,
                activity_type="ARRIVAL", source="hardcoded_arrival_departure",
            )
            order += 1
            self._add_drawer(
                shelf=shelf, name="Transfer to lodge",
                description="Transfer to the lodge. No excursion is assumed during this arrival transfer.",
                start_time=dt_time(15, 30), duration_minutes=60, sort_order=order,
                activity_type="TRANSFER", source="hardcoded_arrival_departure",
            )
            order += 1
            self._add_drawer(
                shelf=shelf, name="Dinner at the lodge", description=None,
                start_time=dt_time(19, 30), duration_minutes=90, sort_order=order,
                activity_type="MEAL", source="hardcoded_meal",
            )
            return None

        if is_last_day:
            self._add_drawer(
                shelf=shelf, name="Breakfast", description=None,
                start_time=dt_time(7, 0), duration_minutes=45, sort_order=order,
                activity_type="MEAL", source="hardcoded_meal",
            )
            order += 1
            self._add_drawer(
                shelf=shelf, name="Transfer to airport", description=None,
                start_time=dt_time(9, 0), duration_minutes=120, sort_order=order,
                activity_type="TRANSFER", source="hardcoded_arrival_departure",
            )
            order += 1
            self._add_drawer(
                shelf=shelf, name="Departure", description=None,
                start_time=dt_time(12, 0), duration_minutes=30, sort_order=order,
                activity_type="DEPARTURE", source="hardcoded_arrival_departure",
            )
            return None

        if is_arrival_day:
            order, transfer_end_minutes = self._destination_arrival_transfer(
                shelf=shelf, order=order, dest_id=dest_id, origin_dest_id=origin_dest_id,
            )
            order += 1
        else:
            transfer_end_minutes = None

        morning = self._consume_next_activity(pool=pool, cursor=cursor, dest_id=dest_id)

        if morning:
            first_activity_id = morning["id"]
            self._add_drawer(
                shelf=shelf, name=morning["name"], description=morning["description"],
                start_time=_default_start_for(morning["category"]), duration_minutes=240,
                sort_order=order, activity_type="EXPERIENCE", activity_id=morning["id"],
                source="activities_table",
            )
        else:
            title, description = _fallback_drawer_text(dest_type, fallback_counters[dest_id])
            fallback_counters[dest_id] += 1
            self._add_drawer(
                shelf=shelf, name=title, description=description,
                start_time=_default_start_for(None), duration_minutes=180,
                sort_order=order, activity_type="EXPERIENCE",
                source="fallback_estimate", is_fallback=True,
            )
            logger.warning(
                "Day %s at %s: activity pool exhausted (morning slot); fallback used.",
                day_number, dest_id,
            )

        order += 1
        self._add_drawer(
            shelf=shelf, name="Lunch at the lodge", description=None,
            start_time=dt_time(13, 0), duration_minutes=60, sort_order=order,
            activity_type="MEAL", source="hardcoded_meal",
        )
        order += 1

        # --- Overlap-scheduling decision applied here ---
        #
        # On a non-arrival day, transfer_end_minutes is None and the
        # afternoon slot keeps its normal fixed start. On an arrival
        # day, the afternoon slot's start (and whether it exists at
        # all) is derived from the transfer's actual measured end time
        # rather than always assuming the fixed default -- this is
        # what prevents the "Arrival transfer 14:00-19:00 overlaps
        # Afternoon game drive 16:00" class of conflict that was
        # previously only caught (and not always resolved) by
        # ScheduleRepairEngine after the fact.
        afternoon_start_minutes = self._minutes(AFTERNOON_SLOT_DEFAULT_START)
        evening_start_minutes = self._minutes(EVENING_SLOT_START)
        skip_afternoon_slot = False

        if transfer_end_minutes is not None:
            candidate_start = transfer_end_minutes + POST_TRANSFER_BUFFER_MINUTES

            if candidate_start >= evening_start_minutes - MIN_USABLE_AFTERNOON_MINUTES:
                # The transfer alone consumes the useful afternoon.
                # Do not fabricate an activity slot that couldn't
                # actually happen -- drop it and be honest about why.
                skip_afternoon_slot = True
            else:
                afternoon_start_minutes = max(afternoon_start_minutes, candidate_start)

        if skip_afternoon_slot:
            settling_in_start_minutes = transfer_end_minutes + POST_TRANSFER_BUFFER_MINUTES
            settling_in_end_minutes = settling_in_start_minutes + 60

            self._add_drawer(
                shelf=shelf, name="Settling in after arrival",
                description=(
                    "The transfer into this destination takes up most of "
                    "the afternoon. No activity is scheduled for this slot "
                    "so there is time to settle in at the lodge."
                ),
                start_time=self._time_from_minutes(settling_in_start_minutes),
                duration_minutes=60, sort_order=order, activity_type="EXPERIENCE",
                source="hardcoded_long_transfer_arrival", is_fallback=True,
            )
            order += 1

            # A long enough transfer can push "settling in" itself into
            # or past the fixed evening slot. Rather than unconditionally
            # adding a Sundowner at a fixed time regardless of what else
            # already occupies that time (the original defect this whole
            # rule exists to fix), skip it when it would collide -- the
            # relaxed_pace preference is about not over-scheduling the
            # evening, and a redundant "arrived late, still added a
            # cocktail slot on top" entry works against that intent.
            evening_slot_occupied = settling_in_end_minutes > evening_start_minutes

            if "relaxed_pace" in travel_style and not evening_slot_occupied:
                self._add_drawer(
                    shelf=shelf, name="Sundowner at the lodge",
                    description="Relaxed evening time at the lodge.",
                    start_time=EVENING_SLOT_START, duration_minutes=60, sort_order=order,
                    activity_type="EXPERIENCE", source="hardcoded_relaxed_pace", is_fallback=True,
                )

            return first_activity_id

        afternoon_start = self._time_from_minutes(afternoon_start_minutes)
        afternoon = self._consume_next_activity(pool=pool, cursor=cursor, dest_id=dest_id)

        if afternoon:
            self._add_drawer(
                shelf=shelf, name=afternoon["name"], description=afternoon["description"],
                start_time=afternoon_start, duration_minutes=150,
                sort_order=order, activity_type="EXPERIENCE", activity_id=afternoon["id"],
                source="activities_table",
            )
        else:
            title, description = _fallback_drawer_text(dest_type, fallback_counters[dest_id])
            fallback_counters[dest_id] += 1
            self._add_drawer(
                shelf=shelf, name=title, description=description,
                start_time=afternoon_start, duration_minutes=150,
                sort_order=order, activity_type="EXPERIENCE", source="fallback_estimate", is_fallback=True,
            )
            logger.warning(
                "Day %s at %s: activity pool exhausted (afternoon slot); fallback used.",
                day_number, dest_id,
            )
        order += 1

        afternoon_end_minutes = afternoon_start_minutes + 150
        evening_slot_occupied = afternoon_end_minutes > evening_start_minutes

        if "relaxed_pace" in travel_style and not evening_slot_occupied:
            self._add_drawer(
                shelf=shelf, name="Sundowner at the lodge",
                description="Relaxed evening time at the lodge.",
                start_time=EVENING_SLOT_START, duration_minutes=60, sort_order=order,
                activity_type="EXPERIENCE", source="hardcoded_relaxed_pace", is_fallback=True,
            )

        return first_activity_id

    @staticmethod
    def _minutes(t: dt_time) -> int:
        return t.hour * 60 + t.minute

    @staticmethod
    def _time_from_minutes(minutes: int) -> dt_time:
        # Clamp to a valid time-of-day. A transfer that would push past
        # midnight is a sign the itinerary needs a rest day or a split
        # transfer, not something this method should silently wrap --
        # clamping to 23:59 keeps the drawer constructible while
        # ValidationEngine's overlap/long-transfer checks surface the
        # underlying problem for a human to resolve.
        minutes = max(0, min(minutes, 23 * 60 + 59))
        return dt_time(minutes // 60, minutes % 60)

    def _destination_arrival_transfer(
        self, shelf: Shelf, order: int, dest_id: str, origin_dest_id: str | None,
    ) -> tuple[int, int]:
        """
        Returns (order, transfer_end_minutes) so the caller can adapt
        the rest of the day's schedule to the transfer's actual
        duration rather than assuming a fixed default.

        FIX (schema-mismatch decision, Option A -- see schema.sql
        SECTION 4): this query now filters on BOTH from_destination_id
        and to_destination_id, matching route_geography.py's directed
        lookup. The previous version filtered only on the arrival
        destination, ignoring which destination the traveler was
        actually coming from -- meaning it could return a drive_times
        row describing a completely different route into the same
        destination. origin_dest_id is the destination immediately
        before this one in the requested route order (see build()'s
        main loop, where it is derived from destination_ids[index-1]).

        If origin_dest_id is unavailable (should not normally happen
        when is_arrival_day is True, since that flag itself implies a
        previous destination exists -- but defended against rather
        than assumed), no directed lookup is possible and this method
        honestly falls through to the no-fabrication branch instead of
        guessing at an undirected match.
        """

        row = None

        if origin_dest_id is not None:
            row = self.db.execute(
                text(
                    """
                    SELECT distance_km, duration_minutes_dry_season
                    FROM drive_times
                    WHERE from_destination_id = CAST(:from_destination_id AS uuid)
                      AND to_destination_id = CAST(:to_destination_id AS uuid)
                    """
                ),
                {"from_destination_id": origin_dest_id, "to_destination_id": dest_id},
            ).fetchone()
        else:
            logger.warning(
                "_destination_arrival_transfer called for %s with no "
                "origin_dest_id; cannot perform a directed drive_times "
                "lookup. Using the no-fabrication fallback instead of "
                "an undirected (and potentially wrong-route) query.",
                dest_id,
            )

        duration = int(row[1]) if row and row[1] else None

        # FIX (no-fabrication tolerance): honest description when
        # duration is unavailable, rather than assuming a number for
        # display purposes.
        if duration is not None:
            description = (
                f"Transfer into the destination. Approximately "
                f"{duration} minutes based on available route data."
            )
        else:
            description = (
                "Transfer into the destination. Duration is not yet "
                "confirmed from available route data; a conservative "
                "placeholder duration is used for scheduling purposes only."
            )

        # An unknown duration falls back to a conservative fixed
        # estimate for SCHEDULING purposes (we still need some value to
        # compute what comes after it in the day) -- but the persisted
        # drawer's description and source are honest about this being
        # unconfirmed, per the no-fabrication rule applied consistently
        # with route_geography.py and the None-duration handling
        # elsewhere in this file.
        effective_duration = duration if duration is not None else FALLBACK_ARRIVAL_TRANSFER_MINUTES

        start_minutes = self._minutes(ARRIVAL_TRANSFER_START)
        end_minutes = start_minutes + effective_duration

        self._add_drawer(
            shelf=shelf, name="Arrival transfer", description=description,
            start_time=ARRIVAL_TRANSFER_START, duration_minutes=effective_duration,
            sort_order=order, activity_type="TRANSFER",
            source=("drive_times" if row and duration is not None else "fallback_estimate"),
            is_fallback=not bool(row and duration is not None),
        )
        return order, end_minutes

    @staticmethod
    def _consume_next_activity(pool: list[dict[str, Any]], cursor: dict[str, int], dest_id: str) -> dict[str, Any] | None:
        position = cursor.get(dest_id, 0)
        if position >= len(pool):
            return None
        activity = pool[position]
        cursor[dest_id] = position + 1
        return activity

    @staticmethod
    def _add_drawer(
        shelf: Shelf, name: str, description: str | None, start_time: dt_time,
        duration_minutes: int, sort_order: int, activity_type: str,
        activity_id: Any = None, source: str = "activities_table", is_fallback: bool = False,
    ) -> Drawer:
        drawer = Drawer(
            shelf_id=shelf.id, activity_id=activity_id, name=name, description=description,
            start_time=start_time, duration_minutes=duration_minutes, sort_order=sort_order,
            activity_type=activity_type, source=source, is_fallback=is_fallback,
        )
        shelf.drawers.append(drawer)
        return drawer

    def _build_hinges(
        self, cabinet: Any, destination_ids: list[str], meta: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:

        cabinet_id = cabinet.id
        legs: list[dict[str, Any]] = []
        sequence = 0

        for index in range(len(destination_ids) - 1):
            frm = destination_ids[index]
            to = destination_ids[index + 1]
            if frm == to:
                continue

            from_country = meta.get(frm, {}).get("country")
            to_country = meta.get(to, {}).get("country")
            is_inter_country = bool(from_country and to_country and from_country != to_country)

            # FIX (schema-mismatch decision, Option A -- see schema.sql
            # SECTION 4): directed lookup matching route_geography.py,
            # instead of the previous undirected query that ignored
            # which destination the traveler was coming from.
            drive_row = self.db.execute(
                text(
                    """
                    SELECT distance_km, duration_minutes_dry_season
                    FROM drive_times
                    WHERE from_destination_id = CAST(:from_dest AS uuid)
                      AND to_destination_id = CAST(:to_dest AS uuid)
                    """
                ),
                {"from_dest": frm, "to_dest": to},
            ).fetchone()

            distance_km = None
            duration_minutes = None
            source = None
            mode = "private_4x4"

            if drive_row:
                if drive_row[0] is not None:
                    distance_km = float(drive_row[0])
                if drive_row[1] is not None:
                    duration_minutes = int(drive_row[1])
                source = "drive_times"

            flight_row = None
            if is_inter_country or (
                duration_minutes is not None and duration_minutes > DRIVE_TO_FLIGHT_THRESHOLD_MINUTES
            ):
                flight_row = self.db.execute(
                    text(
                        """
                        SELECT f.duration_minutes
                        FROM flights f
                        WHERE f.origin_airport_id IN (
                            SELECT airport_id FROM destination_airports
                            WHERE destination_id = CAST(:frm AS uuid) AND is_primary_gateway
                        )
                        AND f.destination_airport_id IN (
                            SELECT airport_id FROM destination_airports
                            WHERE destination_id = CAST(:to AS uuid) AND is_primary_gateway
                        )
                        ORDER BY f.duration_minutes ASC NULLS LAST
                        LIMIT 1
                        """
                    ),
                    {"frm": frm, "to": to},
                ).fetchone()

            if flight_row:
                duration_minutes = int(flight_row[0]) if flight_row[0] is not None else None
                mode = "scheduled_flight"
                source = "flights_table"
                distance_km = None

            # FIX: no-fabrication tolerance. If neither drive nor
            # flight data is available, we now leave duration_minutes
            # and distance_km as None with source="unavailable"
            # instead of substituting a fabricated placeholder
            # (previously: 480min charter-flight guess, or 180min /
            # 150km overland guess). This matches route_geography.py's
            # honesty contract and is now handled downstream by
            # ValidationEngine (warns rather than crashes) and the
            # armrest/description formatting below.
            if duration_minutes is None:
                mode = "charter_flight" if is_inter_country else "private_4x4"
                source = "unavailable"
                distance_km = None
                logger.warning(
                    "No measured drive or flight route for %s -> %s. "
                    "Duration is unavailable; not fabricating an estimate.",
                    frm, to,
                )

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
                    logger.warning(
                        "Inter-country overland leg %s -> %s has no border_crossings record.", frm, to,
                    )

            sequence += 1
            hinge = Hinge(
                cabinet_id=cabinet_id, from_destination_id=frm, to_destination_id=to,
                sequence_order=sequence, distance_km=distance_km, duration_minutes=duration_minutes,
                mode=mode, source=source, is_inter_country=is_inter_country,
                requires_border_crossing=is_inter_country, border_crossing_id=border_crossing_id,
            )
            self.db.add(hinge)

            # FIX: same relationship-population issue as Shelf above --
            # setting cabinet_id alone does not update cabinet.hinges in
            # memory. Populate it explicitly so callers reading
            # cabinet.hinges immediately after build() (e.g.
            # itinerary_v2.py, ValidationEngine) see it correctly.
            if hasattr(hinge, "cabinet"):
                hinge.cabinet = cabinet
            elif hinge not in cabinet.hinges:
                cabinet.hinges.append(hinge)

            legs.append({
                "from": frm, "to": to, "duration_minutes": duration_minutes, "source": source,
                "mode": mode, "is_inter_country": is_inter_country, "border_crossing_id": border_crossing_id,
            })

        return legs

    def _allocate_days(
        self, destination_ids: list[str], meta: dict[str, dict[str, Any]],
        total_days: int, travel_style: list[str],
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

        allocation = [total_days // n] * n
        remainder = total_days % n
        for index in range(remainder):
            allocation[index] += 1

        warnings: list[str] = []

        for i in range(1, n):
            previous_destination = destination_ids[i - 1]
            current_destination = destination_ids[i]
            previous_country = meta.get(previous_destination, {}).get("country")
            current_country = meta.get(current_destination, {}).get("country")

            if not (previous_country and current_country and previous_country != current_country):
                continue
            if BORDER_BUFFER_NIGHTS <= 0:
                continue

            donor_candidates: list[tuple[int, int]] = []
            for donor_index in range(n):
                minimum = meta.get(destination_ids[donor_index], {}).get("min_nights", 1)
                slack = allocation[donor_index] - minimum
                # Confirmed fix (already present in this canonical
                # version, preserved here): >= BORDER_BUFFER_NIGHTS,
                # not > 0, so a destination can never be donated from
                # below its recommended minimum.
                if slack >= BORDER_BUFFER_NIGHTS:
                    donor_candidates.append((slack, donor_index))

            if not donor_candidates:
                label = meta.get(current_destination, {}).get("headline_label", current_destination)
                warnings.append(
                    f"Could not add a border-buffer night before entering {label} "
                    "without shortening another destination below its recommended "
                    "minimum stay."
                )
                continue

            _, donor_index = max(donor_candidates, key=lambda item: item[0])
            if donor_index == i:
                continue

            allocation[donor_index] -= BORDER_BUFFER_NIGHTS
            allocation[i] += BORDER_BUFFER_NIGHTS

        if any(value < 0 for value in allocation):
            logger.error("Negative day allocation detected: %s", allocation)
            allocation = [max(0, value) for value in allocation]
            while sum(allocation) < total_days:
                allocation[-1] += 1

        if sum(allocation) != total_days:
            logger.error("Day allocation invariant violated: %s != %s", sum(allocation), total_days)
            allocation[-1] += total_days - sum(allocation)

        return allocation, warnings

    def _populate_headboard(self, shelf: Shelf, dest_id: str, budget_tier: str, remaining_nights_here: int) -> None:
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
            WHERE destination_id = CAST(:dest_id AS uuid)
              AND tier::text = ANY(CAST(:tiers AS text[]))
            ORDER BY star_rating DESC NULLS LAST
            LIMIT 1
            """
        ),
        {"dest_id": dest_id, "tiers": list(tiers)},
    ).fetchone()

    check_out = None
    if shelf.date and remaining_nights_here > 0:
        check_out = shelf.date + timedelta(days=remaining_nights_here)

    if row:
        headboard = Headboard(
            shelf_id=shelf.id, lodge_id=row[0], name=row[1], tier=row[2],
            check_in=shelf.date, check_out=check_out, nights=remaining_nights_here,
        )
    else:
        headboard = Headboard(
            shelf_id=shelf.id, name=f"{budget_tier.title()} lodge", tier=budget_tier,
            check_in=shelf.date, check_out=check_out, nights=remaining_nights_here,
        )

    self.db.add(headboard)


    def _populate_armrest(self, shelf: Shelf, legs: list[dict[str, Any]], destination_index: int, is_arrival_day: bool) -> None:
        if is_arrival_day and destination_index > 0 and destination_index - 1 < len(legs):
            leg = legs[destination_index - 1]
            minutes = leg["duration_minutes"]
            mode = leg.get("mode") or "private_4x4"

            # FIX: honest formatting when minutes is None.
            description = _format_transfer_description(mode, minutes)

            armrest = Armrest(
                shelf_id=shelf.id, mode=mode, description=description,
                duration_minutes=minutes, # left as None when genuinely unknown
                is_private=(mode == "private_4x4"),
            )
        else:
            armrest = Armrest(
                shelf_id=shelf.id, mode="private_4x4",
                description="Private 4x4 · local destination transport",
                duration_minutes=0, is_private=True,
            )

        self.db.add(armrest)

    def _populate_trays(self, shelf: Shelf, is_first_day: bool, is_last_day: bool) -> None:
        if is_first_day:
            meals = ["dinner"]
        elif is_last_day:
            meals = ["breakfast"]
        else:
            meals = ["breakfast", "lunch", "dinner"]

        for meal in meals:
            self.db.add(Tray(shelf_id=shelf.id, meal_type=meal, included=True))

    def _populate_day_photo(self, shelf: Shelf, activity_id: Any, destination_id: str) -> None:
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
                    WHERE activity_id = CAST(:activity_id AS uuid) AND url IS NOT NULL
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
                        WHERE destination_id = CAST(:destination_id AS uuid) AND url IS NOT NULL
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
        for field_name in ("hero_image_url", "image_url", "photo_url", "cover_image_url"):
            if hasattr(shelf, field_name):
                setattr(shelf, field_name, image_url)
                return

    @staticmethod
    def _theme_for(idx: int, night_idx: int, is_first: bool, is_last: bool, destination_type: str | None) -> str:
        if is_first:
            return "Arrival & slow start"
        if is_last:
            return "Departure"
        if destination_type in {"national_park", "game_reserve"}:
            return "Wildlife & wide horizons" if night_idx == 0 else "Deeper into the park"
        if destination_type in {"island", "beach", "marine_park"}:
            return "Coast, water & open horizons"
        if destination_type in {"mountain", "waterfall", "forest_reserve"}:
            return "Nature & exploration"
        if destination_type in {"city", "cultural_site", "unesco_site"}:
            return "Culture & discovery"
        return "Explore the destination"

    @staticmethod
    def _infer_style(request: dict[str, Any]) -> list[str]:
        styles: list[str] = []
        focus = request.get("focus")

        if focus == "wildlife":
            styles.append("wildlife")
        elif focus in {"beach", "adventure", "culture", "cultural", "photography", "birding", "walking"}:
            styles.append(str(focus))

        if request.get("budget_tier") == "luxury":
            styles.append("luxury")

        if ItineraryPlanningEngine._safe_int(request.get("travelers"), 2) <= 2:
            styles.append("private")

        styles.append("relaxed_pace")
        return list(dict.fromkeys(styles))

    @staticmethod
    def _default_title(request: dict[str, Any]) -> str:
        country = request.get("country_name", "Africa")
        return f"{country}, Wild & Unhurried"

