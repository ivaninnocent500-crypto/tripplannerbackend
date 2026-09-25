"""
Trip lifecycle routes — the entire backend contract for the app screens:
generate -> match operators -> request/track/compare quotes -> book ->
confirm. One endpoint per screen, all backed by persisted Cabinet state
(no ephemeral in-memory response — see app/db/models_furniture.py).

Each engine call is wrapped in call_engine() (app/engines/resilience.py),
same pattern your original orchestrator.py used: failures degrade
gracefully into an EngineResult with .degraded=True rather than crashing
the whole request. Every call_engine(...) here passes db=db so a caught
engine failure rolls back the session — without that, a failed insert
mid-engine leaves the transaction aborted and every subsequent query in
the same request (including the route's own db.commit()) fails too,
turning one soft "degraded" result into a hard 500.

CHANGE LOG (this rewrite — day_kind actually added to _day_to_dict())
--------------------------------------------------------------------
6. DAY_KIND ADDED TO _day_to_dict() — confirmed via production device
   logs (Android crash: NullPointerException on DayKind.ordinal() at
   CabinetDayScreen.kt:269) that "day_kind" was still absent from
   every day object in the live deployed response, despite an earlier
   patch draft for this exact fix. This revision actually applies it:
   _day_to_dict() now always emits "day_kind", read via
   getattr(shelf, "day_kind", "STANDARD") — the same safe-default
   pattern already used for this exact attribute in
   ItineraryOrchestrator._apply_day_themes() (app/engines/itinerary_v2.py).
   The key is now unconditionally present in the dict (never omitted),
   which is what actually matters for the Android side: Gson only
   fails to apply its Kotlin default when a JSON key is missing
   entirely, so as long as this dict always emits "day_kind" — even as
   "STANDARD" in the worst case — the Android app will never receive a
   null dayKind from this endpoint again. (The Android app has also
   been hardened independently to treat a null/missing value as
   STANDARD, so this fix is defense in depth, not the only thing
   preventing a crash.)

CHANGE LOG (previous rewrite — adds trip-level map data to GET /{cabinet_id})
--------------------------------------------------------------------
5. MAP DATA ADDED TO GET /{cabinet_id} — the Android Map tab
   (OSMMapScreen) previously had nothing to render from the API and
   was hardcoded to a fixed Serengeti coordinate. get_trip() now
   re-runs RouteGeographyEngine.analyze(cabinet.route_destination_ids)
   — the same engine ItineraryOrchestrator already ran once during
   generation — and returns its stops/legs as a new "map" field.

   CONFIRMED WORKING end-to-end against the live deployment (device
   logs show a fully populated "map" object with resolved stops,
   coordinates, and honest route_available=false legs where no
   measured transport data exists). No changes made to this section in
   this revision — kept exactly as previously verified.

   This is a deliberate re-run, not a persisted read, because
   RouteAnalysis is never written to the Cabinet; it exists only
   transiently inside ItineraryOrchestrator.generate() (see
   app/engines/itinerary_v2.py). Re-running it here is cheap: it is
   DB reads (travel_places, physical_geography, flights,
   border_crossings) plus haversine math, no external calls, no LLM.

   Chosen as an inline field on GET /{cabinet_id} rather than a
   separate GET /{cabinet_id}/map endpoint, matching how "days" and
   "why_itinerary" already work on this same route — one call, no
   extra round trip for a screen that already fetches the trip.

   Not added to generate_trip()'s response: the orchestrator already
   computed and discarded a RouteAnalysis during generation, and the
   app calls getTrip() fresh after generation completes (matching the
   existing pattern where generate_trip() also omits "route",
   "estimated_budget", and "dates" — those are getTrip()-only too).

   allow_coordinate_estimate is left at its default (False) here: the
   map should show real measured/flight-table facts or an honest gap,
   never a haversine-derived "road" that could visually mislead a
   traveler about how they'll actually get from A to B. If a future
   product decision wants estimated legs plotted (dashed line, clearly
   labelled), that is a one-line change to the analyze() call below —
   not something to guess at silently.

CHANGE LOG (previous rewrite — consolidates patches 001/005/009/010)
--------------------------------------------------------------------
1. NATIONALITY KEY MISMATCH FIXED — the previous version of this file
   read `request["nationality"]`, but GenerateTripV2RequestDto (the
   Android side) sends the JSON key "traveler_nationality" via
   @SerializedName. Two different dict keys meant nationality was
   silently dropped on every trip generation — request.get() returns
   None instead of raising, so nothing ever surfaced this. Fixed below
   to read request.get("traveler_nationality").

2. COLUMN NAME RECONCILED — the previous version wrote to
   `cabinet.nationality`, but migration 005
   (schema/005_multi_country_and_visa_FIXED.sql, already run
   successfully) created the column as
   `cabinets.traveler_nationality` (the `world_country_code` domain,
   not the closed African-only `country_code` enum — see that
   migration's own docstring for why). This file now uses
   `cabinet.traveler_nationality` throughout, matching the real DB
   column. See the CO-REQUISITE note below — models_furniture.py's
   Cabinet class must expose this as an ORM attribute for these lines
   to work; if it doesn't yet, add it there too (exact line given
   below).

3. destination_slug / destination_name RESTORED on _day_to_dict() —
   needed by the Android Day-by-Day screen's accommodation-image
   carousel (GET /api/places/{slug}/images) and its LOCATION summary
   row. A prior edit reverted this function to a single-arg signature
   with neither field; restored here with both, resolved via one
   extra query per shelf (at most ~21 per request — a trip's day count
   is capped at 21 by the app's wizard slider, so this is not worth
   batching).

4. Everything else (generate_trip's orchestration pipeline,
   match_operators, request_quotes, track_quotes, compare_quotes,
   book_trip, confirm_booking, _wardrobe_to_dict) is UNCHANGED from
   the version you're running — no bugs found in those paths.

CHANGE LOG (production incident fix — pre-existing, unchanged by this
rewrite)
--------------------------------------------------------------------
generate_trip() and get_trip() previously called ItineraryPlanningEngine
and ValidationEngine/ExplanationEngine directly and separately, which
never ran RouteGeographyEngine, DayArchetypeEngine, or
ScheduleRepairEngine at all. Both routes now call
ItineraryOrchestrator.generate() (app/engines/itinerary_v2.py) instead,
which runs the full RulesEngine -> RouteGeographyEngine ->
DayArchetypeEngine -> ItineraryPlanningEngine -> ScheduleRepairEngine ->
ValidationEngine pipeline and already performs the ValidationEngine
call as its own Stage 6 — so validation_result is read from the
orchestrator's result rather than called a second time here.

ExplanationEngine is unaffected by this — it is still called directly
here (both in generate_trip and get_trip) since it is not part of
ItineraryOrchestrator's pipeline; it operates on the already-persisted
Cabinet independently of how that Cabinet was built.

--------------------------------------------------------------------
CO-REQUISITE — models_furniture.py (not included in this file; edit
separately)
--------------------------------------------------------------------
The Cabinet ORM class must declare traveler_nationality as a Column,
matching migration 005's DB column, or every `cabinet.traveler_nationality`
reference below will raise AttributeError. Add this line to Cabinet's
column list in models_furniture.py, alongside its other Column(...)
declarations (e.g. near `primary_country = Column(Text)`):

    traveler_nationality = Column(Text)

Plain Text is correct here (not a Postgres-level enum type on the ORM
side) — the DB enforces the `^[A-Z]{2}$` format via the
world_country_code domain's CHECK constraint; the ORM column just
needs to read/write that string.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.db.session import get_supabase_db
from app.db.destinations import resolve_slugs_to_ids
from app.db.models_furniture import Bench, Cabinet, Counter, Wardrobe
from app.engines.resilience import call_engine
from app.engines.itinerary_v2 import ItineraryOrchestrator
from app.engines.explanation import ExplanationEngine
from app.engines.operator_match_v2 import OperatorMatchEngine
from app.engines.quote_engine import QuoteEngine
from app.engines.booking_engine import BookingEngine
from app.engines.visa_engine import VisaIntelligenceEngine
from app.engines.route_geography import RouteAnalysis, RouteGeographyEngine

# Attach to uvicorn's active handler so messages stream to Render stdout
logger = logging.getLogger("uvicorn.error")

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trips", tags=["trips"])


def _get_cabinet_or_404(db: Session, cabinet_id: str) -> Cabinet:
    cabinet = db.get(Cabinet, cabinet_id)
    if not cabinet:
        logger.warning("Cabinet ID %s not found in database", cabinet_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip not found")
    return cabinet


def _operator_summary(db: Session, tour_operator_id) -> dict:
    row = db.execute(
        text("select name, years_in_operation, headquarters_country, verification_status "
             "from tour_operators where id = :id"),
        {"id": tour_operator_id},
    ).fetchone()
    if not row:
        return {"name": None, "years_in_operation": None, "headquarters_country": None, "verification_status": None}
    return {"name": row[0], "years_in_operation": row[1], "headquarters_country": row[2], "verification_status": row[3]}


# ---------------------------------------------------------------------
# MAP SERIALIZATION
# ---------------------------------------------------------------------

def _route_analysis_to_map_dict(analysis: RouteAnalysis) -> dict:
    """
    Trip-level map payload for the Android Map tab (OSMMapScreen).

    Deliberately a narrower projection than
    route_geography.route_analysis_to_dict(): that function's full
    output includes `alternatives`, `raw`, per-leg `warnings`, and
    engine/version metadata meant for generation logs and debugging,
    none of which the map needs to render markers and polylines. This
    keeps the response small and keeps the map's contract (stops +
    legs, nothing else) stable even if route_analysis_to_dict()'s
    debug shape changes later.

    NO-FABRICATION RULE (inherited from RouteGeographyEngine): a leg
    with unknown transport is serialized with mode="unknown",
    distance_km=None, duration_minutes=None, route_available=False.
    Android must render that as a plain geographic connection, never
    as "duration unavailable" or a fabricated transport label — see
    ItineraryTheme/CabinetDayScreen's existing handling of the same
    rule for transit days.
    """
    stops = [
        {
            "destination_id": stop.destination_id,
            "destination_slug": None, # resolved separately below
            "destination_name": stop.name,
            "latitude": stop.point.latitude,
            "longitude": stop.point.longitude,
            "stop_order": stop.index,
            "resolved": stop.resolved,
        }
        for stop in analysis.stops
    ]

    legs = [
        {
            "from_destination_id": leg.from_destination_id,
            "to_destination_id": leg.to_destination_id,
            "sequence": leg.sequence,
            "transport_mode": leg.mode if not leg.is_unavailable else None,
            "duration_minutes": leg.duration_minutes,
            "distance_km": leg.distance_km,
            "estimated": leg.estimated,
            "is_inter_country": leg.is_inter_country,
            "requires_border_crossing": leg.requires_border_crossing,
            "route_available": not leg.is_unavailable,
        }
        for leg in analysis.legs
    ]

    return {"stops": stops, "legs": legs}


def _attach_destination_slugs(db: Session, map_dict: dict) -> dict:
    """
    RouteGeographyEngine resolves destination identity from
    travel_places but does not carry `slug` (it wasn't part of that
    engine's contract — see its _fetch_destinations query, which
    selects name/country/destination_type/coordinates only). The
    Android map screen keys its own place-detail lookups
    (GET /api/places/{slug}/images) on slug the same way the Day-by-Day
    screen's _day_to_dict() does, so slugs are resolved here with the
    same one-query-per-stop pattern _day_to_dict() already uses for
    the same reason — trip stop counts are small (capped by the same
    21-day wizard limit that bounds shelf count).
    """
    for stop in map_dict["stops"]:
        if not stop["destination_id"]:
            continue
        row = db.execute(
            text("SELECT slug FROM travel_places WHERE id = :id"),
            {"id": stop["destination_id"]},
        ).fetchone()
        if row:
            stop["destination_slug"] = row.slug
    return map_dict


# ---------------------------------------------------------------------
@router.post("/generate")
def generate_trip(request: dict, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    logger.info("Executing POST /generate with payload: %s", request)

    slug_to_id = resolve_slugs_to_ids(db, request["destinations"])
    ordered_ids = [slug_to_id[s] for s in request["destinations"] if s in slug_to_id]
    unmatched = [s for s in request["destinations"] if s not in slug_to_id]
    if not ordered_ids:
        logger.error("Failed to resolve destination slugs for request: %s", request["destinations"])
        raise HTTPException(422, "None of the requested destinations could be resolved.")

    # Runs the full pipeline via ItineraryOrchestrator (RulesEngine ->
    # RouteGeographyEngine -> DayArchetypeEngine -> ItineraryPlanningEngine
    # -> ScheduleRepairEngine -> ValidationEngine). ValidationEngine is
    # the orchestrator's own Stage 6, so it is NOT called again below.
    orchestration_result = call_engine(
        "ItineraryOrchestrator",
        lambda: ItineraryOrchestrator(db).generate(request, ordered_ids),
        fallback=None, db=db,
    )
    if orchestration_result.value is None:
        logger.error("ItineraryOrchestrator execution returned None")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Trip generation failed unexpectedly.")

    generation_result = orchestration_result.value

    if generation_result.cabinet is None:
        # RulesEngine failed fast (Stage 1) or RouteGeographyEngine
        # could not resolve any destination (Stage 2) -- no Cabinet
        # was ever persisted. Genuine request-validation failure, not
        # a degraded/partial result, so surfaced as 422.
        logger.warning(
            "ItineraryOrchestrator did not produce a cabinet: %s",
            generation_result.warnings,
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"message": "Trip could not be generated from this request.", "warnings": generation_result.warnings},
        )

    cabinet = generation_result.cabinet

    # FIXED: reads "traveler_nationality" — the JSON key
    # GenerateTripV2RequestDto.travelerNationality actually sends via
    # @SerializedName("traveler_nationality"). Previously read
    # "nationality", a key the app never sends, so this silently never
    # persisted. Written to cabinet.traveler_nationality, matching
    # migration 005's real column name (see CO-REQUISITE note above).
    if request.get("traveler_nationality"):
        cabinet.traveler_nationality = request["traveler_nationality"].strip().upper()
        db.add(cabinet)

    if generation_result.rules_result and not generation_result.rules_result.get("validated", True):
        logger.warning("Rules validation did not pass: %s", generation_result.rules_result)

    explanation_result = call_engine(
        "ExplanationEngine",
        lambda: ExplanationEngine().explain(cabinet),
        fallback={"facts": [], "generated_by": "unavailable"}, db=db,
    )

    db.commit()

    logger.info("Trip successfully generated with Cabinet ID: %s", cabinet.id)

    return {
        "cabinet_id": str(cabinet.id),
        "status": cabinet.status,
        "validation": generation_result.validation_result,
        "why_itinerary": explanation_result.value["facts"],
        "days": [_day_to_dict(db, s) for s in cabinet.shelves],
        "generation_meta": {
            "degraded": (
                orchestration_result.degraded
                or explanation_result.degraded
                or bool(generation_result.warnings)
            ),
            "unmatched_destinations": unmatched,
            "warnings": generation_result.warnings,
        },
    }


@router.get("/{cabinet_id}")
def get_trip(cabinet_id: str, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    logger.info("Executing GET /%s", cabinet_id)
    cabinet = _get_cabinet_or_404(db, cabinet_id)
    explanation_result = call_engine(
        "ExplanationEngine", lambda: ExplanationEngine().explain(cabinet), fallback={"facts": []}, db=db,
    )

    # Map data: re-run RouteGeographyEngine over the persisted route.
    # See _route_analysis_to_map_dict()'s docstring above for why this
    # is a re-run rather than a persisted read, and why
    # allow_coordinate_estimate is left False. Wrapped in call_engine
    # like every other engine call in this file so a geography failure
    # degrades to an empty map rather than failing the whole trip
    # fetch — the Overview/Days tabs must keep working even if the Map
    # tab can't. CONFIRMED WORKING against the live deployment.
    map_result = call_engine(
        "RouteGeographyEngine.analyze",
        lambda: RouteGeographyEngine(db).analyze(
            [str(x) for x in cabinet.route_destination_ids],
            allow_coordinate_estimate=False,
        ),
        fallback=RouteAnalysis(), db=db,
    )
    map_dict = _attach_destination_slugs(db, _route_analysis_to_map_dict(map_result.value))

    return {
        "cabinet_id": str(cabinet.id),
        "title": cabinet.title,
        "status": cabinet.status,
        "duration_days": cabinet.duration_days,
        "travelers": cabinet.travelers_adults + cabinet.travelers_children,
        "style": cabinet.travel_style,
        "route": [str(x) for x in cabinet.route_destination_ids],
        "dates": {"start": cabinet.start_date, "end": cabinet.end_date},
        "estimated_budget": {"low": cabinet.estimated_budget_low, "high": cabinet.estimated_budget_high},
        "days": [_day_to_dict(db, s) for s in cabinet.shelves],
        "why_itinerary": explanation_result.value["facts"],
        "map": map_dict,
    }


# ---------------------------------------------------------------------
@router.post("/{cabinet_id}/match-operators")
def match_operators(cabinet_id: str, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    logger.info("Executing POST /%s/match-operators", cabinet_id)
    cabinet = _get_cabinet_or_404(db, cabinet_id)
    match_result = call_engine(
        "OperatorMatchEngine", lambda: OperatorMatchEngine(db).match(cabinet), fallback=[], db=db,
    )
    cabinet.status = "matching"
    db.add(cabinet)
    db.commit()

    matches = []
    for s in match_result.value:
        summary = _operator_summary(db, s.tour_operator_id)
        matches.append({
            "tour_operator_id": str(s.tour_operator_id),
            "operator_name": summary["name"],
            "years_in_operation": summary["years_in_operation"],
            "headquarters_country": summary["headquarters_country"],
            "verification_status": summary["verification_status"],
            "trip_match_pct": s.trip_match_pct,
            "confidence_pct": s.confidence_pct,
            "country_coverage_pct": s.country_coverage_pct,
            "has_placeholder_subscores": s.has_placeholder_subscores,
            "badge": s.badge,
            "strengths": s.strengths,
            "estimated_price_pp": float(s.estimated_price_pp) if s.estimated_price_pp else None,
        })

    logger.info("Matched %d operators for cabinet: %s", len(matches), cabinet_id)
    return {"degraded": match_result.degraded, "matches": matches}


# ---------------------------------------------------------------------
@router.post("/{cabinet_id}/quotes/request")
def request_quotes(cabinet_id: str, body: dict, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    logger.info("Executing POST /%s/quotes/request with body: %s", cabinet_id, body)
    cabinet = _get_cabinet_or_404(db, cabinet_id)
    result = call_engine(
        "QuoteEngine.request_quotes",
        lambda: QuoteEngine(db).request_quotes(cabinet, body["tour_operator_ids"], body.get("note")),
        fallback=[], db=db,
    )
    db.commit()
    logger.info("Quotes requested successfully for cabinet: %s", cabinet_id)
    return {"degraded": result.degraded, "benches": [str(b.id) for b in result.value], "status": "request_sent"}


@router.get("/{cabinet_id}/quotes")
def track_quotes(cabinet_id: str, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    logger.info("Executing GET /%s/quotes", cabinet_id)
    cabinet = _get_cabinet_or_404(db, cabinet_id)
    result = call_engine(
        "QuoteEngine.tracking_summary", lambda: QuoteEngine(db).tracking_summary(cabinet),
        fallback={"requests_sent": 0, "quotes_received": 0, "awaiting_response": 0, "benches": []}, db=db,
    )
    return result.value


@router.get("/{cabinet_id}/quotes/compare")
def compare_quotes(cabinet_id: str, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    logger.info("Executing GET /%s/quotes/compare", cabinet_id)
    cabinet = _get_cabinet_or_404(db, cabinet_id)
    result = call_engine(
        "QuoteEngine.compare", lambda: QuoteEngine(db).compare(cabinet),
        fallback={"quotes": [], "best_value_bench_id": None, "best_fit_bench_id": None}, db=db,
    )
    return result.value


# ---------------------------------------------------------------------
@router.get("/{cabinet_id}/visa-info")
def get_visa_info(
    cabinet_id: str, nationality: str | None = Query(None),
    db: Session = Depends(get_supabase_db), _=Depends(require_api_key),
):
    """
    Query param `nationality` is optional (ISO-2, e.g. `US`). When
    omitted, falls back to cabinet.traveler_nationality — the value
    captured during trip generation (see generate_trip() above).
    Returns 422 if neither is available: this engine never guesses a
    nationality to check against.

    Response is always one of: a verified requirement, verified bloc
    coverage, or an explicit unverified flag — see VisaIntelligenceEngine's
    docstring for why it never guesses either.
    """
    cabinet = _get_cabinet_or_404(db, cabinet_id)
    nationality = (nationality or cabinet.traveler_nationality or "").strip().upper()
    if not nationality:
        raise HTTPException(422, "Nationality is required (pass query param or set on trip generation).")

    logger.info("Executing GET /%s/visa-info for nationality: %s", cabinet_id, nationality)
    destination_countries = list(cabinet.route_countries or [])
    if not destination_countries:
        logger.error("Cabinet %s has no route_countries recorded", cabinet_id)
        raise HTTPException(422, "This trip has no route_countries recorded — regenerate the itinerary first.")

    result = call_engine(
        "VisaIntelligenceEngine",
        lambda: VisaIntelligenceEngine(db).check(nationality, destination_countries),
        fallback={"nationality": nationality, "countries": [], "bloc_exit_warning": None}, db=db,
    )
    return result.value


# ---------------------------------------------------------------------
@router.post("/{cabinet_id}/book")
def book_trip(cabinet_id: str, body: dict, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    logger.info("Executing POST /%s/book with body: %s", cabinet_id, body)
    cabinet = _get_cabinet_or_404(db, cabinet_id)
    counter = db.get(Counter, body["counter_id"])
    if not counter:
        logger.warning("Counter ID %s not found for booking", body.get("counter_id"))
        raise HTTPException(404, "Quote not found")

    result = call_engine(
        "BookingEngine.create_booking", lambda: BookingEngine(db).create_booking(cabinet, counter), fallback=None, db=db,
    )
    if result.value is None:
        logger.error("BookingEngine.create_booking returned None for cabinet: %s", cabinet_id)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Booking failed unexpectedly.")

    db.commit()
    logger.info("Booking created successfully with Wardrobe ID: %s", result.value.id)
    return _wardrobe_to_dict(db, result.value)


@router.post("/bookings/{wardrobe_id}/confirm")
def confirm_booking(wardrobe_id: str, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    logger.info("Executing POST /bookings/%s/confirm", wardrobe_id)
    wardrobe = db.get(Wardrobe, wardrobe_id)
    if not wardrobe:
        logger.warning("Wardrobe ID %s not found for confirmation", wardrobe_id)
        raise HTTPException(404, "Booking not found")

    result = call_engine(
        "BookingEngine.confirm_booking", lambda: BookingEngine(db).confirm_booking(wardrobe), fallback=None, db=db,
    )
    if result.value is None:
        logger.error("BookingEngine.confirm_booking returned None for wardrobe: %s", wardrobe_id)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Confirmation failed unexpectedly.")

    db.commit()
    logger.info("Booking confirmed successfully for Wardrobe ID: %s", wardrobe_id)
    return _wardrobe_to_dict(db, wardrobe)


# ---------------------------------------------------------------------
def _wardrobe_to_dict(db: Session, wardrobe: Wardrobe) -> dict:
    """
    Full payload for the "Your safari is ready" / "Booking confirmed"
    screens — trip title, operator name, dates, travelers,
    accommodation, transport, price, deposit, status.
    """
    cabinet = wardrobe.cabinet
    operator_row = db.execute(
        text("select name from tour_operators where id = :id"),
        {"id": wardrobe.tour_operator_id},
    ).fetchone()
    operator_name = operator_row[0] if operator_row else None

    first_shelf = cabinet.shelves[0] if cabinet.shelves else None
    accommodation = None
    transport = None
    if first_shelf:
        nights_total = sum(h.nights for s in cabinet.shelves for h in s.headboards)
        tier = first_shelf.headboards[0].tier if first_shelf.headboards else None
        accommodation = f"{(tier or 'Standard').title()} lodge · {nights_total} nights" if nights_total else None
        transport = first_shelf.armrests[0].description.split(" · ")[0] if first_shelf.armrests else None

    return {
        "wardrobe_id": str(wardrobe.id),
        "confirmation_code": wardrobe.confirmation_code,
        "trip_title": cabinet.title,
        "operator_name": operator_name,
        "dates": {"start": cabinet.start_date, "end": cabinet.end_date},
        "travelers": cabinet.travelers_adults + cabinet.travelers_children,
        "accommodation": accommodation,
        "transport": transport,
        "price_per_person": float(wardrobe.price_per_person),
        "total_price": float(wardrobe.total_price),
        "deposit_amount": float(wardrobe.deposit_amount) if wardrobe.deposit_amount else None,
        "status": wardrobe.status,
    }


# ---------------------------------------------------------------------
def _day_to_dict(db: Session, shelf) -> dict:
    """
    RESTORED: destination_slug + destination_name, resolved from
    travel_places via shelf.destination_id. Needed by the Android
    Day-by-Day screen's accommodation-image carousel
    (GET /api/places/{slug}/images, keyed on destination_slug) and its
    LOCATION summary row (destination_name). One extra single-row
    query per shelf — a trip is capped at 21 days by the app's wizard
    slider, so this is not worth batching into an IN-query.

    FIX APPLIED (this revision) — "day_kind" added below. Confirmed
    via production device logs that this key was absent from every
    day object in the live deployed response, which crashed the
    Android app's CabinetDayScreen (Gson does not apply Kotlin default
    parameter values for a missing JSON key — see the Android-side fix
    in TripV2Dtos.kt / CabinetDayScreen.kt for the full explanation).
    Read via getattr with a "STANDARD" default, matching the exact
    pattern ItineraryOrchestrator._apply_day_themes() already uses for
    this same attribute in itinerary_v2.py, so shelf objects that
    predate this column (if any) don't raise AttributeError. The key
    is now unconditionally present in the returned dict.
    """
    destination_slug = None
    destination_name = None
    if shelf.destination_id:
        place_row = db.execute(
            text("SELECT slug, name FROM travel_places WHERE id = :id"),
            {"id": shelf.destination_id},
        ).fetchone()
        if place_row:
            destination_slug = place_row.slug
            destination_name = place_row.name

    return {
        "day": shelf.day_number,
        "date": shelf.date,
        "destination_id": str(shelf.destination_id) if shelf.destination_id else None,
        "destination_slug": destination_slug,
        "destination_name": destination_name,
        "theme": shelf.theme,
        # NEW — always present, so Gson never sees a missing key here.
        "day_kind": str(getattr(shelf, "day_kind", "STANDARD") or "STANDARD").upper(),
        "activities": [
            {"name": d.name, "description": d.description, "start_time": str(d.start_time) if d.start_time else None,
             "duration_minutes": d.duration_minutes, "type": d.activity_type}
            for d in sorted(shelf.drawers, key=lambda x: x.sort_order)
        ],
        "accommodation": shelf.headboards[0].name if shelf.headboards else None,
        "transport": shelf.armrests[0].description if shelf.armrests else None,
        "meals": [t.meal_type for t in shelf.trays if t.included],
    }

