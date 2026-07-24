"""
Operator Recommendation Engine — migrated to tour_operators +
destination_tour_operators. Scoring formula UNCHANGED (trust 25%, price
20%, response 15%, reviews 20%, luxury 10%, availability 10%).

KEY DIFFERENCE FROM BEFORE: the old Operator model had tier_rank,
avg_response_min, and available_window fields the scoring formula
depends on directly. The new tour_operators table doesn't have these —
it has verification_status, years_in_operation, rating, review_count.
This is a real gap: price/response/availability factors can't be
computed from real data yet with the current schema, and are set to
conservative neutral defaults below rather than invented numbers. See
the TODO comments for exactly which schema additions would close this
gap (e.g. an avg_response_minutes column, a tier/price_band column).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any
from sqlalchemy.orm import Session

from app.db.models_v2 import TourOperator, DestinationTourOperator
from app.db.destinations import resolve_slugs_to_ids


@dataclass
class OperatorCard:
    operator_id: str
    name: str
    overall_match: int
    badge: str
    factors: dict[str, int]
    tagline: str
    contact: dict[str, str]
    hero_image: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OperatorEngine:
    WEIGHTS = {
        "trust": 0.25, "price": 0.20, "response": 0.15,
        "reviews": 0.20, "luxury": 0.10, "availability": 0.10,
    }

    def __init__(self, db: Session):
        self.db = db

    def rank(self, budget_tier: str, focus: str, destination_slugs: list[str] | None = None) -> list[OperatorCard]:
        query = self.db.query(TourOperator).filter(
            TourOperator.verification_status == "verified"
        )

        if destination_slugs:
            slug_to_id = resolve_slugs_to_ids(self.db, destination_slugs)
            dest_ids = list(slug_to_id.values())
            if dest_ids:
                operator_ids = {
                    row.tour_operator_id for row in
                    self.db.query(DestinationTourOperator)
                    .filter(DestinationTourOperator.destination_id.in_(dest_ids))
                    .all()
                }
                if operator_ids:
                    query = query.filter(TourOperator.id.in_(operator_ids))

        operators = query.all()

        cards: list[OperatorCard] = []
        for op in operators:
            factors = self.score_factors(op)
            total = int(round(sum(factors[k] * self.WEIGHTS[k] for k in self.WEIGHTS)))
            cards.append(
                OperatorCard(
                    operator_id=op.id,
                    name=op.name,
                    overall_match=total,
                    badge=self._badge_for(op, focus),
                    factors=factors,
                    tagline=self._tagline(op),
                    contact={
                        "phone": op.contact_phone or "",
                        "whatsapp": op.contact_phone or "",
                        "email": op.contact_email or "",
                    },
                    hero_image="",
                )
            )
        cards.sort(key=lambda x: x.overall_match, reverse=True)
        return cards

    def score_factors(self, op: TourOperator) -> dict[str, int]:
        trust = 100 if op.verification_status == "verified" else 40

        # TODO: price factor needs a real price-tier or price-band column
        # on tour_operators (the old schema's tier_rank served this
        # purpose). Neutral default until that column exists — NOT an
        # invented score presented as real.
        price = 70

        # TODO: response factor needs an avg_response_minutes column.
        # Neutral default until then.
        response = 70

        reviews = int(min(100, float(op.rating or 3.5) * 20))

        # TODO: luxury factor needs the same price-tier column as `price`.
        luxury = 70

        # TODO: availability needs a real availability-tracking mechanism
        # (this was `available_window: bool` before, itself already a
        # simplification). Neutral default.
        availability = 80

        return {
            "trust": trust, "price": price, "response": response,
            "reviews": reviews, "luxury": luxury, "availability": availability,
        }

    def _badge_for(self, op: TourOperator, focus: str) -> str:
        if op.verification_status == "verified":
            return "Verified operator"
        return "Operator"

    def _tagline(self, op: TourOperator) -> str:
        years = op.years_in_operation or 0
        reviews = op.review_count or 0
        return f"{years} years in operation, {reviews} verified traveller reviews."
