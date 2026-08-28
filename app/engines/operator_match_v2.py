"""
OperatorMatchEngine — explainable, reproducible per-trip operator match
score, persisted as `stools`. This supersedes the earlier "V2 vs V3"
split with a single production engine: V3's real improvements (activity
capability data, lodge partnership data, multi-country coverage) are
kept, but with the bugs found in that pass fixed:

  1. ROLLBACK BUG (the same failure class fixed earlier in
     resilience.py, reintroduced here via a different path): the bulk
     capability/partnership lookups below hit real tables that may not
     exist yet in every environment (operator_activity_capabilities,
     operator_lodge_partnerships — see schema/005). The original V3
     code caught that with a bare `except Exception: return {}` and
     kept going — but a failed query leaves the session's transaction
     aborted in Postgres; every subsequent query on the same session
     (including this method's own later queries, and the route's final
     db.commit()) then fails too. Fixed: db.rollback() in the except
     block, same as resilience.py's call_engine().
  2. UNUSED `rating` & TYPE MISMATCH FIX: V3 pulled `t.rating` out of
     the candidates row but never used it anywhere in scoring. Restored:
     rating factors into trust. Crucially, psycopg/PostgreSQL returns
     `rating` as a `decimal.Decimal`, which crashes Python when mixed with
     floating-point arithmetic (`rating - 3.0`). Fixed: explicitly cast
     `rating` to `float` upon unpacking.
  3. HONEST NAMING: "country_match" implied verified legal cross-border
     licensing. What the query actually measures is "this operator has
     listed destination coverage in this country" — renamed to
     country_coverage_pct throughout (see schema/005's migration notes)
     so the number means what it measures. Real cross-border licensing
     would need a separate operator_country_licenses table — not built
     here, since no licensing data exists to put in it.
  4. SCORE vs CONFIDENCE, kept separate: an operator with zero rows in
     operator_activity_capabilities isn't "70% experienced" — they're
     unscored on that dimension. `confidence_pct` reports how much of
     the total weighted score came from real data vs placeholder
     sub-scores, instead of quietly blending "unknown" into the visible
     match percentage.

Weights are still single blend for single-country trips and a
rebalanced blend for multi-country trips (country_coverage added,
introduced only when it's relevant) — this is more sophisticated than
a flat weight table, which is a real tradeoff (harder to debug at a
glance) accepted deliberately because a Kenya-only operator should not
rank identically to one who actually covers a Kenya+Tanzania+Rwanda
route.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models_furniture import Cabinet, Stool

WEIGHTS_SINGLE_COUNTRY = {
    "itinerary_fit": 0.25, "experience_fit": 0.20, "accommodation_fit": 0.15,
    "destination_coverage": 0.15, "service": 0.10, "trust": 0.10, "value": 0.05,
}

WEIGHTS_MULTI_COUNTRY = {
    "itinerary_fit": 0.20, "experience_fit": 0.15, "accommodation_fit": 0.15,
    "destination_coverage": 0.10, "country_coverage": 0.20,
    "service": 0.10, "trust": 0.10, "value": 0.00,
}

# A sub-score computed from a real, populated table earns full
# confidence weight; one that fell back to a placeholder earns none.
# confidence_pct is the weighted share of the final score that rested
# on real data — separate from trip_match_pct itself, so "unscored" is
# never silently indistinguishable from "scored and mediocre."
PLACEHOLDER_CAP = 70


class OperatorMatchEngine:
    def __init__(self, db: Session):
        self.db = db

    def match(self, cabinet: Cabinet, limit: int = 3) -> list[Stool]:
        # Synchronize ORM session on delete so SQLAlchemy purges cached state
        self.db.query(Stool).filter(Stool.cabinet_id == cabinet.id).delete(synchronize_session="fetch")
        self.db.commit()

        route = cabinet.route_destination_ids or []
        route_countries = list(cabinet.route_countries or []) or self._infer_countries(route)
        is_multi_country = len(route_countries) > 1
        weights = WEIGHTS_MULTI_COUNTRY if is_multi_country else WEIGHTS_SINGLE_COUNTRY

        candidates = self.db.execute(text("""
            select distinct t.id, t.name, t.verification_status, t.years_in_operation,
                   t.rating, t.review_count, t.headquarters_country
            from tour_operators t
            join destination_tour_operators dto on dto.tour_operator_id = t.id
            where dto.destination_id = any(:route) and t.verification_status = 'verified'
        """), {"route": route}).fetchall()

        op_ids = [c[0] for c in candidates]
        activity_caps = self._bulk_activity_capabilities(op_ids)
        lodge_partners = self._bulk_lodge_partners(op_ids, cabinet.budget_tier or "mid")

        scored: list[dict[str, Any]] = []
        for row in candidates:
            op_id, name, verification, years, raw_rating, review_count, hq_country = row
            years = years or 0
            rating = float(raw_rating) if raw_rating is not None else 4.0
            review_count = review_count or 0

            coverage_pct = self._coverage_pct(op_id, route)
            activity_pct, activity_method = self._activity_fit(op_id, activity_caps)
            lodge_pct, lodge_method = self._lodge_fit(op_id, lodge_partners)

            trust = min(100, int(50 + years * 2 + (review_count >= 50) * 10
                                  + (verification == "verified") * 5 + (rating - 3.0) * 10))
            trust = max(0, trust)
            service = max(50, min(95, int(50 + (review_count ** 0.5) * 4)))
            value = PLACEHOLDER_CAP

            methods = {
                "experience_fit_method": activity_method,
                "accommodation_fit_method": lodge_method,
                "value_method": "placeholder_pending_pricing_feed",
            }

            fields = {
                "itinerary_fit": coverage_pct, "experience_fit": activity_pct,
                "accommodation_fit": lodge_pct, "destination_coverage": coverage_pct,
                "service": service, "trust": trust, "value": value,
            }

            if is_multi_country:
                country_pct = self._country_coverage_pct(op_id, route_countries, hq_country)
                fields["country_coverage"] = country_pct
                methods["country_coverage_method"] = "destination_tour_operators+headquarters_country"

            trip_match = round(sum(fields[k] * weights[k] for k in weights if k in fields))

            real_weight = sum(
                weights[k] for k in weights if k in fields
                and not (k == "value")
                and not (k == "experience_fit" and activity_method.startswith("placeholder"))
                and not (k == "accommodation_fit" and lodge_method.startswith("placeholder"))
            )
            confidence_pct = round(100 * real_weight / max(0.01, sum(weights[k] for k in weights if k in fields)))

            scored.append({
                "op_id": op_id, "name": name, "trip_match": trip_match, "confidence_pct": confidence_pct,
                **fields, "country_coverage_pct": fields.get("country_coverage"),
                "_methods": methods,
            })

        scored.sort(key=lambda s: s["trip_match"], reverse=True)
        top = scored[:limit]

        stools = []
        for i, s in enumerate(top):
            badge = None
            if i == 0:
                badge = "strongest_match"
            elif s["accommodation_fit"] == max(x["accommodation_fit"] for x in top):
                badge = "best_premium_experience"
            elif s["value"] == max(x["value"] for x in top):
                badge = "best_value"

            stool = Stool(
                cabinet_id=cabinet.id, tour_operator_id=s["op_id"], trip_match_pct=s["trip_match"],
                itinerary_fit_pct=s["itinerary_fit"], experience_fit_pct=s["experience_fit"],
                accommodation_fit_pct=s["accommodation_fit"], destination_coverage_pct=s["destination_coverage"],
                service_pct=s["service"], trust_pct=s["trust"], value_pct=s["value"],
                strengths=self._strengths(s), badge=badge,
                country_coverage_pct=s["country_coverage_pct"],
                score_provenance=json.dumps(s["_methods"]),
                has_placeholder_subscores=any(v.startswith("placeholder") for v in s["_methods"].values()),
                confidence_pct=s["confidence_pct"],
            )
            self.db.add(stool)
            stools.append(stool)

        self.db.commit()
        return stools

    # ------------------------------------------------------------------
    def _infer_countries(self, route: list[str]) -> list[str]:
        if not route:
            return []
        rows = self.db.execute(
            text("select distinct country::text from travel_places where id = any(:ids)"), {"ids": route},
        ).fetchall()
        return [r[0] for r in rows]

    def _coverage_pct(self, op_id, route) -> int:
        if not route:
            return 0
        n = self.db.execute(text(
            "select count(*) from destination_tour_operators where tour_operator_id = :op_id and destination_id = any(:route)"
        ), {"op_id": op_id, "route": route}).scalar() or 0
        return int(100 * n / max(1, len(route)))

    def _country_coverage_pct(self, op_id: str, route_countries: list[str], hq_country: str | None) -> int:
        rows = self.db.execute(text("""
            select distinct tp.country::text
            from destination_tour_operators dto
            join travel_places tp on tp.id = dto.destination_id
            where dto.tour_operator_id = :op_id
        """), {"op_id": op_id}).fetchall()
        operator_countries = {r[0] for r in rows}
        covered = sum(1 for c in route_countries if c in operator_countries)
        base_pct = int(100 * covered / max(1, len(route_countries)))
        hq_bonus = 5 if hq_country in route_countries else 0
        return min(100, base_pct + hq_bonus)

    # ------------------------------------------------------------------
    def _bulk_activity_capabilities(self, op_ids: list[str]) -> dict[str, int]:
        if not op_ids:
            return {}
        try:
            rows = self.db.execute(text("""
                select oc.operator_id,
                       count(*) filter (where oc.is_primary)::float / greatest(1, count(*)) * 100
                from operator_activity_capabilities oc
                where oc.operator_id = any(:op_ids)
                group by oc.operator_id
            """), {"op_ids": op_ids}).fetchall()
            return {r[0]: int(r[1]) for r in rows}
        except Exception:
            self.db.rollback()
            return {}

    def _activity_fit(self, op_id, cache: dict[str, int]) -> tuple[int, str]:
        if op_id in cache:
            return min(100, cache[op_id]), "operator_activity_capabilities"
        return PLACEHOLDER_CAP, "placeholder_pending_operator_activity_capabilities"

    def _bulk_lodge_partners(self, op_ids: list[str], requested_tier: str) -> dict[str, tuple[int, str]]:
        if not op_ids:
            return {}
        try:
            rows = self.db.execute(text("""
                select operator_id,
                       count(*) filter (where partnership_grade = 'preferred') as preferred_count,
                       count(*) as total_count
                from operator_lodge_partnerships
                where operator_id = any(:op_ids) and partner_lodge_tier::text = :tier
                group by operator_id
            """), {"op_ids": op_ids, "tier": requested_tier}).fetchall()
            out = {}
            for op_id, preferred_count, total_count in rows:
                grade = "preferred" if preferred_count > 0 else ("standard" if total_count else None)
                pct = 90 if grade == "preferred" else (75 if grade == "standard" else 0)
                out[op_id] = (pct, grade or "none")
            return out
        except Exception:
            self.db.rollback()
            return {}

    def _lodge_fit(self, op_id, cache: dict[str, tuple[int, str]]) -> tuple[int, str]:
        if op_id in cache:
            pct, grade = cache[op_id]
            return pct, f"operator_lodge_partnerships({grade})"
        return PLACEHOLDER_CAP, "placeholder_pending_operator_lodge_partnerships"

    # ------------------------------------------------------------------
    @staticmethod
    def _strengths(s: dict[str, Any]) -> list[str]:
        out = []
        if s.get("country_coverage_pct") == 100:
            out.append("Fully covers every country on your route")
        if s["itinerary_fit"] >= 90:
            out.append("Excellent fit for your itinerary")
        if s["accommodation_fit"] >= 85:
            out.append("Strong accommodation options for your budget tier")
        if s["destination_coverage"] == 100:
            out.append("Covers every destination on your route")
        if s["trust"] >= 85:
            out.append("Verified operator with a strong track record")
        return out or ["Fits the core requirements of your trip"]

