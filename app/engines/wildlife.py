"""
Wildlife Engine — migrated to the new Supabase schema.

KEY DIFFERENCE FROM BEFORE: the old models.py stored one WildlifeWindow
row per (destination, month) with a JSON blob of all species inside it.
The new schema normalizes this properly — wildlife_calendar has one row
per (destination, species, month), joined against the wildlife master
table for species names. This is actually a real data-quality
improvement (no duplicate species facts across destinations), but it
means fetch() now does a join instead of a single-row lookup — documented
here since the query shape changed meaningfully, not just the table name.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any
from sqlalchemy.orm import Session

from app.db.models_v2 import WildlifeCalendarEntry, Wildlife
from app.db.destinations import resolve_slugs_to_ids


@dataclass
class WildlifeWindow:
    destination: str  # slug, for backward-compat with orchestrator.py's existing usage
    month: str
    species: list[dict[str, Any]]
    best_viewing_window: str  # NOTE: not modeled per-species in the new schema yet; see note below

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FALLBACK_SPECIES = [{"name": "Standard safari mix", "probability_pct": 80, "herd_size": "Variable"}]


class WildlifeEngine:
    def __init__(self, db: Session):
        self.db = db

    def fetch(self, destination_slug: str, month: str) -> WildlifeWindow:
        slug_to_id = resolve_slugs_to_ids(self.db, [destination_slug])
        destination_id = slug_to_id.get(destination_slug)

        if destination_id is None:
            return WildlifeWindow(
                destination=destination_slug, month=month,
                species=_FALLBACK_SPECIES, best_viewing_window="06:00-18:00",
            )

        rows = (
            self.db.query(WildlifeCalendarEntry, Wildlife)
            .join(Wildlife, WildlifeCalendarEntry.wildlife_id == Wildlife.id)
            .filter(
                WildlifeCalendarEntry.destination_id == destination_id,
                WildlifeCalendarEntry.month == month.lower(),
            )
            .all()
        )

        if not rows:
            return WildlifeWindow(
                destination=destination_slug, month=month,
                species=_FALLBACK_SPECIES, best_viewing_window="06:00-18:00",
            )

        species = [
            {
                "name": wildlife.common_name,
                "probability_pct": calendar_entry.sighting_probability_pct or 50,
                "herd_size": "Variable",  # not modeled in wildlife_calendar; herd_size lives on great_migration_calendar for migration-specific entries only
            }
            for calendar_entry, wildlife in rows
        ]

        # NOTE: best_viewing_window was a single string per (destination,
        # month) in the old schema. The new schema doesn't have an
        # equivalent column on wildlife_calendar — this is a genuine gap,
        # not a migration oversight. Falling back to a sensible default
        # rather than inventing a specific time window not backed by data.
        return WildlifeWindow(
            destination=destination_slug,
            month=month,
            species=species,
            best_viewing_window="06:00-09:30 and 15:30-18:00",  # generic safari-standard window, not destination-specific yet
        )

    def fetch_many(self, destination_slugs: list[str], month: str) -> list[WildlifeWindow]:
        return [self.fetch(slug, month) for slug in destination_slugs]

    @staticmethod
    def summarise(windows: list[WildlifeWindow]) -> dict:
        seen: dict[str, int] = {}
        for w in windows:
            for s in w.species:
                seen[s["name"]] = max(seen.get(s["name"], 0), s["probability_pct"])
        top = sorted(seen.items(), key=lambda x: x[1], reverse=True)[:5]
        return {
            "five_species_top_pct": dict(top),
            "best_viewing_window": windows[0].best_viewing_window if windows else "",
        }
