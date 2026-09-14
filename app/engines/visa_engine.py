"""
VisaIntelligenceEngine — Production version.

Design rules:
- visa_requirements is the authoritative source for nationality-specific answers.
- Never infer a visa requirement from destination geography alone.
- Exact nationality + destination rows always take precedence over bloc logic.
- Regional blocs are only a verified fallback when the nationality is already
  associated with that bloc in visa_requirements.
- Missing data is explicitly returned as unverified.
- Route order is preserved, including intentional A -> B -> A routes.
- Database failures are never silently converted into "unverified" visa data.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pycountry
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Country normalization
# ---------------------------------------------------------------------------

def _to_alpha2(value: str) -> str:
    """
    Resolve a country name / alpha-2 / alpha-3 value to ISO alpha-2.

    The function never raises for an unresolved value. Instead, it returns
    the normalized raw input so the database lookup safely produces
    unverified_no_data rather than inventing a country.
    """
    if value is None:
        return ""

    clean = str(value).strip()

    if not clean:
        return ""

    # Already an ISO alpha-2 candidate.
    if len(clean) == 2:
        return clean.upper()

    upper = clean.upper()

    try:
        # Alpha-3.
        match = pycountry.countries.get(alpha_3=upper)
        if match:
            return match.alpha_2.upper()

        # Exact country name.
        match = pycountry.countries.get(name=clean)
        if match:
            return match.alpha_2.upper()

        # Fuzzy lookup is deliberately used only as a fallback.
        matches = pycountry.countries.search_fuzzy(clean)

        if matches:
            resolved = matches[0]

            # If multiple fuzzy results exist, make the ambiguity visible.
            if len(matches) > 1:
                logger.warning(
                    "[VISA_ENGINE] Ambiguous country resolution | input='%s' "
                    "| selected='%s' | candidates=%s",
                    clean,
                    resolved.alpha_2,
                    [m.alpha_2 for m in matches[:5]],
                )

            return resolved.alpha_2.upper()

    except Exception:
        logger.exception(
            "[VISA_ENGINE] pycountry resolution failed | input='%s'",
            clean,
        )

    logger.warning(
        "[VISA_ENGINE] Country could not be resolved to ISO-2 | input='%s' "
        "| fallback='%s'",
        clean,
        upper,
    )

    return upper


def _normalize_route(destination_countries: List[str]) -> List[str]:
    """
    Normalize destination country codes while preserving route order.

    Only consecutive duplicates are removed.

    Example:
        ["KE", "TZ", "TZ", "KE"] -> ["KE", "TZ", "KE"]

    A -> B -> A is intentionally preserved.
    """
    normalized: List[str] = []

    for value in destination_countries or []:
        code = _to_alpha2(value)

        if not code:
            continue

        if normalized and normalized[-1] == code:
            continue

        normalized.append(code)

    return normalized


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class VisaIntelligenceEngine:
    """
    Deterministic visa intelligence engine.

    Resolution precedence:

        1. Exact visa_requirements row
        2. Verified regional bloc applicable to the nationality
        3. Explicit unverified_no_data

    The engine never calculates or guesses requirement values.
    """

    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self,
        nationality: str,
        destination_countries: List[str],
    ) -> Dict[str, Any]:
        """
        Evaluate visa requirements for an ordered route.

        Exact nationality/destination records in visa_requirements are
        authoritative.

        If an exact row does not exist, a regional bloc is considered only
        when that nationality is already associated with the bloc through
        verified visa_requirements data.

        No answer is invented when neither source provides evidence.
        """
        nat_code = _to_alpha2(nationality)
        dest_codes = _normalize_route(destination_countries)

        logger.info(
            "[VISA_ENGINE] Starting check | nationality='%s' -> '%s' "
            "| destinations=%s -> %s",
            nationality,
            nat_code,
            destination_countries,
            dest_codes,
        )

        if not nat_code:
            logger.warning(
                "[VISA_ENGINE] Empty nationality supplied."
            )

        if not dest_codes:
            logger.warning(
                "[VISA_ENGINE] No valid destination countries supplied."
            )

            return {
                "nationality": nat_code,
                "countries": [],
                "bloc_exit_warning": None,
            }

        try:
            # Determine only blocs that are actually associated with this
            # nationality in the authoritative visa data.
            blocs = self._active_blocs_for(
                nationality=nat_code,
                destination_countries=dest_codes,
            )

            results: List[Dict[str, Any]] = []

            for country in dest_codes:
                exact_row = self._exact_requirement(
                    nationality=nat_code,
                    destination=country,
                )

                if exact_row is not None:
                    results.append(
                        self._verified_result(
                            country=country,
                            row=exact_row,
                        )
                    )
                    continue

                bloc_hit = self._bloc_for_country(
                    country=country,
                    blocs=blocs,
                )

                if bloc_hit is not None:
                    results.append(
                        self._bloc_result(
                            country=country,
                            bloc=bloc_hit,
                        )
                    )
                    continue

                results.append(
                    self._unverified_result(
                        nationality=nat_code,
                        country=country,
                    )
                )

            return {
                "nationality": nat_code,
                "countries": results,
                "bloc_exit_warning": self._bloc_exit_warning(
                    destination_countries=dest_codes,
                    blocs=blocs,
                ),
            }

        except Exception:
            logger.exception(
                "[VISA_ENGINE] Critical database/application failure | "
                "nationality='%s' | destinations=%s",
                nat_code,
                dest_codes,
            )
            raise

    # ------------------------------------------------------------------
    # Exact authoritative requirement
    # ------------------------------------------------------------------

    def _exact_requirement(
        self,
        nationality: str,
        destination: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch the authoritative nationality + destination requirement.

        There is a UNIQUE constraint on:
            (nationality_country, destination_country)

        Therefore at most one row should exist.
        """
        row = self.db.execute(
            text(
                """
                SELECT
                    requirement,
                    applicable_bloc_code,
                    fee_usd,
                    processing_days_typical,
                    notes,
                    source_url,
                    verified_date
                FROM visa_requirements
                WHERE nationality_country = :nat
                  AND destination_country = :dest
                LIMIT 1
                """
            ),
            {
                "nat": nationality,
                "dest": destination,
            },
        ).mappings().first()

        if row:
            logger.info(
                "[VISA_ENGINE] Exact match | %s -> %s | requirement=%s",
                nationality,
                destination,
                row["requirement"],
            )

            return dict(row)

        logger.info(
            "[VISA_ENGINE] No exact visa_requirements row | %s -> %s",
            nationality,
            destination,
        )

        return None

    # ------------------------------------------------------------------
    # Regional bloc resolution
    # ------------------------------------------------------------------

    def _active_blocs_for(
        self,
        nationality: str,
        destination_countries: List[str],
    ) -> List[Dict[str, Any]]:
        """
        Return verified blocs that are applicable to this nationality.

        Important:
        A bloc is NOT considered applicable merely because two destinations
        belong to it.

        The nationality must already be associated with that bloc through
        an existing visa_requirements row containing applicable_bloc_code.

        This prevents route geography from becoming an invented visa rule.
        """
        try:
            applicable_rows = self.db.execute(
                text(
                    """
                    SELECT DISTINCT applicable_bloc_code
                    FROM visa_requirements
                    WHERE nationality_country = :nat
                      AND applicable_bloc_code IS NOT NULL
                    """
                ),
                {
                    "nat": nationality,
                },
            ).fetchall()

            applicable_codes = {
                str(row[0]).strip()
                for row in applicable_rows
                if row[0]
            }

            if not applicable_codes:
                logger.info(
                    "[VISA_ENGINE] No verified regional bloc association "
                    "found for nationality='%s'",
                    nationality,
                )
                return []

            rows = self.db.execute(
                text(
                    """
                    SELECT
                        bloc_code,
                        name,
                        member_countries,
                        fee_usd,
                        validity_days,
                        invalidated_on_bloc_exit,
                        notes,
                        source_url,
                        verified_date
                    FROM regional_visa_blocs
                    WHERE bloc_code = ANY(:bloc_codes)
                    """
                ),
                {
                    "bloc_codes": list(applicable_codes),
                },
            ).fetchall()

            blocs: List[Dict[str, Any]] = []

            destination_set = set(destination_countries)

            for (
                bloc_code,
                name,
                member_countries,
                fee_usd,
                validity_days,
                invalidated_on_exit,
                notes,
                source_url,
                verified_date,
            ) in rows:
                members = [
                    _to_alpha2(member)
                    for member in (member_countries or [])
                    if _to_alpha2(member)
                ]

                overlap = [
                    country
                    for country in destination_countries
                    if country in members
                ]

                # A bloc is useful to this route only when it actually
                # contains at least one requested destination.
                if not destination_set.intersection(members):
                    continue

                bloc = {
                    "bloc_code": bloc_code,
                    "name": name,
                    "member_countries": members,
                    "fee_usd": (
                        float(fee_usd)
                        if fee_usd is not None
                        else None
                    ),
                    "validity_days": validity_days,
                    "invalidated_on_bloc_exit": invalidated_on_exit,
                    "notes": notes,
                    "source_url": source_url,
                    "verified_date": (
                        verified_date.isoformat()
                        if verified_date
                        else None
                    ),
                    "route_overlap": overlap,
                }

                blocs.append(bloc)

                logger.info(
                    "[VISA_ENGINE] Applicable bloc found | nationality='%s' "
                    "| bloc='%s' | route_overlap=%s",
                    nationality,
                    bloc_code,
                    overlap,
                )

            return blocs

        except Exception:
            logger.exception(
                "[VISA_ENGINE] Failed to load regional visa blocs | "
                "nationality='%s'",
                nationality,
            )

            # Unlike the previous implementation, a database failure is not
            # silently converted into "no bloc data".
            raise

    @staticmethod
    def _bloc_for_country(
        country: str,
        blocs: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """
        Return the first verified applicable bloc containing the country.

        Bloc order is deterministic because the database result is consumed
        in the order returned and the first matching bloc is selected.
        """
        for bloc in blocs:
            if country in bloc["member_countries"]:
                return bloc

        return None

    # ------------------------------------------------------------------
    # Result construction
    # ------------------------------------------------------------------

    @staticmethod
    def _verified_result(
        country: str,
        row: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Convert an authoritative visa_requirements row into API output.
        """
        verified_date = row.get("verified_date")

        result: Dict[str, Any] = {
            "country": country,
            "status": "verified",
            "requirement": row.get("requirement"),
            "covered_by_bloc": row.get("applicable_bloc_code"),
            "fee_usd": (
                float(row["fee_usd"])
                if row.get("fee_usd") is not None
                else None
            ),
            "processing_days_typical": row.get(
                "processing_days_typical"
            ),
            "notes": row.get("notes"),
            "source_url": row.get("source_url"),
            "verified_date": (
                verified_date.isoformat()
                if hasattr(verified_date, "isoformat")
                else verified_date
            ),
        }

        logger.info(
            "[VISA_ENGINE] Returning VERIFIED result | country='%s' "
            "| requirement='%s'",
            country,
            result["requirement"],
        )

        return result

    @staticmethod
    def _bloc_result(
        country: str,
        bloc: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Return a result backed by an applicable verified regional bloc.

        No requirement value is invented because the bloc itself may define
        coverage without a destination-specific requirement value.
        """
        logger.info(
            "[VISA_ENGINE] Returning VERIFIED_VIA_BLOC result | "
            "country='%s' | bloc='%s'",
            country,
            bloc["bloc_code"],
        )

        return {
            "country": country,
            "status": "verified_via_bloc",
            "requirement": None,
            "covered_by_bloc": bloc["bloc_code"],
            "bloc_name": bloc["name"],
            "fee_usd": bloc["fee_usd"],
            "validity_days": bloc["validity_days"],
            "notes": bloc["notes"],
            "source_url": bloc["source_url"],
            "verified_date": bloc["verified_date"],
        }

    @staticmethod
    def _unverified_result(
        nationality: str,
        country: str,
    ) -> Dict[str, Any]:
        """
        Explicitly represent missing evidence.

        This is not a visa determination.
        """
        logger.warning(
            "[VISA_ENGINE] No verified visa data | nationality='%s' "
            "| destination='%s'",
            nationality,
            country,
        )

        return {
            "country": country,
            "status": "unverified_no_data",
            "requirement": None,
            "notes": (
                "No verified visa requirement on file for this "
                "nationality/destination pair. Confirm directly with the "
                "destination country's embassy or official immigration "
                "portal before booking."
            ),
        }

    # ------------------------------------------------------------------
    # Bloc exit / re-entry warning
    # ------------------------------------------------------------------

    @staticmethod
    def _bloc_exit_warning(
        destination_countries: List[str],
        blocs: List[Dict[str, Any]],
    ) -> Optional[str]:
        """
        Detect an ordered route pattern:

            IN BLOC -> OUTSIDE BLOC -> IN BLOC

        when the verified bloc states that exiting invalidates the visa.

        Route order is authoritative and intentional repeats such as
        A -> B -> A are preserved.
        """
        if len(destination_countries) < 3:
            return None

        for bloc in blocs:
            if not bloc.get("invalidated_on_bloc_exit"):
                continue

            members = set(bloc["member_countries"])

            in_bloc_flags = [
                country in members
                for country in destination_countries
            ]

            for index in range(
                1,
                len(in_bloc_flags) - 1,
            ):
                if (
                    in_bloc_flags[index - 1]
                    and not in_bloc_flags[index]
                    and in_bloc_flags[index + 1]
                ):
                    outside_country = destination_countries[index]

                    logger.warning(
                        "[VISA_ENGINE] Bloc exit/re-entry detected | "
                        "bloc='%s' | outside_country='%s'",
                        bloc["bloc_code"],
                        outside_country,
                    )

                    return (
                        f"This route leaves the {bloc['name']} "
                        f"({bloc['bloc_code']}) bloc at "
                        f"{outside_country} and re-enters it afterward. "
                        f"A {bloc['bloc_code']} visa is invalidated "
                        "immediately on exiting the bloc — a fresh visa "
                        "would be required to re-enter, not just for the "
                        "excursion country."
                    )

        return None


__all__ = [
    "VisaIntelligenceEngine",
]
