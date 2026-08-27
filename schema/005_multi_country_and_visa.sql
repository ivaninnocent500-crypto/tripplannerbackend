-- =====================================================================
-- Migration 005 — Multi-Country Routing, Operator Capability Tables,
-- and Verified Visa Intelligence
-- =====================================================================
-- Consolidates and FIXES the migration set from the "V3" pass shared
-- in chat. Two real gaps found and closed here, not present in what
-- was supplied:
--
-- GAP 1 (critical — the border-crossing feature was silently
-- non-functional without this): the supplied migrations added
-- is_inter_country / border_crossing_id / requires_border_crossing to
-- `drive_times` (knowledge-base leg data) but NEVER to `hinges` (the
-- per-trip persisted route legs a Cabinet actually has). The itinerary
-- engine code writes these fields onto Hinge objects via
-- `hasattr(hinge, "is_inter_country")` guards — which silently return
-- False and skip the write when the column doesn't exist. The whole
-- feature would run without erroring and never persist anything.
-- Fixed below: the same three columns added to `hinges` instead.
--
-- GAP 2 (naming honesty): the supplied `stools.country_match_pct` /
-- OperatorMatchEngineV3's "country_match" implies verified legal
-- cross-border licensing, but the query behind it only checks
-- `destination_tour_operators` coverage — i.e. "this operator has
-- listed destinations in this country," not "this operator is legally
-- licensed to operate across this border." Renamed to
-- `country_coverage_pct` throughout (column, ORM attribute, engine
-- method, response field) so the number means what it actually
-- measures. A real `operator_country_licenses` table is a separate,
-- future addition — not implied by this column's name anymore.
--
-- Idempotent — safe to re-run.
-- =====================================================================

-- ---------------------------------------------------------------------
-- drive_times: cross-border-aware knowledge-base leg data
-- ---------------------------------------------------------------------
ALTER TABLE drive_times
  ADD COLUMN IF NOT EXISTS origin_destination_id      uuid REFERENCES travel_places(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS destination_destination_id uuid REFERENCES travel_places(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS is_inter_country boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS primary_mode text NOT NULL DEFAULT 'private_4x4'
    CHECK (primary_mode IN ('private_4x4','scheduled_flight','charter_flight','ferry','shuttle','train','bus','walking')),
  ADD COLUMN IF NOT EXISTS requires_border_crossing boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS border_crossing_id uuid REFERENCES border_crossings(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_drive_times_origin       ON drive_times(origin_destination_id);
CREATE INDEX IF NOT EXISTS idx_drive_times_destination2 ON drive_times(destination_destination_id);
CREATE INDEX IF NOT EXISTS idx_drive_times_intercountry ON drive_times(is_inter_country) WHERE is_inter_country = true;

-- ---------------------------------------------------------------------
-- hinges: THE MISSING PIECE — per-trip route legs need the same
-- border-awareness columns as the knowledge-base drive_times table,
-- or the feature has nowhere to persist to. See GAP 1 above.
-- ---------------------------------------------------------------------
ALTER TABLE hinges
  ADD COLUMN IF NOT EXISTS is_inter_country boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS requires_border_crossing boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS border_crossing_id uuid REFERENCES border_crossings(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_hinges_intercountry ON hinges(is_inter_country) WHERE is_inter_country = true;
CREATE INDEX IF NOT EXISTS idx_hinges_border_crossing ON hinges(border_crossing_id);

-- ---------------------------------------------------------------------
-- cabinets: multi-country route metadata
-- ---------------------------------------------------------------------
ALTER TABLE cabinets
  ADD COLUMN IF NOT EXISTS route_countries text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS primary_country  text;

-- ---------------------------------------------------------------------
-- Operator capability + partnership tables — the real-data path for
-- experience_fit and accommodation_fit. Fleet/vehicle-type scoring
-- (S_fleet) is deliberately NOT added here: no fleet data exists
-- anywhere in the schema. Adding a table for it now would just be
-- inventing a column to look thorough — stays a documented gap until
-- a real fleet data source exists.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS operator_lodge_partnerships (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  operator_id uuid NOT NULL REFERENCES tour_operators(id) ON DELETE CASCADE,
  lodge_id uuid NOT NULL REFERENCES lodges(id) ON DELETE CASCADE,
  -- Two DIFFERENT things, named to not be confused with each other:
  --   partner_lodge_tier   = the LODGE's own tier (ultra_luxury/luxury/...)
  --   partnership_grade    = how good the OPERATOR's relationship with
  --                          that lodge is (preferred/direct/standard)
  partner_lodge_tier lodge_tier NOT NULL,
  partnership_grade text NOT NULL CHECK (partnership_grade IN ('preferred', 'direct', 'standard')),
  exclusive boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (operator_id, lodge_id)
);
CREATE INDEX IF NOT EXISTS idx_olp_operator ON operator_lodge_partnerships(operator_id);
CREATE INDEX IF NOT EXISTS idx_olp_lodge ON operator_lodge_partnerships(lodge_id);

CREATE TABLE IF NOT EXISTS operator_activity_capabilities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  operator_id uuid NOT NULL REFERENCES tour_operators(id) ON DELETE CASCADE,
  activity_category activity_category NOT NULL,
  is_primary boolean NOT NULL DEFAULT false,
  years_handling integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (operator_id, activity_category)
);
CREATE INDEX IF NOT EXISTS idx_oac_operator ON operator_activity_capabilities(operator_id);

DROP TRIGGER IF EXISTS trg_olp_updated_at ON operator_lodge_partnerships;
CREATE TRIGGER trg_olp_updated_at BEFORE UPDATE ON operator_lodge_partnerships
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP TRIGGER IF EXISTS trg_oac_updated_at ON operator_activity_capabilities;
CREATE TRIGGER trg_oac_updated_at BEFORE UPDATE ON operator_activity_capabilities
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------
-- stools: provenance + multi-country coverage scoring.
-- NAMED country_coverage_pct, not country_match_pct — see GAP 2 above.
-- ---------------------------------------------------------------------
ALTER TABLE stools
  ADD COLUMN IF NOT EXISTS country_coverage_pct integer CHECK (country_coverage_pct BETWEEN 0 AND 100),
  ADD COLUMN IF NOT EXISTS score_provenance jsonb,
  ADD COLUMN IF NOT EXISTS has_placeholder_subscores boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS confidence_pct integer CHECK (confidence_pct BETWEEN 0 AND 100);

-- ---------------------------------------------------------------------
-- drawers: provenance (which real activities row this came from, or
-- that it's a fallback) — unchanged from the supplied migration.
-- ---------------------------------------------------------------------
ALTER TABLE drawers
  ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'activities_table',
  ADD COLUMN IF NOT EXISTS category text,
  ADD COLUMN IF NOT EXISTS destination_id uuid REFERENCES travel_places(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS is_fallback boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_drawers_source ON drawers(source) WHERE source = 'fallback_estimate';
CREATE INDEX IF NOT EXISTS idx_drawers_category ON drawers(category);

-- =====================================================================
-- Visa & Regional Mandate Intelligence
-- =====================================================================
-- EATV facts below verified against 8 independent sources (Rwanda
-- Directorate General of Immigration & Emigration / IREMBO portal,
-- Kenya embassy notices, and several 2026-dated safari-operator
-- guides) on 2026-08-23: $100 fee, 90-day multi-entry validity,
-- KE+UG+RW membership, first-port-of-entry application rule,
-- invalidated immediately on exiting the bloc (including to Tanzania,
-- which is explicitly NOT a member). All sources agree.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS regional_visa_blocs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bloc_code text NOT NULL UNIQUE,
  name text NOT NULL,
  member_countries country_code[] NOT NULL,
  fee_usd numeric(10,2),
  validity_days integer,
  multiple_entry boolean NOT NULL DEFAULT true,
  first_entry_country_required boolean NOT NULL DEFAULT true,
  invalidated_on_bloc_exit boolean NOT NULL DEFAULT true,
  renewable boolean NOT NULL DEFAULT false,
  notes text,
  source_url text NOT NULL,
  verified_date date NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_regional_visa_blocs_updated_at ON regional_visa_blocs;
CREATE TRIGGER trg_regional_visa_blocs_updated_at
  BEFORE UPDATE ON regional_visa_blocs
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO regional_visa_blocs
  (bloc_code, name, member_countries, fee_usd, validity_days, multiple_entry,
   first_entry_country_required, invalidated_on_bloc_exit, renewable, notes,
   source_url, verified_date)
VALUES
  ('EATV', 'East Africa Tourist Visa', ARRAY['KE','UG','RW']::country_code[], 100.00, 90, true,
   true, true, false,
   'Single joint visa for tourism only. Must be applied for through the traveler''s first '
   'country of entry (Kenya eTA portal, Uganda immigration e-visa portal, or Rwanda Irembo '
   'portal, matching whichever is entered first). Tanzania is NOT a member — entering '
   'Tanzania invalidates the EATV immediately even within the 90-day window; a fresh EATV '
   'or individual visa is required to re-enter Kenya/Uganda/Rwanda afterward.',
   'https://www.migration.gov.rw/our-services/visa-issued-under-special-arrangement', '2026-08-23')
ON CONFLICT (bloc_code) DO NOTHING;

-- ---------------------------------------------------------------------
-- visa_requirements — per nationality x destination country.
-- Deliberately NOT seeded with a full nationality matrix. Only rows
-- independently verified go in; everything else falls through to
-- VisaIntelligenceEngine's "unverified — check with your embassy"
-- response rather than a guessed row.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS visa_requirements (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nationality_country country_code NOT NULL,
  destination_country  country_code NOT NULL,
  requirement text NOT NULL CHECK (requirement IN
    ('not_required', 'visa_on_arrival', 'e_visa_required', 'embassy_visa_required')),
  applicable_bloc_code text REFERENCES regional_visa_blocs(bloc_code),
  fee_usd numeric(10,2),
  processing_days_typical integer,
  notes text,
  source_url text NOT NULL,
  verified_date date NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (nationality_country, destination_country)
);

DROP TRIGGER IF EXISTS trg_visa_requirements_updated_at ON visa_requirements;
CREATE TRIGGER trg_visa_requirements_updated_at
  BEFORE UPDATE ON visa_requirements
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS idx_visa_requirements_dest ON visa_requirements(destination_country);

-- Verified 2026-08-23 against multiple 2026-dated sources: Tanzania
-- eVisa fee is commonly cited as $50 for most nationalities, $100 for
-- US citizens specifically (a long-standing US reciprocity fee).
-- Tanzania is explicitly NOT part of the EATV bloc.
INSERT INTO visa_requirements
  (nationality_country, destination_country, requirement, fee_usd,
   processing_days_typical, notes, source_url, verified_date)
VALUES
  ('US', 'TZ', 'e_visa_required', 100.00, 10,
   'Tanzania requires its own eVisa regardless of any EATV held for Kenya/Uganda/Rwanda — '
   'Tanzania is not an EATV member. US citizens pay a higher reciprocal fee ($100) than '
   'most other nationalities (typically $50).',
   'https://www.dumaexplorer.com/blog/east-africa-visa-guide-eatv-tanzania-evisa-costs-rules', '2026-08-23')
ON CONFLICT (nationality_country, destination_country) DO NOTHING;

-- ---------------------------------------------------------------------
-- destination_entry_mandates — non-visa entry requirements (insurance,
-- vaccination). Not seeded: no row here has been independently
-- verified in this pass (this table structure is provided so your team
-- can add verified rows later — e.g. after confirming Zanzibar's
-- travel-insurance mandate terms against an official source, which I
-- could not confirm confidently enough to seed as "verified").
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS destination_entry_mandates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  destination_id uuid REFERENCES travel_places(id) ON DELETE CASCADE,
  country country_code,
  mandate_type text NOT NULL,
  description text NOT NULL,
  is_strictly_enforced boolean NOT NULL DEFAULT true,
  source_url text NOT NULL,
  verified_date date NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT chk_mandate_scope CHECK (
    (destination_id IS NOT NULL)::int + (country IS NOT NULL)::int = 1
  )
);

DROP TRIGGER IF EXISTS trg_destination_entry_mandates_updated_at ON destination_entry_mandates;
CREATE TRIGGER trg_destination_entry_mandates_updated_at
  BEFORE UPDATE ON destination_entry_mandates
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS idx_entry_mandates_destination ON destination_entry_mandates(destination_id);
CREATE INDEX IF NOT EXISTS idx_entry_mandates_country ON destination_entry_mandates(country);
