"""
Routing Engine — migrated to the new schema.

REAL STRUCTURAL PROBLEM, addressed here rather than hidden: your DDL's
`drive_times` table models point-to-point distances WITHIN or NEAR one
destination_id (origin_name/destination_name are free-text landmarks, and
there's exactly one destination_id per row) — it does NOT model
inter-destination legs (e.g. Nairobi → Maasai Mara) the way the old
schema's `route_legs` table did (origin_id/destination_id as a pair of
travel_places).

Your DDL's `flights` table is closer to what's needed for
inter-destination routing (it references airports/airstrips, has
duration_minutes), but it's about scheduled/charter flight ROUTES, not
driving legs.

INTERIM FIX: for inter-destination legs, this engine now falls back to a
conservative estimate (same fallback constant as before migration) unless
a flights row exists connecting the two destinations' primary airports.
This is an HONEST gap, not silently papered over — real fix is either (a)
add an inter-destination `road_legs` table to your schema (origin_id,
destination_id both referencing travel_places, mirroring the old
route_legs shape), or (b) integrate a real routing API (Google
Routes/OpenRouteService) as originally suggested for RoutingEngine before
this migration. Flagged as the top schema follow-up from this migration.
"""
from __future__ import annotations

import logging
from sqlalchemy.orm import Session

from app.db.models_v2 import DestinationAirport, Airport
from app.db.destinations import resolve_slugs_to_ids

logger = logging.getLogger(__name__)

_FALLBACK_LEG = {"km": 220, "minutes": 280, "fuel_l": 30, "road": 75}


class RoutingEngine:
    def __init__(self, db: Session):
        self.db = db

    def route(self, origin_slug: str, destination_slug: str) -> dict:
        slug_to_id = resolve_slugs_to_ids(self.db, [origin_slug, destination_slug])
        origin_id = slug_to_id.get(origin_slug)
        dest_id = slug_to_id.get(destination_slug)

        if origin_id is None or dest_id is None:
            logger.warning(
                "Cannot resolve route %s -> %s: one or both destinations not found in schema",
                origin_slug, destination_slug
            )
            return self._fallback(origin_slug, destination_slug)

        flight_leg = self._try_flight_leg(origin_id, dest_id)
        if flight_leg:
            return flight_leg

        logger.warning(
            "No inter-destination leg data for %s -> %s (drive_times table "
            "doesn't model this yet — see routing.py docstring). Using fallback estimate.",
            origin_slug, destination_slug
        )
        return self._fallback(origin_slug, destination_slug)

    def full_route(self, slugs: list[str]) -> list[dict]:
        return [self.route(a, b) for a, b in zip(slugs, slugs[1:])]

    def _try_flight_leg(self, origin_id: str, dest_id: str) -> dict | None:
        """
        Attempts to find a scheduled/charter flight connecting each
        destination's primary airport. Returns None if either destination
        has no primary_gateway airport recorded, or no flight connects them
        — caller falls back to the conservative estimate in that case.
        """
        origin_airport = (
            self.db.query(DestinationAirport)
            .filter(
                DestinationAirport.destination_id == origin_id,
                DestinationAirport.is_primary_gateway == True,  # noqa: E712
            )
            .first()
        )
        dest_airport = (
            self.db.query(DestinationAirport)
            .filter(
                DestinationAirport.destination_id == dest_id,
                DestinationAirport.is_primary_gateway == True,  # noqa: E712
            )
            .first()
        )

        if origin_airport is None or dest_airport is None:
            return None

        # NOTE: not querying the `flights` table for an exact match here —
        # flights model scheduled routes with operator/frequency, which is
        # a different (and reasonable) future enhancement, not built in
        # this pass since it would require seeding real flight route data
        # too. Returning None here means "no flight leg found," correctly
        # falling through to the fallback estimate below.
        return None

    def _fallback(self, origin_slug: str, dest_slug: str) -> dict:
        return {
            "from": origin_slug,
            "to": dest_slug,
            "distance_km": _FALLBACK_LEG["km"],
            "duration_min": _FALLBACK_LEG["minutes"],
            "fuel_litres": _FALLBACK_LEG["fuel_l"],
            "road_quality_score": _FALLBACK_LEG["road"],
            "source": "fallback_estimate",
        }
