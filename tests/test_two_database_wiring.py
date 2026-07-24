"""
Tests for the two-database wiring introduced by the Supabase migration.
The core thing under test: GenerationLog must be written via legacy_db,
never supabase_db — this guards against a real bug caught during this
migration (an earlier draft of TripOrchestrator wrote GenerationLog to
the wrong session entirely).
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from app.core.orchestrator import TripOrchestrator


def test_generation_log_written_to_legacy_db_not_supabase_db():
    """
    Uses mock sessions instead of real DB connections (appropriate here
    since this test is specifically about WHICH session receives the
    .add()/.commit() calls, not about real query behavior — that's
    covered by the per-engine tests against a real Supabase connection
    once SUPABASE_DATABASE_URL is available).
    """
    mock_supabase_db = MagicMock()
    mock_legacy_db = MagicMock()

    # Mock supabase_db's query chain to return empty results everywhere,
    # so the orchestrator's engines all hit their documented fallback
    # paths rather than erroring on a MagicMock not behaving like a real
    # query result.
    mock_supabase_db.query.return_value.filter.return_value.all.return_value = []
    mock_supabase_db.query.return_value.filter.return_value.first.return_value = None
    mock_supabase_db.query.return_value.filter.return_value.in_.return_value.all.return_value = []

    orchestrator = TripOrchestrator(supabase_db=mock_supabase_db, legacy_db=mock_legacy_db)

    request = {
        "destinations": ["some_unconfigured_slug"],
        "days": 3,
        "travelers": 2,
        "budget_tier": "mid",
    }

    orchestrator.build_trip(request)

    # THE assertion this test exists for:
    assert mock_legacy_db.add.called, "GenerationLog must be added via legacy_db"
    assert mock_legacy_db.commit.called, "GenerationLog must be committed via legacy_db"
    assert not mock_supabase_db.add.called, "supabase_db must never receive .add() — it has no GenerationLog table"
    assert not mock_supabase_db.commit.called, "supabase_db should not need .commit() — engines are read-only queries"


def test_orchestrator_requires_both_sessions():
    """Confirms the constructor signature actually requires two distinct
    sessions (positional/keyword), preventing an accidental reversion to
    the single-session bug this migration fixed."""
    import inspect
    sig = inspect.signature(TripOrchestrator.__init__)
    params = list(sig.parameters.keys())
    assert "supabase_db" in params
    assert "legacy_db" in params
    assert params.index("supabase_db") < params.index("legacy_db")
