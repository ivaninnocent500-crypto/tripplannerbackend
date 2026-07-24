"""
Itinerary Engine — migrated to the new Supabase Travel Intelligence
schema. Public API (build()) and day-allocation algorithm are UNCHANGED
from the ati-production version — only the data access changed.

KEY DIFFERENCE FROM BEFORE: day_weight doesn't exist as a column in the
new schema. Derived from estimated_visit_durations' midpoint instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any
from sqlalchemy.orm import Session

from app.db.models_v2 import TravelPlace, Activity, Lodge, EstimatedVisitDuration
from app.db.destinations import resolve_slugs_to_ids, get_lat_lng
from app.engines.routing import RoutingEngine


@dataclass
class ActivityOut:
    time: str
    title: str
    icon: str = ""
    note: str = ""


@dataclass
class Day:
    day_number: int
    title: str
    region: str
    hero_image: str
    activities: list[ActivityOut] = field(default_factory=list)
    stay: str = ""
    stay_tier: str = ""
    drive_time_km: int = 0
    drive_time_min: int = 0
    latitude: float = 0.0
    longitude: float = 0.0


class ItineraryEngine:
    def __init__(self, db: Session):
        self.db = db
        self.routing = RoutingEngine(db)

    def build(self, request: dict[str, Any]) -> list[Day]:
        slugs: list[str] = request["destinations"]
        days_total = request["days"]
        max_lodge_min_child_age = request.get("max_lodge_min_child_age")

        slug_to_id = resolve_slugs_to_ids(self.db, slugs)
        ordered_ids = [slug_to_id[s] for s in slugs if s in slug_to_id]

        if not ordered_ids:
            return []

        destinations = {
            d.id: d for d in
            self.db.query(TravelPlace).filter(TravelPlace.id.in_(ordered_ids)).all()
        }
        ordered_destinations = [destinations[d_id] for d_id in ordered_ids if d_id in destinations]

        weighted = [(d, self._weight_for(d.id)) for d in ordered_destinations]
        total_weight = sum(w for _, w in weighted) or 1
        allocation: list[tuple[TravelPlace, int]] = []
        used = 0
        for i, (dest, weight) in enumerate(weighted):
            share = int(round(days_total * weight / total_weight))
            if i == len(weighted) - 1:
                share = days_total - used
            allocation.append((dest, max(share, 0)))
            used += share

        days: list[Day] = []
        day_no = 1

        for idx, (dest, count) in enumerate(allocation):
            lat_lng = get_lat_lng(self.db, dest.id)
            lat, lng = lat_lng if lat_lng else (0.0, 0.0)

            for k in range(count):
                is_arrival_day = idx == 0 and k == 0

                if idx > 0 and k == 0:
                    last_dest = allocation[idx - 1][0]
                    leg = self.routing.route(last_dest.id, dest.id)
                    transit_km, transit_min = leg["distance_km"], leg["duration_min"]
                else:
                    transit_km, transit_min = 0, 0

                activities = self._activities_for(dest.id, day_index=k, is_arrival=is_arrival_day)
                day_label = f"{dest.region or dest.name} Arrival" if is_arrival_day else (dest.region or dest.name)
                stay = self._select_lodge(dest.id, max_lodge_min_child_age)

                days.append(
                    Day(
                        day_number=day_no,
                        title=day_label,
                        region=dest.region or dest.name,
                        hero_image="",
                        activities=activities,
                        stay=stay,
                        stay_tier="",
                        drive_time_km=transit_km,
                        drive_time_min=transit_min,
                        latitude=lat,
                        longitude=lng,
                    )
                )
                day_no += 1

        return days

    def _weight_for(self, destination_id: str) -> float:
        row = (
            self.db.query(EstimatedVisitDuration)
            .filter(
                EstimatedVisitDuration.destination_id == destination_id,
                EstimatedVisitDuration.scope == "full_destination",
            )
            .first()
        )
        if row is None or row.recommended_nights_min is None:
            return 1.0
        min_n = row.recommended_nights_min
        max_n = row.recommended_nights_max or min_n
        return (min_n + max_n) / 2.0

    def _activities_for(self, destination_id: str, day_index: int, is_arrival: bool) -> list[ActivityOut]:
        rows = (
            self.db.query(Activity)
            .filter(Activity.destination_id == destination_id)
            .all()
        )
        if not rows:
            return []

        if is_arrival:
            selected = rows[:2]
        else:
            start = (day_index * 2) % max(len(rows), 1)
            selected = rows[start:start + 2] or rows[:2]

        return [
            ActivityOut(time="", title=a.name, note=a.description or "")
            for a in selected
        ]

    def _select_lodge(self, destination_id: str, max_lodge_min_child_age: int | None) -> str:
        candidates = (
            self.db.query(Lodge)
            .filter(Lodge.destination_id == destination_id)
            .all()
        )

        if max_lodge_min_child_age is not None:
            candidates = [c for c in candidates if c.is_family_friendly or max_lodge_min_child_age >= 12]

        if candidates:
            tier_rank = {"ultra_luxury": 4, "luxury": 3, "mid_range": 2, "budget": 1, "camping": 0}
            candidates.sort(key=lambda c: tier_rank.get(c.tier, 0), reverse=True)
            return candidates[0].name

        return "Accommodation TBD"

    @staticmethod
    def to_dict(days: list[Day]) -> list[dict]:
        return [asdict(d) for d in days]
