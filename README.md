# Supabase Travel Intelligence Migration

Migrates the data-access layer of 5 engines from the old `ati-production`
models to the real Supabase Travel Intelligence schema (`travel_places`,
`lodges`, `wildlife`, `wildlife_calendar`, `tour_operators`,
`entry_fees`, `activities`, `estimated_visit_durations`, `drive_times`,
`airports`, `destination_airports`, `packing_recommendations`,
`monthly_weather_patterns`). Business logic in every engine is
unchanged — only where each queries data from.

## Files in this delivery

```
app/
├── db/
│   ├── models_v2.py          NEW — SQLAlchemy models for the real Supabase schema
│   ├── destinations.py        NEW — slug<->UUID resolver (handles the real
│   │                                hyphenated slug format, e.g.
│   │                                "maasai-mara-national-reserve")
│   └── session.py               REPLACES existing — adds SUPABASE_DATABASE_URL,
│                                   two-session support (legacy + supabase)
├── engines/
│   ├── itinerary.py            REPLACES existing — queries travel_places,
│   │                              activities, lodges, estimated_visit_durations
│   ├── wildlife.py              REPLACES existing — queries wildlife_calendar
│   ├── operator.py               REPLACES existing — queries tour_operators
│   ├── routing.py                 REPLACES existing — queries drive_times,
│   │                                 airports (see MIGRATION_NOTES.md — real gap here)
│   ├── budget.py                   REPLACES existing — queries entry_fees for real park fees
│   ├── packing.py                   REPLACES existing — queries packing_recommendations
│   └── weather.py                    REPLACES existing — queries monthly_weather_patterns
├── core/
│   └── orchestrator.py                REPLACES existing — two-session wiring
│                                         (supabase_db for engines, legacy_db for
│                                         GenerationLog only)
└── api/
    └── trip.py                         REPLACES existing — injects both sessions
requirements.txt                          REPLACES existing — adds geoalchemy2
.env.example                                REPLACES existing — adds SUPABASE_DATABASE_URL
tests/
└── test_two_database_wiring.py              NEW — guards the two-session bug
                                                 that was caught and fixed during this build
MIGRATION_NOTES.md                              Full gap list — READ THIS before
                                                   treating the migration as complete
```

## Files NOT in this delivery — carry over UNCHANGED from the previous
## (AI Gateway) delivery, do not overwrite them with anything

- `app/ai/gateway.py`
- `app/api/schemas.py`
- `app/api/auth.py`
- `app/api/reference.py`
- `app/engines/confidence.py` (pure calculation, no DB access — nothing to migrate)
- `app/engines/insights.py` — ⚠️ SEE NOTE BELOW, this one likely DOES need updating
- `app/engines/rules.py` (pure request transformation, no DB access — nothing to migrate)
- `app/engines/resilience.py` (generic wrapper, no DB access — nothing to migrate)
- `app/main.py`
- `app/db/models.py` (the LEGACY models — GenerationLog still lives here, unchanged)
- `app/db/seed_initial_data.py` (seeds the LEGACY db only; the Supabase
  knowledge base is being populated separately by you, directly in Supabase)

## ⚠️ One real gap in this delivery: `insights.py` was not migrated

`AIInsightEngine` (in `app/engines/insights.py`) queries destination data
too — it wasn't included in this migration pass since it wasn't in your
original list of 5 engines to migrate ("ItineraryEngine, WildlifeEngine,
OperatorEngine, RoutingEngine, BudgetEngine"). If `insights.py` still
imports from the old `app.db.models`, it will either fail at import time
(if that file was removed) or silently keep reading stale/absent data
from the legacy DB depending on what's left there. **Check this file
before deploying** — it likely needs the same `resolve_slugs_to_ids`
treatment as the other 5, using the same pattern shown in
`app/engines/wildlife.py` or `app/engines/budget.py` as a template.

## Required before this runs

1. **Set `SUPABASE_DATABASE_URL`** (still pending as of this delivery).
   See `app/db/session.py`'s docstring for the port 6543 (pooled,
   `NullPool`) vs port 5432 (direct) decision — confirm which your
   connection string uses.
2. **Read `MIGRATION_NOTES.md`** — RoutingEngine in particular has a real
   structural gap (no inter-destination distance data yet in the schema)
   that will make every multi-destination route fall back to a generic
   estimate until resolved.
3. **Fix or explicitly defer `insights.py`** per the note above.
4. Add `geoalchemy2==0.15.2` (already in the updated `requirements.txt`
   in this delivery) — required for the PostGIS `Geography` column type.

## Slug format — the fix you're getting in this delivery

Your real Supabase data uses slugs like `"maasai-mara-national-reserve"`,
`"serengeti-national-park"` (confirmed from your Table Editor
screenshot). The API contract and Android app were built assuming short
underscore slugs like `"maasai_mara"`. `app/db/destinations.py`'s
`resolve_slugs_to_ids()` normalizes both formats so this mismatch doesn't
silently break every request — but it's worth deciding whether to
eventually standardize on one format everywhere (real slugs end-to-end)
rather than relying on this normalization layer permanently.
