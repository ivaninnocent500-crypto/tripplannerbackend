# Supabase Migration Notes

Consolidated from every gap flagged inline during this migration. Read
this before treating the migration as "done" — several real
accuracy/completeness tradeoffs were made, documented rather than hidden.

## 🔴 BLOCKING — must resolve before this can run at all

**`SUPABASE_DATABASE_URL` is not yet set.** Every engine will fail with a
clear `RuntimeError` (surfaced as HTTP 503 from `/api/trip/generate`)
until this is configured. See `app/db/session.py`'s docstring for the
port 6543 (pooled, `NullPool`) vs port 5432 (direct) decision you need to
make based on your actual Supabase connection string.

## 🟠 Real accuracy/completeness gaps, by engine

### RoutingEngine — the most significant gap
Your DDL's `drive_times` table models distances *within* one destination
(landmark-to-landmark), not *between* destinations. There is currently
**no real inter-destination distance data** in the new schema — every
multi-destination leg falls back to a generic estimate
(`{km: 220, minutes: 280}`), same fallback as before migration, but now
firing for *every* route instead of exceptional cases.

**Fix options, pick one:**
1. Add a new table (`road_legs` or similar) with `origin_id`/`dest_id`
   both FK to `travel_places`, mirroring the old `route_legs` shape.
2. Integrate a real routing API (Google Routes/OpenRouteService).
3. Populate the `flights` table with real scheduled/charter routes
   between destinations' primary airports.

The orchestrator surfaces this honestly:
`trip.generation_meta.routing_used_fallback` is `true` whenever any leg
used the fallback, so the API contract at least tells the truth about it.

### OperatorEngine — three of six scoring factors are neutral placeholders
`tour_operators` doesn't have columns for price tier, response time, or
availability tracking. `price`, `response`, and `luxury` factors are
hardcoded to `70`/`70`/`70` rather than computed.

**Fix**: add `price_tier` (enum matching `lodge_tier`) and
`avg_response_minutes` (integer) columns to `tour_operators`.

### WildlifeEngine — `best_viewing_window` is generic, not destination-specific
The new `wildlife_calendar` table has `best_time_of_day` per
species-month row, not a single destination-month summary. Currently
returns a generic safari-standard window regardless of destination.

**Fix**: aggregate `best_time_of_day` across a destination's
species-month rows, or add a genuine destination-month-level column.

### ItineraryEngine — day-to-day activity sequencing is simplistic
No equivalent of the old `is_arrival_day_activity` flag or explicit
day-ordering. Arrival day shows the first 2 activities; other days
rotate through the remainder round-robin.

**Fix**: consider an `itinerary_day_activities` join table
(destination_id, day_offset, activity_id) for precise sequencing.

### ItineraryEngine — lodge family-friendliness is coarser than before
Old schema: `Lodge.min_child_age` (precise integer). New schema:
`Lodge.is_family_friendly` (boolean only).

**Fix**: add `min_child_age` (nullable integer) to the real `lodges` table.

## 🟢 Real improvements this migration delivers, not just parity

- **`BudgetEngine`'s park fees** now come from real `entry_fees` rows
  (payer-category-aware), not a flat estimate.
- **`WeatherEngine`** now uses real per-destination `monthly_weather_patterns`
  when available, falling back to the old generic fixture only for
  un-populated destinations.
- **`PackingEngine`** now reads real per-destination `packing_recommendations`
  rows instead of one hardcoded rule list shared across all destinations.
- **`WildlifeEngine`**'s species data is now properly normalized instead
  of a duplicated JSON blob per destination.

## 🔵 Infrastructure decision made during this migration

**Two live database connections during the transition period**:
`LEGACY_DATABASE_URL` (GenerationLog only) and `SUPABASE_DATABASE_URL`
(everything else). `GenerationLog` is operational/audit data, not travel
knowledge, so it doesn't belong in the Travel Intelligence schema.
`TripOrchestrator` now requires **both** sessions explicitly
(`supabase_db`, `legacy_db`) — a real bug where both roles used one
session was caught and fixed during this build;
`tests/test_two_database_wiring.py` guards against a regression.

**When you retire the legacy tables**, decide whether `GenerationLog`
moves to Supabase too or stays separate permanently. Either is
defensible; pick one deliberately.

## Suggested order for closing the 🟠 gaps

1. Populate Maasai Mara fully across every table these 5 engines touch.
2. Run `/api/trip/generate` with `destinations: ["maasai_mara"]` only —
   avoids the RoutingEngine gap entirely (no inter-destination leg
   needed), letting you validate everything else first.
3. Add a second destination (e.g. Nairobi) — this is where the
   RoutingEngine gap becomes visible and needs a real decision.
4. Only then decide on the `OperatorEngine` schema additions — lower
   urgency since neutral placeholders don't break generation.
