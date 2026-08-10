"""
Legacy operational database models (audit logs and generation meta).
"""
from sqlalchemy import Column, Integer, JSON, String, Boolean, DateTime
from sqlalchemy.sql import func
from app.db.session import Base # Or declarative_base() if Base isn't in session.py


class GenerationLog(Base):
    __tablename__ = "generation_logs"

    id = Column(Integer, primary_key=True, index=True)
    request_json = Column(JSON, nullable=False)
    matched_destination_ids = Column(JSON, nullable=True)
    unmatched_terms = Column(JSON, nullable=True)
    confidence_score = Column(Integer, nullable=True)
    total_generation_time_ms = Column(Integer, nullable=False)
    ai_gateway_used = Column(Boolean, default=False)
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

