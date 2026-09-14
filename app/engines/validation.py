"""
ValidationEngine

Final deterministic integrity gate for a persisted Cabinet.

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

This engine does NOT decide whether an itinerary is desirable using AI.

Its responsibility is to verify that the persisted itinerary still
represents a physically and structurally coherent deterministic plan.

Validation layers:

1. Calendar / schedule integrity
2. Accommodation integrity
3. Activity provenance integrity
4. Transfer feasibility
5. Border integrity
6. Route identity and exact sequence
7. Shelf -> destination integrity
8. Drawer -> destination integrity
9. Hinge continuity
10. Transit-day integrity
11. Requested-route preservation where safely comparable
12. Exact requested duration
13. Allocation / upstream warnings

Important principles
--------------------
- A -> B -> A is a valid route and must NOT be collapsed into A -> B.
- Consecutive duplicate destination IDs are treated as one route position
  because they represent multiple days at the same destination, not a new
  geographic transition.
- Non-consecutive repeats are preserved and explicitly supported.
- Unknown transport duration is missing information, not fabricated
  feasibility.
- The validator reports integrity problems; it does not silently repair them.
- A departure day may legitimately have no accommodation when the caller
  explicitly supplies overnight_required[day] == False.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

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

    VALID_DAY_KINDS = {"STANDARD", "TRANSIT"}

    def __init__(self, db: Session):
        self.db = db

    # ==================================================================
    # PUBLIC API
    # ==================================================================

    def validate(
        self,
        cabinet: Cabinet,
        extra_warnings: list[str] | None = None,
        *,
        overnight_required: Mapping[int, bool] | None = None,
    ) -> dict[str, Any]:
        """
        Validate the persisted Cabinet.

        Parameters
        ----------
        cabinet:
            Persisted Cabinet to validate.

        extra_warnings:
            Warnings produced by upstream deterministic stages such as
            route feasibility and day allocation.

        overnight_required:
            Optional day_number -> bool lookup.

            False means that day does not require an overnight stay,
            e.g. a genuine departure day.

            If the mapping is absent, accommodation validation retains
            the conservative historical behavior and requires an
            accommodation record.
        """

        issues: list[Footstool] = []

        # --------------------------------------------------------------
        # Per-day validation
        # --------------------------------------------------------------

        for shelf in sorted(
            list(cabinet.shelves or []),
            key=lambda s: (
                s.day_number if s.day_number is not None else 10**9,
                str(s.id),
            ),
        ):
            issues.extend(
                self._check_time_overlaps(
                    cabinet,
                    shelf,
                )
            )

            issues.extend(
                self._check_accommodation_present(
                    cabinet,
                    shelf,
                    overnight_required=overnight_required,
                )
            )

            issues.extend(
                self._check_fallback_activities(
                    cabinet,
                    shelf,
                )
            )

            issues.extend(
                self._check_shelf_destination_integrity(
                    cabinet,
                    shelf,
                )
            )

            issues.extend(
                self._check_drawer_destination_integrity(
                    cabinet,
                    shelf,
                )
            )

            issues.extend(
                self._check_day_kind_integrity(
                    cabinet,
                    shelf,
                )
            )

        # --------------------------------------------------------------
        # Cross-day / route validation
        # --------------------------------------------------------------

        issues.extend(
            self._check_transfer_feasibility(cabinet)
        )

        issues.extend(
            self._check_border_crossings(cabinet)
        )

        issues.extend(
            self._check_route_integrity(cabinet)
        )

        issues.extend(
            self._check_hinge_integrity(cabinet)
        )

        issues.extend(
            self._check_destination_transition_integrity(cabinet)
        )

        issues.extend(
            self._check_transit_day_integrity(cabinet)
        )

        issues.extend(
            self._check_requested_route_preservation(cabinet)
        )

        issues.extend(
            self._check_allocation_warnings(
                cabinet,
                extra_warnings or [],
            )
        )

        # --------------------------------------------------------------
        # De-duplicate identical issues before persistence.
        #
        # Multiple integrity checks can legitimately discover the same
        # underlying problem. Persisting the exact same Footstool several
        # times makes the validation output noisy without adding evidence.
        # --------------------------------------------------------------

        issues = self._deduplicate_issues(issues)

        for issue in issues:
            self.db.add(issue)

        self.db.flush()

        has_errors = any(
            issue.severity == "error"
            for issue in issues
        )

        has_warnings = any(
            issue.severity == "warning"
            for issue in issues
        )

        cabinet.status = "draft" if has_errors else "ready"
        self.db.add(cabinet)

        return {
            "status": "invalid" if has_errors else "valid",
            "issue_count": len(issues),
            "error_count": sum(
                1 for issue in issues
                if issue.severity == "error"
            ),
            "warning_count": sum(
                1 for issue in issues
                if issue.severity == "warning"
            ),
            "errors": [
                issue.message
                for issue in issues
                if issue.severity == "error"
            ],
            "warnings": [
                issue.message
                for issue in issues
                if issue.severity == "warning"
            ],
            "has_warnings": has_warnings,
        }

    # ==================================================================
    # TIME OVERLAPS
    # ==================================================================

    def _check_time_overlaps(
        self,
        cabinet: Cabinet,
        shelf: Shelf,
    ) -> list[Footstool]:
        """
        Detect overlapping timed activities.

        Full datetime values are retained throughout the calculation.

        Schedule order is based primarily on sort_order. This is
        important because a schedule such as:

            23:00 -> 00:30

        is valid when 00:30 belongs to the following clock-day segment
        of the same itinerary day.

        Activities without both a start time and positive duration are
        not candidates for mathematical overlap validation.
        """

        issues: list[Footstool] = []

        timed = [
            drawer
            for drawer in (shelf.drawers or [])
            if drawer.start_time is not None
            and drawer.duration_minutes is not None
            and drawer.duration_minutes > 0
        ]

        timed.sort(
            key=lambda drawer: (
                drawer.sort_order
                if drawer.sort_order is not None
                else 10**9,
                drawer.start_time,
                str(drawer.id),
            )
        )

        base_date = shelf.date or datetime.today().date()

        previous_end: datetime | None = None
        previous_drawer: Any | None = None

        for drawer in timed:
            start_dt = datetime.combine(
                base_date,
                drawer.start_time,
            )

            # Move the current start forward when its clock time belongs
            # to the next calendar segment of the schedule.
            #
            # Example:
            # previous = 23:00 -> 01:00
            # current = 00:30
            #
            # The 00:30 activity is interpreted as next-day 00:30,
            # rather than earlier than the 23:00 activity.
            if previous_end is not None:
                while start_dt < previous_end:
                    candidate = start_dt + timedelta(days=1)

                    # Only roll forward when doing so actually moves
                    # the activity beyond the previous scheduled start.
                    # This protects against ordinary same-day ordering.
                    if candidate >= previous_end:
                        start_dt = candidate
                        break

                    start_dt = candidate

            end_dt = start_dt + timedelta(
                minutes=drawer.duration_minutes
            )

            if previous_end is not None and start_dt < previous_end:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        shelf_id=shelf.id,
                        severity="error",
                        category="time",
                        message=(
                            f"Day {shelf.day_number}: "
                            f"'{previous_drawer.name}' "
                            f"overlaps with '{drawer.name}'."
                        ),
                    )
                )

            previous_end = end_dt
            previous_drawer = drawer

        return issues

    # ==================================================================
    # ACCOMMODATION
    # ==================================================================

    def _check_accommodation_present(
        self,
        cabinet: Cabinet,
        shelf: Shelf,
        *,
        overnight_required: Mapping[int, bool] | None,
    ) -> list[Footstool]:

        if shelf.headboards:
            return []

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
                message=(
                    f"Day {shelf.day_number}: no accommodation "
                    "assigned for this overnight."
                ),
            )
        ]

    # ==================================================================
    # FALLBACK ACTIVITY PROVENANCE
    # ==================================================================

    def _check_fallback_activities(
        self,
        cabinet: Cabinet,
        shelf: Shelf,
    ) -> list[Footstool]:

        issues: list[Footstool] = []

        for drawer in shelf.drawers or []:
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

    # ==================================================================
    # SHELF DESTINATION INTEGRITY
    # ==================================================================

    def _check_shelf_destination_integrity(
        self,
        cabinet: Cabinet,
        shelf: Shelf,
    ) -> list[Footstool]:

        issues: list[Footstool] = []

        destination_id = getattr(
            shelf,
            "destination_id",
            None,
        )

        if not destination_id:
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    shelf_id=shelf.id,
                    severity="error",
                    category="route",
                    message=(
                        f"Day {shelf.day_number}: shelf has no "
                        "destination_id."
                    ),
                )
            )
            return issues

        route_ids = self._normalized_route_ids(cabinet)

        if route_ids and destination_id not in route_ids:
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    shelf_id=shelf.id,
                    severity="error",
                    category="route",
                    message=(
                        f"Day {shelf.day_number}: shelf references "
                        "a destination outside the cabinet route."
                    ),
                )
            )

        return issues

    # ==================================================================
    # DRAWER DESTINATION INTEGRITY
    # ==================================================================

    def _check_drawer_destination_integrity(
        self,
        cabinet: Cabinet,
        shelf: Shelf,
    ) -> list[Footstool]:

        issues: list[Footstool] = []

        shelf_destination = getattr(
            shelf,
            "destination_id",
            None,
        )

        if not shelf_destination:
            return issues

        for drawer in shelf.drawers or []:
            drawer_destination = getattr(
                drawer,
                "destination_id",
                None,
            )

            if not drawer_destination:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        shelf_id=shelf.id,
                        severity="error",
                        category="route",
                        message=(
                            f"Day {shelf.day_number}: activity "
                            f"'{drawer.name}' has no destination_id."
                        ),
                    )
                )
                continue

            # A Drawer belongs to the destination represented by its
            # Shelf. Transfer drawers are also required to use the
            # destination represented by that day; the Hinge carries
            # the actual from/to transition separately.
            if drawer_destination != shelf_destination:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        shelf_id=shelf.id,
                        severity="error",
                        category="route",
                        message=(
                            f"Day {shelf.day_number}: activity "
                            f"'{drawer.name}' references a destination "
                            "different from the day's shelf destination."
                        ),
                    )
                )

        return issues

    # ==================================================================
    # DAY KIND
    # ==================================================================

    def _check_day_kind_integrity(
        self,
        cabinet: Cabinet,
        shelf: Shelf,
    ) -> list[Footstool]:

        issues: list[Footstool] = []

        day_kind = getattr(
            shelf,
            "day_kind",
            None,
        )

        if day_kind is None:
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    shelf_id=shelf.id,
                    severity="error",
                    category="route",
                    message=(
                        f"Day {shelf.day_number}: day_kind is missing."
                    ),
                )
            )
            return issues

        normalized = str(day_kind).upper()

        if normalized not in self.VALID_DAY_KINDS:
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    shelf_id=shelf.id,
                    severity="error",
                    category="route",
                    message=(
                        f"Day {shelf.day_number}: unsupported day_kind "
                        f"'{day_kind}'. Expected STANDARD or TRANSIT."
                    ),
                )
            )

        return issues

    # ==================================================================
    # TRANSFER FEASIBILITY
    # ==================================================================

    def _check_transfer_feasibility(
        self,
        cabinet: Cabinet,
    ) -> list[Footstool]:

        issues: list[Footstool] = []

        for hinge in cabinet.hinges or []:
            duration = hinge.duration_minutes

            if duration is None:
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

            if duration < 0:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        severity="error",
                        category="transport",
                        message=(
                            f"Transfer of {duration} minutes has an "
                            "invalid negative duration."
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

            source = getattr(
                hinge,
                "source",
                None,
            )

            if source in {
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
                            f"(source: {source})."
                        ),
                    )
                )

        return issues

    # ==================================================================
    # BORDER CROSSINGS
    # ==================================================================

    def _check_border_crossings(
        self,
        cabinet: Cabinet,
    ) -> list[Footstool]:

        issues: list[Footstool] = []

        for hinge in cabinet.hinges or []:
            if not getattr(
                hinge,
                "is_inter_country",
                False,
            ):
                continue

            border_crossing_id = getattr(
                hinge,
                "border_crossing_id",
                None,
            )

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
                {
                    "id": border_crossing_id,
                },
            ).fetchone()

            if not row:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        severity="warning",
                        category="border",
                        message=(
                            "The itinerary references a "
                            "border-crossing record that could not "
                            "be loaded."
                        ),
                    )
                )
                continue

            name, status, visa_notes = row

            if status in {
                "closed",
                "restricted",
            }:
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
                message = (
                    f"Crossing at {name} requires an e-visa "
                    "arranged in advance."
                )

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

            elif status == "visa_on_arrival":
                message = (
                    f"Crossing at {name} offers visa on arrival."
                )

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

    # ==================================================================
    # ROUTE INTEGRITY
    # ==================================================================

    def _check_route_integrity(
        self,
        cabinet: Cabinet,
    ) -> list[Footstool]:

        issues: list[Footstool] = []

        route_ids = list(
            cabinet.route_destination_ids or []
        )

        if not route_ids:
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    severity="error",
                    category="route",
                    message="Cabinet has no route destinations.",
                )
            )
            return issues

        shelves = sorted(
            list(cabinet.shelves or []),
            key=lambda shelf: (
                shelf.day_number
                if shelf.day_number is not None
                else 10**9,
                str(shelf.id),
            ),
        )

        if not shelves:
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    severity="error",
                    category="route",
                    message="Cabinet contains no itinerary days.",
                )
            )
            return issues

        expected_days = cabinet.duration_days or 0

        if len(shelves) != expected_days:
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    severity="error",
                    category="duration",
                    message=(
                        f"Cabinet declares {expected_days} days but "
                        f"contains {len(shelves)} persisted shelves."
                    ),
                )
            )

        shelf_destinations = [
            shelf.destination_id
            for shelf in shelves
            if getattr(shelf, "destination_id", None)
        ]

        if not shelf_destinations:
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    severity="error",
                    category="route",
                    message=(
                        "Cabinet contains no destination-linked days."
                    ),
                )
            )
            return issues

        # --------------------------------------------------------------
        # Membership
        # --------------------------------------------------------------

        invalid_destinations = [
            destination_id
            for destination_id in shelf_destinations
            if destination_id not in route_ids
        ]

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

        # --------------------------------------------------------------
        # Exact geographic sequence.
        #
        # Consecutive duplicates represent multiple days in one place.
        # Non-consecutive repeats remain meaningful:
        #
        # A -> B -> A
        #
        # must remain:
        #
        # A -> B -> A
        #
        # and must never be reduced to A -> B.
        # --------------------------------------------------------------

        expected_sequence = self._compress_consecutive(route_ids)
        actual_sequence = self._compress_consecutive(
            shelf_destinations
        )

        if actual_sequence != expected_sequence:
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    severity="error",
                    category="route",
                    message=(
                        "Persisted itinerary destination sequence does "
                        "not match the cabinet route order. "
                        f"Expected {self._format_ids(expected_sequence)} "
                        f"but found {self._format_ids(actual_sequence)}."
                    ),
                )
            )

        # --------------------------------------------------------------
        # Warn about genuine non-consecutive repetition/backtracking.
        #
        # This is NOT an error. A -> B -> A can be intentional.
        # The validator only makes it visible rather than silently
        # pretending the route is linear.
        # --------------------------------------------------------------

        if self._has_non_consecutive_repeat(expected_sequence):
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    severity="warning",
                    category="route",
                    message=(
                        "The requested route contains a non-consecutive "
                        "destination repeat/backtrack. The route order "
                        "has been preserved exactly and should be "
                        "confirmed by the traveler before booking."
                    ),
                )
            )

        return issues

    # ==================================================================
    # HINGE / ROUTE-LEG INTEGRITY
    # ==================================================================

    def _check_hinge_integrity(
        self,
        cabinet: Cabinet,
    ) -> list[Footstool]:

        issues: list[Footstool] = []

        route = self._normalized_route_ids(cabinet)

        if len(route) < 2:
            if cabinet.hinges:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        severity="error",
                        category="route",
                        message=(
                            "Cabinet contains route legs even though "
                            "the route has fewer than two destinations."
                        ),
                    )
                )
            return issues

        hinges = sorted(
            list(cabinet.hinges or []),
            key=lambda hinge: (
                hinge.sequence_order
                if hinge.sequence_order is not None
                else 10**9,
                str(hinge.id),
            ),
        )

        expected_leg_count = len(route) - 1

        if len(hinges) != expected_leg_count:
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    severity="error",
                    category="route",
                    message=(
                        f"Route contains {len(hinges)} persisted "
                        f"transfer legs but {expected_leg_count} "
                        "are required for the ordered destination route."
                    ),
                )
            )

        comparable_count = min(
            len(hinges),
            expected_leg_count,
        )

        for index in range(comparable_count):
            hinge = hinges[index]

            expected_from = route[index]
            expected_to = route[index + 1]

            actual_from = getattr(
                hinge,
                "from_destination_id",
                None,
            )
            actual_to = getattr(
                hinge,
                "to_destination_id",
                None,
            )

            if actual_from != expected_from:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        severity="error",
                        category="route",
                        message=(
                            f"Route leg {index + 1} starts at the "
                            "wrong destination. "
                            f"Expected {expected_from}, "
                            f"found {actual_from}."
                        ),
                    )
                )

            if actual_to != expected_to:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        severity="error",
                        category="route",
                        message=(
                            f"Route leg {index + 1} ends at the "
                            "wrong destination. "
                            f"Expected {expected_to}, "
                            f"found {actual_to}."
                        ),
                    )
                )

            if index > 0:
                previous = hinges[index - 1]

                previous_to = getattr(
                    previous,
                    "to_destination_id",
                    None,
                )

                if actual_from != previous_to:
                    issues.append(
                        Footstool(
                            cabinet_id=cabinet.id,
                            severity="error",
                            category="route",
                            message=(
                                f"Route legs are discontinuous between "
                                f"legs {index} and {index + 1}. "
                                "The next leg does not start where the "
                                "previous leg ends."
                            ),
                        )
                    )

        # Sequence numbers themselves should describe an ordered route.
        sequence_values = [
            getattr(
                hinge,
                "sequence_order",
                None,
            )
            for hinge in hinges
        ]

        if any(
            value is None
            for value in sequence_values
        ):
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    severity="error",
                    category="route",
                    message=(
                        "One or more route legs have no "
                        "sequence_order."
                    ),
                )
            )
        elif sequence_values != list(
            range(1, len(sequence_values) + 1)
        ):
            issues.append(
                Footstool(
                    cabinet_id=cabinet.id,
                    severity="error",
                    category="route",
                    message=(
                        "Route leg sequence_order values are not "
                        "a continuous ordered sequence."
                    ),
                )
            )

        return issues

    # ==================================================================
    # DESTINATION TRANSITION INTEGRITY
    # ==================================================================

    def _check_destination_transition_integrity(
        self,
        cabinet: Cabinet,
    ) -> list[Footstool]:

        issues: list[Footstool] = []

        shelves = sorted(
            list(cabinet.shelves or []),
            key=lambda shelf: (
                shelf.day_number
                if shelf.day_number is not None
                else 10**9,
                str(shelf.id),
            ),
        )

        if len(shelves) < 2:
            return issues

        route = self._normalized_route_ids(cabinet)

        if len(route) < 2:
            return issues

        hinges = sorted(
            list(cabinet.hinges or []),
            key=lambda hinge: (
                hinge.sequence_order
                if hinge.sequence_order is not None
                else 10**9,
                str(hinge.id),
            ),
        )

        # Only compare geographic destination changes between calendar
        # days. Multiple consecutive days at the same destination are
        # normal and require no transfer.
        previous_destination = getattr(
            shelves[0],
            "destination_id",
            None,
        )

        hinge_by_pair = {
            (
                getattr(
                    hinge,
                    "from_destination_id",
                    None,
                ),
                getattr(
                    hinge,
                    "to_destination_id",
                    None,
                ),
            ): hinge
            for hinge in hinges
        }

        for shelf in shelves[1:]:
            current_destination = getattr(
                shelf,
                "destination_id",
                None,
            )

            if not previous_destination or not current_destination:
                previous_destination = current_destination
                continue

            if current_destination == previous_destination:
                previous_destination = current_destination
                continue

            if (
                previous_destination,
                current_destination,
            ) not in hinge_by_pair:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        shelf_id=shelf.id,
                        severity="error",
                        category="route",
                        message=(
                            f"Day {shelf.day_number}: itinerary moves "
                            f"from destination {previous_destination} "
                            f"to {current_destination}, but no matching "
                            "persisted route leg exists."
                        ),
                    )
                )

            previous_destination = current_destination

        return issues

    # ==================================================================
    # TRANSIT DAY INTEGRITY
    # ==================================================================

    def _check_transit_day_integrity(
        self,
        cabinet: Cabinet,
    ) -> list[Footstool]:

        issues: list[Footstool] = []

        shelves = sorted(
            list(cabinet.shelves or []),
            key=lambda shelf: (
                shelf.day_number
                if shelf.day_number is not None
                else 10**9,
                str(shelf.id),
            ),
        )

        hinges = sorted(
            list(cabinet.hinges or []),
            key=lambda hinge: (
                hinge.sequence_order
                if hinge.sequence_order is not None
                else 10**9,
                str(hinge.id),
            ),
        )

        if not shelves:
            return issues

        hinge_pairs = {
            (
                getattr(
                    hinge,
                    "from_destination_id",
                    None,
                ),
                getattr(
                    hinge,
                    "to_destination_id",
                    None,
                ),
            )
            for hinge in hinges
        }

        for index, shelf in enumerate(shelves):
            day_kind = str(
                getattr(
                    shelf,
                    "day_kind",
                    "",
                )
            ).upper()

            if day_kind != "TRANSIT":
                continue

            current_destination = getattr(
                shelf,
                "destination_id",
                None,
            )

            if not current_destination:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        shelf_id=shelf.id,
                        severity="error",
                        category="route",
                        message=(
                            f"Day {shelf.day_number}: TRANSIT day "
                            "has no destination_id."
                        ),
                    )
                )
                continue

            # A transit day represents an actual route transition.
            # It therefore needs an identifiable preceding destination
            # unless this is a special first-day construction. We do not
            # invent an airport/gateway destination here.
            if index == 0:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        shelf_id=shelf.id,
                        severity="warning",
                        category="route",
                        message=(
                            f"Day {shelf.day_number} is marked TRANSIT "
                            "but is the first persisted itinerary day. "
                            "Confirm that the day classification "
                            "represents the actual arrival/transition "
                            "semantics."
                        ),
                    )
                )
                continue

            previous_shelf = shelves[index - 1]

            previous_destination = getattr(
                previous_shelf,
                "destination_id",
                None,
            )

            if not previous_destination:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        shelf_id=shelf.id,
                        severity="error",
                        category="route",
                        message=(
                            f"Day {shelf.day_number}: TRANSIT day "
                            "follows a day without a destination."
                        ),
                    )
                )
                continue

            if previous_destination == current_destination:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        shelf_id=shelf.id,
                        severity="error",
                        category="route",
                        message=(
                            f"Day {shelf.day_number}: day is marked "
                            "TRANSIT but the previous and current "
                            "days have the same destination."
                        ),
                    )
                )
                continue

            if (
                previous_destination,
                current_destination,
            ) not in hinge_pairs:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        shelf_id=shelf.id,
                        severity="error",
                        category="route",
                        message=(
                            f"Day {shelf.day_number}: TRANSIT day "
                            "does not correspond to a persisted "
                            "route transition from the previous "
                            "destination to the current destination."
                        ),
                    )
                )

        return issues

    # ==================================================================
    # USER REQUESTED ROUTE PRESERVATION
    # ==================================================================

    def _check_requested_route_preservation(
        self,
        cabinet: Cabinet,
    ) -> list[Footstool]:

        issues: list[Footstool] = []

        request_json = getattr(
            cabinet,
            "request_json",
            None,
        )

        if not isinstance(request_json, Mapping):
            return issues

        requested = request_json.get("destinations")

        if not isinstance(requested, Sequence) or isinstance(
            requested,
            (str, bytes),
        ):
            return issues

        requested_ids = [
            value
            for value in requested
            if value not in (None, "")
        ]

        if not requested_ids:
            return issues

        final_route = list(
            cabinet.route_destination_ids or []
        )

        # Only perform a hard equality comparison when the identifiers
        # are directly comparable. This avoids falsely reporting a
        # mismatch when request_json stores destination slugs/names while
        # route_destination_ids stores UUIDs.
        if self._identifiers_are_comparable(
            requested_ids,
            final_route,
        ):
            requested_sequence = self._compress_consecutive(
                requested_ids
            )
            final_sequence = self._compress_consecutive(
                final_route
            )

            if requested_sequence != final_sequence:
                issues.append(
                    Footstool(
                        cabinet_id=cabinet.id,
                        severity="error",
                        category="route",
                        message=(
                            "The persisted final route does not preserve "
                            "the destination order requested by the user."
                        ),
                    )
                )

        return issues

    # ==================================================================
    # ALLOCATION WARNINGS
    # ==================================================================

    def _check_allocation_warnings(
        self,
        cabinet: Cabinet,
        extra_warnings: list[str],
    ) -> list[Footstool]:

        return [
            Footstool(
                cabinet_id=cabinet.id,
                severity="warning",
                category="allocation",
                message=message,
            )
            for message in extra_warnings
            if message
        ]

    # ==================================================================
    # HELPERS
    # ==================================================================

    @staticmethod
    def _compress_consecutive(
        values: Sequence[Any],
    ) -> list[Any]:
        """
        Remove only consecutive duplicates.

        IMPORTANT:

            A -> B -> A

        remains:

            A -> B -> A

        while:

            A -> A -> B

        becomes:

            A -> B
        """

        result: list[Any] = []

        for value in values:
            if value in (None, ""):
                continue

            if not result or result[-1] != value:
                result.append(value)

        return result

    @staticmethod
    def _normalized_route_ids(
        cabinet: Cabinet,
    ) -> list[Any]:
        return ValidationEngine._compress_consecutive(
            list(cabinet.route_destination_ids or [])
        )

    @staticmethod
    def _has_non_consecutive_repeat(
        values: Sequence[Any],
    ) -> bool:
        seen: set[Any] = set()

        for value in values:
            try:
                if value in seen:
                    return True
                seen.add(value)
            except TypeError:
                # UUID/string IDs should normally be hashable. If a
                # caller supplies an unusual unhashable representation,
                # simply skip the quality warning rather than failing
                # the entire validator.
                continue

        return False

    @staticmethod
    def _identifiers_are_comparable(
        requested: Sequence[Any],
        final: Sequence[Any],
    ) -> bool:
        """
        Determine whether request_json destination identifiers can be
        compared directly to route_destination_ids.

        We intentionally avoid assuming whether the request stores UUIDs,
        slugs, or names.
        """

        if not requested or not final:
            return False

        requested_types = {
            type(value)
            for value in requested
            if value not in (None, "")
        }

        final_types = {
            type(value)
            for value in final
            if value not in (None, "")
        }

        if requested_types == final_types:
            return True

        # UUID values can arrive as strings in request_json and as UUID
        # objects in SQLAlchemy models. Convert both to their textual form
        # only when the representations clearly look UUID-like.
        def looks_uuid(value: Any) -> bool:
            text_value = str(value)

            return (
                len(text_value) == 36
                and text_value.count("-") == 4
            )

        requested_uuid_like = all(
            looks_uuid(value)
            for value in requested
            if value not in (None, "")
        )

        final_uuid_like = all(
            looks_uuid(value)
            for value in final
            if value not in (None, "")
        )

        return requested_uuid_like and final_uuid_like

    @staticmethod
    def _format_ids(
        values: Sequence[Any],
    ) -> str:
        return "[" + ", ".join(
            str(value)
            for value in values
        ) + "]"

    @staticmethod
    def _deduplicate_issues(
        issues: list[Footstool],
    ) -> list[Footstool]:

        result: list[Footstool] = []
        seen: set[tuple[Any, ...]] = set()

        for issue in issues:
            key = (
                getattr(issue, "severity", None),
                getattr(issue, "category", None),
                getattr(issue, "shelf_id", None),
                getattr(issue, "message", None),
            )

            if key in seen:
                continue

            seen.add(key)
            result.append(issue)

        return result
