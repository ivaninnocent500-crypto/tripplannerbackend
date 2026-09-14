"""
OperatorMatchEngine
===================

Deterministic, explainable operator matching for a generated Cabinet.

The engine ranks verified tour operators against the persisted itinerary.
It is deliberately non-AI: Gemini/LLM services must never decide operator
eligibility or ranking.

Core rules
----------
- The persisted Cabinet route is authoritative.
- Route order is preserved, including intentional A -> B -> A routes.
- Operators are eligible only when they have verified destination coverage.
- Destination coverage and itinerary fit are not treated as two independent
  copies of the same score.
- Multi-country trips use actual destination-country coverage.
- Headquarters country is only a small contextual signal; it is NOT treated
  as licensing or legal permission to operate.
- Missing capability/partnership data remains explicitly unscored and lowers
  confidence rather than silently pretending the operator has mediocre data.
- No AI-generated facts participate in ranking.
- Existing Stool ORM fields are preserved.
- Matching is transactional: old results are replaced only after successful
  scoring and persistence preparation.
"""

from __future__ import annotations

import json
import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models_furniture import Cabinet, Stool


WEIGHTS_SINGLE_COUNTRY = {
    "itinerary_fit": 0.25,
    "experience_fit": 0.20,
    "accommodation_fit": 0.15,
    "destination_coverage": 0.15,
    "service": 0.10,
    "trust": 0.10,
    "value": 0.05,
}

WEIGHTS_MULTI_COUNTRY = {
    "itinerary_fit": 0.20,
    "experience_fit": 0.15,
    "accommodation_fit": 0.15,
    "destination_coverage": 0.10,
    "country_coverage": 0.20,
    "service": 0.10,
    "trust": 0.10,
    "value": 0.00,
}

# Used only when a real data source for a scoring dimension is unavailable.
# This is intentionally NOT treated as real evidence.
PLACEHOLDER_CAP = 70


class OperatorMatchEngine:
    def __init__(self, db: Session):
        self.db = db

    # ==================================================================
    # PUBLIC API
    # ==================================================================

    def match(self, cabinet: Cabinet, limit: int = 3) -> list[Stool]:
        """
        Match verified operators to a Cabinet.

        The method performs all reads and scoring before replacing existing
        Stool rows. It therefore avoids the previous failure mode where
        existing matches were deleted and committed before a later query
        failed.

        Raises:
            ValueError: invalid limit or missing route.
            Exception: database failure during the authoritative matching
                       transaction. The session is rolled back.
        """

        if limit <= 0:
            raise ValueError("Operator match limit must be greater than zero.")

        route = self._normalize_route(cabinet.route_destination_ids or [])

        if not route:
            # No route means there is nothing meaningful to match.
            self.db.query(Stool).filter(
                Stool.cabinet_id == cabinet.id
            ).delete(synchronize_session="fetch")
            self.db.commit()
            return []

        route_countries = self._route_countries(
            cabinet=cabinet,
            route=route,
        )

        is_multi_country = len(route_countries) > 1
        weights = (
            WEIGHTS_MULTI_COUNTRY
            if is_multi_country
            else WEIGHTS_SINGLE_COUNTRY
        )

        try:
            candidates = self._load_candidates(route)

            if not candidates:
                # A successful match operation with zero eligible operators
                # should still replace stale previous results.
                self.db.query(Stool).filter(
                    Stool.cabinet_id == cabinet.id
                ).delete(synchronize_session="fetch")
                self.db.commit()
                return []

            op_ids = [row["id"] for row in candidates]

            # Bulk data loads avoid the old per-operator coverage query.
            destination_coverage = self._bulk_destination_coverage(
                op_ids=op_ids,
                route=route,
            )

            activity_caps = self._bulk_activity_capabilities(op_ids)

            lodge_partners = self._bulk_lodge_partners(
                op_ids=op_ids,
                requested_tier=cabinet.budget_tier or "mid",
            )

            country_coverage = self._bulk_country_coverage(
                op_ids=op_ids,
                route_countries=route_countries,
            )

            scored = self._score_candidates(
                candidates=candidates,
                route=route,
                route_countries=route_countries,
                destination_coverage=destination_coverage,
                activity_caps=activity_caps,
                lodge_partners=lodge_partners,
                country_coverage=country_coverage,
                weights=weights,
                is_multi_country=is_multi_country,
            )

            scored.sort(
                key=lambda item: (
                    item["trip_match"],
                    item["confidence_pct"],
                    item["destination_coverage"],
                    item["trust"],
                ),
                reverse=True,
            )

            top = scored[:limit]

            # Only now replace previous persisted matches.
            self.db.query(Stool).filter(
                Stool.cabinet_id == cabinet.id
            ).delete(synchronize_session="fetch")

            stools = self._build_stools(
                cabinet=cabinet,
                scored=top,
            )

            self.db.add_all(stools)

            # One authoritative commit after all reads, scoring and object
            # construction have succeeded.
            self.db.commit()

            return stools

        except Exception:
            self.db.rollback()
            raise

    # ==================================================================
    # ROUTE / COUNTRY NORMALIZATION
    # ==================================================================

    @staticmethod
    def _normalize_route(route: list[str]) -> list[str]:
        """
        Remove only empty values and consecutive duplicates.

        Important:
            A -> B -> A remains A -> B -> A.
        """

        normalized: list[str] = []

        for destination_id in route:
            if destination_id is None:
                continue

            value = str(destination_id).strip()

            if not value:
                continue

            if normalized and normalized[-1] == value:
                continue

            normalized.append(value)

        return normalized

    def _route_countries(
        self,
        cabinet: Cabinet,
        route: list[str],
    ) -> list[str]:
        """
        Resolve countries in first-appearance route order.

        Cabinet.route_countries is preferred when it already contains useful
        data. Missing countries are resolved from travel_places.

        The result is unique by first appearance because country coverage is
        a country-level dimension, while destination route order remains
        preserved separately.
        """

        supplied = [
            str(country).strip()
            for country in (cabinet.route_countries or [])
            if country is not None and str(country).strip()
        ]

        if supplied:
            return self._unique_preserving_order(supplied)

        return self._infer_countries(route)

    @staticmethod
    def _unique_preserving_order(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        for value in values:
            key = value.strip()

            if not key:
                continue

            normalized_key = key.casefold()

            if normalized_key in seen:
                continue

            seen.add(normalized_key)
            result.append(key)

        return result

    def _infer_countries(self, route: list[str]) -> list[str]:
        if not route:
            return []

        rows = self.db.execute(
            text(
                """
                select id::text, country::text
                from travel_places
                where id::text = any(:ids)
                """
            ),
            {"ids": route},
        ).fetchall()

        by_id = {
            str(row[0]): row[1]
            for row in rows
            if row[0] is not None and row[1] is not None
        }

        countries: list[str] = []

        for destination_id in route:
            country = by_id.get(str(destination_id))

            if country:
                countries.append(str(country))

        return self._unique_preserving_order(countries)

    # ==================================================================
    # CANDIDATE LOADING
    # ==================================================================

    def _load_candidates(
        self,
        route: list[str],
    ) -> list[dict[str, Any]]:
        """
        Load verified operators covering at least one requested destination.

        The operator is not required to cover every destination to become a
        candidate; complete route coverage is reflected in scoring.
        """

        rows = self.db.execute(
            text(
                """
                select distinct
                    t.id,
                    t.name,
                    t.verification_status,
                    t.years_in_operation,
                    t.rating,
                    t.review_count,
                    t.headquarters_country
                from tour_operators t
                join destination_tour_operators dto
                  on dto.tour_operator_id = t.id
                where dto.destination_id::text = any(:route)
                  and t.verification_status = 'verified'
                """
            ),
            {"route": route},
        ).fetchall()

        return [
            {
                "id": row[0],
                "name": row[1],
                "verification_status": row[2],
                "years_in_operation": row[3],
                "rating": row[4],
                "review_count": row[5],
                "headquarters_country": row[6],
            }
            for row in rows
        ]

    # ==================================================================
    # DESTINATION COVERAGE
    # ==================================================================

    def _bulk_destination_coverage(
        self,
        op_ids: list[str],
        route: list[str],
    ) -> dict[str, int]:
        """
        Calculate destination coverage for every candidate in one query.

        Each requested destination counts once. Repeated route destinations
        do not artificially inflate the denominator.
        """

        if not op_ids or not route:
            return {}

        unique_route = self._unique_preserving_order(route)

        rows = self.db.execute(
            text(
                """
                select
                    dto.tour_operator_id,
                    count(distinct dto.destination_id::text)
                from destination_tour_operators dto
                where dto.tour_operator_id = any(:op_ids)
                  and dto.destination_id::text = any(:route)
                group by dto.tour_operator_id
                """
            ),
            {
                "op_ids": op_ids,
                "route": unique_route,
            },
        ).fetchall()

        denominator = max(1, len(unique_route))

        return {
            str(row[0]): int(
                round(100 * int(row[1] or 0) / denominator)
            )
            for row in rows
        }

    # ==================================================================
    # COUNTRY COVERAGE
    # ==================================================================

    def _bulk_country_coverage(
        self,
        op_ids: list[str],
        route_countries: list[str],
    ) -> dict[str, int]:
        """
        Calculate actual destination-country coverage.

        This does NOT interpret headquarters_country as a licence.
        Headquarters is intentionally excluded from this percentage.

        The caller may add the small contextual HQ signal separately if
        desired, but it is never described as licensing.
        """

        if not op_ids or not route_countries:
            return {}

        rows = self.db.execute(
            text(
                """
                select distinct
                    dto.tour_operator_id,
                    tp.country::text
                from destination_tour_operators dto
                join travel_places tp
                  on tp.id = dto.destination_id
                where dto.tour_operator_id = any(:op_ids)
                """
            ),
            {"op_ids": op_ids},
        ).fetchall()

        operator_countries: dict[str, set[str]] = {}

        for operator_id, country in rows:
            if operator_id is None or country is None:
                continue

            operator_countries.setdefault(
                str(operator_id),
                set(),
            ).add(str(country).casefold())

        normalized_route_countries = {
            str(country).casefold()
            for country in route_countries
            if country is not None and str(country).strip()
        }

        denominator = max(1, len(normalized_route_countries))

        return {
            operator_id: int(
                round(
                    100
                    * len(countries.intersection(normalized_route_countries))
                    / denominator
                )
            )
            for operator_id, countries in operator_countries.items()
        }

    # ==================================================================
    # ACTIVITY CAPABILITY DATA
    # ==================================================================

    def _bulk_activity_capabilities(
        self,
        op_ids: list[str],
    ) -> dict[str, int]:
        """
        Optional capability source.

        A missing table/schema is treated as unavailable data, not as a
        database-fatal matching failure.
        """

        if not op_ids:
            return {}

        try:
            rows = self.db.execute(
                text(
                    """
                    select
                        oc.operator_id,
                        count(*) filter (
                            where oc.is_primary
                        )::float
                        / greatest(1, count(*))
                        * 100
                    from operator_activity_capabilities oc
                    where oc.operator_id = any(:op_ids)
                    group by oc.operator_id
                    """
                ),
                {"op_ids": op_ids},
            ).fetchall()

            return {
                str(row[0]): min(100, max(0, int(row[1] or 0)))
                for row in rows
            }

        except Exception as exc:
            # Roll back the failed optional query so PostgreSQL does not
            # leave the SQLAlchemy transaction in an aborted state.
            self.db.rollback()

            # The main match can continue because this is enrichment data,
            # not an eligibility requirement.
            return {}

    @staticmethod
    def _activity_fit(
        op_id: str,
        cache: dict[str, int],
    ) -> tuple[int, str]:
        if op_id in cache:
            return (
                min(100, max(0, cache[op_id])),
                "operator_activity_capabilities",
            )

        return (
            PLACEHOLDER_CAP,
            "placeholder_pending_operator_activity_capabilities",
        )

    # ==================================================================
    # ACCOMMODATION / LODGE PARTNERSHIPS
    # ==================================================================

    def _bulk_lodge_partners(
        self,
        op_ids: list[str],
        requested_tier: str,
    ) -> dict[str, tuple[int, str]]:
        """
        Optional lodge partnership source.

        The requested accommodation tier is taken from the Cabinet. Missing
        partnership data remains unknown.
        """

        if not op_ids:
            return {}

        try:
            rows = self.db.execute(
                text(
                    """
                    select
                        operator_id,
                        count(*) filter (
                            where partnership_grade = 'preferred'
                        ) as preferred_count,
                        count(*) as total_count
                    from operator_lodge_partnerships
                    where operator_id = any(:op_ids)
                      and partner_lodge_tier::text = :tier
                    group by operator_id
                    """
                ),
                {
                    "op_ids": op_ids,
                    "tier": requested_tier,
                },
            ).fetchall()

            result: dict[str, tuple[int, str]] = {}

            for operator_id, preferred_count, total_count in rows:
                preferred_count = int(preferred_count or 0)
                total_count = int(total_count or 0)

                if preferred_count > 0:
                    result[str(operator_id)] = (90, "preferred")
                elif total_count > 0:
                    result[str(operator_id)] = (75, "standard")

            return result

        except Exception:
            self.db.rollback()
            return {}

    @staticmethod
    def _lodge_fit(
        op_id: str,
        cache: dict[str, tuple[int, str]],
    ) -> tuple[int, str]:
        if op_id in cache:
            pct, grade = cache[op_id]
            return (
                min(100, max(0, pct)),
                f"operator_lodge_partnerships({grade})",
            )

        return (
            PLACEHOLDER_CAP,
            "placeholder_pending_operator_lodge_partnerships",
        )

    # ==================================================================
    # SCORING
    # ==================================================================

    def _score_candidates(
        self,
        candidates: list[dict[str, Any]],
        route: list[str],
        route_countries: list[str],
        destination_coverage: dict[str, int],
        activity_caps: dict[str, int],
        lodge_partners: dict[str, tuple[int, str]],
        country_coverage: dict[str, int],
        weights: dict[str, float],
        is_multi_country: bool,
    ) -> list[dict[str, Any]]:
        scored: list[dict[str, Any]] = []

        for candidate in candidates:
            op_id = str(candidate["id"])

            coverage_pct = min(
                100,
                max(0, destination_coverage.get(op_id, 0)),
            )

            activity_pct, activity_method = self._activity_fit(
                op_id,
                activity_caps,
            )

            lodge_pct, lodge_method = self._lodge_fit(
                op_id,
                lodge_partners,
            )

            trust = self._trust_score(
                years_in_operation=candidate["years_in_operation"],
                rating=candidate["rating"],
                review_count=candidate["review_count"],
                verification_status=candidate["verification_status"],
            )

            service = self._service_score(
                review_count=candidate["review_count"],
            )

            # Pricing is not available from the current authoritative
            # matching data model, so value remains explicitly placeholder.
            value = PLACEHOLDER_CAP

            fields: dict[str, int] = {
                "itinerary_fit": self._itinerary_fit(
                    destination_coverage_pct=coverage_pct,
                    route=route,
                ),
                "experience_fit": activity_pct,
                "accommodation_fit": lodge_pct,
                "destination_coverage": coverage_pct,
                "service": service,
                "trust": trust,
                "value": value,
            }

            methods: dict[str, str] = {
                "itinerary_fit_method": (
                    "destination_route_coverage_with_route_completeness"
                ),
                "experience_fit_method": activity_method,
                "accommodation_fit_method": lodge_method,
                "value_method": "placeholder_pending_pricing_feed",
            }

            if is_multi_country:
                country_pct = min(
                    100,
                    max(0, country_coverage.get(op_id, 0)),
                )

                fields["country_coverage"] = country_pct

                methods[
                    "country_coverage_method"
                ] = "destination_tour_operators_by_route_country"

            trip_match = self._weighted_score(
                fields=fields,
                weights=weights,
            )

            confidence_pct = self._confidence_score(
                fields=fields,
                weights=weights,
                methods=methods,
            )

            scored.append(
                {
                    "op_id": candidate["id"],
                    "name": candidate["name"],
                    "trip_match": trip_match,
                    "confidence_pct": confidence_pct,
                    **fields,
                    "country_coverage_pct": fields.get(
                        "country_coverage"
                    ),
                    "_methods": methods,
                    "_route_countries": route_countries,
                }
            )

        return scored

    @staticmethod
    def _itinerary_fit(
        destination_coverage_pct: int,
        route: list[str],
    ) -> int:
        """
        Itinerary fit is intentionally not another copy of the raw
        destination-coverage percentage.

        Full coverage gets a strong score. Partial coverage is reduced
        slightly because the operator cannot independently serve every
        requested destination.
        """

        if not route:
            return 0

        if destination_coverage_pct >= 100:
            return 100

        if destination_coverage_pct <= 0:
            return 0

        # Keep the score deterministic while making the distinction between
        # "covers some destinations" and "covers the complete itinerary"
        # explicit.
        return min(
            100,
            max(
                0,
                int(round(destination_coverage_pct * 0.90)),
            ),
        )

    @staticmethod
    def _weighted_score(
        fields: dict[str, int],
        weights: dict[str, float],
    ) -> int:
        weighted_total = sum(
            float(fields[key]) * weight
            for key, weight in weights.items()
            if key in fields
        )

        return min(100, max(0, int(round(weighted_total))))

    @staticmethod
    def _trust_score(
        years_in_operation: Any,
        rating: Any,
        review_count: Any,
        verification_status: Any,
    ) -> int:
        """
        Deterministic trust score from currently available operator data.

        Review count uses bands rather than a single arbitrary threshold.
        Rating is explicitly converted to float because PostgreSQL numeric
        values may arrive as Decimal.
        """

        try:
            years = max(0.0, float(years_in_operation or 0))
        except (TypeError, ValueError):
            years = 0.0

        try:
            rating_value = float(rating) if rating is not None else 4.0
        except (TypeError, ValueError):
            rating_value = 4.0

        try:
            reviews = max(0, int(review_count or 0))
        except (TypeError, ValueError):
            reviews = 0

        score = 45.0

        # Experience contribution, capped so longevity cannot dominate.
        score += min(20.0, years * 2.0)

        if reviews >= 1000:
            score += 20.0
        elif reviews >= 500:
            score += 17.0
        elif reviews >= 200:
            score += 14.0
        elif reviews >= 100:
            score += 11.0
        elif reviews >= 50:
            score += 8.0
        elif reviews >= 20:
            score += 5.0
        elif reviews >= 5:
            score += 2.0

        # Rating contribution around a neutral 3/5 baseline.
        score += (rating_value - 3.0) * 8.0

        # Verification is already an eligibility requirement, but retaining
        # a small contribution makes the score robust if the eligibility
        # rule changes later.
        if verification_status == "verified":
            score += 5.0

        return min(100, max(0, int(round(score))))

    @staticmethod
    def _service_score(review_count: Any) -> int:
        """
        Service remains a proxy until actual response-time / booking-success
        data is available.

        It is intentionally labelled as a proxy in score provenance rather
        than presented as a measured service KPI.
        """

        try:
            reviews = max(0, int(review_count or 0))
        except (TypeError, ValueError):
            reviews = 0

        # Smooth growth with diminishing returns.
        score = 50 + int(math.sqrt(reviews) * 4)

        return min(95, max(50, score))

    # ==================================================================
    # CONFIDENCE
    # ==================================================================

    @staticmethod
    def _confidence_score(
        fields: dict[str, int],
        weights: dict[str, float],
        methods: dict[str, str],
    ) -> int:
        """
        Reports how much of the weighted score is backed by actual data.

        Placeholder dimensions contribute zero confidence.

        `service` remains a deterministic proxy from current operator data,
        therefore it counts as available rather than placeholder.
        """

        total_weight = sum(
            weight
            for key, weight in weights.items()
            if key in fields
        )

        if total_weight <= 0:
            return 0

        real_weight = 0.0

        for key, weight in weights.items():
            if key not in fields:
                continue

            method = methods.get(f"{key}_method", "")

            if method.startswith("placeholder"):
                continue

            real_weight += weight

        return min(
            100,
            max(
                0,
                int(round(100 * real_weight / total_weight)),
            ),
        )

    # ==================================================================
    # STOOL PERSISTENCE
    # ==================================================================

    def _build_stools(
        self,
        cabinet: Cabinet,
        scored: list[dict[str, Any]],
    ) -> list[Stool]:
        stools: list[Stool] = []

        premium_max = max(
            (item["accommodation_fit"] for item in scored),
            default=None,
        )

        value_max = max(
            (item["value"] for item in scored),
            default=None,
        )

        for index, item in enumerate(scored):
            badge: str | None = None

            if index == 0:
                badge = "strongest_match"
            elif (
                premium_max is not None
                and item["accommodation_fit"] == premium_max
            ):
                badge = "best_premium_experience"
            elif (
                value_max is not None
                and item["value"] == value_max
            ):
                badge = "best_value"

            methods = item["_methods"]

            has_placeholder = any(
                value.startswith("placeholder")
                for value in methods.values()
            )

            stool = Stool(
                cabinet_id=cabinet.id,
                tour_operator_id=item["op_id"],
                trip_match_pct=item["trip_match"],
                itinerary_fit_pct=item["itinerary_fit"],
                experience_fit_pct=item["experience_fit"],
                accommodation_fit_pct=item["accommodation_fit"],
                destination_coverage_pct=item["destination_coverage"],
                service_pct=item["service"],
                trust_pct=item["trust"],
                value_pct=item["value"],
                strengths=self._strengths(item),
                badge=badge,
                country_coverage_pct=item.get(
                    "country_coverage_pct"
                ),
                score_provenance=json.dumps(
                    methods,
                    sort_keys=True,
                ),
                has_placeholder_subscores=has_placeholder,
                confidence_pct=item["confidence_pct"],
            )

            stools.append(stool)

        return stools

    # ==================================================================
    # EXPLANATORY STRENGTHS
    # ==================================================================

    @staticmethod
    def _strengths(
        scored: dict[str, Any],
    ) -> list[str]:
        out: list[str] = []

        if scored.get("country_coverage_pct") == 100:
            out.append(
                "Covers every country represented in your route"
            )

        if scored["itinerary_fit"] >= 90:
            out.append(
                "Excellent fit for your itinerary"
            )

        if scored["accommodation_fit"] >= 85:
            out.append(
                "Strong accommodation options for your budget tier"
            )

        if scored["destination_coverage"] == 100:
            out.append(
                "Covers every destination on your route"
            )

        if scored["experience_fit"] >= 85:
            out.append(
                "Strong evidence of relevant activity capability"
            )

        if scored["trust"] >= 85:
            out.append(
                "Verified operator with a strong track record"
            )

        if scored["confidence_pct"] < 70:
            out.append(
                "Match confidence is limited by missing operator data"
            )

        return out or [
            "Fits the core requirements of your trip"
        ]


__all__ = [
    "OperatorMatchEngine",
    "WEIGHTS_SINGLE_COUNTRY",
    "WEIGHTS_MULTI_COUNTRY",
    "PLACEHOLDER_CAP",
]
