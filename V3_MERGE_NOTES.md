# V3 merge — what changed, what was fixed, what's still a gap

## Context

A separate pass ("V3": `OperatorMatchEngineV3`, border-crossing
validation, multi-country hinge/allocation patches, a visa-intelligence
migration) was shared in chat as code to merge in. I hadn't written any
of it — only diffs against files I'd never seen. Before merging
anything in, I: (1) verified the visa facts it claimed as "verified"
against independent sources, since wrong visa information can strand a
real traveler, and (2) read the code for bugs rather than trusting the
accompanying review.

## Real bugs found and fixed

1. **Rollback bug, same failure class fixed earlier this project,
   reintroduced via a new code path.** `_bulk_activity_capabilities`
   and `_bulk_lodge_partners` query tables that may not exist yet
   (`operator_activity_capabilities`, `operator_lodge_partnerships`).
   The supplied code caught that with a bare `except: return {}` —
   but a failed query leaves the session's transaction aborted;
   every later query in the same request then fails too. Fixed:
   `self.db.rollback()` in both except blocks.
2. **`rating` was extracted from the operator row and never used
   anywhere in scoring.** A 4.9-rated and a 3.2-rated operator with
   identical years/reviews would have scored identically. Fixed:
   rating now factors into `trust`.
3. **Missing `hinges` columns — the border-crossing feature would have
   silently done nothing.** The supplied migrations added
   `is_inter_country` / `border_crossing_id` to `drive_times`
   (knowledge-base data) but never to `hinges` (the per-trip persisted
   route legs). The code wrote these fields via `hasattr()` guards,
   which return `False` and skip the write when the ORM has no such
   column — so it would run without erroring and never persist
   anything. Fixed: added the same three columns to `hinges`, and to
   the `Hinge`/`Cabinet`/`Stool`/`Drawer` ORM classes in
   `models_furniture.py`.
4. **Misleading naming: `country_match_pct` implied verified legal
   cross-border licensing.** The query behind it only checks
   `destination_tour_operators` coverage — "this operator has listed
   destinations in this country," not "licensed to operate across this
   border." Renamed to `country_coverage_pct` everywhere (column, ORM
   attribute, method, API response field) so it means what it measures.
5. **Score vs. confidence conflated.** An operator with no
   `operator_activity_capabilities` rows isn't "70% experienced" —
   they're unscored on that dimension, and the placeholder cap was
   quietly blending into the visible match percentage. Added
   `confidence_pct`: the share of the weighted score that came from
   real data, reported separately from `trip_match_pct`.

## Verified before including (not assumed)

The EATV (East Africa Tourist Visa) facts — $100 fee, 90-day multi-entry
validity, Kenya+Uganda+Rwanda membership, first-port-of-entry
application rule, immediate invalidation on exiting the bloc, Tanzania
explicitly excluded — checked against 8 independent sources and
included in `schema/005` with source URLs and a verification date.
Same for the Tanzania eVisa fee split ($100 US citizens / $50 most
other nationalities). Everything else in `visa_requirements` stays
unseeded on purpose — a missing row returns an explicit
"unverified — check with the embassy" response, never a guess.

## What I did NOT merge, and why

`schemas_trip_request.py`, `quote_engine_v2.py`, `trip_v2_additions.py`
(draft-checkout routes with Brevo email), and `models_drafts.py`
(`TripDraft`) were referenced throughout the shared pass but their
actual contents were never shown — only diffs against them. I wrote a
real, working `VisaIntelligenceEngine` from scratch (flagged clearly at
the top of that file) since only a schema for it existed, but I'm not
inventing full versions of the other four files — that's exactly the
"invent a plausible column and hope" problem this whole exercise exists
to avoid. If you have those files, share them and I'll wire them in
against what's actually there.

## Scope check worth having explicitly

This merge adds real product surface — multi-country routing, visa
intelligence, border-crossing validation — well beyond where the app
was a few turns ago (single-destination trips, basic operator
matching). Before rolling this out, confirm that's actually the
direction you want next, rather than it arriving as a side effect of
a code review.
