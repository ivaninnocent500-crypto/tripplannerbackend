"""
ORM models for the trip-instance layer (schema/002_furniture_schema.sql).

These sit alongside app/db/models_v2.py (the knowledge-base models:
TravelPlace, Activity, Lodge, TourOperator, ...) without touching it.
Import TravelPlace/Lodge/TourOperator from models_v2 for the FK
relationships below — adjust the import path if your actual class
names differ (I don't have models_v2.py's contents; see the note at
the end of this delivery).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    ARRAY, Boolean, Column, Date, DateTime, ForeignKey, Integer,
    Numeric, String, Text, Time, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.db.base import Base  # adjust if your declarative base lives elsewhere


def _uuid_col(**kw):
    return Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **kw)


class Cabinet(Base):
    """A generated trip. Formerly ephemeral — this is the persisted Trip ID."""
    __tablename__ = "cabinets"

    id = _uuid_col()
    request_json = Column(JSONB, nullable=False)
    title = Column(Text, nullable=False)
    duration_days = Column(Integer, nullable=False)
    travelers_adults = Column(Integer, nullable=False, default=1)
    travelers_children = Column(Integer, nullable=False, default=0)
    travel_style = Column(ARRAY(Text), nullable=False, default=list)
    budget_tier = Column(Text)
    status = Column(Text, nullable=False, default="draft")
    start_date = Column(Date)
    end_date = Column(Date)
    estimated_budget_low = Column(Numeric(10, 2))
    estimated_budget_high = Column(Numeric(10, 2))
    currency = Column(Text, nullable=False, default="USD")
    primary_destination_id = Column(UUID(as_uuid=True), ForeignKey("travel_places.id"))
    route_destination_ids = Column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    confidence_score = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    shelves = relationship("Shelf", backref="cabinet", cascade="all, delete-orphan", order_by="Shelf.day_number")
    hinges = relationship("Hinge", backref="cabinet", cascade="all, delete-orphan", order_by="Hinge.sequence_order")
    footstools = relationship("Footstool", backref="cabinet", cascade="all, delete-orphan")
    stools = relationship("Stool", backref="cabinet", cascade="all, delete-orphan")
    benches = relationship("Bench", backref="cabinet", cascade="all, delete-orphan")
    wardrobes = relationship("Wardrobe", backref="cabinet", cascade="all, delete-orphan")


class Shelf(Base):
    """One day of a trip."""
    __tablename__ = "shelves"
    __table_args__ = (UniqueConstraint("cabinet_id", "day_number"),)

    id = _uuid_col()
    cabinet_id = Column(UUID(as_uuid=True), ForeignKey("cabinets.id", ondelete="CASCADE"), nullable=False)
    day_number = Column(Integer, nullable=False)
    date = Column(Date)
    destination_id = Column(UUID(as_uuid=True), ForeignKey("travel_places.id"))
    theme = Column(Text)
    hero_image_url = Column(Text)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    drawers = relationship("Drawer", backref="shelf", cascade="all, delete-orphan", order_by="Drawer.sort_order")
    headboards = relationship("Headboard", backref="shelf", cascade="all, delete-orphan")
    armrests = relationship("Armrest", backref="shelf", cascade="all, delete-orphan")
    trays = relationship("Tray", backref="shelf", cascade="all, delete-orphan")


class Drawer(Base):
    """One scheduled activity within a day."""
    __tablename__ = "drawers"

    id = _uuid_col()
    shelf_id = Column(UUID(as_uuid=True), ForeignKey("shelves.id", ondelete="CASCADE"), nullable=False)
    activity_id = Column(UUID(as_uuid=True), ForeignKey("activities.id"))
    name = Column(Text, nullable=False)
    description = Column(Text)
    start_time = Column(Time)
    duration_minutes = Column(Integer)
    sort_order = Column(Integer, nullable=False, default=0)
    activity_type = Column(Text, nullable=False, default="EXPERIENCE")
    location_name = Column(Text)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class Headboard(Base):
    """Accommodation for a night."""
    __tablename__ = "headboards"

    id = _uuid_col()
    shelf_id = Column(UUID(as_uuid=True), ForeignKey("shelves.id", ondelete="CASCADE"), nullable=False)
    lodge_id = Column(UUID(as_uuid=True), ForeignKey("lodges.id"))
    name = Column(Text, nullable=False)
    tier = Column(Text)
    check_in = Column(Date)
    check_out = Column(Date)
    nights = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class Armrest(Base):
    """Transport used on a given day."""
    __tablename__ = "armrests"

    id = _uuid_col()
    shelf_id = Column(UUID(as_uuid=True), ForeignKey("shelves.id", ondelete="CASCADE"), nullable=False)
    mode = Column(Text, nullable=False)
    description = Column(Text)
    duration_minutes = Column(Integer)
    is_private = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class Tray(Base):
    """Meals included on a given day."""
    __tablename__ = "trays"

    id = _uuid_col()
    shelf_id = Column(UUID(as_uuid=True), ForeignKey("shelves.id", ondelete="CASCADE"), nullable=False)
    meal_type = Column(Text, nullable=False)
    included = Column(Boolean, nullable=False, default=True)
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class Hinge(Base):
    """Route leg between two destinations, trip-level."""
    __tablename__ = "hinges"

    id = _uuid_col()
    cabinet_id = Column(UUID(as_uuid=True), ForeignKey("cabinets.id", ondelete="CASCADE"), nullable=False)
    from_destination_id = Column(UUID(as_uuid=True), ForeignKey("travel_places.id"))
    to_destination_id = Column(UUID(as_uuid=True), ForeignKey("travel_places.id"))
    sequence_order = Column(Integer, nullable=False)
    distance_km = Column(Numeric(8, 2))
    duration_minutes = Column(Integer)
    mode = Column(Text)
    source = Column(Text, nullable=False, default="drive_times")
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class Footstool(Base):
    """Validation engine output / repair log."""
    __tablename__ = "footstools"

    id = _uuid_col()
    cabinet_id = Column(UUID(as_uuid=True), ForeignKey("cabinets.id", ondelete="CASCADE"), nullable=False)
    shelf_id = Column(UUID(as_uuid=True), ForeignKey("shelves.id", ondelete="CASCADE"))
    severity = Column(Text, nullable=False, default="info")
    category = Column(Text, nullable=False)
    message = Column(Text, nullable=False)
    auto_repaired = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class Stool(Base):
    """Operator match score for a trip."""
    __tablename__ = "stools"
    __table_args__ = (UniqueConstraint("cabinet_id", "tour_operator_id"),)

    id = _uuid_col()
    cabinet_id = Column(UUID(as_uuid=True), ForeignKey("cabinets.id", ondelete="CASCADE"), nullable=False)
    tour_operator_id = Column(UUID(as_uuid=True), ForeignKey("tour_operators.id", ondelete="CASCADE"), nullable=False)
    trip_match_pct = Column(Integer, nullable=False)
    itinerary_fit_pct = Column(Integer)
    experience_fit_pct = Column(Integer)
    accommodation_fit_pct = Column(Integer)
    destination_coverage_pct = Column(Integer)
    service_pct = Column(Integer)
    trust_pct = Column(Integer)
    value_pct = Column(Integer)
    strengths = Column(ARRAY(Text), nullable=False, default=list)
    badge = Column(Text)
    estimated_price_pp = Column(Numeric(10, 2))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class Bench(Base):
    """A quote request sent to one operator."""
    __tablename__ = "benches"

    id = _uuid_col()
    cabinet_id = Column(UUID(as_uuid=True), ForeignKey("cabinets.id", ondelete="CASCADE"), nullable=False)
    tour_operator_id = Column(UUID(as_uuid=True), ForeignKey("tour_operators.id", ondelete="CASCADE"), nullable=False)
    status = Column(Text, nullable=False, default="request_sent")
    note = Column(Text)
    requested_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    responded_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    counters = relationship("Counter", backref="bench", cascade="all, delete-orphan")


class Counter(Base):
    """The quote an operator sends back for a bench (request)."""
    __tablename__ = "counters"

    id = _uuid_col()
    bench_id = Column(UUID(as_uuid=True), ForeignKey("benches.id", ondelete="CASCADE"), nullable=False)
    price_per_person = Column(Numeric(10, 2), nullable=False)
    currency = Column(Text, nullable=False, default="USD")
    validity_date = Column(Date)
    accommodation_summary = Column(Text)
    activities_summary = Column(Text)
    transport_summary = Column(Text)
    meals_summary = Column(Text)
    park_fees_included = Column(Boolean, nullable=False, default=True)
    transfers_included = Column(Boolean, nullable=False, default=True)
    difference_notes = Column(Text)
    status = Column(Text, nullable=False, default="received")
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class Wardrobe(Base):
    """A confirmed booking."""
    __tablename__ = "wardrobes"

    id = _uuid_col()
    cabinet_id = Column(UUID(as_uuid=True), ForeignKey("cabinets.id", ondelete="CASCADE"), nullable=False)
    counter_id = Column(UUID(as_uuid=True), ForeignKey("counters.id"))
    confirmation_code = Column(Text, nullable=False, unique=True)
    tour_operator_id = Column(UUID(as_uuid=True), ForeignKey("tour_operators.id"), nullable=False)
    price_per_person = Column(Numeric(10, 2), nullable=False)
    total_price = Column(Numeric(10, 2), nullable=False)
    deposit_amount = Column(Numeric(10, 2))
    status = Column(Text, nullable=False, default="reserved")
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    chests = relationship("Chest", backref="wardrobe", cascade="all, delete-orphan")


class Chest(Base):
    """A payment against a booking."""
    __tablename__ = "chests"

    id = _uuid_col()
    wardrobe_id = Column(UUID(as_uuid=True), ForeignKey("wardrobes.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(Text, nullable=False, default="USD")
    payment_type = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="pending")
    paid_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class Mirror(Base):
    """A notification tied to a trip/quote-request/booking."""
    __tablename__ = "mirrors"

    id = _uuid_col()
    cabinet_id = Column(UUID(as_uuid=True), ForeignKey("cabinets.id", ondelete="CASCADE"))
    bench_id = Column(UUID(as_uuid=True), ForeignKey("benches.id", ondelete="CASCADE"))
    wardrobe_id = Column(UUID(as_uuid=True), ForeignKey("wardrobes.id", ondelete="CASCADE"))
    channel = Column(Text, nullable=False, default="push")
    message = Column(Text, nullable=False)
    sent = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
