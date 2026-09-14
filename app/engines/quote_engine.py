"""
QuoteEngine
===========

Transactional marketplace layer for:

    1. requesting safari quotes
    2. tracking operator responses
    3. recording received quotes
    4. comparing received quotes

Design rules
------------
- Deterministic only. No AI participates in quote selection.
- Operators requested for quotes must belong to the Cabinet's persisted
  operator matches (`Stool`s).
- Duplicate requests for the same operator are not created.
- Quote recording is tied to the correct Bench/Cabinet.
- Existing ORM models are preserved; no new fields are invented.
- Missing quote information remains missing rather than fabricated.
- Database writes are transactional.
- Operator names are bulk-loaded to avoid N+1 queries.
- "Best value" means the lowest known price.
- "Best fit" is derived from the persisted OperatorMatchEngine score,
  not from quote insertion order.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models_furniture import (
    Bench,
    Cabinet,
    Counter,
    Mirror,
    Stool,
)


class QuoteEngine:
    def __init__(self, db: Session):
        self.db = db

    # ==================================================================
    # OPERATOR LOOKUPS
    # ==================================================================

    def _operator_name(self, operator_id) -> str | None:
        """
        Compatibility helper for callers that need one operator name.

        Main tracking/compare paths use the bulk version below to avoid
        N+1 queries.
        """

        row = self.db.execute(
            text(
                """
                select name
                from tour_operators
                where id = :id
                """
            ),
            {"id": operator_id},
        ).fetchone()

        return row[0] if row else None

    def _operator_names(
        self,
        operator_ids: list[Any],
    ) -> dict[str, str]:
        if not operator_ids:
            return {}

        rows = self.db.execute(
            text(
                """
                select id::text, name
                from tour_operators
                where id::text = any(:ids)
                """
            ),
            {
                "ids": [str(operator_id) for operator_id in operator_ids]
            },
        ).fetchall()

        return {
            str(row[0]): row[1]
            for row in rows
            if row[0] is not None
        }

    # ==================================================================
    # MATCHED OPERATOR VALIDATION
    # ==================================================================

    def _matched_operator_ids(
        self,
        cabinet: Cabinet,
    ) -> set[str]:
        """
        Return operators actually matched to this Cabinet.

        Quote requests are intentionally restricted to persisted Stool
        matches. This prevents the API from bypassing OperatorMatchEngine
        by submitting arbitrary operator IDs.
        """

        rows = self.db.execute(
            text(
                """
                select distinct tour_operator_id::text
                from stools
                where cabinet_id = :cabinet_id
                """
            ),
            {"cabinet_id": cabinet.id},
        ).fetchall()

        return {
            str(row[0])
            for row in rows
            if row[0] is not None
        }

    # ==================================================================
    # REQUEST QUOTES
    # ==================================================================

    def request_quotes(
        self,
        cabinet: Cabinet,
        tour_operator_ids: list[str],
        note: str | None,
    ) -> list[Bench]:
        """
        Create quote requests for matched operators.

        Existing requests for the same Cabinet/operator are reused rather
        than duplicated.

        The method commits the transaction because this operation represents
        a completed marketplace state change.
        """

        if not tour_operator_ids:
            raise ValueError(
                "At least one tour operator must be selected for quotes."
            )

        if cabinet.id is None:
            raise ValueError(
                "Cannot request quotes for an unsaved Cabinet."
            )

        # Normalize while preserving caller order.
        requested_ids = self._unique_ids(tour_operator_ids)

        matched_ids = self._matched_operator_ids(cabinet)

        invalid_ids = [
            operator_id
            for operator_id in requested_ids
            if operator_id not in matched_ids
        ]

        if invalid_ids:
            raise ValueError(
                "One or more requested operators are not matched to this "
                f"cabinet: {', '.join(invalid_ids)}"
            )

        try:
            existing_rows = self.db.execute(
                text(
                    """
                    select id::text, tour_operator_id::text, status
                    from benches
                    where cabinet_id = :cabinet_id
                      and tour_operator_id::text = any(:operator_ids)
                    """
                ),
                {
                    "cabinet_id": cabinet.id,
                    "operator_ids": requested_ids,
                },
            ).fetchall()

            existing_by_operator = {
                str(row[1]): {
                    "id": str(row[0]),
                    "status": row[2],
                }
                for row in existing_rows
            }

            benches: list[Bench] = []

            for operator_id in requested_ids:
                existing = existing_by_operator.get(operator_id)

                if existing:
                    # Do not resend a request merely because the UI was
                    # tapped twice. Existing quote state is authoritative.
                    bench = (
                        self.db.query(Bench)
                        .filter(Bench.id == existing["id"])
                        .one()
                    )
                    benches.append(bench)
                    continue

                bench = Bench(
                    cabinet_id=cabinet.id,
                    tour_operator_id=operator_id,
                    status="request_sent",
                    note=note,
                )

                self.db.add(bench)
                self.db.flush()

                self.db.add(
                    Mirror(
                        cabinet_id=cabinet.id,
                        bench_id=bench.id,
                        channel="push",
                        message=(
                            "We'll notify you the moment a quote lands."
                        ),
                    )
                )

                benches.append(bench)

            cabinet.status = "quoting"
            self.db.add(cabinet)

            self.db.commit()

            return benches

        except Exception:
            self.db.rollback()
            raise

    # ==================================================================
    # RECORD QUOTE
    # ==================================================================

    def record_quote(
        self,
        bench: Bench,
        quote_data: dict[str, Any],
    ) -> Counter:
        """
        Persist an operator quote against an existing Bench.

        A Bench may technically have historical Counter rows. The newest
        successfully recorded quote becomes the current response by virtue
        of being appended last; comparison logic explicitly chooses the
        latest Counter rather than blindly taking counters[0].
        """

        if bench.id is None:
            raise ValueError(
                "Cannot record a quote for an unsaved Bench."
            )

        if not bench.cabinet_id:
            raise ValueError(
                "Bench is not associated with a Cabinet."
            )

        price = self._required_decimal(
            quote_data.get("price_per_person"),
            "price_per_person",
        )

        if price < 0:
            raise ValueError(
                "price_per_person cannot be negative."
            )

        currency = self._normalize_currency(
            quote_data.get("currency", "USD")
        )

        try:
            # Re-read the Bench from the current transaction so a stale
            # detached object cannot accidentally receive a quote.
            persisted_bench = (
                self.db.query(Bench)
                .filter(Bench.id == bench.id)
                .one_or_none()
            )

            if persisted_bench is None:
                raise ValueError(
                    f"Bench {bench.id} does not exist."
                )

            if persisted_bench.cabinet_id is None:
                raise ValueError(
                    f"Bench {bench.id} has no cabinet."
                )

            counter = Counter(
                bench_id=persisted_bench.id,
                price_per_person=price,
                currency=currency,
                validity_date=self._normalize_validity_date(
                    quote_data.get("validity_date")
                ),
                accommodation_summary=quote_data.get(
                    "accommodation_summary"
                ),
                activities_summary=quote_data.get(
                    "activities_summary"
                ),
                transport_summary=quote_data.get(
                    "transport_summary"
                ),
                meals_summary=quote_data.get(
                    "meals_summary"
                ),
                difference_notes=quote_data.get(
                    "difference_notes"
                ),
            )

            self.db.add(counter)

            persisted_bench.status = "quote_received"
            self.db.add(persisted_bench)

            self.db.flush()

            # Keep the Cabinet active in the quoting stage. A later
            # booking/confirmation workflow can transition it further.
            cabinet = (
                self.db.query(Cabinet)
                .filter(Cabinet.id == persisted_bench.cabinet_id)
                .one_or_none()
            )

            if cabinet is not None and cabinet.status != "booked":
                cabinet.status = "quoting"
                self.db.add(cabinet)

            self.db.commit()

            return counter

        except Exception:
            self.db.rollback()
            raise

    # ==================================================================
    # TRACKING
    # ==================================================================

    def tracking_summary(
        self,
        cabinet: Cabinet,
    ) -> dict[str, Any]:
        """
        Return quote-request tracking state.

        Operator names are loaded in one query.
        The latest Counter is used when historical quote rows exist.
        """

        benches = list(cabinet.benches or [])

        operator_ids = [
            b.tour_operator_id
            for b in benches
            if b.tour_operator_id is not None
        ]

        names = self._operator_names(operator_ids)

        quote_received = 0
        awaiting_response = 0

        serialized: list[dict[str, Any]] = []

        for bench in benches:
            status = bench.status

            if status == "quote_received":
                quote_received += 1

            if status in (
                "request_sent",
                "operator_reviewing",
            ):
                awaiting_response += 1

            latest_counter = self._latest_counter(
                list(bench.counters or [])
            )

            serialized.append(
                {
                    "bench_id": str(bench.id),
                    "tour_operator_id": str(
                        bench.tour_operator_id
                    ),
                    "operator_name": names.get(
                        str(bench.tour_operator_id)
                    ),
                    "status": status,
                    "quote": (
                        self._quote_summary(latest_counter)
                        if latest_counter
                        else None
                    ),
                }
            )

        return {
            "requests_sent": len(benches),
            "quotes_received": quote_received,
            "awaiting_response": awaiting_response,
            "benches": serialized,
        }

    # ==================================================================
    # COMPARISON
    # ==================================================================

    def compare(
        self,
        cabinet: Cabinet,
    ) -> dict[str, Any]:
        """
        Compare all benches with received quotes.

        Best value:
            Lowest known price per person.

        Best fit:
            Operator with the highest persisted Stool trip_match_pct among
            operators that actually returned a quote.

        This fixes the previous behaviour where `rows[0]` was incorrectly
        labelled "best fit".
        """

        benches = list(cabinet.benches or [])

        operator_ids = [
            b.tour_operator_id
            for b in benches
            if b.tour_operator_id is not None
        ]

        names = self._operator_names(operator_ids)

        stools_by_operator = self._load_match_scores(
            cabinet.id
        )

        rows: list[dict[str, Any]] = []

        for bench in benches:
            latest_counter = self._latest_counter(
                list(bench.counters or [])
            )

            if latest_counter is None:
                continue

            operator_key = str(bench.tour_operator_id)

            match_score = stools_by_operator.get(
                operator_key
            )

            row = {
                "bench_id": str(bench.id),
                "tour_operator_id": operator_key,
                "operator_name": names.get(operator_key),
                "price_per_person": float(
                    latest_counter.price_per_person
                ),
                "currency": latest_counter.currency,
                "accommodation": (
                    latest_counter.accommodation_summary
                ),
                "activities": (
                    latest_counter.activities_summary
                ),
                "transport": (
                    latest_counter.transport_summary
                ),
                "meals": (
                    latest_counter.meals_summary
                ),
                "park_fees_included": (
                    latest_counter.park_fees_included
                ),
                "transfers_included": (
                    latest_counter.transfers_included
                ),
                "validity_date": (
                    latest_counter.validity_date.isoformat()
                    if latest_counter.validity_date
                    else None
                ),
                "difference_notes": (
                    latest_counter.difference_notes
                ),
                "trip_match_pct": match_score,
            }

            rows.append(row)

        # Stable deterministic ordering:
        # strongest operator fit first, then lower price.
        rows.sort(
            key=lambda row: (
                row["trip_match_pct"]
                if row["trip_match_pct"] is not None
                else -1,
                -row["price_per_person"],
            ),
            reverse=True,
        )

        best_value = min(
            rows,
            key=lambda row: row["price_per_person"],
            default=None,
        )

        fit_candidates = [
            row
            for row in rows
            if row["trip_match_pct"] is not None
        ]

        best_fit = max(
            fit_candidates,
            key=lambda row: row["trip_match_pct"],
            default=None,
        )

        return {
            "quotes": rows,
            "best_value_bench_id": (
                best_value["bench_id"]
                if best_value
                else None
            ),
            "best_fit_bench_id": (
                best_fit["bench_id"]
                if best_fit
                else None
            ),
        }

    # ==================================================================
    # MATCH SCORE LOOKUP
    # ==================================================================

    def _load_match_scores(
        self,
        cabinet_id,
    ) -> dict[str, int]:
        """
        Load persisted OperatorMatchEngine scores in one query.

        If duplicate Stool rows somehow exist, the highest match score is
        retained rather than depending on arbitrary database row order.
        """

        rows = self.db.execute(
            text(
                """
                select
                    tour_operator_id::text,
                    trip_match_pct
                from stools
                where cabinet_id = :cabinet_id
                """
            ),
            {"cabinet_id": cabinet_id},
        ).fetchall()

        result: dict[str, int] = {}

        for operator_id, score in rows:
            if operator_id is None or score is None:
                continue

            key = str(operator_id)
            numeric_score = int(score)

            result[key] = max(
                result.get(key, numeric_score),
                numeric_score,
            )

        return result

    # ==================================================================
    # COUNTER HELPERS
    # ==================================================================

    @staticmethod
    def _latest_counter(
        counters: list[Counter],
    ) -> Counter | None:
        """
        Return the latest quote row.

        The Counter model shown here has no guaranteed created_at field, so
        we deliberately do not invent one. SQLAlchemy relationship order is
        used only as a final fallback when no explicit ordering metadata is
        available.
        """

        if not counters:
            return None

        # If a future/actual Counter model exposes created_at, use it without
        # requiring the current schema to have that field.
        with_created_at = [
            counter
            for counter in counters
            if hasattr(counter, "created_at")
            and getattr(counter, "created_at", None) is not None
        ]

        if with_created_at:
            return max(
                with_created_at,
                key=lambda counter: counter.created_at,
            )

        return counters[-1]

    @staticmethod
    def _quote_summary(
        counter: Counter,
    ) -> dict[str, Any]:
        return {
            "price_per_person": float(
                counter.price_per_person
            ),
            "currency": counter.currency,
            "validity_date": (
                counter.validity_date.isoformat()
                if counter.validity_date
                else None
            ),
        }

    # ==================================================================
    # INPUT NORMALIZATION
    # ==================================================================

    @staticmethod
    def _unique_ids(
        values: list[str],
    ) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        for value in values:
            if value is None:
                continue

            normalized = str(value).strip()

            if not normalized:
                continue

            if normalized in seen:
                continue

            seen.add(normalized)
            result.append(normalized)

        return result

    @staticmethod
    def _required_decimal(
        value: Any,
        field_name: str,
    ) -> Decimal:
        if value is None or value == "":
            raise ValueError(
                f"{field_name} is required."
            )

        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError(
                f"{field_name} must be a valid numeric amount."
            ) from None

        if not decimal_value.is_finite():
            raise ValueError(
                f"{field_name} must be a finite numeric amount."
            )

        return decimal_value

    @staticmethod
    def _normalize_currency(
        currency: Any,
    ) -> str:
        if currency is None:
            return "USD"

        normalized = str(currency).strip().upper()

        if not normalized:
            return "USD"

        if len(normalized) > 10:
            raise ValueError(
                "currency code is unexpectedly long."
            )

        return normalized

    @staticmethod
    def _normalize_validity_date(
        value: Any,
    ) -> Any:
        if value is None or value == "":
            return None

        if isinstance(value, date):
            return value

        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                raise ValueError(
                    "validity_date must use YYYY-MM-DD format."
                ) from None

        # Do not silently convert arbitrary objects into dates.
        raise ValueError(
            "validity_date must be a date or YYYY-MM-DD string."
        )


__all__ = [
    "QuoteEngine",
]
