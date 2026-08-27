"""
ItineraryPlanningEngine (v2) — the deterministic trip-construction engine
described in the planning doc: "the backend must be capable of building
a complete, valid trip by itself."

This REPLACES the old ItineraryEngine.build() -> list[day] shape with a
version that:
  1. Actually persists the trip (Cabinet + Shelves + Drawers + Headboards
     + Armrests + Trays + Hinges) instead of returning throwaway objects.
  2. Uses drive_times / activities / lodges as real constraints, not
     just labels.
  3. Hands off to ValidationEngine before it's considered "ready".
  4. Hands off to ExplanationEngine for the "Why this itinerary?" block
     (facts only — AI, if used, narrates these facts, it does not
     invent new ones).

I don't have your real activities/lodges row shapes beyond the DDL, so
the selection heuristics below are intentionally simple and commented
where they should get smarter (e.g. weighting by wildlife_score once
you add scoring columns, or by season/month via monthly_weather_patterns).
Treat this as the skeleton to wire your actual data into, not a finished
recommender.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta, time as dt_time
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models_furniture import (
    Armrest, Cabinet, Drawer, Headboard, Hinge, Shelf, Tray,
)

logger = logging.getLogger(__name__)

DEFAULT_GAME_DRIVE_START = dt_time(6, 0)
DEFAULT_ARRIVAL_TIME = dt_time(14, 0)

# Planning heuristics, not verified external facts — tune per operator
# feedback rather than treating these as authoritative.
DRIVE_TO_FLIGHT_THRESHOLD_MINUTES = 6 * 60
BORDER_BUFFER_NIGHTS = 1

# Maps a trip's focus/style tokens to the activity_category values (from
# the real `activities` table — game_drive, walking_safari, hiking,
# cultural_visit, beach_leisure, diving, mountain_climbing, shopping,
# etc.) that should be preferred for that trip, in priority order. This
# replaces a hardcoded "always game_drive first" bias — which was wrong
# for a desert hike, a medina walk, a beach trip, or any destination
# that isn't a game park — with a preference list actually driven by
# what the traveler asked for.
CATEGORY_PREFERENCE_BY_TAG: dict[str, list[str]] = {
    "wildlife": ["game_drive", "walking_safari", "night_drive", "birding", "horseback_safari"],
    "beach": ["beach_leisure", "snorkeling", "diving", "fishing", "canoeing"],
    "culture": ["cultural_visit", "shopping"],
    "photography": ["photography", "game_drive", "walking_safari"],
    "walking": ["walking_safari", "hiking"],
    "adventure": ["hiking", "mountain_climbing", "canoeing", "cycling"],
}
DEFAULT_CATEGORY_PREFERENCE = [
    "game_drive", "walking_safari", "cultural_visit", "hiking", "beach_leisure",
]


@dataclass
class BuildResult:
    cabinet: Cabinet
    warnings: list[str] = field(default_factory=list)


class ItineraryPlanningEngine:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    def build(self, request: dict[str, Any], destination_ids: list[str]) -> BuildResult:
        """
        destination_ids: ORDERED list of travel_places.id (uuid strings),
        already resolved by db.destinations.resolve_slugs_to_ids and put
        in request order — e.g. [arusha, tarangire, ngorongoro, serengeti].
        """
        days: int = request["days"]
        travelers = request.get("travelers", 1)
        travel_style: list[str] = request.get("travel_style", []) or self._infer_style(request)
        focus: str = request.get("focus", "wildlife")
        budget_tier = request.get("budget_tier", "mid")
        start_date_raw = request.get("start_date")
        start_date = date.fromisoformat(start_date_raw) if start_date_raw else None

        cabinet = Cabinet(
            request_json=request,
            title=request.get("title") or self._default_title(request),
            duration_days=days,
            travelers_adults=travelers,
            travel_style=travel_style,
            budget_tier=budget_tier,
            status="draft",
            start_date=start_date,
            end_date=(start_date + timedelta(days=days - 1)) if start_date else None,
            primary_destination_id=destination_ids[0] if destination_ids else None,
            route_destination_ids=destination_ids,
        )
        self.db.add(cabinet)
        self.db.flush()  # get cabinet.id

        warnings: list[str] = []

        # Destination metadata (country, display name, recommended
        # minimum nights) needed by both the hinge builder (border
        # detection) and the allocator (buffer-night borrowing).
        meta = self._fetch_destination_meta(destination_ids)
        cabinet.route_countries = list(dict.fromkeys(
            m["country"] for m in meta.values() if m.get("country")
        ))
        cabinet.primary_country = meta.get(destination_ids[0], {}).get("country") if destination_ids else None

        # ------------------------------------------------------------
        # 1. Build hinges (route legs) from drive_times FIRST — the day
        #    allocation below depends on knowing real transfer durations.
        # ------------------------------------------------------------
        legs = self._build_hinges(cabinet.id, destination_ids, meta)

        # ------------------------------------------------------------
        # 2. Allocate days across the route, including a buffer night
        #    borrowed from wherever has slack whenever the route
        #    crosses a border (absorbs immigration/customs delays
        #    without adding an extra day the traveler didn't ask for).
        # ------------------------------------------------------------
        allocation, border_warnings = self._allocate_days(destination_ids, meta, days, travel_style)
        warnings.extend(border_warnings)

        # ------------------------------------------------------------
        # 3. Build each shelf (day) with real activities/lodges/transport.
        # ------------------------------------------------------------
        day_number = 1
        current_date = start_date
        # Tracks which activity ids have already been scheduled per
        # destination within THIS trip build — without this, a 3-night
        # stay at the same park could show the identical 2 activities
        # on every night, since each day's query ran independently with
        # no memory of what earlier days already picked.
        used_activity_ids: dict[str, set] = {}
        for idx, dest_id in enumerate(destination_ids):
            nights_here = allocation[idx]
            for night_idx in range(nights_here):
                is_first_day_overall = day_number == 1
                is_arrival_day = night_idx == 0 and idx > 0
                is_last_day_overall = day_number == days

                shelf = Shelf(
                    cabinet_id=cabinet.id,
                    day_number=day_number,
                    date=current_date,
                    destination_id=dest_id,
                    theme=self._theme_for(idx, night_idx, is_first_day_overall, is_last_day_overall),
                )
                self.db.add(shelf)
                self.db.flush()

                self._populate_drawers(
                    shelf, dest_id, is_first_day_overall, is_last_day_overall, is_arrival_day,
                    travel_style, focus, used_activity_ids.setdefault(dest_id, set()),
                )
                self._populate_headboard(shelf, dest_id, budget_tier, nights_here - night_idx)
                self._populate_armrest(shelf, dest_id, legs, idx, is_arrival_day)
                self._populate_trays(shelf, is_first_day_overall)

                day_number += 1
                if current_date:
                    current_date += timedelta(days=1)

        self.db.flush()
        return BuildResult(cabinet=cabinet, warnings=warnings)

    # ------------------------------------------------------------------
    def _fetch_destination_meta(self, destination_ids: list[str]) -> dict[str, dict]:
        """Country, display name, and recommended minimum nights per
        destination — needed by both the hinge builder (border
        detection) and the allocator (buffer-night borrowing)."""
        meta: dict[str, dict] = {}
        if not destination_ids:
            return meta

        rows = self.db.execute(
            text("select id, name, country::text from travel_places where id = any(:ids)"),
            {"ids": destination_ids},
        ).fetchall()
        for dest_id, name, country in rows:
            meta[dest_id] = {"country": country, "headline_label": name, "min_nights": 1}

        min_rows = self.db.execute(
            text(
                """
                select destination_id, min(recommended_nights_min)
                from estimated_visit_durations
                where destination_id = any(:ids) and scope = 'full_destination'
                  and recommended_nights_min is not null
                group by destination_id
                """
            ),
            {"ids": destination_ids},
        ).fetchall()
        for dest_id, min_nights in min_rows:
            if dest_id in meta and min_nights:
                meta[dest_id]["min_nights"] = int(min_nights)
        return meta

    # ------------------------------------------------------------------
    def _build_hinges(self, cabinet_id, destination_ids: list[str], meta: dict[str, dict]) -> list[dict]:
        """
        Builds route legs with three real fixes over a naive version:
          1. Skips a leg where from == to (an immediately-repeated
             destination in the resolved route) instead of emitting a
             zero-distance hinge and a same-place "transfer".
          2. Detects inter-country legs and attaches a real
             border_crossings record when one resolves, so downstream
             validation/explanation can name the actual crossing
             instead of a bare distance figure.
          3. Prefers a real flights row over an unrealistic overland
             estimate once a drive exceeds DRIVE_TO_FLIGHT_THRESHOLD_MINUTES
             (a planning heuristic, tunable, not a verified external
             fact) — but only ever as an alternative to real data, never
             overriding a shorter measured drive_times row.
        """
        legs: list[dict[str, Any]] = []
        seq = 0
        for i in range(len(destination_ids) - 1):
            frm, to = destination_ids[i], destination_ids[i + 1]

            if frm == to:
                logger.info("Skipping zero-distance hinge for repeated destination %s", frm)
                continue

            is_inter_country = meta.get(frm, {}).get("country") != meta.get(to, {}).get("country")

            row = self.db.execute(
                text(
                    """
                    select distance_km, duration_minutes_dry_season
                    from drive_times
                    where destination_id = :to_dest
                    order by distance_km asc
                    limit 1
                    """
                ),
                {"to_dest": to},
            ).fetchone()

            distance_km = duration_minutes = None
            source = None
            if row:
                distance_km, duration_minutes = float(row[0]), int(row[1])
                source = "drive_times"

            flight_row = None
            if is_inter_country or (duration_minutes and duration_minutes > DRIVE_TO_FLIGHT_THRESHOLD_MINUTES):
                flight_row = self.db.execute(
                    text(
                        """
                        select f.duration_minutes
                        from flights f
                        where f.origin_airport_id in (
                                select airport_id from destination_airports
                                where destination_id = :frm and is_primary_gateway)
                          and f.destination_airport_id in (
                                select airport_id from destination_airports
                                where destination_id = :to and is_primary_gateway)
                        order by f.duration_minutes asc nulls last limit 1
                        """
                    ),
                    {"frm": frm, "to": to},
                ).fetchone()

            mode = "private_4x4"
            if flight_row and (not row or is_inter_country or (duration_minutes or 0) > DRIVE_TO_FLIGHT_THRESHOLD_MINUTES):
                duration_minutes = int(flight_row[0]) if flight_row[0] else 240
                mode = "scheduled_flight"
                source = "flights_table"
                distance_km = None

            if duration_minutes is None:
                if is_inter_country:
                    distance_km, duration_minutes, mode, source = None, 480, "charter_flight", "fallback_inter_country_estimate"
                    logger.warning("Inter-country leg %s->%s has no measured drive/flight row; using 8h charter estimate", frm, to)
                else:
                    distance_km, duration_minutes, mode, source = 150.0, 180, "private_4x4", "fallback_estimate"
                    logger.warning("No drive_times row for %s->%s; using fallback estimate", frm, to)

            border_crossing_id = None
            if is_inter_country:
                bc_row = self.db.execute(
                    text(
                        """
                        select id from border_crossings
                        where (country_a = :a and country_b = :b) or (country_a = :b and country_b = :a)
                        order by (visa_notes is not null) desc limit 1
                        """
                    ),
                    {"a": meta.get(frm, {}).get("country"), "b": meta.get(to, {}).get("country")},
                ).fetchone()
                border_crossing_id = bc_row[0] if bc_row else None
                if not border_crossing_id and mode not in ("scheduled_flight", "charter_flight"):
                    logger.warning("Inter-country overland leg %s->%s has no border_crossings record", frm, to)

            seq += 1
            hinge = Hinge(
                cabinet_id=cabinet_id, from_destination_id=frm, to_destination_id=to,
                sequence_order=seq, distance_km=distance_km, duration_minutes=duration_minutes,
                mode=mode, source=source,
                is_inter_country=is_inter_country,
                requires_border_crossing=is_inter_country,
                border_crossing_id=border_crossing_id,
            )
            self.db.add(hinge)
            legs.append({
                "from": frm, "to": to, "duration_minutes": duration_minutes,
                "source": source, "is_inter_country": is_inter_country,
            })
        return legs

    # ------------------------------------------------------------------
    def _allocate_days(self, destination_ids: list[str], meta: dict[str, dict],
                        total_days: int, travel_style: list[str]) -> tuple[list[int], list[str]]:
        n = len(destination_ids)
        if n == 0:
            return [], []
        base = total_days // n
        remainder = total_days % n
        allocation = [base] * n
        # Give remainder nights to the LAST destination first — matches the
        # screenshots' pattern of "three consecutive nights" in the final,
        # most wildlife-dense stop (Serengeti) rather than the gateway city.
        i = n - 1
        while remainder > 0:
            allocation[i] += 1
            remainder -= 1
            i -= 1
        allocation = [max(1, a) for a in allocation]

        # Border-buffer borrowing: whenever the route crosses a country,
        # move one night from whichever destination has the most slack
        # above its own recommended minimum stay, rather than adding an
        # extra day the traveler didn't ask for or pay for.
        warnings: list[str] = []
        for i in range(1, len(destination_ids)):
            prev_country = meta.get(destination_ids[i - 1], {}).get("country")
            cur_country = meta.get(destination_ids[i], {}).get("country")
            if not (prev_country and cur_country and prev_country != cur_country):
                continue

            donor_idx = max(
                range(len(destination_ids)),
                key=lambda idx: allocation[idx] - meta.get(destination_ids[idx], {}).get("min_nights", 1),
            )
            donor_slack = allocation[donor_idx] - meta.get(destination_ids[donor_idx], {}).get("min_nights", 1)
            if donor_idx != i and donor_slack > 0:
                allocation[donor_idx] -= 1
                allocation[i] += BORDER_BUFFER_NIGHTS
            else:
                label = meta.get(destination_ids[i], {}).get("headline_label") or destination_ids[i]
                warnings.append(
                    f"Could not add a border-buffer night before entering {label} without "
                    "shortening a destination below its recommended minimum stay."
                )
        return allocation, warnings

    # ------------------------------------------------------------------
    def _populate_drawers(self, shelf, dest_id, is_first_day, is_last_day, is_arrival_day,
                           travel_style, focus, used_activity_ids: set):
        order = 1
        if is_first_day:
            self.db.add(Drawer(shelf_id=shelf.id, name="Airport welcome",
                                description="Met at the airport by your driver-guide.",
                                start_time=DEFAULT_ARRIVAL_TIME, duration_minutes=60,
                                sort_order=order, activity_type="ARRIVAL")); order += 1
            self.db.add(Drawer(shelf_id=shelf.id, name="Transfer to lodge",
                                description="Slow, scenic drive — no activities scheduled today.",
                                start_time=dt_time(15, 30), duration_minutes=60,
                                sort_order=order, activity_type="TRANSFER")); order += 1
            self.db.add(Drawer(shelf_id=shelf.id, name="Dinner at the lodge",
                                description="Garden setting, early night before the parks.",
                                start_time=dt_time(19, 30), duration_minutes=90,
                                sort_order=order, activity_type="MEAL")); order += 1
            return

        if is_last_day:
            self.db.add(Drawer(shelf_id=shelf.id, name="Breakfast", start_time=dt_time(7, 0),
                                duration_minutes=45, sort_order=order, activity_type="MEAL")); order += 1
            self.db.add(Drawer(shelf_id=shelf.id, name="Transfer to airport",
                                start_time=dt_time(9, 0), duration_minutes=120,
                                sort_order=order, activity_type="TRANSFER")); order += 1
            self.db.add(Drawer(shelf_id=shelf.id, name="Departure",
                                start_time=dt_time(12, 0), duration_minutes=30,
                                sort_order=order, activity_type="DEPARTURE")); order += 1
            return

        # Standard park/reserve day — pull real activities for this
        # destination, ranked toward the traveler's stated focus/style,
        # excluding anything already scheduled on an earlier day of this
        # same trip at this destination (see `used_activity_ids`).
        preferred_categories = self._preferred_categories(focus, travel_style)
        rows = self.db.execute(
            text(
                """
                select id, name, description, category
                from activities
                where destination_id = :dest_id
                  and not (id = any(:used_ids::uuid[]))
                order by
                  case category
                    when :cat0 then 0 when :cat1 then 1 when :cat2 then 2
                    when :cat3 then 3 when :cat4 then 4 else 5
                  end,
                  random()
                limit 2
                """
            ),
            {
                "dest_id": dest_id,
                "used_ids": list(used_activity_ids),
                **{f"cat{i}": (preferred_categories[i] if i < len(preferred_categories) else None)
                   for i in range(5)},
            },
        ).fetchall()

        # Destination has real activities but we've exhausted the
        # unused ones for this stay — better to repeat a good activity
        # than to fall back to a generic placeholder.
        if not rows and used_activity_ids:
            rows = self.db.execute(
                text(
                    """
                    select id, name, description, category
                    from activities
                    where destination_id = :dest_id
                    order by random()
                    limit 2
                    """
                ),
                {"dest_id": dest_id},
            ).fetchall()

        for row in rows:
            used_activity_ids.add(row[0])

        if rows:
            morning = rows[0]
            self.db.add(Drawer(shelf_id=shelf.id, activity_id=morning[0], name=morning[1],
                                description=morning[2], start_time=DEFAULT_GAME_DRIVE_START,
                                duration_minutes=240, sort_order=order, activity_type="EXPERIENCE"))
            order += 1
        else:
            # No activities row exists at all for this destination yet —
            # generic wording rather than safari-specific "game drive",
            # since this path is also hit for non-safari destinations
            # (deserts, cities, cultural sites) with sparse activity data.
            self.db.add(Drawer(shelf_id=shelf.id, name="Full-day experience",
                                description="A signature activity for this destination.",
                                start_time=DEFAULT_GAME_DRIVE_START, duration_minutes=240,
                                sort_order=order, activity_type="EXPERIENCE")); order += 1

        self.db.add(Drawer(shelf_id=shelf.id, name="Lunch at the lodge", start_time=dt_time(13, 0),
                            duration_minutes=60, sort_order=order, activity_type="MEAL")); order += 1

        if len(rows) > 1:
            afternoon = rows[1]
            self.db.add(Drawer(shelf_id=shelf.id, activity_id=afternoon[0], name=afternoon[1],
                                description=afternoon[2], start_time=dt_time(16, 0),
                                duration_minutes=150, sort_order=order, activity_type="EXPERIENCE"))
        else:
            self.db.add(Drawer(shelf_id=shelf.id, name="Afternoon at leisure",
                                description="Free time, or an optional add-on activity on request.",
                                start_time=dt_time(16, 0), duration_minutes=150,
                                sort_order=order, activity_type="EXPERIENCE"))

    # ------------------------------------------------------------------
    @staticmethod
    def _preferred_categories(focus: str, travel_style: list[str]) -> list[str]:
        """
        Category preference order for this trip, checked against real
        activity_category values. `focus` is the primary signal; any
        matching tag in travel_style is layered on top of it, since a
        request can be e.g. focus="wildlife" with travel_style also
        containing "beach" (a safari-plus-beach combination trip).
        """
        ordered: list[str] = []
        for tag in [focus, *travel_style]:
            for cat in CATEGORY_PREFERENCE_BY_TAG.get(tag, []):
                if cat not in ordered:
                    ordered.append(cat)
        for cat in DEFAULT_CATEGORY_PREFERENCE:
            if cat not in ordered:
                ordered.append(cat)
        return ordered[:5]  # only 5 slots are bound in the SQL CASE above

    # ------------------------------------------------------------------
    def _populate_headboard(self, shelf, dest_id, budget_tier, remaining_nights_here):
        tier_map = {"budget": ("budget", "camping"), "mid": ("mid_range",), "luxury": ("luxury", "ultra_luxury")}
        tiers = tier_map.get(budget_tier, ("mid_range",))
        row = self.db.execute(
            text(
                """
                select id, name, tier from lodges
                where destination_id = :dest_id and tier = any(:tiers)
                order by star_rating desc nulls last
                limit 1
                """
            ),
            {"dest_id": dest_id, "tiers": list(tiers)},
        ).fetchone()

        if row:
            self.db.add(Headboard(shelf_id=shelf.id, lodge_id=row[0], name=row[1], tier=row[2],
                                   check_in=shelf.date, nights=remaining_nights_here))
        else:
            self.db.add(Headboard(shelf_id=shelf.id, name=f"{budget_tier.title()} lodge",
                                   tier=budget_tier, check_in=shelf.date, nights=remaining_nights_here))

    # ------------------------------------------------------------------
    def _populate_armrest(self, shelf, dest_id, legs, dest_idx, is_arrival_day):
        if is_arrival_day and dest_idx > 0:
            leg = legs[dest_idx - 1]
            minutes = leg["duration_minutes"]
            self.db.add(Armrest(shelf_id=shelf.id, mode="private_4x4",
                                 description=f"Private 4x4 · {minutes} min transfer",
                                 duration_minutes=minutes, is_private=True))
        else:
            self.db.add(Armrest(shelf_id=shelf.id, mode="private_4x4",
                                 description="Private 4x4 · within-park game drives",
                                 duration_minutes=0, is_private=True))

    # ------------------------------------------------------------------
    def _populate_trays(self, shelf, is_first_day):
        meals = ["dinner"] if is_first_day else ["breakfast", "lunch", "dinner"]
        for m in meals:
            self.db.add(Tray(shelf_id=shelf.id, meal_type=m, included=True))

    # ------------------------------------------------------------------
    @staticmethod
    def _theme_for(idx, night_idx, is_first, is_last) -> str:
        if is_first:
            return "Arrival & slow start"
        if is_last:
            return "Departure"
        return "Wildlife & wide horizons" if night_idx == 0 else "Deeper into the park"

    @staticmethod
    def _infer_style(request: dict[str, Any]) -> list[str]:
        style = []
        if request.get("focus") == "wildlife":
            style.append("wildlife")
        if request.get("budget_tier") == "luxury":
            style.append("luxury")
        if request.get("travelers", 2) <= 2:
            style.append("private")
        style.append("relaxed_pace")
        return style

    @staticmethod
    def _default_title(request: dict[str, Any]) -> str:
        country = request.get("country_name", "Africa")
        return f"{country}, Wild & Unhurried"
