"""
Route Geography Engine
======================

Deterministic geographic truth layer for itinerary planning.

Responsibilities
----------------
- Resolve destinations from ``travel_places``.
- Resolve coordinates from ``physical_geography`` when available.
- Preserve the caller's exact destination order.
- Preserve non-consecutive repeats such as A -> B -> A.
- Build ordered route legs between consecutive destinations.
- Prefer factual transport data from the real database schema.
- Resolve scheduled flight options through destination gateways/airstrips.
- Detect country changes.
- Resolve border-crossing records only for factual overland legs.
- Distinguish measured facts from coordinate-derived estimates.
- Return ``unavailable`` when the database cannot establish a route fact.
- Provide structured route facts to downstream engines.

This engine does NOT:
- Generate itinerary days.
- Allocate nights.
- Decide which destinations should be removed.
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

ROUTE ORDER IS AUTHORITATIVE
----------------------------
The input sequence is preserved exactly.

Example:

    A -> B -> C

becomes:

    A -> B
    B -> C

And:

    A -> B -> A

becomes:

    A -> B
    B -> A

The engine removes only:
    - null/empty destination IDs
    - consecutive duplicate IDs

It never:
    - sorts
    - deduplicates globally
    - optimizes
    - shortens
    - substitutes destinations

SCHEMA RULE
-----------
The known ``drive_times`` table represents movement within a destination.
It is NOT treated as a destination-to-destination route table.

Therefore this engine deliberately does NOT query:

    drive_times

for:

    destination A -> destination B

unless a future authoritative destination-route source is explicitly
implemented inside ``_find_destination_drive_option``.

When no factual destination-to-destination road source exists, the result
is ``unavailable`` unless coordinate estimation was explicitly enabled.
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

# A measured domestic road duration at or above this threshold makes
# flight comparison worthwhile.
DRIVE_TO_FLIGHT_COMPARISON_MINUTES = 6 * 60

# Do not waste a flight lookup for very short measured domestic drives.
MINIMUM_DOMESTIC_FLIGHT_COMPARISON_MINUTES = 3 * 60

# Factual long-transfer warning threshold.
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
    """Resolved destination in exact caller-provided route order."""

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
    """Resolved border-crossing information."""

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
    estimate and must never be presented as measured geography.
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

    This engine supplies geographic facts to the orchestrator and planner.

    It does not decide whether a route is desirable or feasible.
    """

    name = "RouteGeographyEngine"
    version = "4.0"

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

        Route order is authoritative.

        No destination is removed, reordered, or substituted.

        No route duration is fabricated.
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

            # This should already be impossible after normalization.
            # Keep the guard so the route cannot silently acquire a
            # self-transition if this code changes later.
            if from_stop.destination_id == to_stop.destination_id:
                raise InvalidRouteError(
                    "Route contains a consecutive duplicate destination "
                    f"after normalization: {from_stop.destination_id}"
                )

            leg = self._build_leg(
                sequence=index + 1,
                from_stop=from_stop,
                to_stop=to_stop,
                allow_coordinate_estimate=allow_coordinate_estimate,
            )

            legs.append(leg)
            warnings.extend(leg.warnings)

        analysis = RouteAnalysis(
            stops=stops,
            legs=legs,
            countries=countries,
            international_legs=sum(
                1
                for leg in legs
                if leg.is_inter_country
            ),
            long_transfer_legs=sum(
                1
                for leg in legs
                if leg.long_transfer
            ),
            unavailable_legs=sum(
                1
                for leg in legs
                if leg.is_unavailable
            ),
            warnings=self._unique_strings(warnings),
        )

        self._assert_route_order(
            analysis,
            cleaned_ids,
        )

        unresolved_stops = [
            stop
            for stop in stops
            if not stop.resolved
        ]

        if unresolved_stops:
            analysis.warnings.append(
                "The following destination IDs could not be resolved "
                "in travel_places: "
                + ", ".join(
                    stop.destination_id
                    for stop in unresolved_stops
                )
            )

        return analysis

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

        ``travel_places`` provides the destination identity.

        ``physical_geography`` provides optional centroid coordinates.

        The LEFT JOIN intentionally preserves a destination even when
        physical geography is unavailable.
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
                    FROM travel_places AS tp
                    LEFT JOIN physical_geography AS pg
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
            destination_id = str(row.destination_id)

            result[destination_id] = {
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
        # Destination-to-destination road fact
        # ------------------------------------------------------------
        #
        # Currently no authoritative destination-route table exists
        # in the known schema. The adapter deliberately returns None.
        # ------------------------------------------------------------

        drive_option = self._find_destination_drive_option(
            from_stop.destination_id,
            to_stop.destination_id,
        )

        if drive_option is not None:
            alternatives.append(drive_option)

        # ------------------------------------------------------------
        # Flight fact
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
        # Select factual option
        # ------------------------------------------------------------

        selected = self._select_transport_option(
            drive_option=drive_option,
            flight_option=flight_option,
        )

        # ------------------------------------------------------------
        # Explicit coordinate estimate fallback
        # ------------------------------------------------------------

        if selected is None and allow_coordinate_estimate:
            selected = self._coordinate_estimate(
                from_stop,
                to_stop,
                is_inter_country=is_inter_country,
            )

            if selected.source == "coordinate_estimate":
                alternatives.append(selected)

                warnings.append(
                    f"{from_stop.destination_id} -> "
                    f"{to_stop.destination_id}: no measured transport "
                    "data was available; using an explicitly labelled "
                    "coordinate-derived estimate."
                )

        # ------------------------------------------------------------
        # Complete unavailable fallback
        # ------------------------------------------------------------

        if selected is None:
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
        # Destination resolution warning
        # ------------------------------------------------------------

        if not from_stop.resolved or not to_stop.resolved:
            warnings.append(
                f"{from_stop.destination_id} -> "
                f"{to_stop.destination_id}: one or both destinations "
                "could not be resolved in travel_places."
            )

        # ------------------------------------------------------------
        # Border semantics
        # ------------------------------------------------------------
        #
        # IMPORTANT:
        #
        # Unknown transport mode is NOT proof of overland travel.
        #
        # A border is required only when:
        # 1. countries differ
        # 2. the selected mode is explicitly overland
        #
        # Scheduled flight never requires a land border crossing.
        # Coordinate estimates are treated as estimated road-like
        # movement only for domestic routing. For international
        # coordinate estimates, the mode remains "unknown", so we do
        # not falsely claim a border crossing.
        # ------------------------------------------------------------

        requires_border_crossing = (
            is_inter_country
            and self._is_overland_mode(selected.mode)
        )

        border: BorderInfo | None = None

        if requires_border_crossing:
            border = self._resolve_border_crossing(
                from_country,
                to_country,
            )

            if border is None:
                warnings.append(
                    f"{from_stop.destination_id} -> "
                    f"{to_stop.destination_id}: no border-crossing "
                    "record was resolved for the factual overland "
                    "international leg."
                )

            elif not border.available:
                warnings.append(
                    f"{from_stop.destination_id} -> "
                    f"{to_stop.destination_id}: the resolved border "
                    f"crossing '{border.name}' is listed as "
                    f"'{border.status}'."
                )

        elif is_inter_country and selected.mode == "unknown":
            warnings.append(
                f"{from_stop.destination_id} -> "
                f"{to_stop.destination_id}: international transport "
                "mode is unknown; border-crossing requirement cannot "
                "be established from the available route facts."
            )

        # ------------------------------------------------------------
        # Long transfer
        # ------------------------------------------------------------

        long_transfer = (
            selected.duration_minutes is not None
            and selected.duration_minutes
            > LONG_TRANSFER_WARNING_MINUTES
        )

        if long_transfer:
            warnings.append(
                f"{from_stop.destination_id} -> "
                f"{to_stop.destination_id}: selected transport "
                f"duration is {selected.duration_minutes} minutes."
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
            warnings=tuple(
                self._unique_strings(warnings)
            ),
        )

    # ------------------------------------------------------------------
    # TRANSPORT SELECTION
    # ------------------------------------------------------------------

    @staticmethod
    def _select_transport_option(
        *,
        drive_option: TransportOption | None,
        flight_option: TransportOption | None,
    ) -> TransportOption | None:
        """
        Select a factual transport option.

        Selection rules:

        1. If only one factual option exists, use it.
        2. If both exist, choose the shorter factual duration.
        3. Never choose an estimate over a factual option here.

        This prevents the geography layer from automatically preferring
        flights merely because a flight record exists.
        """

        factual_options = [
            option
            for option in (
                drive_option,
                flight_option,
            )
            if option is not None
            and option.duration_minutes is not None
            and not option.estimated
        ]

        if not factual_options:
            return None

        return min(
            factual_options,
            key=lambda option: (
                option.duration_minutes,
                option.mode,
            ),
        )

    @staticmethod
    def _is_overland_mode(mode: str) -> bool:
        """
        Return True only for an explicitly overland transport mode.

        Unknown transport is deliberately NOT treated as overland.
        """

        normalized = (mode or "").strip().casefold()

        return normalized in {
            "drive",
            "road",
            "private_4x4",
            "vehicle",
            "overland",
            "road_transfer",
            "ground_transfer",
        }

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

        The currently known ``drive_times`` table describes movement
        within a destination and is therefore deliberately NOT queried.

        Until an authoritative destination-route source is introduced,
        this adapter returns None.

        Returning None means:

            "No factual destination-to-destination road fact exists
             in the configured source."

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
        Find the fastest factual scheduled flight connecting the
        destination gateways/airstrips.

        Supports:
            - primary gateway airports
            - destination airstrips

        The query does not fabricate airport-transfer durations.
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
                                FROM destination_airports AS da
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
                                FROM airstrips AS a
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
                                FROM destination_airports AS da
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
                                FROM airstrips AS a
                                WHERE a.destination_id =
                                      CAST(
                                          :to_destination_id
                                          AS uuid
                                      )
                            )
                        )
                        AND f.duration_minutes IS NOT NULL
                        AND f.duration_minutes > 0
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

        This method is used only when the caller explicitly enables
        coordinate estimation.

        It is never presented as measured road geography.
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

        duration_minutes = max(
            1,
            int(
                round(
                    (distance_km / ASSUMED_ROAD_SPEED_KMH)
                    * 60
                )
            ),
        )

        # For international movement, do NOT call the estimate a
        # private 4x4 route. There may be borders, flights, ferries,
        # inaccessible roads, etc.
        mode = (
            "private_4x4"
            if not is_inter_country
            else "unknown"
        )

        return TransportOption(
            mode=mode,
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
                    "border processing, traffic, ferries, flights, "
                    "or actual routing."
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

        distance = (
            2
            * EARTH_RADIUS_KM
            * math.asin(
                math.sqrt(haversine)
            )
        )

        if not math.isfinite(distance):
            return None

        return distance

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
            check when no measured road duration exists or when the
            measured road duration is long enough to justify comparison.
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
                            WHEN LOWER(CAST(status AS text)) = 'open'
                                THEN 0
                            WHEN status IS NULL
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

        normalized_status = (
            status.casefold()
            if status
            else None
        )

        return BorderInfo(
            border_crossing_id=(
                str(border_crossing_id)
                if border_crossing_id
                else None
            ),
            name=self._normalise_text(name),
            status=status,
            visa_notes=self._normalise_text(
                visa_notes
            ),
            available=(
                normalized_status
                not in {
                    "closed",
                    "restricted",
                }
            ),
            source="border_crossings",
        )

    # ------------------------------------------------------------------
    # ROUTE INTEGRITY
    # ------------------------------------------------------------------

    @staticmethod
    def _assert_route_order(
        analysis: RouteAnalysis,
        expected_destination_ids: Sequence[str],
    ) -> None:
        """
        Hard-check that geographic analysis preserved the requested route.

        This is deliberately exact.

        For:

            A -> B -> A

        the stops must remain:

            A -> B -> A

        A global set/membership comparison is insufficient.
        """

        actual = [
            stop.destination_id
            for stop in analysis.stops
        ]

        expected = list(expected_destination_ids)

        if actual != expected:
            raise InvalidRouteError(
                "Route geography changed the requested destination order. "
                f"Expected {expected}, got {actual}."
            )

        expected_leg_count = max(
            0,
            len(expected) - 1,
        )

        if len(analysis.legs) != expected_leg_count:
            raise InvalidRouteError(
                "Route geography produced an unexpected number of legs. "
                f"Expected {expected_leg_count}, "
                f"got {len(analysis.legs)}."
            )

        for index, leg in enumerate(analysis.legs):
            expected_from = expected[index]
            expected_to = expected[index + 1]

            actual_from = leg.from_stop.destination_id
            actual_to = leg.to_stop.destination_id

            if (
                actual_from != expected_from
                or actual_to != expected_to
            ):
                raise InvalidRouteError(
                    "Route geography produced an invalid leg sequence. "
                    f"Expected {expected_from} -> {expected_to}, "
                    f"got {actual_from} -> {actual_to}."
                )

            if leg.sequence != index + 1:
                raise InvalidRouteError(
                    "Route geography produced non-contiguous leg "
                    f"sequence at position {index + 1}."
                )

    # ------------------------------------------------------------------
    # NORMALIZATION HELPERS
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

        Preserves:
            A -> B -> A

        Does NOT:
            - sort
            - globally deduplicate
            - optimize
            - remove destinations
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
        """
        Return unique countries in first-appearance order.

        This is intentionally different from destination order.

        A -> B -> A where A and B are in different countries returns:

            [country(A), country(B)]

        while the stop sequence remains:

            [A, B, A]
        """

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
    def _unique_strings(
        values: Sequence[str],
    ) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        for value in values:
            if not value:
                continue

            normalized = str(value).strip()

            if not normalized or normalized in seen:
                continue

            seen.add(normalized)
            result.append(normalized)

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
        "raw": dict(option.raw),
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
        - downstream adapters
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
