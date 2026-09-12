"""
Route Geography Engine
======================

Deterministic geographic truth layer for itinerary planning.

Responsibilities
----------------
- Resolve destinations from ``travel_places``.
- Resolve coordinates from ``physical_geography`` when available.
- Preserve the caller's requested destination order.
- Build ordered route legs between consecutive destinations.
- Prefer factual transport data from the real database schema.
- Resolve scheduled flight options through destination gateways/airstrips.
- Detect country changes.
- Resolve border-crossing records for factual inter-country overland
  possibilities.
- Distinguish measured facts from coordinate-derived estimates.
- Return ``unavailable`` when the database cannot establish a route fact.
- Provide structured route facts to downstream engines.

This engine does NOT:
- Generate itinerary days.
- Allocate nights.
- Decide which destinations should be removed.
- Decide how many transit days the itinerary receives.
- Reorder destinations.
- Persist Cabinet/Shelf/Drawer/Hinge records.
- Invent road distances or road durations.

NO-FABRICATION RULE
-------------------
If no factual route duration/distance can be established:

    distance_km = None
    duration_minutes = None
    source = "unavailable"
    estimated = False

Coordinate-derived estimates are allowed only when the caller explicitly
passes:

    allow_coordinate_estimate=True

and are always returned as:

    source = "coordinate_estimate"
    estimated = True

IMPORTANT SCHEMA RULE
---------------------
The real ``drive_times`` table represents movement within a destination
and is not a directed destination-to-destination route table.

Therefore this engine MUST NOT query:

    drive_times_between_destinations

or pretend that ``drive_times`` can establish:

    destination A -> destination B

when the schema cannot support that fact.

For destination-to-destination routing, this engine uses:
- factual flight records when available;
- explicitly supplied coordinate estimates when enabled;
- otherwise ``unavailable``.

Route order is authoritative
----------------------------
The input sequence is preserved exactly:

    A -> B -> C

is analyzed as:

    A -> B
    B -> C

The engine never optimizes or reorders the route.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ============================================================================
# CONSTANTS
# ============================================================================

# Measured drive duration at which a domestic flight comparison becomes
# worthwhile. This is a decision threshold, not a fabricated travel fact.
DRIVE_TO_FLIGHT_COMPARISON_MINUTES = 6 * 60

# Domestic routes shorter than this do not require a flight comparison
# when a factual route duration is already known.
MINIMUM_DOMESTIC_FLIGHT_COMPARISON_MINUTES = 3 * 60

# Used only to label a factual long-transfer warning.
LONG_TRANSFER_WARNING_MINUTES = 4 * 60

# Coordinate-estimate constants.
EARTH_RADIUS_KM = 6371.0088
ASSUMED_ROAD_SPEED_KMH = 45.0


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass(frozen=True)
class GeoPoint:
    """Optional geographic coordinate for a destination."""

    latitude: float | None = None
    longitude: float | None = None

    @property
    def available(self) -> bool:
        return (
            self.latitude is not None
            and self.longitude is not None
            and math.isfinite(self.latitude)
            and math.isfinite(self.longitude)
        )


@dataclass(frozen=True)
class RouteStop:
    """Resolved destination in the exact caller-provided route order."""

    index: int
    destination_id: str
    name: str | None
    country: str | None
    destination_type: str | None
    point: GeoPoint
    resolved: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BorderInfo:
    """Resolved border-crossing information for an inter-country leg."""

    border_crossing_id: str | None
    name: str | None
    status: str | None
    visa_notes: str | None
    available: bool
    source: str


@dataclass(frozen=True)
class TransportOption:
    """
    One factual or explicitly labelled estimated transport option.

    ``estimated=False`` means the value came from an actual database fact.

    ``estimated=True`` means the value is deliberately labelled as an
    estimate and must never be treated as measured geography.
    """

    mode: str

    distance_km: float | None
    duration_minutes: int | None

    source: str
    estimated: bool

    confidence: float

    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteLeg:
    """Deterministic geographic facts for one ordered route leg."""

    sequence: int

    from_stop: RouteStop
    to_stop: RouteStop

    from_country: str | None
    to_country: str | None

    is_inter_country: bool

    selected: TransportOption
    alternatives: tuple[TransportOption, ...]

    border_crossing: BorderInfo | None
    requires_border_crossing: bool

    long_transfer: bool

    warnings: tuple[str, ...] = ()

    @property
    def distance_km(self) -> float | None:
        return self.selected.distance_km

    @property
    def duration_minutes(self) -> int | None:
        return self.selected.duration_minutes

    @property
    def mode(self) -> str:
        return self.selected.mode

    @property
    def source(self) -> str:
        return self.selected.source

    @property
    def estimated(self) -> bool:
        return self.selected.estimated

    @property
    def is_unavailable(self) -> bool:
        return self.selected.duration_minutes is None


@dataclass
class RouteAnalysis:
    """Complete deterministic route analysis in requested order."""

    stops: list[RouteStop] = field(default_factory=list)
    legs: list[RouteLeg] = field(default_factory=list)

    countries: list[str] = field(default_factory=list)

    international_legs: int = 0
    long_transfer_legs: int = 0
    unavailable_legs: int = 0

    warnings: list[str] = field(default_factory=list)

    generated_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def stop_count(self) -> int:
        return len(self.stops)

    @property
    def leg_count(self) -> int:
        return len(self.legs)

    @property
    def has_cross_border_travel(self) -> bool:
        return self.international_legs > 0

    @property
    def has_unavailable_data(self) -> bool:
        return self.unavailable_legs > 0

    @property
    def total_known_distance_km(self) -> float:
        return sum(
            leg.distance_km
            for leg in self.legs
            if leg.distance_km is not None
        )

    @property
    def total_known_duration_minutes(self) -> int:
        return sum(
            leg.duration_minutes
            for leg in self.legs
            if leg.duration_minutes is not None
        )


# ============================================================================
# EXCEPTIONS
# ============================================================================

class RouteGeographyError(Exception):
    """Base exception for route-geography failures."""


class InvalidRouteError(RouteGeographyError):
    """Raised when the supplied route is structurally invalid."""


# ============================================================================
# ENGINE
# ============================================================================

class RouteGeographyEngine:
    """
    Deterministic geographic truth layer.

    This engine supplies facts to the orchestrator and itinerary planner.

    Example:

        engine = RouteGeographyEngine(db)

        analysis = engine.analyze(
            destination_ids=[
                "tarangire-id",
                "ngorongoro-id",
                "serengeti-id",
            ]
        )

        for leg in analysis.legs:
            print(
                leg.from_stop.name,
                "->",
                leg.to_stop.name,
                leg.duration_minutes,
                leg.source,
            )
    """

    name = "RouteGeographyEngine"
    version = "3.0"

    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------

    def analyze(
        self,
        destination_ids: Sequence[str],
        *,
        allow_coordinate_estimate: bool = False,
    ) -> RouteAnalysis:
        """
        Analyze the caller's ordered destination sequence.

        No destination is reordered.

        No itinerary decision is made.

        No route duration is fabricated.

        Parameters
        ----------
        destination_ids:
            Ordered destination IDs.

        allow_coordinate_estimate:
            If True, a destination pair with no measured transport fact
            may receive a clearly labelled coordinate-derived estimate.

            Default is False.
        """

        cleaned_ids = self._clean_destination_ids(destination_ids)

        if not cleaned_ids:
            return RouteAnalysis(
                warnings=["Route contains no stops."]
            )

        stops = self._resolve_stops(cleaned_ids)

        countries = self._ordered_countries(stops)

        legs: list[RouteLeg] = []
        warnings: list[str] = []

        for index in range(len(stops) - 1):
            from_stop = stops[index]
            to_stop = stops[index + 1]

            # Consecutive duplicate destinations were already collapsed,
            # but this protects the engine if the method is changed later.
            if from_stop.destination_id == to_stop.destination_id:
                logger.info(
                    "Skipping duplicate route leg %s -> %s.",
                    from_stop.destination_id,
                    to_stop.destination_id,
                )
                continue

            leg = self._build_leg(
                sequence=len(legs) + 1,
                from_stop=from_stop,
                to_stop=to_stop,
                allow_coordinate_estimate=allow_coordinate_estimate,
            )

            legs.append(leg)
            warnings.extend(leg.warnings)

        international_legs = sum(
            1
            for leg in legs
            if leg.is_inter_country
        )

        long_transfer_legs = sum(
            1
            for leg in legs
            if leg.long_transfer
        )

        unavailable_legs = sum(
            1
            for leg in legs
            if leg.is_unavailable
        )

        unresolved_stops = [
            stop
            for stop in stops
            if not stop.resolved
        ]

        if unresolved_stops:
            warnings.append(
                "The following destination IDs could not be resolved "
                "in travel_places: "
                + ", ".join(
                    stop.destination_id
                    for stop in unresolved_stops
                )
            )

        return RouteAnalysis(
            stops=stops,
            legs=legs,
            countries=countries,
            international_legs=international_legs,
            long_transfer_legs=long_transfer_legs,
            unavailable_legs=unavailable_legs,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # STOP RESOLUTION
    # ------------------------------------------------------------------

    def _resolve_stops(
        self,
        destination_ids: list[str],
    ) -> list[RouteStop]:
        records = self._fetch_destinations(destination_ids)

        stops: list[RouteStop] = []

        for index, destination_id in enumerate(destination_ids):
            record = records.get(destination_id)

            if record is None:
                logger.warning(
                    "Destination %s was not found in travel_places.",
                    destination_id,
                )

                stops.append(
                    RouteStop(
                        index=index,
                        destination_id=destination_id,
                        name=None,
                        country=None,
                        destination_type=None,
                        point=GeoPoint(),
                        resolved=False,
                        raw={},
                    )
                )

                continue

            stops.append(
                RouteStop(
                    index=index,
                    destination_id=destination_id,
                    name=record["name"],
                    country=record["country"],
                    destination_type=record["destination_type"],
                    point=GeoPoint(
                        latitude=record["latitude"],
                        longitude=record["longitude"],
                    ),
                    resolved=True,
                    raw=record,
                )
            )

        return stops

    def _fetch_destinations(
        self,
        destination_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """
        Fetch destination metadata.

        ``travel_places`` supplies:
            - id
            - name
            - country
            - destination_type

        ``physical_geography`` supplies:
            - centroid

        The LEFT JOIN intentionally preserves a destination even when
        physical geography is missing.
        """

        if not destination_ids:
            return {}

        try:
            rows = self.db.execute(
                text(
                    """
                    SELECT
                        CAST(tp.id AS text) AS destination_id,
                        tp.name,
                        CAST(tp.country AS text) AS country,
                        CAST(tp.destination_type AS text)
                            AS destination_type,
                        ST_Y(pg.centroid::geometry) AS latitude,
                        ST_X(pg.centroid::geometry) AS longitude
                    FROM travel_places tp
                    LEFT JOIN physical_geography pg
                        ON pg.destination_id = tp.id
                    WHERE tp.id = ANY(
                        CAST(:destination_ids AS uuid[])
                    )
                    """
                ),
                {
                    "destination_ids": list(destination_ids),
                },
            ).fetchall()

        except Exception:
            logger.exception(
                "Failed to fetch destination metadata for %s.",
                destination_ids,
            )
            raise

        result: dict[str, dict[str, Any]] = {}

        for row in rows:
            result[str(row.destination_id)] = {
                "name": row.name,
                "country": self._normalise_text(row.country),
                "destination_type": self._normalise_text(
                    row.destination_type
                ),
                "latitude": self._safe_float(row.latitude),
                "longitude": self._safe_float(row.longitude),
            }

        return result

    # ------------------------------------------------------------------
    # LEG CONSTRUCTION
    # ------------------------------------------------------------------

    def _build_leg(
        self,
        sequence: int,
        from_stop: RouteStop,
        to_stop: RouteStop,
        allow_coordinate_estimate: bool,
    ) -> RouteLeg:

        from_country = from_stop.country
        to_country = to_stop.country

        is_inter_country = self._countries_differ(
            from_country,
            to_country,
        )

        warnings: list[str] = []

        alternatives: list[TransportOption] = []

        # ------------------------------------------------------------
        # Destination-to-destination road data
        # ------------------------------------------------------------
        #
        # IMPORTANT:
        #
        # The real drive_times table does not represent directed
        # travel_places -> travel_places routing.
        #
        # Therefore we intentionally do NOT query it here.
        #
        # If a future authoritative route table/API is introduced,
        # this is the correct place to add that adapter.
        # ------------------------------------------------------------

        drive_option = self._find_destination_drive_option(
            from_stop.destination_id,
            to_stop.destination_id,
        )

        if drive_option is not None:
            alternatives.append(drive_option)

        # ------------------------------------------------------------
        # Flight comparison
        # ------------------------------------------------------------

        should_check_flight = self._should_compare_flight(
            is_inter_country=is_inter_country,
            drive_duration_minutes=(
                drive_option.duration_minutes
                if drive_option is not None
                else None
            ),
        )

        flight_option: TransportOption | None = None

        if should_check_flight:
            flight_option = self._find_flight_option(
                from_stop.destination_id,
                to_stop.destination_id,
            )

            if flight_option is not None:
                alternatives.append(flight_option)

        # ------------------------------------------------------------
        # Select factual route option.
        # ------------------------------------------------------------

        selected: TransportOption

        if flight_option is not None:
            selected = flight_option

        elif drive_option is not None:
            selected = drive_option

        elif allow_coordinate_estimate:
            selected = self._coordinate_estimate(
                from_stop,
                to_stop,
                is_inter_country=is_inter_country,
            )

            alternatives.append(selected)

            if selected.source == "coordinate_estimate":
                warnings.append(
                    f"{from_stop.destination_id} -> "
                    f"{to_stop.destination_id}: no measured transport "
                    "data was available; using an explicitly labelled "
                    "coordinate-derived estimate."
                )
            else:
                warnings.append(
                    f"{from_stop.destination_id} -> "
                    f"{to_stop.destination_id}: no measured transport "
                    "data or usable coordinates are available."
                )

        else:
            selected = TransportOption(
                mode="unknown",
                distance_km=None,
                duration_minutes=None,
                source="unavailable",
                estimated=False,
                confidence=0.0,
                raw={},
            )

            warnings.append(
                f"{from_stop.destination_id} -> "
                f"{to_stop.destination_id}: no measured route data "
                "is available. Duration and distance remain unavailable."
            )

        # ------------------------------------------------------------
        # Border-crossing information
        # ------------------------------------------------------------
        #
        # A scheduled flight does not use a land-border crossing.
        #
        # If an overland route is selected and countries differ,
        # resolve the factual border record.
        # ------------------------------------------------------------

        border: BorderInfo | None = None
        requires_border_crossing = False

        if (
            is_inter_country
            and selected.mode != "scheduled_flight"
        ):
            requires_border_crossing = True

            border = self._resolve_border_crossing(
                from_country,
                to_country,
            )

            if border is None:
                warnings.append(
                    f"{from_stop.destination_id} -> "
                    f"{to_stop.destination_id}: no border-crossing "
                    "record was resolved for this international "
                    "overland leg. Confirm route viability and entry "
                    "requirements before booking."
                )

            elif not border.available:
                warnings.append(
                    f"{from_stop.destination_id} -> "
                    f"{to_stop.destination_id}: the resolved border "
                    f"crossing '{border.name}' is listed as "
                    f"'{border.status}'."
                )

        # ------------------------------------------------------------
        # Long-transfer flag
        # ------------------------------------------------------------

        long_transfer = (
            selected.duration_minutes is not None
            and selected.duration_minutes
            > LONG_TRANSFER_WARNING_MINUTES
        )

        if long_transfer:
            warnings.append(
                f"{from_stop.destination_id} -> "
                f"{to_stop.destination_id}: measured/selected "
                f"transfer duration is "
                f"{selected.duration_minutes} minutes."
            )

        # ------------------------------------------------------------
        # Resolution warning
        # ------------------------------------------------------------

        if not from_stop.resolved or not to_stop.resolved:
            warnings.append(
                f"{from_stop.destination_id} -> "
                f"{to_stop.destination_id}: one or both destinations "
                "could not be resolved in travel_places."
            )

        return RouteLeg(
            sequence=sequence,
            from_stop=from_stop,
            to_stop=to_stop,
            from_country=from_country,
            to_country=to_country,
            is_inter_country=is_inter_country,
            selected=selected,
            alternatives=tuple(alternatives),
            border_crossing=border,
            requires_border_crossing=requires_border_crossing,
            long_transfer=long_transfer,
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------------
    # DESTINATION-TO-DESTINATION DRIVE ADAPTER
    # ------------------------------------------------------------------

    def _find_destination_drive_option(
        self,
        from_destination_id: str,
        to_destination_id: str,
    ) -> TransportOption | None:
        """
        Resolve an authoritative destination-to-destination road fact.

        Current schema note
        -------------------
        The known ``drive_times`` table is for movement within a
        destination. It is therefore deliberately NOT queried here.

        There is currently no known authoritative table in the supplied
        schema that establishes:

            travel_place A -> travel_place B

        Consequently this method returns None.

        This explicit adapter exists so that a future authoritative
        destination-route source can be added without putting geography
        logic into ItineraryPlanningEngine.

        Returning None is intentional and means:

            "No factual destination-to-destination road route found."

        It does NOT mean:

            "The route is impossible."
        """

        logger.debug(
            "No authoritative destination-to-destination road source "
            "configured for %s -> %s.",
            from_destination_id,
            to_destination_id,
        )

        return None

    # ------------------------------------------------------------------
    # FLIGHT DATA
    # ------------------------------------------------------------------

    def _find_flight_option(
        self,
        from_destination_id: str,
        to_destination_id: str,
    ) -> TransportOption | None:
        """
        Find the fastest factual flight connecting the two destinations.

        Supports:
            - primary gateway airports
            - destination airstrips

        This follows the real flights schema where airport and airstrip
        origin/destination are mutually exclusive.
        """

        try:
            row = self.db.execute(
                text(
                    """
                    SELECT
                        f.duration_minutes
                    FROM flights AS f
                    WHERE
                        (
                            f.origin_airport_id IN (
                                SELECT da.airport_id
                                FROM destination_airports da
                                WHERE da.destination_id =
                                      CAST(
                                          :from_destination_id
                                          AS uuid
                                      )
                                  AND da.is_primary_gateway = TRUE
                            )
                            OR
                            f.origin_airstrip_id IN (
                                SELECT a.id
                                FROM airstrips a
                                WHERE a.destination_id =
                                      CAST(
                                          :from_destination_id
                                          AS uuid
                                      )
                            )
                        )
                        AND
                        (
                            f.destination_airport_id IN (
                                SELECT da.airport_id
                                FROM destination_airports da
                                WHERE da.destination_id =
                                      CAST(
                                          :to_destination_id
                                          AS uuid
                                      )
                                  AND da.is_primary_gateway = TRUE
                            )
                            OR
                            f.destination_airstrip_id IN (
                                SELECT a.id
                                FROM airstrips a
                                WHERE a.destination_id =
                                      CAST(
                                          :to_destination_id
                                          AS uuid
                                      )
                            )
                        )
                        AND f.duration_minutes IS NOT NULL
                    ORDER BY
                        f.duration_minutes ASC
                    LIMIT 1
                    """
                ),
                {
                    "from_destination_id": from_destination_id,
                    "to_destination_id": to_destination_id,
                },
            ).fetchone()

        except Exception as exc:
            logger.debug(
                "Flight lookup unavailable for %s -> %s: %s",
                from_destination_id,
                to_destination_id,
                exc,
            )
            return None

        if row is None:
            return None

        duration_minutes = self._safe_int(row[0])

        if duration_minutes is None or duration_minutes <= 0:
            return None

        return TransportOption(
            mode="scheduled_flight",
            distance_km=None,
            duration_minutes=duration_minutes,
            source="flights_table",
            estimated=False,
            confidence=1.0,
            raw={
                "table": "flights",
            },
        )

    # ------------------------------------------------------------------
    # COORDINATE ESTIMATE
    # ------------------------------------------------------------------

    def _coordinate_estimate(
        self,
        from_stop: RouteStop,
        to_stop: RouteStop,
        *,
        is_inter_country: bool,
    ) -> TransportOption:
        """
        Produce an explicitly labelled coordinate-derived estimate.

        This is NEVER returned unless the caller explicitly enables:

            allow_coordinate_estimate=True

        The value is not a measured road duration.
        """

        distance_km = self._haversine_km(
            from_stop.point,
            to_stop.point,
        )

        if distance_km is None:
            return TransportOption(
                mode="unknown",
                distance_km=None,
                duration_minutes=None,
                source="unavailable",
                estimated=False,
                confidence=0.0,
                raw={},
            )

        duration_minutes = int(
            round(
                (distance_km / ASSUMED_ROAD_SPEED_KMH)
                * 60
            )
        )

        return TransportOption(
            mode=(
                "private_4x4"
                if not is_inter_country
                else "unknown"
            ),
            distance_km=round(distance_km, 2),
            duration_minutes=duration_minutes,
            source="coordinate_estimate",
            estimated=True,
            confidence=0.25,
            raw={
                "method": "haversine",
                "assumed_speed_kmh": ASSUMED_ROAD_SPEED_KMH,
                "warning": (
                    "Not a measured road-network route. "
                    "Does not account for roads, terrain, gates, "
                    "border processing, traffic, or actual routing."
                ),
            },
        )

    @staticmethod
    def _haversine_km(
        a: GeoPoint,
        b: GeoPoint,
    ) -> float | None:
        if not a.available or not b.available:
            return None

        lat1 = math.radians(a.latitude)
        lat2 = math.radians(b.latitude)

        delta_lat = math.radians(
            b.latitude - a.latitude
        )

        delta_lon = math.radians(
            b.longitude - a.longitude
        )

        haversine = (
            math.sin(delta_lat / 2) ** 2
            + math.cos(lat1)
            * math.cos(lat2)
            * math.sin(delta_lon / 2) ** 2
        )

        haversine = max(
            0.0,
            min(1.0, haversine),
        )

        return (
            2
            * EARTH_RADIUS_KM
            * math.asin(
                math.sqrt(haversine)
            )
        )

    # ------------------------------------------------------------------
    # FLIGHT COMPARISON
    # ------------------------------------------------------------------

    @staticmethod
    def _should_compare_flight(
        is_inter_country: bool,
        drive_duration_minutes: int | None,
    ) -> bool:
        """
        Determine whether a flight lookup should be attempted.

        International:
            always check.

        Domestic:
            check if no factual drive duration exists or if the drive
            is sufficiently long to make air transport relevant.
        """

        if is_inter_country:
            return True

        if drive_duration_minutes is None:
            return True

        return (
            drive_duration_minutes
            >= MINIMUM_DOMESTIC_FLIGHT_COMPARISON_MINUTES
        )

    # ------------------------------------------------------------------
    # BORDER CROSSING
    # ------------------------------------------------------------------

    def _resolve_border_crossing(
        self,
        from_country: str | None,
        to_country: str | None,
    ) -> BorderInfo | None:

        if not from_country or not to_country:
            return None

        try:
            row = self.db.execute(
                text(
                    """
                    SELECT
                        CAST(id AS text),
                        name,
                        CAST(status AS text),
                        visa_notes
                    FROM border_crossings
                    WHERE
                        (
                            country_a::text = :country_a
                            AND country_b::text = :country_b
                        )
                        OR
                        (
                            country_a::text = :country_b
                            AND country_b::text = :country_a
                        )
                    ORDER BY
                        CASE
                            WHEN CAST(status AS text) = 'open'
                                THEN 0
                            WHEN CAST(status AS text) IS NULL
                                THEN 1
                            ELSE 2
                        END,
                        (visa_notes IS NOT NULL) DESC,
                        name ASC
                    LIMIT 1
                    """
                ),
                {
                    "country_a": from_country,
                    "country_b": to_country,
                },
            ).fetchone()

        except Exception as exc:
            logger.debug(
                "Border crossing lookup unavailable for %s -> %s: %s",
                from_country,
                to_country,
                exc,
            )
            return None

        if row is None:
            return None

        (
            border_crossing_id,
            name,
            status,
            visa_notes,
        ) = row

        status = self._normalise_text(status)

        return BorderInfo(
            border_crossing_id=(
                str(border_crossing_id)
                if border_crossing_id
                else None
            ),
            name=self._normalise_text(name),
            status=status,
            visa_notes=visa_notes,
            available=(
                status not in {
                    "closed",
                    "restricted",
                }
            ),
            source="border_crossings",
        )

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_destination_ids(
        destination_ids: Sequence[str],
    ) -> list[str]:
        """
        Normalize IDs without changing route order.

        Removes:
            - None
            - empty strings
            - consecutive duplicate IDs

        Does NOT:
            - sort
            - optimize
            - remove non-consecutive destinations
        """

        if destination_ids is None:
            raise InvalidRouteError(
                "destination_ids cannot be None."
            )

        result: list[str] = []

        for raw_id in destination_ids:
            if raw_id is None:
                continue

            destination_id = str(raw_id).strip()

            if not destination_id:
                continue

            if (
                result
                and result[-1] == destination_id
            ):
                continue

            result.append(destination_id)

        return result

    @staticmethod
    def _countries_differ(
        from_country: str | None,
        to_country: str | None,
    ) -> bool:
        if not from_country or not to_country:
            return False

        return (
            from_country.casefold()
            != to_country.casefold()
        )

    @staticmethod
    def _ordered_countries(
        stops: Sequence[RouteStop],
    ) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        for stop in stops:
            if not stop.country:
                continue

            key = stop.country.casefold()

            if key in seen:
                continue

            seen.add(key)
            result.append(stop.country)

        return result

    @staticmethod
    def _normalise_text(
        value: Any,
    ) -> str | None:
        if value is None:
            return None

        value = str(value).strip()

        return value or None

    @staticmethod
    def _safe_float(
        value: Any,
    ) -> float | None:
        if value is None:
            return None

        try:
            result = float(value)
        except (
            TypeError,
            ValueError,
        ):
            return None

        if not math.isfinite(result):
            return None

        return result

    @staticmethod
    def _safe_int(
        value: Any,
    ) -> int | None:
        if value is None:
            return None

        try:
            return int(value)
        except (
            TypeError,
            ValueError,
        ):
            return None


# ============================================================================
# SERIALIZATION
# ============================================================================

def _transport_option_to_dict(
    option: TransportOption,
) -> dict[str, Any]:
    return {
        "mode": option.mode,
        "distance_km": option.distance_km,
        "duration_minutes": option.duration_minutes,
        "source": option.source,
        "estimated": option.estimated,
        "confidence": option.confidence,
    }


def _border_info_to_dict(
    border: BorderInfo | None,
) -> dict[str, Any] | None:
    if border is None:
        return None

    return {
        "border_crossing_id": border.border_crossing_id,
        "name": border.name,
        "status": border.status,
        "visa_notes": border.visa_notes,
        "available": border.available,
        "source": border.source,
    }


def _stop_to_dict(
    stop: RouteStop,
) -> dict[str, Any]:
    return {
        "index": stop.index,
        "destination_id": stop.destination_id,
        "name": stop.name,
        "country": stop.country,
        "destination_type": stop.destination_type,
        "latitude": stop.point.latitude,
        "longitude": stop.point.longitude,
        "resolved": stop.resolved,
    }


def _leg_to_dict(
    leg: RouteLeg,
) -> dict[str, Any]:
    return {
        "sequence": leg.sequence,
        "from_destination_id": (
            leg.from_stop.destination_id
        ),
        "to_destination_id": (
            leg.to_stop.destination_id
        ),
        "from_country": leg.from_country,
        "to_country": leg.to_country,
        "is_inter_country": leg.is_inter_country,
        "mode": leg.mode,
        "distance_km": leg.distance_km,
        "duration_minutes": leg.duration_minutes,
        "source": leg.source,
        "estimated": leg.estimated,
        "is_unavailable": leg.is_unavailable,
        "confidence": leg.selected.confidence,
        "alternatives": [
            _transport_option_to_dict(option)
            for option in leg.alternatives
        ],
        "requires_border_crossing": (
            leg.requires_border_crossing
        ),
        "border_crossing": _border_info_to_dict(
            leg.border_crossing
        ),
        "long_transfer": leg.long_transfer,
        "warnings": list(leg.warnings),
    }


def route_analysis_to_dict(
    analysis: RouteAnalysis,
) -> dict[str, Any]:
    """
    Convert RouteAnalysis into JSON-safe primitives.

    Suitable for:
        - generation logs
        - API responses
        - debugging
        - downstream engine adapters
    """

    return {
        "engine": RouteGeographyEngine.name,
        "version": RouteGeographyEngine.version,
        "stops": [
            _stop_to_dict(stop)
            for stop in analysis.stops
        ],
        "legs": [
            _leg_to_dict(leg)
            for leg in analysis.legs
        ],
        "summary": {
            "stop_count": analysis.stop_count,
            "leg_count": analysis.leg_count,
            "countries": list(analysis.countries),
            "international_legs": (
                analysis.international_legs
            ),
            "long_transfer_legs": (
                analysis.long_transfer_legs
            ),
            "unavailable_legs": (
                analysis.unavailable_legs
            ),
            "has_cross_border_travel": (
                analysis.has_cross_border_travel
            ),
            "has_unavailable_data": (
                analysis.has_unavailable_data
            ),
            "total_known_distance_km": round(
                analysis.total_known_distance_km,
                2,
            ),
            "total_known_duration_minutes": (
                analysis.total_known_duration_minutes
            ),
        },
        "warnings": list(analysis.warnings),
        "generated_at": (
            analysis.generated_at.isoformat()
        ),
    }


# ============================================================================
# CONVENIENCE FUNCTION
# ============================================================================

def analyze_route(
    db: Session,
    destination_ids: Sequence[str],
    *,
    allow_coordinate_estimate: bool = False,
) -> RouteAnalysis:
    """
    Convenience wrapper around RouteGeographyEngine.
    """

    return RouteGeographyEngine(db).analyze(
        destination_ids,
        allow_coordinate_estimate=allow_coordinate_estimate,
    )


# ============================================================================
# PUBLIC EXPORTS
# ============================================================================

__all__ = [
    "GeoPoint",
    "RouteStop",
    "BorderInfo",
    "TransportOption",
    "RouteLeg",
    "RouteAnalysis",
    "RouteGeographyError",
    "InvalidRouteError",
    "RouteGeographyEngine",
    "route_analysis_to_dict",
    "analyze_route",
]
