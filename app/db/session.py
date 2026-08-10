"""
Two independent SQLAlchemy engines/sessions, matching TripOrchestrator's
two-session design (see app/core/orchestrator.py's docstring and
tests/test_two_database_wiring.py, which specifically asserts
GenerationLog is written via legacy_db and never supabase_db).

    get_supabase_db() -> the Travel Intelligence knowledge base
                           (travel_places, wildlife, lodges, etc.)
    get_legacy_db() -> the original ati-production DB, used ONLY
                           for GenerationLog (operational/audit data)

PORT 6543 vs 5432
------------------
Supabase exposes Postgres two ways:
  - port 5432 = direct connection (session mode). Fine for long-lived
    servers that keep a small persistent pool, but each connection holds
    a real Postgres backend process open.
  - port 6543 = PgBouncer transaction-mode pooler. Required if you expect
    many short-lived connections (e.g. serverless/autoscaling), but it
    does NOT support session-level features some drivers assume
    (prepared statement caching in particular) — hence NullPool below
    when pooled mode is detected.

Your .env.example currently has port 5432 in SUPABASE_DATABASE_URL. This
module does NOT silently rewrite that port for you — guessing wrong here
causes real, confusing connection failures (exactly what the .env.example
comment warns about). Instead it detects which mode you configured from
the URL itself and picks the matching SQLAlchemy pool class, so whichever
port you correctly set will work without code changes. Confirm with
Supabase's dashboard (Project Settings -> Database -> Connection string)
which port your project actually expects before deploying.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

logger = logging.getLogger(__name__)


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when a required *_DATABASE_URL env var is missing at request time."""


_supabase_engine: Engine | None = None
_legacy_engine: Engine | None = None
_SupabaseSession: sessionmaker | None = None
_LegacySession: sessionmaker | None = None


def _build_supabase_engine() -> Engine:
    url = os.environ.get("SUPABASE_DATABASE_URL")
    if not url:
        raise DatabaseNotConfiguredError(
            "SUPABASE_DATABASE_URL is not set. Every travel-data engine "
            "(ItineraryEngine, BudgetEngine, OperatorEngine, WeatherEngine, "
            "WildlifeEngine, RoutingEngine, PackingEngine) requires this to "
            "query the Travel Intelligence knowledge base. Set it in your "
            "environment / Render dashboard — see .env.example."
        )

    # Port 6543 = PgBouncer transaction pooler -> use NullPool, since
    # SQLAlchemy's own connection pooling on top of an external pooler
    # causes prepared-statement / session-state bugs. Port 5432 (or
    # anything else) = direct connection -> a normal bounded QueuePool
    # is appropriate and more efficient for a single long-lived server.
    is_pooled = ":6543" in url
    pool_class = NullPool if is_pooled else QueuePool

    kwargs: dict = {"pool_pre_ping": True}
    if pool_class is QueuePool:
        kwargs.update(pool_size=5, max_overflow=10, pool_recycle=1800)

    return create_engine(url, poolclass=pool_class, **kwargs)


def _build_legacy_engine() -> Engine:
    url = os.environ.get("DATABASE_URL") or os.environ.get("LEGACY_DATABASE_URL")
    if not url:
        raise DatabaseNotConfiguredError(
            "DATABASE_URL or LEGACY_DATABASE_URL is not set. This is required for GenerationLog "
            "persistence (operational/audit data) — see .env.example."
        )
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10, pool_recycle=1800)


def _get_supabase_sessionmaker() -> sessionmaker:
    global _supabase_engine, _SupabaseSession
    if _SupabaseSession is None:
        _supabase_engine = _build_supabase_engine()
        _SupabaseSession = sessionmaker(bind=_supabase_engine, autoflush=False, autocommit=False)
    return _SupabaseSession


def _get_legacy_sessionmaker() -> sessionmaker:
    global _legacy_engine, _LegacySession
    if _LegacySession is None:
        _legacy_engine = _build_legacy_engine()
        _LegacySession = sessionmaker(bind=_legacy_engine, autoflush=False, autocommit=False)
    return _LegacySession


def get_supabase_db() -> Iterator[Session]:
    """FastAPI dependency: yields a Supabase Travel Intelligence session, closes it after the request."""
    SessionLocal = _get_supabase_sessionmaker()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_legacy_db() -> Iterator[Session]:
    """FastAPI dependency: yields a legacy ati-production session, closes it after the request."""
    SessionLocal = _get_legacy_sessionmaker()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def supabase_session() -> Iterator[Session]:
    """Non-FastAPI context-manager form, for scripts/health checks."""
    SessionLocal = _get_supabase_sessionmaker()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def legacy_session() -> Iterator[Session]:
    SessionLocal = _get_legacy_sessionmaker()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_supabase_connection() -> bool:
    try:
        from sqlalchemy import text
        with supabase_session() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("❌ Supabase DB Connection Error: %s", exc, exc_info=True)
        return False


def check_legacy_connection() -> bool:
    try:
        from sqlalchemy import text
        with legacy_session() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("❌ Legacy DB Connection Error: %s", exc, exc_info=True)
        return False

