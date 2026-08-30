"""
ValidationEngine

Validates a persisted Cabinet against deterministic travel constraints.

Pipeline:

    GENERATE
        |
    VALIDATE
        |
    REPAIR
        |
    VALIDATE AGAIN
        |
    RETURN

No AI is used to determine whether an itinerary is physically viable.

CHANGE LOG (audit-confirmed fixes)
------------------------------------
1. Midnight-safe overlap detection.

   The previous implementation computed an activity's end time via
   ``(datetime.combine(...) + timedelta(...)).time()`` and then
   compared bare ``datetime.time`` objects. Discarding the date
   component after the addition silently loses any day rollover: a
   22:30 start + 180 minutes becomes "01:30", which then compares as
   LESS than a 22:30 start on the same nominal day, even though it is
   actually later, on the next calendar day. Overlap detection now
   keeps full ``datetime`` values (not bare ``time`` values) all the
   way through the comparison, so a cross-midnight activity is
   correctly ordered relative to the rest of the day's schedule.

2. Departure-day accommodation semantics.

   The previous implementation flagged EVERY shelf with no attached
   Headboard as an error, with no way to know that a departure day is
   a checkout day rather than an overnight stay. This engine now
   accepts an optional ``overnight_required`` lookup (day_number ->
   bool), sourced from DayArchetypeEngine's classification. When a
   day's archetype indicates overnight accommodation is not expected
   (e.g. DEPARTURE), a missing Headboard is no longer an error. When
   the archetype is unknown (the lookup is not supplied, or contains
   no entry for that day), the engine falls back to the previous
   conservative behavior of requiring accommodation -- it does not
   silently loosen validation just because the day-archetype pipeline
   has not been wired in yet for a given caller.
"""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models_furniture import (
    Cabinet,
    Footstool,
    Hinge,
    Shelf,
)


class ValidationEngine:
    LONG_TRANSFER_MINUTES = 240
    HARD_TRANSFER_MINUTES = 8 * 60

    def __init__(self, db: Session):
        self.db = db

    def validate(
        self,
        cabinet: Cabinet,
        extra_warnings: list[str] | None = None,
        *,
        overnight_required: Mapping[int, bool] | None = None,
    ) -> dict[str, Any]:
        """
        Parameters
        ----------
        cabinet:
            The persisted Cabinet to validate.
        extra_warnings:
            Warnings surfaced during upstream day allocation.
        overnight_required:
            Optional mapping of day_number -> bool, sourced from
            DayArchetypeEngine's classification (or equivalent). When a
            day is present in this mapping with value False, a missing
            Headboard for that day is not treated as an error -- e.g. a
            DEPARTURE day is a checkout day, not an overnight stay. Any
            day NOT present in this mapping keeps the previous
            conservative default of requiring accommodation, so callers
            that have not yet wired in day-archetype classification see
            no change in behavior.
        """

        issues: list[Footstool] = []

        for shelf in cabinet.shelves:
            issues += self._check_time_overlaps(cabinet, shelf)
            issues += self._check_accommodation_present(
                cabinet,
                shelf,
                overnight_required=overnight_required,
            )
            issues += self._check_fallback_activities(cabinet, shelf)

        issues += self._check_transfer_feasibility(cabinet)
        issues += self._check_border_crossings(cabinet)
        issues += self._check_route_integrity(cabinet)
        issues += self._check_allocation_warnings(cabinet, extra_warnings or [])

        for issue in issues:
            self.db.add(issue)
        self.db.flush()

        has_errors = any(i.severity == "error" for i in issues)
        has_warnings = any(i.severity == "warning" for i in issues)

        cabinet.status = "draft" if has_errors else "ready"
        self.db.add(cabinet)

        return {
            "status": "invalid" if has_errors else "valid",
            "issue_count": len(issues),
            "error_count": sum(1 for i in issues if i.severity == "error"),
            "warning_count": sum(1 for i in issues if i.severity == "warning"),
            "errors": [i.message for i in issues if i.severity == "error"],
            "warnings": [i.message for i in issues if i.severity == "warning"],
            "has_warnings": has_warnings,
        }

    # ------------------------------------------------------------------
    # TIME OVERLAPS -- midnight-safe
    # ------------------------------------------------------------------

    def _check_time_overlaps(self, cabinet: Cabinet, shelf: Shelf) -> list[Footstool]:
        issues: list[Footstool] = []

        timed = sorted(
            [
                d
                for d in shelf.drawers
                if d.start_time
                and d.duration_minutes is not None
                and d.duration_minutes > 0
            ],
            key=lambda d: (d.start_time, d.sort_order),
        )

        base_date = shelf.date or datetime.today().date()

        # Build full datetime start/end pairs up front, in the order
        # the drawers were scheduled within the day. We deliberately do
        # NOT sort by computed end time here -- sort_order/start_time
        # reflects the traveler's actual sequence through the day, and
        # that is what "does A overlap the thing immediately after it"
        # needs to walk.
        spans: list[tuple[Any, datetime, datetime]] = []

        for drawer in timed:
            start_dt = datetime.combine(base_date, drawer.start_time)
            end_dt = start_dt + timedelta(minutes=drawer.duration_minutes)
            spans.append((drawer, start_dt, end_dt))

        for (current, current_start, current_end), (following, following_start, _) in zip(
            spans, spans[1:]
        ):
            # If a later-in-sequence activity's start is technically
            # "earlier" in clock time only because IT rolled into the
            # next day too (e.g. two late-night activities both after
            # midnight), align it forward by a day so the comparison
            # still reflects real elapsed time rather than clock-face
            # time. This only fires when the current activity's END
            # has already crossed midnight relative to its own start.
            if current_end.date() > current_start.date() and following_start < current_start:
                following_start = following_start + timedelta(days=1)

            if current_end > following_start:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        shelf_id=shelf.id,
                        severity="error",
                        category="time",
                        message=(
                            f"Day {shelf.day_number}: '{current.name}' "
                            f"({current_start.strftime('%H:%M')}-"
                            f"{current_end.strftime('%H:%M')}"
                            f"{' next day' if current_end.date() > current_start.date() else ''}) "
                            f"overlaps with '{following.name}' "
                            f"({following_start.strftime('%H:%M')})."
                        ),
                    )
                )

        return issues

    # ------------------------------------------------------------------
    # ACCOMMODATION -- archetype-aware
    # ------------------------------------------------------------------

    def _check_accommodation_present(
        self,
        cabinet: Cabinet,
        shelf: Shelf,
        *,
        overnight_required: Mapping[int, bool] | None,
    ) -> list[Footstool]:

        if shelf.headboards:
            return []

        # A day explicitly marked as NOT requiring overnight
        # accommodation (e.g. DayArchetype.DEPARTURE) is not an error
        # just because it has no Headboard.
        if (
            overnight_required is not None
            and shelf.day_number in overnight_required
            and overnight_required[shelf.day_number] is False
        ):
            return []

        return [
            Footstool(
                cabinet_id=cabinet.id,
                shelf_id=shelf.id,
                severity="error",
                category="accommodation",
                message=f"Day {shelf.day_number}: no accommodation assigned for this overnight.",
            )
        ]

    # ------------------------------------------------------------------
    # FALLBACK ACTIVITY PROVENANCE
    # ------------------------------------------------------------------

    def _check_fallback_activities(self, cabinet: Cabinet, shelf: Shelf) -> list[Footstool]:
        issues: list[Footstool] = []

        for drawer in shelf.drawers:
            if not getattr(drawer, "is_fallback", False):
                continue

            if drawer.activity_id:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        shelf_id=shelf.id,
                        severity="error",
                        category="activity_provenance",
                        message=(
                            f"Day {shelf.day_number}: fallback activity "
                            f"'{drawer.name}' incorrectly contains a "
                            "seeded activity ID."
                        ),
                    )
                )
                continue

            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    shelf_id=shelf.id,
                    severity="warning",
                    category="activity_data",
                    message=(
                        f"Day {shelf.day_number}: '{drawer.name}' "
                        "is estimated fallback time, not a confirmed "
                        "booked activity."
                    ),
                )
            )

        return issues

    # ------------------------------------------------------------------
    # TRANSFER FEASIBILITY
    # ------------------------------------------------------------------

    def _check_transfer_feasibility(self, cabinet: Cabinet) -> list[Footstool]:
        issues: list[Footstool] = []

        for hinge in cabinet.hinges:
            duration = hinge.duration_minutes

            if duration is None:
                # route_geography.py may honestly report an unavailable
                # duration rather than fabricate one. That is not a
                # feasibility violation -- it's missing data -- so it
                # gets a warning, not an error, and does not go through
                # the numeric threshold checks below.
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        severity="warning",
                        category="transport",
                        message=(
                            "A transfer has no known duration. Route "
                            "data is unavailable for this leg and "
                            "should be confirmed before booking."
                        ),
                    )
                )
                continue

            if duration > self.HARD_TRANSFER_MINUTES:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        severity="error",
                        category="transport",
                        message=(
                            f"Transfer of {duration} minutes exceeds "
                            "the 8-hour hard limit and should be split "
                            "or replaced."
                        ),
                    )
                )
            elif duration > self.LONG_TRANSFER_MINUTES:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        severity="warning",
                        category="transport",
                        message=(
                            f"Transfer of {duration} minutes exceeds "
                            "the 4-hour same-day comfort threshold."
                        ),
                    )
                )

            if hinge.source in {
                "fallback_estimate",
                "fallback_inter_country_estimate",
                "coordinate_estimate",
            }:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        severity="warning",
                        category="transport",
                        message=(
                            "A transfer uses an estimate rather than "
                            "measured route data "
                            f"(source: {hinge.source})."
                        ),
                    )
                )

        return issues

    # ------------------------------------------------------------------
    # BORDER CROSSINGS
    # ------------------------------------------------------------------

    def _check_border_crossings(self, cabinet: Cabinet) -> list[Footstool]:
        issues: list[Footstool] = []

        for hinge in cabinet.hinges:
            if not getattr(hinge, "is_inter_country", False):
                continue

            border_crossing_id = getattr(hinge, "border_crossing_id", None)

            if not border_crossing_id:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        severity="warning",
                        category="border",
                        message=(
                            "This trip crosses an international border "
                            "but no matching border-crossing record was "
                            "found. Entry requirements and crossing "
                            "status must be confirmed before booking."
                        ),
                    )
                )
                continue

            row = self.db.execute(
                text(
                    """
                    SELECT name, CAST(status AS text), visa_notes
                    FROM border_crossings
                    WHERE id = CAST(:id AS uuid)
                    """
                ),
                {"id": border_crossing_id},
            ).fetchone()

            if not row:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        severity="warning",
                        category="border",
                        message=(
                            "The itinerary references a border-crossing "
                            "record that could not be loaded."
                        ),
                    )
                )
                continue

            name, status, visa_notes = row

            if status in {"closed", "restricted"}:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        severity="error",
                        category="border",
                        message=(
                            f"The {name} border crossing is currently "
                            f"listed as '{status}'. This route is not "
                            "currently viable as planned."
                        ),
                    )
                )
            elif status == "e_visa_required":
                message = f"Crossing at {name} requires an e-visa arranged in advance."
                if visa_notes:
                    message += f" {visa_notes}"
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        severity="warning",
                        category="border",
                        message=message,
                    )
                )

        return issues

    # ------------------------------------------------------------------
    # ROUTE INTEGRITY
    # ------------------------------------------------------------------

    def _check_route_integrity(self, cabinet: Cabinet) -> list[Footstool]:
        issues: list[Footstool] = []

        destination_ids = list(cabinet.route_destination_ids or [])

        if not destination_ids:
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    severity="error",
                    category="route",
                    message="Cabinet has no route destinations.",
                )
            )
            return issues

        shelf_destinations = [s.destination_id for s in cabinet.shelves if s.destination_id]

        if not shelf_destinations:
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    severity="error",
                    category="route",
                    message="Cabinet contains no destination-linked days.",
                )
            )
            return issues

        invalid_destinations = [d for d in shelf_destinations if d not in destination_ids]

        if invalid_destinations:
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    severity="error",
                    category="route",
                    message=(
                        "One or more itinerary days reference a "
                        "destination outside the cabinet route."
                    ),
                )
            )

        expected_days = cabinet.duration_days or 0

        if len(cabinet.shelves) != expected_days:
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    severity="error",
                    category="duration",
                    message=(
                        f"Cabinet declares {expected_days} days but "
                        f"contains {len(cabinet.shelves)} persisted shelves."
                    ),
                )
            )

        return issues

    # ------------------------------------------------------------------
    # ALLOCATION WARNINGS
    # ------------------------------------------------------------------

    def _check_allocation_warnings(
        self, cabinet: Cabinet, extra_warnings: list[str]
    ) -> list[Footstool]:
        return [
            Footstool(
                cabinet_id=cabinet.id,
                severity="warning",
                category="allocation",
                message=msg,
            )
            for msg in extra_warnings
            if msg
        ]

