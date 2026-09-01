"""
Route Geography Engine
=======================

Deterministic geographic truth layer for itinerary planning.

Canonical specification (per audit resolution):

    Doc B (SQLAlchemy/PostgreSQL integration baseline)
        +
    Doc A (GeoPoint / RouteStop / TransportOption / rich RouteLeg /
            confidence / provenance / warnings / serialization)
        +
    NO-FABRICATION RULE (overrides both)

Responsibilities
-----------------
- Resolve destinations into geographic facts (travel_places).
- Preserve the caller's requested route order. Never reorders.
- Build ordered route legs between consecutive stops.
- Prefer measured database data (drive_times, flights) over any estimate.
- Detect country changes and resolve border_crossings records.
- Distinguish MEASURED data from DEFENSIBLE ESTIMATES from UNKNOWN.
- Never return a fabricated distance/duration as if it were fact.
- Provide structured, serializable facts to downstream engines
  (day_archetype, activity_constraints, schedule_repair, itinerary
  planning, validation) without making itinerary decisions itself.

This engine does NOT:
- Generate itinerary days, activities, or prose.
- Reorder or optimize the requested route (A -> B -> C stays A -> B -> C).
- Invent a distance/duration when no defensible data exists.
- Create or persist Cabinet/Shelf/Drawer/Headboard/Armrest/Tray/Hinge
  ORM records. That remains ItineraryPlanningEngine's responsibility.

Non-negotiable rule: NO FABRICATED GEOGRAPHY
----------------------------------------------
If the database cannot establish a defensible route, this engine
returns:

    distance_km = None
    duration_minutes = None
    source = "unavailable"
    estimated = False

A generic "150km / 180min" placeholder is not a geographic fact and
must never be returned as one. A coordinate-derived great-circle
estimate MAY be returned, but only explicitly labeled:

    source = "coordinate_estimate"
    estimated = True

Downstream engines (starting with ItineraryPlanningEngine) are
responsible for deciding how to present an "unavailable" duration to
the traveler. That tolerance fix is tracked separately and is
intentionally out of scope for this file.

Route order is authoritative
------------------------------
The engine analyzes exactly the sequence it is given. If the caller
wants a different order, that is a separate planning decision made
before calling this engine, never inside it.
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

# A drive at or beyond this measured duration becomes a candidate for
# comparison against a scheduled flight. This is a *comparison trigger*,
# not a fabricated fact, so it is safe to keep as a constant.
DRIVE_TO_FLIGHT_COMPARISON_MINUTES = 6 * 60

# Below this measured drive duration, we do not bother querying flights
# for a domestic leg -- the drive is already short enough. International
# legs always trigger a flight lookup regardless of this threshold.
MINIMUM_DOMESTIC_FLIGHT_COMPARISON_MINUTES = 3 * 60

LONG_TRANSFER_WARNING_MINUTES = 4 * 60

EARTH_RADIUS_KM = 6371.0088

# Only used to label (never silently fabricate) a coordinate-derived
# estimate when explicitly requested via allow_coordinate_estimate=True.
ASSUMED_ROAD_SPEED_KMH = 45.0


# ============================================================================
# DATA CLASSES (Doc A domain model)
# ============================================================================

@dataclass(frozen=True)
class GeoPoint:
    """Geographic coordinate. Optional -- not every row has lat/lon."""

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
    """A resolved stop in the caller's requested route, in order."""

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
    """One known, factual transport option between two stops.

    Every field that is not measured/known is explicitly None -- this
    object never carries an invented value.
    """

    mode: str # "private_4x4" | "scheduled_flight" | "unknown"

    distance_km: float | None
    duration_minutes: int | None

    source: str
    # True only when duration/distance came from a labeled estimate
    # (e.g. coordinate-derived), never for a flat fallback constant.
    estimated: bool

    confidence: float # 0.0 - 1.0, deterministic function of source

    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteLeg:
    """Deterministic geographic/transport facts for one ordered leg."""

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
    """Complete deterministic route analysis, in requested order."""

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
    Deterministic route geography engine.

    Usage:

        engine = RouteGeographyEngine(db)
        analysis = engine.analyze(destination_ids=["a", "b", "c"])

        for leg in analysis.legs:
            if leg.is_unavailable:
                # leg.duration_minutes is None -- do not invent one.
                ...
    """

    name = "RouteGeographyEngine"
    version = "2.0"

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
        Analyze an ordered destination route.

        The input order is authoritative and is never reordered.

        Parameters
        ----------
        destination_ids:
            Ordered destination IDs, exactly as requested.
        allow_coordinate_estimate:
            If True, a leg with no measured drive/flight data may fall
            back to a great-circle coordinate estimate, explicitly
            labeled ``source="coordinate_estimate"``. If False (the
            default), an unmeasurable leg returns
            ``distance_km=None, duration_minutes=None,
            source="unavailable"`` rather than any numeric guess.
        """

        cleaned_ids = self._clean_destination_ids(destination_ids)

        if not cleaned_ids:
            return RouteAnalysis(
                warnings=["Route contains no stops."],
            )

        stops = self._resolve_stops(cleaned_ids)

        countries = self._ordered_countries(stops)

        legs: list[RouteLeg] = []
        warnings: list[str] = []

        for index in range(len(stops) - 1):
            from_stop = stops[index]
            to_stop = stops[index + 1]

            if from_stop.destination_id == to_stop.destination_id:
                logger.info(
                    "Skipping zero-distance leg %s -> %s (same "
                    "destination requested consecutively).",
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
            1 for leg in legs if leg.is_inter_country
        )

        long_transfer_legs = sum(
            1 for leg in legs if leg.long_transfer
        )

        unavailable_legs = sum(
            1 for leg in legs if leg.is_unavailable
        )

        unresolved_stops = [
            stop for stop in stops if not stop.resolved
        ]

        if unresolved_stops:
            warnings.append(
                "The following destination IDs could not be resolved "
                "in travel_places and were analyzed with no geographic "
                "facts: "
                + ", ".join(
                    stop.destination_id for stop in unresolved_stops
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

        by_id = self._fetch_destinations(destination_ids)

        stops: list[RouteStop] = []

        for index, destination_id in enumerate(destination_ids):
            record = by_id.get(destination_id)

            if record is None:
                logger.warning(
                    "Destination %s not found in travel_places.",
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
        Load geographic metadata from travel_places, joined against
        physical_geography for coordinates.

        FIX (real-schema alignment): travel_places itself has no
        latitude/longitude columns in the real Supabase schema --
        spatial data lives in the separate physical_geography table
        (one row per destination, PostGIS geography(Point, 4326)
        centroid column), joined here via destination_id. ST_Y/ST_X
        extract latitude/longitude from that centroid; both are NULL
        for a destination with no physical_geography row (a LEFT JOIN
        is used so a missing geography row degrades to unresolved
        coordinates rather than dropping the destination from results
        entirely -- name/country/destination_type are still usable
        even without a centroid).

        Uses CAST() rather than PostgreSQL's :: syntax so the query
        remains safe under SQLAlchemy bind-parameter processing, and
        passes destination_ids as a native Python list -- never a
        hand-built PostgreSQL array literal string.
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
                        CAST(tp.destination_type AS text) AS destination_type,
                        ST_Y(pg.centroid::geometry) AS latitude,
                        ST_X(pg.centroid::geometry) AS longitude
                    FROM travel_places tp
                    LEFT JOIN physical_geography pg
                        ON pg.destination_id = tp.id
                    WHERE tp.id = ANY(CAST(:destination_ids AS uuid[]))
                    """
                ),
                {"destination_ids": list(destination_ids)},
            ).fetchall()

        except Exception:
            logger.exception(
                "Failed to fetch destination metadata for %s",
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

        is_inter_country = bool(
            from_country
            and to_country
            and from_country.casefold() != to_country.casefold()
        )

        warnings: list[str] = []

        # ------------------------------------------------------------
        # Gather every factual option we can measure.
        # ------------------------------------------------------------

        alternatives: list[TransportOption] = []

        drive_option = self._find_drive_option(
            from_stop.destination_id,
            to_stop.destination_id,
        )

        if drive_option is not None:
            alternatives.append(drive_option)

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
        # Deterministically select the best factual option.
        #
        # Priority: a scheduled flight found via should_check_flight
        # is preferred whenever it exists (a real flight beats an
        # overland guess for a long/international leg). Otherwise the
        # measured drive is used. If neither exists, fall through to
        # an explicit "unavailable" or, only if the caller opted in,
        # a clearly labeled coordinate estimate.
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

            warnings.append(
                f"{from_stop.destination_id} -> {to_stop.destination_id}: "
                "no measured route data was available; using a "
                "coordinate-derived estimate explicitly labeled as such."
            )

        else:
            selected = TransportOption(
                mode="unknown",
                distance_km=None,
                duration_minutes=None,
                source="unavailable",
                estimated=False,
                confidence=0.0,
            )

            warnings.append(
                f"{from_stop.destination_id} -> {to_stop.destination_id}: "
                "no measured or estimated route data is available for "
                "this leg."
            )

        # ------------------------------------------------------------
        # Border crossing resolution (overland inter-country legs only;
        # a scheduled flight does not require a land border record).
        # ------------------------------------------------------------

        border: BorderInfo | None = None
        requires_border_crossing = False

        if is_inter_country and selected.mode != "scheduled_flight":
            requires_border_crossing = True
            border = self._resolve_border_crossing(
                from_country,
                to_country,
            )

            if border is None:
                warnings.append(
                    f"{from_stop.destination_id} -> {to_stop.destination_id}: "
                    "international overland leg has no resolved "
                    "border-crossing record. Entry requirements and "
                    "crossing status must be confirmed before booking."
                )
            elif not border.available:
                warnings.append(
                    f"{from_stop.destination_id} -> {to_stop.destination_id}: "
                    f"the {border.name} border crossing is currently "
                    f"listed as '{border.status}'. This route is not "
                    "currently viable as planned."
                )

        long_transfer = (
            selected.duration_minutes is not None
            and selected.duration_minutes > LONG_TRANSFER_WARNING_MINUTES
        )

        if long_transfer:
            warnings.append(
                f"{from_stop.destination_id} -> {to_stop.destination_id}: "
                f"transfer of {selected.duration_minutes} minutes exceeds "
                "the same-day comfort threshold."
            )

        if not from_stop.resolved or not to_stop.resolved:
            warnings.append(
                f"{from_stop.destination_id} -> {to_stop.destination_id}: "
                "one or both destinations could not be resolved in "
                "travel_places; country/geography facts for this leg "
                "may be incomplete."
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
    # DRIVE DATA (measured -- directed origin/destination pair)
    # ------------------------------------------------------------------

    def _find_drive_option(
        self,
        from_destination_id: str,
        to_destination_id: str,
    ) -> TransportOption | None:
        """
        Resolve a measured drive route for a specific, directed
        origin -> destination pair.

        Deliberately queries on BOTH from_destination_id and
        to_destination_id. A query that filters only on the
        destination side would return the closest drive-time row to
        the destination regardless of where the traveler is actually
        coming from, which is not a fact about this specific leg.
        """

        try:
            # FIX (real-schema alignment): queries
            # drive_times_between_destinations, not drive_times.
            # Supabase's real drive_times table models point-to-point
            # drives WITHIN one destination (gate -> lodge, by free-
            # text name); this method needs inter-destination transfer
            # data keyed by two travel_places UUIDs, which is a
            # different table. See missing_tables.sql.
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
                    ORDER BY
                        duration_minutes_dry_season ASC NULLS LAST,
                        distance_km ASC NULLS LAST
                    LIMIT 1
                    """
                ),
                {
                    "from_destination_id": from_destination_id,
                    "to_destination_id": to_destination_id,
                },
            ).fetchone()

        except Exception as exc:
            # Debug-level: some deployments may still be running an
            # older drive_times schema without directed columns. This
            # is not fatal -- it simply means no measured drive option
            # is available for this leg.
            logger.debug(
                "Directed drive_times lookup unavailable for %s -> %s: %s",
                from_destination_id,
                to_destination_id,
                exc,
            )
            return None

        if row is None:
            return None

        distance_km = self._safe_float(row[0])
        duration_minutes = self._safe_int(row[1])

        if duration_minutes is None:
            # A row with no usable duration is not a usable fact.
            return None

        return TransportOption(
            mode="private_4x4",
            distance_km=distance_km,
            duration_minutes=duration_minutes,
            source="drive_times_between_destinations",
            estimated=False,
            confidence=1.0,
            raw={"table": "drive_times_between_destinations"},
        )

    # ------------------------------------------------------------------
    # FLIGHT DATA (measured -- gateway airport pair)
    # ------------------------------------------------------------------

    def _find_flight_option(
        self,
        from_destination_id: str,
        to_destination_id: str,
    ) -> TransportOption | None:
        """
        Find the fastest measured scheduled flight connecting two
        destinations, via either a primary gateway airport or an
        airstrip.

        FIX (real-schema alignment): doc 18's flights table allows
        either an airport or an airstrip as origin/destination
        (mutually exclusive, enforced by a CHECK constraint) -- a bush
        flight from a remote airstrip is a normal safari transfer and
        was previously invisible here, which only checked
        origin_airport_id/destination_airport_id.
        """

        try:
            row = self.db.execute(
                text(
                    """
                    SELECT f.duration_minutes
                    FROM flights AS f
                    WHERE (
                        f.origin_airport_id IN (
                            SELECT da.airport_id
                            FROM destination_airports AS da
                            WHERE da.destination_id =
                                  CAST(:from_destination_id AS uuid)
                              AND da.is_primary_gateway = TRUE
                        )
                        OR f.origin_airstrip_id IN (
                            SELECT id FROM airstrips
                            WHERE destination_id =
                                  CAST(:from_destination_id AS uuid)
                        )
                    )
                    AND (
                        f.destination_airport_id IN (
                            SELECT da.airport_id
                            FROM destination_airports AS da
                            WHERE da.destination_id =
                                  CAST(:to_destination_id AS uuid)
                              AND da.is_primary_gateway = TRUE
                        )
                        OR f.destination_airstrip_id IN (
                            SELECT id FROM airstrips
                            WHERE destination_id =
                                  CAST(:to_destination_id AS uuid)
                        )
                    )
                    AND f.duration_minutes IS NOT NULL
                    ORDER BY f.duration_minutes ASC
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
            raw={"table": "flights"},
        )

    # ------------------------------------------------------------------
    # COORDINATE ESTIMATE (opt-in only, always explicitly labeled)
    # ------------------------------------------------------------------

    def _coordinate_estimate(
        self,
        from_stop: RouteStop,
        to_stop: RouteStop,
        *,
        is_inter_country: bool,
    ) -> TransportOption:
        """
        A great-circle distance/duration estimate, used ONLY when the
        caller explicitly opts in via allow_coordinate_estimate=True
        and no measured data exists. Always labeled source =
        "coordinate_estimate" and estimated=True -- this must never be
        confused with a measured database fact by any downstream
        consumer.
        """

        distance_km = self._haversine_km(
            from_stop.point,
            to_stop.point,
        )

        if distance_km is None:
            # Not even coordinates are available. Stay honest.
            return TransportOption(
                mode="unknown",
                distance_km=None,
                duration_minutes=None,
                source="unavailable",
                estimated=False,
                confidence=0.0,
            )

        duration_minutes = int(
            round((distance_km / ASSUMED_ROAD_SPEED_KMH) * 60)
        )

        return TransportOption(
            mode="private_4x4" if not is_inter_country else "unknown",
            distance_km=round(distance_km, 2),
            duration_minutes=duration_minutes,
            source="coordinate_estimate",
            estimated=True,
            # Coordinate estimates are inherently low-confidence: no
            # road network, terrain, or border-crossing time is
            # reflected in a straight-line calculation.
            confidence=0.25,
            raw={
                "method": "haversine",
                "assumed_speed_kmh": ASSUMED_ROAD_SPEED_KMH,
            },
        )

    @staticmethod
    def _haversine_km(a: GeoPoint, b: GeoPoint) -> float | None:
        if not a.available or not b.available:
            return None

        lat1 = math.radians(a.latitude)
        lat2 = math.radians(b.latitude)

        delta_lat = math.radians(b.latitude - a.latitude)
        delta_lon = math.radians(b.longitude - a.longitude)

        haversine = (
            math.sin(delta_lat / 2) ** 2
            + math.cos(lat1)
            * math.cos(lat2)
            * math.sin(delta_lon / 2) ** 2
        )

        return (
            2
            * EARTH_RADIUS_KM
            * math.asin(math.sqrt(max(0.0, min(1.0, haversine))))
        )

    # ------------------------------------------------------------------
    # FLIGHT-COMPARISON DECISION
    # ------------------------------------------------------------------

    @staticmethod
    def _should_compare_flight(
        is_inter_country: bool,
        drive_duration_minutes: int | None,
    ) -> bool:
        """
        Decide whether a flight lookup is worth performing.

        International legs are always checked -- a real scheduled
        flight is preferable to assuming an overland route exists.

        Domestic legs are checked only when either no drive data
        exists at all (so a flight might be the only measurable
        option) or the measured drive is long enough that a flight
        is a realistic alternative.
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
                    WHERE (
                        country_a::text = :country_a
                        AND country_b::text = :country_b
                    )
                    OR (
                        country_a::text = :country_b
                        AND country_b::text = :country_a
                    )
                    ORDER BY
                        CASE
                            WHEN CAST(status AS text) = 'open' THEN 0
                            WHEN CAST(status AS text) IS NULL THEN 1
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

        except Exception:
            logger.exception(
                "Failed to resolve border crossing %s -> %s",
                from_country,
                to_country,
            )
            return None

        if row is None:
            return None

        border_crossing_id, name, status, visa_notes = row

        status = self._normalise_text(status)

        return BorderInfo(
            border_crossing_id=(
                str(border_crossing_id) if border_crossing_id else None
            ),
            name=name,
            status=status,
            visa_notes=visa_notes,
            available=status not in ("closed", "restricted"),
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
        Normalize to strings, drop empties, collapse consecutive
        duplicates -- while preserving the caller's requested order.
        This never reorders; it only removes exact adjacent repeats
        (e.g. the same destination requested twice in a row).
        """

        if destination_ids is None:
            raise InvalidRouteError("destination_ids cannot be None.")

        result: list[str] = []

        for raw_id in destination_ids:
            if raw_id is None:
                continue

            destination_id = str(raw_id).strip()

            if not destination_id:
                continue

            if result and result[-1] == destination_id:
                continue

            result.append(destination_id)

        return result

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
    def _normalise_text(value: Any) -> str | None:
        if value is None:
            return None

        value = str(value).strip()

        return value or None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value is None:
            return None

        try:
            result = float(value)
        except (TypeError, ValueError):
            return None

        return result if math.isfinite(result) else None

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        if value is None:
            return None

        try:
            return int(value)
        except (TypeError, ValueError):
            return None


# ============================================================================
# SERIALIZATION
# ============================================================================

def _transport_option_to_dict(option: TransportOption) -> dict[str, Any]:
    return {
        "mode": option.mode,
        "distance_km": option.distance_km,
        "duration_minutes": option.duration_minutes,
        "source": option.source,
        "estimated": option.estimated,
        "confidence": option.confidence,
    }


def _border_info_to_dict(border: BorderInfo | None) -> dict[str, Any] | None:
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


def _stop_to_dict(stop: RouteStop) -> dict[str, Any]:
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


def _leg_to_dict(leg: RouteLeg) -> dict[str, Any]:
    return {
        "sequence": leg.sequence,
        "from_destination_id": leg.from_stop.destination_id,
        "to_destination_id": leg.to_stop.destination_id,
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
        "requires_border_crossing": leg.requires_border_crossing,
        "border_crossing": _border_info_to_dict(leg.border_crossing),
        "long_transfer": leg.long_transfer,
        "warnings": list(leg.warnings),
    }


def route_analysis_to_dict(analysis: RouteAnalysis) -> dict[str, Any]:
    """
    Convert a RouteAnalysis into JSON-safe primitives, suitable for
    generation logs, API responses, or debugging.
    """

    return {
        "engine": RouteGeographyEngine.name,
        "version": RouteGeographyEngine.version,
        "stops": [_stop_to_dict(stop) for stop in analysis.stops],
        "legs": [_leg_to_dict(leg) for leg in analysis.legs],
        "summary": {
            "stop_count": analysis.stop_count,
            "leg_count": analysis.leg_count,
            "countries": list(analysis.countries),
            "international_legs": analysis.international_legs,
            "long_transfer_legs": analysis.long_transfer_legs,
            "unavailable_legs": analysis.unavailable_legs,
            "has_cross_border_travel": analysis.has_cross_border_travel,
            "has_unavailable_data": analysis.has_unavailable_data,
            "total_known_distance_km": round(
                analysis.total_known_distance_km, 2
            ),
            "total_known_duration_minutes": (
                analysis.total_known_duration_minutes
            ),
        },
        "warnings": list(analysis.warnings),
        "generated_at": analysis.generated_at.isoformat(),
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
    Functional wrapper for callers that do not need to retain the
    engine instance.
    """

    return RouteGeographyEngine(db).analyze(
        destination_ids,
        allow_coordinate_estimate=allow_coordinate_estimate,
    )


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

