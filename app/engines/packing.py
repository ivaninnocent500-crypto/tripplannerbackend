"""
Packing Engine — migrated to `packing_recommendations`. This is a real
upgrade over the old hardcoded BASE_RULES list: your schema stores
per-destination packing items directly (item_category, item_name,
is_essential, season_type), so the rule-matching logic that used to
filter a Python constant now filters real rows instead.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any
from sqlalchemy import Column, Text, Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Session

from app.db.destinations import resolve_slugs_to_ids
from app.db.models_v2 import Base, gen_uuid


class PackingRecommendation(Base):
    """
    Minimal model for `packing_recommendations` — not added to
    models_v2.py's main table set since only this engine queries it;
    kept local to avoid growing the shared models file with every table
    before it's needed elsewhere (same "only model what's queried"
    principle used throughout this migration).
    """
    __tablename__ = "packing_recommendations"
    __table_args__ = {"extend_existing": True}

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    destination_id = Column(UUID(as_uuid=False), ForeignKey("travel_places.id", ondelete="CASCADE"))
    item_category = Column(Text, nullable=False)
    item_name = Column(Text, nullable=False)
    is_essential = Column(Boolean, nullable=False, default=True)
    season_type = Column(Text)  # season_type enum, treated as text for filter simplicity


@dataclass
class PackingItem:
    name: str
    category: str
    qty: int = 1
    reason: str = ""
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PackingEngine:
    def __init__(self, db: Session):
        self.db = db

    def build(self, destination_slugs: list[str], season: str, budget_tier: str) -> list[PackingItem]:
        slug_to_id = resolve_slugs_to_ids(self.db, destination_slugs)
        if not slug_to_id:
            return []

        query = self.db.query(PackingRecommendation).filter(
            PackingRecommendation.destination_id.in_(slug_to_id.values())
        )

        rows = query.all()

        items: list[PackingItem] = []
        seen: set[str] = set()
        for row in rows:
            # Season filter: include if row has no season_type (universal
            # item) or matches the requested season.
            if row.season_type and row.season_type != season.lower():
                continue

            key = row.item_name.lower()
            if key in seen:
                continue
            seen.add(key)

            items.append(
                PackingItem(
                    name=row.item_name,
                    category=row.item_category,
                    optional=not row.is_essential,
                )
            )

        return items
