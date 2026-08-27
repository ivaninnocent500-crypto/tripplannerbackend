# Data Entry Guide — Africa Travel OS Database

Read this before adding anything in the Supabase SQL editor. The single
biggest source of errors will be treating the wrong table as "the
database to fill in." There are two completely different systems here.

---

## The one rule that prevents most mistakes

| | Knowledge base (schema 001) | Furniture tables (schema 002/003) |
|---|---|---|
| **What it is** | Permanent facts about Africa: parks, lodges, activities, operators, drive times, wildlife calendars | One specific trip a specific person is building right now |
| **Tables** | `travel_places`, `lodges`, `activities`, `tour_operators`, `drive_times`, `wildlife`, `seasons`, `entry_fees`, etc. | `cabinets`, `shelves`, `drawers`, `headboards`, `armrests`, `trays`, `hinges`, `stools`, `benches`, `counters`, `wardrobes`, `chests`, `mirrors` |
| **Who fills it in** | **Your team, by hand, ongoing** — this is the actual content work | **The backend, automatically**, every time someone generates a trip |
| **When to add rows manually** | Whenever you have real content: a new lodge, a confirmed drive time, a verified operator | Almost never — see the one exception below |

**If someone says "we need more real data in the database," 95% of the
time they mean the knowledge base, not the furniture tables.** Adding
rows to `cabinets`/`shelves`/etc. by hand creates fake trips that don't
belong to any real person and will just clutter the app's "my trips"
lists once real users exist. Don't do it in production.

---

## Part 1 — The one legitimate reason to touch a furniture table by hand

Right now there's no webhook that automatically records an operator's
reply when they email or call back with a price. Until that exists,
your ops team needs to type the quote in manually. This is the *only*
routine manual write to a furniture table in production.

**Where it goes:** the `counters` table (one row = one quote from one
operator, attached to the `benches` row that represents the original
request).

**Exact steps:**

1. Find the `bench_id` — the request that's getting a reply:
   ```sql
   select id, cabinet_id, tour_operator_id, status
   from benches
   where cabinet_id = '<the cabinet_id from the app>'
   order by requested_at desc;
   ```
2. Insert the quote, using values the operator actually gave you —
   never invent a price or a validity date:
   ```sql
   insert into counters (
     bench_id, price_per_person, currency, validity_date,
     accommodation_summary, activities_summary, transport_summary,
     meals_summary, park_fees_included, transfers_included,
     difference_notes, status
   ) values (
     '<bench_id from step 1>',
     4280.00, 'USD', '2026-08-24',
     'Luxury lodge · 6 nights', '9 game drives', 'Private 4x4', 'All meals',
     true, true,
     'Closest to the original itinerary',  -- or null if nothing to note
     'received'
   );
   ```
3. Mark the request as answered:
   ```sql
   update benches set status = 'quote_received', responded_at = now()
   where id = '<bench_id from step 1>';
   ```

**Valid values — copy these exactly, anything else throws a constraint error:**
- `counters.status`: `'received'` · `'accepted'` · `'declined'` · `'expired'`
- `benches.status`: `'request_sent'` · `'operator_reviewing'` · `'quote_received'` · `'expired'` · `'declined'`
- `park_fees_included` / `transfers_included`: `true` / `false`, never `'yes'`/`'no'`

**Never do this instead:** don't insert directly into `cabinets`,
`shelves`, `drawers`, `headboards`, `armrests`, or `trays` to "fix" or
"add to" someone's itinerary. Those are only ever written by
`ItineraryPlanningEngine` — hand-editing them will desync from what the
app's validation engine already checked, and there's no code path that
re-validates a manually-edited itinerary.

---

## Part 2 — Adding real knowledge-base content (the actual ongoing work)

This is where "fill in more real data for production" really applies.

### Insertion order matters — respect foreign keys

Postgres will reject a row that references something that doesn't
exist yet. Always insert in this order:

1. `travel_places` first — everything else points at it
2. `physical_geography`, `park_gates`, `drive_times`, `airports`,
   `airstrips`, `lodges`, `activities` — anything with `destination_id`
3. `wildlife` (master species list — insert once, ever, per species)
4. `destination_wildlife`, `wildlife_calendar` — link wildlife to a destination, after both exist
5. `tour_operators` (master list — insert once per operator)
6. `destination_tour_operators` — link an operator to a destination, after both exist

If you get a `foreign key violation` (Postgres error `23503`), it means
you inserted a child row before its parent. Fix the order, not the data.

### Required fields and valid values — the ones that actually bite

**`travel_places`** (do this one first, always):
- `name`, `slug`, `destination_type`, `country` are `not null` — the
  insert fails without them
- `slug` must be **unique** — check first: `select 1 from travel_places
  where slug = 'your-new-slug';` — if that returns a row, pick a
  different slug or you'll get error `23505` (unique violation)
- `destination_type` must be exactly one of: `national_park`,
  `game_reserve`, `island`, `beach`, `mountain`, `city`,
  `cultural_site`, `unesco_site`, `lake`, `desert`, `waterfall`,
  `marine_park`, `forest_reserve`, `wetland`
- `country` must be a real two-letter code from the enum (`TZ`, `KE`,
  `UG`, `RW`, `ZA`, `NA`, `BW`, `ZW`, `ZM`, `MZ`, `MW`, `ET`, `MG`,
  `SC`, `MU`, `TN`, `MA`, `EG`, `GH`, `SN`, `CI`, `NG`, `GA`, `CD`,
  `CM`, `BI`, `SS`, `DJ`, `ER`, `SO`, `AO`, `LS`, `SZ`, `CV`, `ST`,
  `GM`, `GW`, `SL`, `LR`, `BF`, `ML`, `NE`, `TD`, `CF`, `CG`, `GQ`,
  `TG`, `BJ`, `MR`, `DZ`, `LY`, `SD`, `EH`, `KM`) — a country not on
  this list needs a schema migration first, don't guess a close one

**`lodges`**:
- `destination_id`, `name`, `tier` are `not null`
- `tier` must be exactly: `ultra_luxury`, `luxury`, `mid_range`,
  `budget`, or `camping`
- `star_rating` must be between 0 and 5 or the insert fails on the
  check constraint

**`activities`**:
- `destination_id`, `name`, `category` are `not null`
- `category` must be exactly one of: `game_drive`, `walking_safari`,
  `boat_safari`, `balloon_safari`, `birding`, `photography`,
  `cultural_visit`, `hiking`, `diving`, `snorkeling`, `fishing`,
  `beach_leisure`, `mountain_climbing`, `canoeing`, `horseback_safari`,
  `night_drive`, `cycling`, `camping`, `shopping`, `spa_wellness`

**`drive_times`**:
- `destination_id`, `origin_name`, `destination_name`, `distance_km`,
  `duration_minutes_dry_season` are all `not null`
- This is the table the itinerary engine leans on hardest to reject
  physically-impossible schedules — a missing row here just means the
  engine falls back to a generic estimate and flags it, it won't crash,
  but the itinerary will be less accurate. Prioritize filling this in
  for any route your operators actually drive.

**`tour_operators`**:
- `name` is the only `not null` field, but `verification_status`
  defaults to `'unverified'` — **operators sitting at `'unverified'`
  are invisible to `OperatorMatchEngine`**, which only matches
  `verification_status = 'verified'`. If a real operator you've
  verified isn't showing up in the app, this is almost always why:
  ```sql
  update tour_operators set verification_status = 'verified', verification_date = current_date
  where id = '<operator id>';
  ```
- `verification_status` must be exactly: `verified`,
  `pending_verification`, `unverified`, or `suspended`

**`destination_tour_operators`** (this is what makes an operator
eligible to be matched for a given park):
- An operator with zero rows here for a destination will never be
  suggested for a trip that includes it, even if fully verified
  elsewhere. If you add a new operator, add their coverage here too.

### Every table has `id uuid ... default gen_random_uuid()`

Don't generate UUIDs yourself and don't type one in by hand unless
you're deliberately linking to a row whose UUID you already know (e.g.
`destination_id` pointing at an existing `travel_places.id`). Leave
`id` out of your `insert` column list entirely and let Postgres
generate it:

```sql
-- Right:
insert into travel_places (name, slug, destination_type, country) values (...);

-- Wrong — invites a typo that produces error 22P02 (invalid uuid syntax):
insert into travel_places (id, name, slug, destination_type, country)
values ('11111111-1111-1111-1111-11111111111', ...);  -- one digit short
```

---

## Part 3 — Before you run anything against production

1. **Test in a transaction you can undo.** Wrap your inserts in
   `begin; ... ; rollback;` first, check the row counts/output look
   right, then re-run the same statements with `commit;` at the end
   instead of `rollback;`. This costs nothing and catches most mistakes
   before they're permanent.
2. **Insert one row first, not a batch.** If you're adding 40 lodges
   from a spreadsheet, insert row 1 alone, verify it in the table
   editor, then run the rest. A batch insert either all succeeds or
   all fails — better to find a bad row early.
3. **Check uniqueness before you insert**, don't rely on the error to
   tell you:
   ```sql
   select 1 from travel_places where slug = 'planned-slug';
   ```
4. **Never bulk-`update`/`delete` without a `where` clause you've
   tested as a `select` first.** Run the `select` version, look at what
   it would touch, then change to `update`/`delete`.

### Error messages you'll actually see, and what they mean

| Error | Meaning | Fix |
|---|---|---|
| `22P02: invalid input syntax for type uuid` | A UUID string is malformed (wrong length, non-hex character) | Check for typos, or let Postgres generate it instead |
| `23505: duplicate key value violates unique constraint` | You're inserting a `slug`/`scientific_name`/etc. that already exists | Check with `select` first; update the existing row instead if that's the real intent |
| `23503: insert or update violates foreign key constraint` | Referenced a `destination_id`/`lodge_id`/etc. that doesn't exist yet | Insert the parent row first, or double-check the id you copied |
| `23502: null value violates not-null constraint` | A required field was left out | Check the table's `not null` columns above before inserting |
| `23514: violates check constraint` | A value isn't in the allowed enum/range (e.g. `tier = 'fancy'` instead of `'luxury'`) | Use the exact valid values listed above |
| `42P01: relation does not exist` | Table name typo, or you're on the wrong schema/project | Re-check spelling; confirm you're connected to the right Supabase project |

If an error doesn't match anything above, stop and ask rather than
retrying with slightly different values — silently "fixing" an error by
guessing can leave inconsistent data (e.g. a lodge with a `destination_id`
that resolves to the wrong park) that's much harder to spot later than a
failed insert.
