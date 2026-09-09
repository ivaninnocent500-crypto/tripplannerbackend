"""
VisaIntelligenceEngine — Production version with dynamic pycountry resolution
and Render structured logging.

Design rule: Never invent a visa answer. Every response is either backed by
a verified row in visa_requirements / regional_visa_blocs or explicitly
labeled unverified.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional
import pycountry
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _to_alpha2(val: str) -> str:
    """
    Dynamically converts country name, common name, or alpha-3 to ISO Alpha-2.
    Logs warnings for Render if resolution falls back or fails.
    """
    if not val:
        return ""

    clean = val.strip()

    # Direct 2-letter code check
    if len(clean) == 2:
        return clean.upper()

    try:
        # Check standard name or alpha-3
        match = pycountry.countries.get(alpha_3=clean.upper()) or pycountry.countries.get(name=clean)
        if match:
            return match.alpha_2

        # Fuzzy lookup fallback (e.g., "USA", "United States", "Tanzania")
        matches = pycountry.countries.search_fuzzy(clean)
        if matches:
            return matches[0].alpha_2
    except Exception as e:
        logger.warning(f"[VISA_ENGINE] pycountry resolution failed for '{val}': {e}")

    logger.info(f"[VISA_ENGINE] Fallback applied for raw country input: '{clean}' -> '{clean.upper()}'")
    return clean.upper()


class VisaIntelligenceEngine:
    def __init__(self, db: Session):
        self.db = db

    def check(self, nationality: str, destination_countries: List[str]) -> dict[str, Any]:
        """
        Evaluates visa requirements per destination country.
        Normalizes input strings to ISO Alpha-2 codes and logs execution details.
        """
        results = []

        nat_code = _to_alpha2(nationality)
        dest_codes = [_to_alpha2(c) for c in destination_countries]

        logger.info(
            f"[VISA_ENGINE] Starting check | Input Nationality: '{nationality}' -> '{nat_code}' | "
            f"Input Destinations: {destination_countries} -> {dest_codes}"
        )

        try:
            blocs = self._active_blocs_for(nat_code, dest_codes)

            for country in dest_codes:
                bloc_hit = next((b for b in blocs if country in b["member_countries"]), None)

                row = self.db.execute(
                    text(
                        """
                        SELECT requirement, applicable_bloc_code, fee_usd, processing_days_typical,
                               notes, source_url, verified_date
                        FROM visa_requirements
                        WHERE nationality_country = :nat AND destination_country = :dest
                        """
                    ),
                    {"nat": nat_code, "dest": country},
                ).fetchone()

                if row:
                    requirement, bloc_code, fee, processing_days, notes, source_url, verified_date = row
                    logger.info(f"[VISA_ENGINE] Match found in visa_requirements for {nat_code} -> {country}")
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
                    logger.info(f"[VISA_ENGINE] Regional bloc match found for {country} via {bloc_hit['bloc_code']}")
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
                    logger.warning(f"[VISA_ENGINE] No visa record found for pair: nationality='{nat_code}', destination='{country}'")
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
                "nationality": nat_code,
                "countries": results,
                "bloc_exit_warning": self._bloc_exit_warning(dest_codes, blocs),
            }

        except Exception as e:
            logger.error(f"[VISA_ENGINE] Critical error during check() for nat='{nationality}': {str(e)}", exc_info=True)
            raise e

    # ------------------------------------------------------------------
    def _active_blocs_for(self, nationality: str, destination_countries: List[str]) -> List[dict]:
        try:
            rows = self.db.execute(
                text(
                    """
                    SELECT bloc_code, name, member_countries, fee_usd, validity_days,
                           invalidated_on_bloc_exit, notes, source_url, verified_date
                    FROM regional_visa_blocs
                    """
                )
            ).fetchall()

            blocs = []
            for bloc_code, name, member_countries, fee_usd, validity_days, invalidated_on_exit, notes, source_url, verified_date in rows:
                members = [_to_alpha2(m) for m in (member_countries or [])]
                overlap = [c for c in destination_countries if c in members]

                if len(overlap) >= 2:
                    blocs.append({
                        "bloc_code": bloc_code,
                        "name": name,
                        "member_countries": members,
                        "fee_usd": float(fee_usd) if fee_usd is not None else None,
                        "validity_days": validity_days,
                        "invalidated_on_bloc_exit": invalidated_on_exit,
                        "notes": notes,
                        "source_url": source_url,
                        "verified_date": verified_date.isoformat() if verified_date else None,
                    })
            return blocs

        except Exception as e:
            logger.error(f"[VISA_ENGINE] Error fetching regional_visa_blocs: {str(e)}", exc_info=True)
            return []

    @staticmethod
    def _bloc_exit_warning(destination_countries: List[str], blocs: List[dict]) -> Optional[str]:
        """
        Surfaces warning if a route exits a regional bloc and re-enters.
        """
        for bloc in blocs:
            if not bloc["invalidated_on_bloc_exit"]:
                continue
            members = set(bloc["member_countries"])
            in_bloc_flags = [c in members for c in destination_countries]
            for i in range(1, len(in_bloc_flags) - 1):
                if in_bloc_flags[i - 1] and not in_bloc_flags[i] and in_bloc_flags[i + 1]:
                    return (
                        f"This route leaves the {bloc['name']} ({bloc['bloc_code']}) bloc at "
                        f"{destination_countries[i]} and re-enters it afterward. A {bloc['bloc_code']} "
                        "visa is invalidated immediately on exiting the bloc — a fresh visa would be "
                        "required to re-enter, not just for the excursion country."
                    )
        return None
