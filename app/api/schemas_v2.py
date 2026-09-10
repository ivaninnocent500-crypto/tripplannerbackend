"""Response models — one per screen shown in the screenshots.

CHANGE LOG (this rewrite)
--------------------------------------------------------------------
Added DrawerOut.time_label.

Root cause being fixed: the mobile app was rendering activity times
as bare "16:00:00" strings with no AM/PM or day-part context, which
read to users as ambiguous or as if it were an unusual hour (see the
screenshots — "Game Drive 16:00:00" was misread as an evening/night
activity when it is actually 4:00 PM, a completely normal "late
afternoon game drive" slot by safari-industry convention).

start_time (raw "HH:MM:SS", 24-hour) is UNCHANGED and still present —
some callers may want the raw value for sorting/computation. time_label
is a new, additional field: a pre-formatted "6:00 AM" / "4:00 PM"
string, computed once here so every client renders the same thing
without re-implementing 24h->12h conversion or AM/PM logic themselves.

time_label is Optional and defaults to None when start_time is None,
matching start_time's own optionality -- never fabricated when no
time was scheduled.

This file only defines the schema. Populating time_label from the
persisted Drawer.start_time (a datetime.time) happens wherever a
DrawerOut is actually constructed (app/api/trip_v2.py's _day_to_dict
today builds plain dicts rather than DrawerOut instances -- wiring
that through, and/or adding the same formatting to the plain-dict
path, is a separate follow-up change, tracked but intentionally not
bundled into this file).
"""
from __future__ import annotations

from datetime import date, time as dt_time
from typing import Optional

from pydantic import BaseModel


def format_time_label(value: Optional[dt_time]) -> Optional[str]:
    """
    Formats a datetime.time as a human-readable 12-hour label, e.g.
    dt_time(6, 0) -> "6:00 AM", dt_time(16, 0) -> "4:00 PM".

    Returns None when value is None -- never fabricates a time that
    wasn't actually scheduled. This is the single source of truth for
    12-hour formatting so every caller (schema construction, any
    future serializer) produces an identical label instead of each
    re-implementing the same %I-strip-leading-zero logic slightly
    differently.
    """
    if value is None:
        return None

    # %I is zero-padded ("06:00 AM"); strip the leading zero for a
    # more natural mobile-UI label ("6:00 AM"), but leave 12 PM/AM
    # alone since "2:00 PM" style stripping only removes a leading
    # "0", never touching the "12" hour.
    label = value.strftime("%I:%M %p")
    if label.startswith("0"):
        label = label[1:]
    return label


class HealthResponse(BaseModel):
    status: str
    supabase_connected: bool
    legacy_db_connected: bool
    ai_gateway_enabled: bool


class DrawerOut(BaseModel):
    name: str
    description: Optional[str] = None
    start_time: Optional[str] = None
    # NEW: human-readable 12-hour label ("6:00 AM", "4:00 PM"), derived
    # from the same underlying value as start_time. Added so clients
    # never need to parse/reformat the raw "HH:MM:SS" string themselves
    # -- see format_time_label() above for the exact rule, and the
    # module docstring for why this was needed.
    time_label: Optional[str] = None
    duration_minutes: Optional[int] = None
    activity_type: str


class DayOut(BaseModel):
    day_number: int
    destination: str
    date: Optional[date] = None
    theme: Optional[str] = None
    activities: list[DrawerOut]
    accommodation: Optional[str] = None
    transport: Optional[str] = None
    meals: list[str] = []


class WhyItineraryFact(BaseModel):
    heading: str
    detail: str


class TripSummaryOut(BaseModel):
    cabinet_id: str
    title: str
    duration_days: int
    travelers: int
    style: list[str]
    dates: dict
    route: list[str]
    estimated_budget: dict
    status: str


class TripDetailOut(BaseModel):
    summary: TripSummaryOut
    days: list[DayOut]
    why_itinerary: list[WhyItineraryFact]


class OperatorMatchOut(BaseModel):
    tour_operator_id: str
    name: str
    trip_match_pct: int
    badge: Optional[str] = None
    strengths: list[str]
    estimated_price_pp: Optional[float] = None


class QuoteTrackingOut(BaseModel):
    requests_sent: int
    quotes_received: int
    awaiting_response: int
    benches: list[dict]


class QuoteComparisonOut(BaseModel):
    quotes: list[dict]
    best_value_bench_id: Optional[str]
    best_fit_bench_id: Optional[str]


class BookingOut(BaseModel):
    confirmation_code: str
    trip_title: str
    operator_name: str
    dates: dict
    travelers: int
    price_per_person: float
    total_price: float
    deposit_amount: Optional[float]
    status: str


__all__ = [
    "format_time_label",
    "HealthResponse",
    "DrawerOut",
    "DayOut",
    "WhyItineraryFact",
    "TripSummaryOut",
    "TripDetailOut",
    "OperatorMatchOut",
    "QuoteTrackingOut",
    "QuoteComparisonOut",
    "BookingOut",
]
