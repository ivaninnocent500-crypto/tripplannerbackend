"""
itineraryPlanningEngine.py
==========================

Production itinerary builder for the persisted furniture schema.

CHANGE LOG (this rewrite -- transit-day fix, backend stage 2)
------------------------------------------------------------------
1. migration 006_shelf_day_kind.sql -- adds shelves.day_kind
   ("STANDARD" | "TRANSIT"), and its matching models_furniture.py
   Shelf.day_kind column.
2. pipeline_adapters.py -- allocate_days_for_route() extracted as a
   standalone pure function; transit_days_from_day_plan() added.

WHAT CHANGED AND WHY
---------------------
(A) Day allocation is no longer computed inside this engine.
    ItineraryPlanningEngine._allocate_days() has been DELETED from
    this file -- that logic now lives in
    pipeline_adapters.allocate_days_for_route(), called by
    ItineraryOrchestrator BEFORE this engine runs, specifically so
    DayArchetypeEngine can classify every day (including detecting
    LONG_TRANSFER days) before any Drawer is constructed. build() now
    REQUIRES the caller to pass `day_allocation` (the same
    list[int]-per-destination shape _allocate_days() used to return)
    and `transit_days` (day_number -> bool, from
    pipeline_adapters.transit_days_from_day_plan()).

(B) TRANSIT-day drawer template.
    When transit_days.get(day_number) is True, _populate_drawers()
    now takes a completely different path: it builds ONLY structural
    transfer/settle-in drawers (matching the "Airport -> Lodge -> free
    time -> Dinner" reference shape) and marks the Shelf itself as
    day_kind="TRANSIT". It does NOT force a lunch slot, an EXPERIENCE
    activity, or a sundowner onto that day. If the leg's real 
    duration/mode is known, that real fact is shown; if not, the existing
    honest "duration unavailable" wording is preserved.

(C) Clock times removed except the two safari game-drive slots.
    Per direct product decision: only the morning game-drive slot
    (DEFAULT_GAME_DRIVE_START = 06:00) and an evening/late-afternoon 
    game-drive slot keep a real dt_time start_time. Every other drawer 
    is built with start_time=None and relies on duration_minutes + 
    sort_order for display ordering.

(D) Arrival-day lunch-after-transfer ordering: lunch is sequenced AFTER 
    the arrival transfer drawer via sort_order, never via a fixed clock 
    comparison, since fixed clock times for lunch no longer exist to 
    compare against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta, time as dt_time
from typing import Any, Mapping

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


# The ONLY two drawers in the entire engine that still carry a real
# clock time, per direct product decision -- animals are genuinely
# most active in these windows.
DEFAULT_GAME_DRIVE_START = dt_time(6, 0)
EVENING_GAME_DRIVE_START = dt_time(16, 0)

DRIVE_TO_FLIGHT_THRESHOLD_MINUTES = 6 * 60

FALLBACK_ARRIVAL_TRANSFER_MINUTES = 60
FALLBACK_DEPARTURE_TRANSFER_MINUTES = 60
FALLBACK_TRANSIT_LEG_MINUTES = 60


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


def _is_game_drive_category(category: str | None) -> bool:
    return category in {"game_drive", "walking_safari", "hiking", "mountain_climbing"}


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

    def fetch_destination_meta(self, destination_ids: list[str]) -> dict[str, dict[str, Any]]:
        """
        Public contract method called by ItineraryOrchestrator and internally.
        Fetches metadata for requested destinations from travel_places (and
        estimated_visit_durations if available).
        """
        return self._fetch_destination_meta(destination_ids)

    def build(
        self,
        request: dict[str, Any],
        destination_ids: list[str],
        *,
        day_allocation: list[int],
        transit_days: Mapping[int, bool] | None = None,
    ) -> BuildResult:
        """
        Builds and persists the full Cabinet model structure.
        
        Requires externally computed `day_allocation` and optional `transit_days` 
        map (day_number -> bool) from DayArchetypeEngine / pipeline_adapters.
        """
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

        if len(day_allocation) != len(destination_ids):
            raise ValueError(
                "day_allocation length "
                f"({len(day_allocation)}) does not match destination_ids "
                f"length ({len(destination_ids)}) after cleaning. The "
                "caller must recompute allocation if destination_ids "
                "was trimmed to fit `days`."
            )

        transit_days = transit_days or {}

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

        allocation = day_allocation

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
                is_transit_day = bool(transit_days.get(day_number, False))

                shelf = Shelf(
                    cabinet_id=cabinet.id,
                    day_number=day_number,
                    date=current_date,
                    destination_id=destination_id,
                    theme=self._theme_for(
                        destination_index, night_index, is_first_day, is_last_day,
                        destination_type, is_transit_day,
                    ),
                    day_kind=("TRANSIT" if is_transit_day else "STANDARD"),
                )
                self.db.add(shelf)

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
                    is_transit_day=is_transit_day,
                    legs=legs,
                    destination_index=destination_index,
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
                    shelf=shelf, is_first_day=is_first_day, is_last_day=is_last_day,
                    is_transit_day=is_transit_day,
                )

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
                "id": destination_id,
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

    # ------------------------------------------------------------------
    # DRAWER CONSTRUCTION
    # ------------------------------------------------------------------

    def _populate_drawers(
        self, shelf: Shelf, pool: list[dict[str, Any]], cursor: dict[str, int],
        fallback_counters: dict[str, int], dest_id: str, dest_type: str | None,
        travel_style: list[str], focus: str | None, day_number: int,
        is_first_day: bool, is_last_day: bool, is_arrival_day: bool,
        is_transit_day: bool, legs: list[dict[str, Any]], destination_index: int,
        origin_dest_id: str | None = None,
    ) -> str | None:

        if is_first_day:
            return self._populate_first_day_drawers(shelf)

        if is_last_day:
            return self._populate_last_day_drawers(shelf)

        if is_transit_day:
            return self._populate_transit_day_drawers(
                shelf=shelf, legs=legs, destination_index=destination_index,
                is_arrival_day=is_arrival_day,
            )

        return self._populate_standard_day_drawers(
            shelf=shelf, pool=pool, cursor=cursor, fallback_counters=fallback_counters,
            dest_id=dest_id, dest_type=dest_type, travel_style=travel_style,
            day_number=day_number, is_arrival_day=is_arrival_day,
            origin_dest_id=origin_dest_id,
        )

    def _populate_first_day_drawers(self, shelf: Shelf) -> None:
        order = 1
        self._add_drawer(
            shelf=shelf, name="Airport welcome",
            description="Met at the airport by your driver-guide.",
            start_time=None, duration_minutes=20, sort_order=order,
            activity_type="ARRIVAL", source="hardcoded_arrival_departure",
        )
        order += 1
        self._add_drawer(
            shelf=shelf, name="Transfer to lodge",
            description="Transfer to the lodge. No excursion is assumed during this arrival transfer.",
            start_time=None, duration_minutes=80, sort_order=order,
            activity_type="TRANSFER", source="hardcoded_arrival_departure",
        )
        order += 1
        self._add_drawer(
            shelf=shelf, name="Free time at the lodge",
            description="Settle in and rest ahead of dinner.",
            start_time=None, duration_minutes=240, sort_order=order,
            activity_type="FREE_TIME", source="hardcoded_arrival_departure", is_fallback=True,
        )
        order += 1
        self._add_drawer(
            shelf=shelf, name="Dinner at the lodge", description=None,
            start_time=None, duration_minutes=90, sort_order=order,
            activity_type="MEAL", source="hardcoded_meal",
        )
        return None

    def _populate_last_day_drawers(self, shelf: Shelf) -> None:
        order = 1
        self._add_drawer(
            shelf=shelf, name="Breakfast", description=None,
            start_time=None, duration_minutes=45, sort_order=order,
            activity_type="MEAL", source="hardcoded_meal",
        )
        order += 1
        self._add_drawer(
            shelf=shelf, name="Transfer to airport", description=None,
            start_time=None, duration_minutes=120, sort_order=order,
            activity_type="TRANSFER", source="hardcoded_arrival_departure",
        )
        order += 1
        self._add_drawer(
            shelf=shelf, name="Departure", description=None,
            start_time=None, duration_minutes=30, sort_order=order,
            activity_type="DEPARTURE", source="hardcoded_arrival_departure",
        )
        return None

    def _populate_transit_day_drawers(
        self, shelf: Shelf, legs: list[dict[str, Any]], destination_index: int,
        is_arrival_day: bool,
    ) -> None:
        order = 1

        leg = None
        if is_arrival_day and destination_index > 0 and destination_index - 1 < len(legs):
            leg = legs[destination_index - 1]

        mode = (leg or {}).get("mode") or "charter_flight"
        duration_minutes = (leg or {}).get("duration_minutes")

        transfer_description = _format_transfer_description(mode, duration_minutes)
        effective_duration = (
            duration_minutes if duration_minutes is not None else FALLBACK_TRANSIT_LEG_MINUTES
        )

        self._add_drawer(
            shelf=shelf, name="Long-distance transfer",
            description=transfer_description,
            start_time=None, duration_minutes=effective_duration, sort_order=order,
            activity_type="TRANSFER",
            source=(leg.get("source") if leg else "fallback_estimate") or "fallback_estimate",
            is_fallback=(duration_minutes is None),
        )
        order += 1

        self._add_drawer(
            shelf=shelf, name="Free time at the lodge",
            description=(
                "Most of this day is spent travelling between destinations. "
                "No activity is scheduled so there is time to rest and "
                "settle in on arrival."
            ),
            start_time=None, duration_minutes=180, sort_order=order,
            activity_type="FREE_TIME", source="hardcoded_transit_day", is_fallback=True,
        )
        order += 1

        self._add_drawer(
            shelf=shelf, name="Dinner at the lodge", description=None,
            start_time=None, duration_minutes=90, sort_order=order,
            activity_type="MEAL", source="hardcoded_meal",
        )

        return None

    def _populate_standard_day_drawers(
        self, shelf: Shelf, pool: list[dict[str, Any]], cursor: dict[str, int],
        fallback_counters: dict[str, int], dest_id: str, dest_type: str | None,
        travel_style: list[str], day_number: int, is_arrival_day: bool,
        origin_dest_id: str | None,
    ) -> str | None:

        order = 1
        first_activity_id: str | None = None

        if is_arrival_day:
            order, _ = self._destination_arrival_transfer(
                shelf=shelf, order=order, dest_id=dest_id, origin_dest_id=origin_dest_id,
            )
            order += 1

        if is_arrival_day:
            morning = None
        else:
            morning = self._consume_next_activity(pool=pool, cursor=cursor, dest_id=dest_id)

        if morning:
            first_activity_id = morning["id"]
            morning_is_game_drive = _is_game_drive_category(morning["category"])
            self._add_drawer(
                shelf=shelf, name=morning["name"], description=morning["description"],
                start_time=(DEFAULT_GAME_DRIVE_START if morning_is_game_drive else None),
                duration_minutes=240,
                sort_order=order, activity_type="EXPERIENCE", activity_id=morning["id"],
                source="activities_table",
            )
            order += 1
        elif not is_arrival_day:
            title, description = _fallback_drawer_text(dest_type, fallback_counters[dest_id])
            fallback_counters[dest_id] += 1
            self._add_drawer(
                shelf=shelf, name=title, description=description,
                start_time=None, duration_minutes=180,
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
            start_time=None, duration_minutes=60, sort_order=order,
            activity_type="MEAL", source="hardcoded_meal",
        )
        order += 1

        afternoon = self._consume_next_activity(pool=pool, cursor=cursor, dest_id=dest_id)

        if afternoon:
            if first_activity_id is None:
                first_activity_id = afternoon["id"]
            afternoon_is_game_drive = _is_game_drive_category(afternoon["category"])
            self._add_drawer(
                shelf=shelf, name=afternoon["name"], description=afternoon["description"],
                start_time=(EVENING_GAME_DRIVE_START if afternoon_is_game_drive else None),
                duration_minutes=150,
                sort_order=order, activity_type="EXPERIENCE", activity_id=afternoon["id"],
                source="activities_table",
            )
        else:
            title, description = _fallback_drawer_text(dest_type, fallback_counters[dest_id])
            fallback_counters[dest_id] += 1
            self._add_drawer(
                shelf=shelf, name=title, description=description,
                start_time=None, duration_minutes=150,
                sort_order=order, activity_type="EXPERIENCE", source="fallback_estimate", is_fallback=True,
            )
            logger.warning(
                "Day %s at %s: activity pool exhausted (afternoon slot); fallback used.",
                day_number, dest_id,
            )
        order += 1

        if "relaxed_pace" in travel_style:
            self._add_drawer(
                shelf=shelf, name="Sundowner at the lodge",
                description="Relaxed evening time at the lodge.",
                start_time=None, duration_minutes=60, sort_order=order,
                activity_type="EXPERIENCE", source="hardcoded_relaxed_pace", is_fallback=True,
            )

        return first_activity_id

    def _destination_arrival_transfer(
        self, shelf: Shelf, order: int, dest_id: str, origin_dest_id: str | None,
    ) -> tuple[int, bool]:
        row = None

        if origin_dest_id is not None:
            row = self.db.execute(
                text(
                    """
                    SELECT distance_km, duration_minutes_dry_season
                    FROM drive_times_between_destinations
                    WHERE from_destination_id = CAST(:from_destination_id AS uuid)
                      AND to_destination_id = CAST(:to_destination_id AS uuid)
                    """
                ),
                {"from_destination_id": origin_dest_id, "to_destination_id": dest_id},
            ).fetchone()
        else:
            logger.warning(
                "_destination_arrival_transfer called for %s with no "
                "origin_dest_id; using fallback estimate.",
                dest_id,
            )

        duration = int(row[1]) if row and row[1] else None

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

        effective_duration = duration if duration is not None else FALLBACK_ARRIVAL_TRANSFER_MINUTES

        self._add_drawer(
            shelf=shelf, name="Arrival transfer", description=description,
            start_time=None, duration_minutes=effective_duration,
            sort_order=order, activity_type="TRANSFER",
            source=("drive_times_between_destinations" if row and duration is not None else "fallback_estimate"),
            is_fallback=not bool(row and duration is not None),
        )
        return order, bool(row and duration is not None)

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
        shelf: Shelf, name: str, description: str | None, start_time: dt_time | None,
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

            drive_row = self.db.execute(
                text(
                    """
                    SELECT distance_km, duration_minutes_dry_season
                    FROM drive_times_between_destinations
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
                source = "drive_times_between_destinations"

            flight_row = None
            if is_inter_country or (
                duration_minutes is not None and duration_minutes > DRIVE_TO_FLIGHT_THRESHOLD_MINUTES
            ):
                flight_row = self.db.execute(
                    text(
                        """
                        SELECT f.duration_minutes
                        FROM flights f
                        WHERE (
                            f.origin_airport_id IN (
                                SELECT airport_id FROM destination_airports
                                WHERE destination_id = CAST(:frm AS uuid) AND is_primary_gateway
                            )
                            OR f.origin_airstrip_id IN (
                                SELECT id FROM airstrips
                                WHERE destination_id = CAST(:frm AS uuid)
                            )
                        )
                        AND (
                            f.destination_airport_id IN (
                                SELECT airport_id FROM destination_airports
                                WHERE destination_id = CAST(:to AS uuid) AND is_primary_gateway
                            )
                            OR f.destination_airstrip_id IN (
                                SELECT id FROM airstrips
                                WHERE destination_id = CAST(:to AS uuid)
                            )
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
                        WHERE (country_a::text = :country_a AND country_b::text = :country_b)
                           OR (country_a::text = :country_b AND country_b::text = :country_a)
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

            if hasattr(hinge, "cabinet"):
                hinge.cabinet = cabinet
            elif hinge not in cabinet.hinges:
                cabinet.hinges.append(hinge)

            legs.append({
                "from": frm, "to": to, "duration_minutes": duration_minutes, "source": source,
                "mode": mode, "is_inter_country": is_inter_country, "border_crossing_id": border_crossing_id,
            })

        return legs

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

            description = _format_transfer_description(mode, minutes)

            armrest = Armrest(
                shelf_id=shelf.id, mode=mode, description=description,
                duration_minutes=minutes,
                is_private=(mode == "private_4x4"),
            )
        else:
            armrest = Armrest(
                shelf_id=shelf.id, mode="private_4x4",
                description="Private 4x4 · local destination transport",
                duration_minutes=0, is_private=True,
            )

        self.db.add(armrest)

    def _populate_trays(
        self, shelf: Shelf, is_first_day: bool, is_last_day: bool, is_transit_day: bool,
    ) -> None:
        if is_first_day:
            meals = ["dinner"]
        elif is_last_day:
            meals = ["breakfast"]
        elif is_transit_day:
            meals = ["dinner"]
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
    def _theme_for(
        idx: int, night_idx: int, is_first: bool, is_last: bool,
        destination_type: str | None, is_transit_day: bool = False,
    ) -> str:
        if is_transit_day:
            return "Travel day"
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

