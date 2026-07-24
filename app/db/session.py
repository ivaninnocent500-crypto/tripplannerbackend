"""
Database session management — TWO connections during the migration
period, per the decision flagged in orchestrator.py:

  1. LEGACY_DATABASE_URL: the original ati-production database
     (GenerationLog only — operational/audit data, not travel knowledge).
     This is your EXISTING Render Postgres/SQLite, unchanged.

  2. SUPABASE_DATABASE_URL: the new Travel Intelligence knowledge base
     (travel_places, lodges, wildlife, tour_operators, etc.) — this is
     what every engine now queries.

⚠️ SUPABASE_DATABASE_URL IS STILL A PLACEHOLDER. You said the connection
string is pending. get_supabase_db() below will raise a clear
RuntimeError (not a silent failure or a guessed default) until you set
SUPABASE_DATABASE_URL. This is intentional — better to fail loudly at
startup than to silently connect to the wrong database or crash deep
inside an engine query with a confusing error.

WHEN YOU HAVE THE CONNECTION STRING: Supabase's connection pooler
(PgBouncer, typically port 6543) requires NullPool in SQLAlchemy —
transaction-mode pooling doesn't support the persistent connections
SQLAlchemy's default pool assumes. Port 5432 (direct connection, not
pooled) works with SQLAlchemy's normal pooling. Use whichever your
Supabase project's connection string uses; if unsure, port 6543 +
NullPool is the safer default for a serverless-style deployment like
Render. This is set up below but commented, since guessing wrong here
causes real connection failures — confirm which port your string uses
before uncommenting.

Once you have the real connection string: set SUPABASE_DATABASE_URL as a
Render environment variable, same as GEMINI_API_KEY and ATI_API_KEY were
added earlier in this project — never commit it to any file.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool

# ---- Legacy DB (GenerationLog only) — unchanged from before migration ----
LEGACY_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./ati_dev.db")
_legacy_connect_args = {"check_same_thread": False} if LEGACY_DATABASE_URL.startswith("sqlite") else {}
legacy_engine = create_engine(LEGACY_DATABASE_URL, connect_args=_legacy_connect_args, pool_pre_ping=True)
LegacySessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=legacy_engine)


def get_db() -> Generator[Session, None, None]:
    """Existing dependency name, unchanged — still points at the legacy
    DB, since GenerationLog is the only thing still living there."""
    db = LegacySessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---- Supabase Travel Intelligence DB — PENDING connection string ----
SUPABASE_DATABASE_URL = os.environ.get("SUPABASE_DATABASE_URL", "")

_supabase_engine = None
SupabaseSessionLocal = None

if SUPABASE_DATABASE_URL:
    # Uncomment and choose ONE of these once you confirm your Supabase
    # connection string's port/pooling mode:
    #
    # Option A — pooled connection (port 6543, PgBouncer transaction mode):
    # _supabase_engine = create_engine(SUPABASE_DATABASE_URL, poolclass=NullPool)
    #
    # Option B — direct connection (port 5432, no pooler):
    # _supabase_engine = create_engine(SUPABASE_DATABASE_URL, pool_pre_ping=True)
    #
    # Defaulting to Option A (NullPool) as the safer choice for a
    # serverless-style host like Render, per the docstring above — change
    # to Option B if your string is the direct (5432) connection.
    _supabase_engine = create_engine(SUPABASE_DATABASE_URL, poolclass=NullPool)
    SupabaseSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_supabase_engine)


def get_supabase_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency for the new Travel Intelligence knowledge base.
    Every engine (ItineraryEngine, WildlifeEngine, OperatorEngine,
    RoutingEngine, BudgetEngine, PackingEngine, WeatherEngine) should
    receive a session from THIS function, not get_db(), once wired into
    the API layer.

    Raises RuntimeError with a clear message if SUPABASE_DATABASE_URL
    isn't set — this is deliberate: failing loudly at request time (or
    ideally at app startup, see main.py) is far better than a confusing
    downstream error inside an engine's first query.
    """
    if SupabaseSessionLocal is None:
        raise RuntimeError(
            "SUPABASE_DATABASE_URL is not configured. Set it as an "
            "environment variable (Render: Environment tab) once your "
            "Supabase connection string is ready. See session.py's "
            "docstring for pooling-mode guidance (port 6543 + NullPool "
            "vs port 5432 direct)."
        )
    db = SupabaseSessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def supabase_db_session() -> Generator[Session, None, None]:
    """Context-manager version for scripts (e.g. a future seed script
    for the Supabase knowledge base), mirroring the legacy db_session()
    pattern already used by seed_initial_data.py."""
    if SupabaseSessionLocal is None:
        raise RuntimeError("SUPABASE_DATABASE_URL is not configured. See get_supabase_db().")
    db = SupabaseSessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
