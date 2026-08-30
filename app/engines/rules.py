"""
Deterministic business-rule engine for itinerary generation.

This engine is intentionally:

- AI-free
- side-effect free
- database independent
- deterministic
- reusable by planning and validation layers

It does not create database rows.

CHANGE LOG (audit-confirmed fix)
---------------------------------
- _validate_dates() no longer compares start_date/end_date as raw
  strings (str(end_date) < str(start_date)). Lexicographic string
  comparison only happens to work for values that are already
  zero-padded ISO-8601 ("YYYY-MM-DD"); any other format (e.g.
  "10/05/2026") silently produces the wrong answer. Dates are now
  coerced to real date objects before comparison.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


class RulesEngine:
    MIN_TRIP_DAYS = 1
    MAX_PRACTICAL_DESTINATIONS_PER_DAY = 1

    LONG_TRANSFER_MINUTES = 240
    HARD_TRANSFER_MINUTES = 8 * 60

    VALID_BUDGET_TIERS = {
        "budget",
        "mid",
        "luxury",
    }

    VALID_TRANSPORT_MODES = {
        "private_4x4",
        "scheduled_flight",
        "charter_flight",
        "shared_transfer",
        "boat",
        "train",
    }

    def evaluate_rules(
        self,
        data: dict[str, Any],
    ) -> dict[str, Any]:

        errors: list[str] = []
        warnings: list[str] = []

        days = self._int(
            data.get("days"),
            default=0,
        )

        travelers = self._int(
            data.get("travelers"),
            default=1,
        )

        destinations = list(
            dict.fromkeys(
                data.get("destination_ids") or []
            )
        )

        budget_tier = str(
            data.get("budget_tier") or "mid"
        ).lower()

        if days < self.MIN_TRIP_DAYS:
            errors.append(
                "Trip must contain at least one day."
            )

        if travelers < 1:
            errors.append(
                "Trip must contain at least one traveler."
            )

        if not destinations:
            errors.append(
                "At least one destination is required."
            )

        if len(destinations) > days:
            warnings.append(
                "There are more requested destinations than trip days. "
                "The planner cannot give every destination a full overnight."
            )

        if budget_tier not in self.VALID_BUDGET_TIERS:
            warnings.append(
                f"Unknown budget tier '{budget_tier}'. "
                "The planner will use its mid-range fallback."
            )

        self._validate_dates(
            data,
            errors,
        )

        transfer_minutes = self._int(
            data.get("transfer_minutes"),
            default=0,
        )

        if transfer_minutes > self.HARD_TRANSFER_MINUTES:
            errors.append(
                "A single planned transfer exceeds 8 hours "
                "and should be split or replaced."
            )

        elif transfer_minutes > self.LONG_TRANSFER_MINUTES:
            warnings.append(
                "A transfer exceeds the 4-hour comfort threshold."
            )

        transport_mode = str(
            data.get("transport_mode") or ""
        ).lower()

        if (
            transport_mode
            and transport_mode not in self.VALID_TRANSPORT_MODES
        ):
            warnings.append(
                f"Transport mode '{transport_mode}' is not "
                "a recognized itinerary transport mode."
            )

        if (
            data.get("inter_country")
            and transport_mode
            not in {
                "scheduled_flight",
                "charter_flight",
            }
            and not data.get("border_crossing_id")
        ):
            warnings.append(
                "Inter-country overland travel has no resolved "
                "border-crossing record."
            )

        activity_count = self._int(
            data.get("seeded_activity_count"),
            default=0,
        )

        required_slots = max(
            0,
            days - 2,
        ) * 2

        if (
            activity_count < required_slots
            and required_slots
        ):
            warnings.append(
                "The activity dataset may be too sparse for the "
                "requested stay. The planner will use explicit "
                "fallback time rather than inventing bookings."
            )

        if data.get("activity_ids"):
            activity_ids = list(
                data["activity_ids"]
            )

            if len(activity_ids) != len(
                set(activity_ids)
            ):
                errors.append(
                    "Activity IDs must not be duplicated "
                    "within a destination stay."
                )

        if data.get("destination_countries"):
            countries = list(
                dict.fromkeys(
                    data["destination_countries"]
                )
            )

            if len(countries) > 1:
                warnings.append(
                    "This itinerary crosses countries and "
                    "requires border or flight feasibility checks."
                )

        return {
            "status": (
                "invalid"
                if errors
                else "success"
            ),
            "validated": not errors,
            "errors": errors,
            "warnings": warnings,
            "rules_applied": [
                "minimum_trip_duration",
                "traveler_count",
                "destination_count",
                "budget_tier",
                "date_order",
                "transfer_duration",
                "transport_mode",
                "international_border",
                "activity_data_sparsity",
                "activity_uniqueness",
                "country_route",
            ],
        }

    @staticmethod
    def _coerce_date(value: Any) -> date | None:
        """
        Coerce a value that may be a date, a datetime, or a string into
        a real ``date`` object.

        Returns None if the value cannot be safely interpreted as a
        date rather than silently guessing. A caller receiving None
        should treat the date as unknown, not as "earliest possible" or
        "latest possible" -- that would be a fabricated assumption of
        exactly the kind this codebase's design principles reject
        elsewhere (see route_geography.py's no-fabrication rule).
        """

        if value is None:
            return None

        if isinstance(value, datetime):
            return value.date()

        if isinstance(value, date):
            return value

        text_value = str(value).strip()

        if not text_value:
            return None

        # Try strict ISO-8601 first (the format every other engine in
        # this codebase already assumes for start_date/end_date).
        try:
            return date.fromisoformat(text_value)
        except ValueError:
            pass

        # Fall back to a small set of common explicit formats rather
        # than a permissive/ambiguous parser. Ambiguous formats like
        # "10/05/2026" are deliberately NOT guessed at (is that
        # October 5 or May 10?) -- an unrecognized format returns None
        # rather than silently picking an interpretation.
        for fmt in ("%Y/%m/%d", "%d-%m-%Y", "%Y%m%d"):
            try:
                return datetime.strptime(text_value, fmt).date()
            except ValueError:
                continue

        return None

    @classmethod
    def _validate_dates(
        cls,
        data: dict[str, Any],
        errors: list[str],
    ) -> None:

        start_date_raw = data.get("start_date")
        end_date_raw = data.get("end_date")

        if not start_date_raw or not end_date_raw:
            return

        start_date = cls._coerce_date(start_date_raw)
        end_date = cls._coerce_date(end_date_raw)

        if start_date is None or end_date is None:
            errors.append(
                "start_date and end_date must be valid dates "
                "(ISO-8601 'YYYY-MM-DD' is preferred)."
            )
            return

        if end_date < start_date:
            errors.append(
                "End date cannot be before start date."
            )

    @staticmethod
    def _int(
        value: Any,
        default: int = 0,
    ) -> int:

        try:
            return int(value)
        except (
            TypeError,
            ValueError,
        ):
            return default

