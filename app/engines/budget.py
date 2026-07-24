"""
Budget Engine — migrated to the new schema. Tier pricing table (lodging/
meals/transport/activities base rates) is UNCHANGED — that's a pricing
policy constant, not destination data, so it correctly stays as a Python
constant rather than a database table (same reasoning as before
migration). What changed: park fees now come from the real, normalized
`entry_fees` table (payer_category-based) instead of the old schema's
flat Destination.park_fee_adult_usd column — this is a real accuracy
improvement, not just a rename, since entry_fees supports different rates
for foreign/local/child/vehicle correctly.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any
from sqlalchemy.orm import Session

from app.db.models_v2 import EntryFee
from app.db.destinations import resolve_slugs_to_ids


@dataclass
class BudgetLine:
    category: str
    amount_usd: int
    share_pct: int
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Budget:
    tier: str
    travelers: int
    currency: str
    lines: list[BudgetLine] = field(default_factory=list)
    total_usd: int = 0
    per_person_usd: int = 0
    emergency_fund_usd: int = 0
    confidence_pct: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier, "travelers": self.travelers, "currency": self.currency,
            "total_usd": self.total_usd, "per_person_usd": self.per_person_usd,
            "emergency_fund_usd": self.emergency_fund_usd, "confidence_pct": self.confidence_pct,
            "lines": [line.to_dict() for line in self.lines],
        }


class BudgetEngine:
    BASE_PER_DAY = {
        "value": {"lodging": 150, "meals": 40, "transport": 60, "activities": 40, "fees": 30},
        "mid": {"lodging": 320, "meals": 70, "transport": 110, "activities": 80, "fees": 60},
        "luxury": {"lodging": 600, "meals": 120, "transport": 200, "activities": 150, "fees": 90},
        "ultra": {"lodging": 950, "meals": 180, "transport": 320, "activities": 220, "fees": 120},
    }
    EMERGENCY_PCT = 8
    TIPS_PCT = 6

    def __init__(self, db: Session, days: int, travelers: int, tier: str, destination_slugs: list[str]):
        self.db = db
        self.days = days
        self.travelers = travelers
        self.tier = tier.lower() if tier.lower() in self.BASE_PER_DAY else "luxury"
        self.destination_slugs = destination_slugs

    def calculate(self) -> Budget:
        base = self.BASE_PER_DAY[self.tier]

        real_fees_total = self._real_park_fees_total()

        lodging = base["lodging"] * self.days * self.travelers
        meals = base["meals"] * self.days * self.travelers
        transport = base["transport"] * self.days * self.travelers
        activities = base["activities"] * self.days * self.travelers
        fees = real_fees_total if real_fees_total is not None else (base["fees"] * self.days * self.travelers)
        fees_note = "Real entry fee data (foreign non-resident rate)." if real_fees_total is not None else "Estimated — no verified fee data for these destinations yet."
        tips = int(round((lodging + meals + activities) * self.TIPS_PCT / 100))

        subtotal = lodging + meals + transport + activities + fees + tips
        emergency = int(round(subtotal * self.EMERGENCY_PCT / 100))
        total = subtotal + emergency

        total_rounded = int(round(total / 100) * 100)
        emergency_rounded = int(round(emergency / 100) * 100)
        per_person = int(total_rounded / max(self.travelers, 1))

        lines = [
            BudgetLine("Accommodations", lodging, self._pct(lodging, total), "Tier-based nightly rates."),
            BudgetLine("Park & Reserve Fees", fees, self._pct(fees, total), fees_note),
            BudgetLine("Transfers & Flights", transport, self._pct(transport, total), "4x4 vehicle + transfers."),
            BudgetLine("Activities", activities, self._pct(activities, total), "Game drives and excursions."),
            BudgetLine("Meals & Drinks", meals, self._pct(meals, total), "Full board at lodges/camps."),
            BudgetLine("Tips & Extras", tips, self._pct(tips, total), "Guides, drivers, lodge staff."),
        ]

        target_per_person = base["lodging"] * 5
        confidence = int(min(100, round(per_person / target_per_person * 100)))

        return Budget(
            tier=self.tier, travelers=self.travelers, currency="USD", lines=lines,
            total_usd=total_rounded, per_person_usd=per_person,
            emergency_fund_usd=emergency_rounded, confidence_pct=confidence,
        )

    def _real_park_fees_total(self) -> int | None:
        slug_to_id = resolve_slugs_to_ids(self.db, self.destination_slugs)
        if not slug_to_id:
            return None

        fee_rows = (
            self.db.query(EntryFee)
            .filter(
                EntryFee.destination_id.in_(slug_to_id.values()),
                EntryFee.payer_category == "foreign_non_resident",
            )
            .all()
        )
        if not fee_rows:
            return None

        # Sum one day's fee per destination per traveler — matches the old
        # engine's "per typical_visit_days, capped at trip length" logic
        # loosely; since estimated_visit_durations is a separate table
        # now, this uses a simpler "one fee-day per destination" model.
        # Revisit if per-destination visit-length-aware fee totals matter.
        total = sum(int(row.fee_amount) for row in fee_rows) * self.travelers
        return total

    @staticmethod
    def _pct(amount: int, total: int) -> int:
        return int(amount / total * 100) if total else 0
