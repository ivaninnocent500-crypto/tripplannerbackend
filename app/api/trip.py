"""
New routes for the trip-instance lifecycle. These sit alongside your
existing app/api/trip.py (POST /api/itineraries/generate) rather than
replacing it outright — see GAP_ANALYSIS.md for the recommended
migration path (point the Android app at these once verified, then
retire the old ephemeral response shape).

I don't have your app/api/auth.py or app/db/session.py contents beyond
what's imported in your existing trip.py, so `require_api_key` /
`get_supabase_db` below are assumed to have the same signatures you
already use.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.db.session import get_supabase_db
from app.db.destinations import resolve_slugs_to_ids
from app.db.models_furniture import Bench, Cabinet, Counter, Wardrobe
from app.engines.itinerary_v2 import ItineraryPlanningEngine
from app.engines.validation import ValidationEngine
from app.engines.explanation import ExplanationEngine
from app.engines.operator_match_v2 import OperatorMatchEngine
from app.engines.quote_engine import QuoteEngine
from app.engines.booking_engine import BookingEngine

router = APIRouter(prefix="/api/trips", tags=["trips-v2"])


def _get_cabinet_or_404(db: Session, cabinet_id: str) -> Cabinet:
    cabinet = db.get(Cabinet, cabinet_id)
    if not cabinet:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip not found")
    return cabinet


# ---------------------------------------------------------------------
@router.post("/generate")
def generate_trip(request: dict, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    slug_to_id = resolve_slugs_to_ids(db, request["destinations"])
    ordered_ids = [slug_to_id[s] for s in request["destinations"] if s in slug_to_id]
    if not ordered_ids:
        raise HTTPException(422, "None of the requested destinations could be resolved.")

    result = ItineraryPlanningEngine(db).build(request, ordered_ids)
    validation = ValidationEngine(db).validate(result.cabinet)
    why = ExplanationEngine().explain(result.cabinet)
    db.commit()

    return {
        "cabinet_id": str(result.cabinet.id),
        "status": result.cabinet.status,
        "validation": validation,
        "why_itinerary": why["facts"],
        "days": [_day_to_dict(s) for s in result.cabinet.shelves],
    }


@router.get("/{cabinet_id}")
def get_trip(cabinet_id: str, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    cabinet = _get_cabinet_or_404(db, cabinet_id)
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
        "days": [_day_to_dict(s) for s in cabinet.shelves],
        "why_itinerary": ExplanationEngine().explain(cabinet)["facts"],
    }


# ---------------------------------------------------------------------
@router.post("/{cabinet_id}/match-operators")
def match_operators(cabinet_id: str, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    cabinet = _get_cabinet_or_404(db, cabinet_id)
    stools = OperatorMatchEngine(db).match(cabinet)
    cabinet.status = "matching"
    db.add(cabinet)
    db.commit()
    return {"matches": [
        {
            "tour_operator_id": str(s.tour_operator_id),
            "trip_match_pct": s.trip_match_pct,
            "badge": s.badge,
            "strengths": s.strengths,
            "estimated_price_pp": float(s.estimated_price_pp) if s.estimated_price_pp else None,
        } for s in stools
    ]}


# ---------------------------------------------------------------------
@router.post("/{cabinet_id}/quotes/request")
def request_quotes(cabinet_id: str, body: dict, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    cabinet = _get_cabinet_or_404(db, cabinet_id)
    benches = QuoteEngine(db).request_quotes(cabinet, body["tour_operator_ids"], body.get("note"))
    db.commit()
    return {"benches": [str(b.id) for b in benches], "status": "request_sent"}


@router.get("/{cabinet_id}/quotes")
def track_quotes(cabinet_id: str, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    cabinet = _get_cabinet_or_404(db, cabinet_id)
    return QuoteEngine(db).tracking_summary(cabinet)


@router.get("/{cabinet_id}/quotes/compare")
def compare_quotes(cabinet_id: str, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    cabinet = _get_cabinet_or_404(db, cabinet_id)
    return QuoteEngine(db).compare(cabinet)


# ---------------------------------------------------------------------
@router.post("/{cabinet_id}/book")
def book_trip(cabinet_id: str, body: dict, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    cabinet = _get_cabinet_or_404(db, cabinet_id)
    counter = db.get(Counter, body["counter_id"])
    if not counter:
        raise HTTPException(404, "Quote not found")
    wardrobe = BookingEngine(db).create_booking(cabinet, counter)
    db.commit()
    return {"wardrobe_id": str(wardrobe.id), "confirmation_code": wardrobe.confirmation_code,
            "total_price": float(wardrobe.total_price), "deposit_amount": float(wardrobe.deposit_amount),
            "status": wardrobe.status}


@router.post("/bookings/{wardrobe_id}/confirm")
def confirm_booking(wardrobe_id: str, db: Session = Depends(get_supabase_db), _=Depends(require_api_key)):
    wardrobe = db.get(Wardrobe, wardrobe_id)
    if not wardrobe:
        raise HTTPException(404, "Booking not found")
    BookingEngine(db).confirm_booking(wardrobe)
    db.commit()
    return {"confirmation_code": wardrobe.confirmation_code, "status": wardrobe.status}


# ---------------------------------------------------------------------
def _day_to_dict(shelf) -> dict:
    return {
        "day": shelf.day_number,
        "date": shelf.date,
        "destination_id": str(shelf.destination_id) if shelf.destination_id else None,
        "theme": shelf.theme,
        "activities": [
            {"name": d.name, "description": d.description, "start_time": str(d.start_time) if d.start_time else None,
             "duration_minutes": d.duration_minutes, "type": d.activity_type}
            for d in sorted(shelf.drawers, key=lambda x: x.sort_order)
        ],
        "accommodation": shelf.headboards[0].name if shelf.headboards else None,
        "transport": shelf.armrests[0].description if shelf.armrests else None,
        "meals": [t.meal_type for t in shelf.trays if t.included],
    }
