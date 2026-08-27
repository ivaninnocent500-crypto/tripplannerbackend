"""
VisaIntelligenceEngine — NEWLY WRITTEN HERE. The chat message that
referenced this class ("visa_engine.py") never included its actual
code — only a migration for the tables it reads. This is my own
implementation against that schema (schema/005_multi_country_and_visa.sql),
not a confirmation of whatever the original file contained. If a real
visa_engine.py already exists in your repo, diff against this rather
than overwriting blind.

Design rule this follows throughout: never invent a visa answer. Every
response is either backed by a verified row in visa_requirements /
regional_visa_blocs, or explicitly labeled unverified — never a
plausible-sounding guess. Wrong visa information can strand a real
traveler at a border; "I don't know, verify with the embassy" is always
the safe answer when no verified row exists.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


class VisaIntelligenceEngine:
    def __init__(self, db: Session):
        self.db = db

    def check(self, nationality: str, destination_countries: list[str]) -> dict[str, Any]:
        """
        One entry per country on the route. Each entry is either a
        verified requirement, a verified regional-bloc coverage note,
        or an explicit "unverified" flag — never a guess.
        """
        results = []
        blocs = self._active_blocs_for(nationality, destination_countries)

        for country in destination_countries:
            bloc_hit = next((b for b in blocs if country in b["member_countries"]), None)

            row = self.db.execute(
                text(
                    """
                    select requirement, applicable_bloc_code, fee_usd, processing_days_typical,
                           notes, source_url, verified_date
                    from visa_requirements
                    where nationality_country = :nat and destination_country = :dest
                    """
                ),
                {"nat": nationality, "dest": country},
            ).fetchone()

            if row:
                requirement, bloc_code, fee, processing_days, notes, source_url, verified_date = row
                results.append({
                    "country": country,
                    "status": "verified",
                    "requirement": requirement,
                    "covered_by_bloc": bloc_code,
                    "fee_usd": float(fee) if fee is not None else None,
                    "processing_days_typical": processing_days,
                    "notes": notes,
                    "source_url": source_url,
                    "verified_date": verified_date.isoformat() if verified_date else None,
                })
            elif bloc_hit:
                results.append({
                    "country": country,
                    "status": "verified_via_bloc",
                    "requirement": None,
                    "covered_by_bloc": bloc_hit["bloc_code"],
                    "bloc_name": bloc_hit["name"],
                    "fee_usd": bloc_hit["fee_usd"],
                    "notes": bloc_hit["notes"],
                    "source_url": bloc_hit["source_url"],
                    "verified_date": bloc_hit["verified_date"],
                })
            else:
                results.append({
                    "country": country,
                    "status": "unverified_no_data",
                    "requirement": None,
                    "notes": (
                        "No verified visa requirement on file for this nationality/destination "
                        "pair. Confirm directly with the destination country's embassy or "
                        "official immigration portal before booking."
                    ),
                })

        return {
            "nationality": nationality,
            "countries": results,
            "bloc_exit_warning": self._bloc_exit_warning(destination_countries, blocs),
        }

    # ------------------------------------------------------------------
    def _active_blocs_for(self, nationality: str, destination_countries: list[str]) -> list[dict]:
        rows = self.db.execute(
            text(
                """
                select bloc_code, name, member_countries, fee_usd, validity_days,
                       invalidated_on_bloc_exit, notes, source_url, verified_date
                from regional_visa_blocs
                """
            )
        ).fetchall()
        blocs = []
        for bloc_code, name, member_countries, fee_usd, validity_days, invalidated_on_exit, notes, source_url, verified_date in rows:
            # Only relevant if the route touches 2+ member countries —
            # a single-country trip gets the plain visa_requirements row.
            overlap = [c for c in destination_countries if c in member_countries]
            if len(overlap) >= 2:
                blocs.append({
                    "bloc_code": bloc_code, "name": name, "member_countries": member_countries,
                    "fee_usd": float(fee_usd) if fee_usd is not None else None,
                    "validity_days": validity_days, "invalidated_on_bloc_exit": invalidated_on_exit,
                    "notes": notes, "source_url": source_url,
                    "verified_date": verified_date.isoformat() if verified_date else None,
                })
        return blocs

    @staticmethod
    def _bloc_exit_warning(destination_countries: list[str], blocs: list[dict]) -> str | None:
        """
        The single most important warning this engine can surface: a
        route that enters a bloc, leaves it for a non-member country,
        then re-enters — which invalidates a bloc visa like the EATV
        entirely, not just for the excursion.
        """
        for bloc in blocs:
            if not bloc["invalidated_on_bloc_exit"]:
                continue
            members = set(bloc["member_countries"])
            in_bloc_flags = [c in members for c in destination_countries]
            # Look for a member -> non-member -> member pattern anywhere in the route.
            for i in range(1, len(in_bloc_flags) - 1):
                if in_bloc_flags[i - 1] and not in_bloc_flags[i] and in_bloc_flags[i + 1]:
                    return (
                        f"This route leaves the {bloc['name']} ({bloc['bloc_code']}) bloc at "
                        f"{destination_countries[i]} and re-enters it afterward. A {bloc['bloc_code']} "
                        "visa is invalidated immediately on exiting the bloc — a fresh visa would be "
                        "required to re-enter, not just for the excursion country."
                    )
        return None
