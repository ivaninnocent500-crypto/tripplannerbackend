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
        logger.error("❌ SUPABASE_DATABASE_URL environment variable is missing.")
        raise DatabaseNotConfiguredError("SUPABASE_DATABASE_URL is not set.")

    # Remove accidental surrounding quotes or trailing spaces
    url = url.strip("'\" ")

    is_pooled = ":6543" in url
    pool_class = NullPool if is_pooled else QueuePool

    kwargs: dict = {"pool_pre_ping": True}
    if pool_class is QueuePool:
        kwargs.update(pool_size=5, max_overflow=10, pool_recycle=1800)

    try:
        return create_engine(url, poolclass=pool_class, **kwargs)
    except Exception as exc:
        logger.error("❌ Failed to create Supabase engine for URL '%s...': %s", url[:20], exc, exc_info=True)
        raise


def _build_legacy_engine() -> Engine:
    url = os.environ.get("DATABASE_URL") or os.environ.get("LEGACY_DATABASE_URL")
    if not url:
        logger.error("❌ DATABASE_URL / LEGACY_DATABASE_URL environment variable is missing.")
        raise DatabaseNotConfiguredError("DATABASE_URL is not set.")

    url = url.strip("'\" ")

    try:
        return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10, pool_recycle=1800)
    except Exception as exc:
        logger.error("❌ Failed to create Legacy engine for URL '%s...': %s", url[:20], exc, exc_info=True)
        raise


def check_supabase_connection() -> bool:
    try:
        from sqlalchemy import text
        with supabase_session() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("❌ Supabase connection failed: %s", exc, exc_info=True)
        return False


def check_legacy_connection() -> bool:
    try:
        from sqlalchemy import text
        with legacy_session() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("❌ Legacy DB connection failed: %s", exc, exc_info=True)
        return False

