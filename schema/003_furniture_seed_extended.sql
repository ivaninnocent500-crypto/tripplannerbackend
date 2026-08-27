-- =====================================================================
-- EXTENDED SEED — fills in what 002_furniture_seed.sql left thin:
--   1. Days 2-7 of the Tanzania cabinet (002 only seeded Day 1)
--   2. Received quotes for ALL THREE operators, not just Safari Horizons,
--      so "Compare your quotes" actually has 3 rows to compare (matches
--      the screenshot) instead of 1
--   3. Mirrors (notifications) for each quote request
--   4. A second chest (balance payment, still pending) so the payment
--      lifecycle isn't just "one deposit and done"
--   5. A couple more footstools showing different validation categories
--
-- Depends on 002_furniture_seed.sql having run first — reuses cabinet
-- 'aaaaaaaa-0000-0000-0000-000000000001' and its Day 1 shelf
-- ('bbbbbbbb-0000-0000-0000-000000000001') without touching them.
--
-- Allocation across the 7 days (matches the "why this itinerary" copy
-- about "three consecutive nights in one place"):
--   Day 1        Arusha       (arrival — already seeded)
--   Day 2-3      Tarangire    (2 nights)
--   Day 4        Ngorongoro   (1 night)
--   Day 5-7      Serengeti    (3 nights, day 7 = departure)
-- =====================================================================

begin;

-- ---------------------------------------------------------------------
-- DAY 2 — Tarangire (arrival day: transfer + first activities)
-- ---------------------------------------------------------------------
insert into shelves (id, cabinet_id, day_number, date, destination_id, theme) values
('bbbbbbbb-0000-0000-0000-000000000002', 'aaaaaaaa-0000-0000-0000-000000000001',
 2, '2026-09-15', '11111111-1111-1111-1111-111111111113', 'Wildlife & wide horizons');

insert into drawers (shelf_id, name, description, start_time, duration_minutes, sort_order, activity_type, location_name) values
('bbbbbbbb-0000-0000-0000-000000000002', 'Breakfast', 'At the Arusha lodge before departure.', '07:00', 45, 1, 'MEAL', 'Arusha'),
('bbbbbbbb-0000-0000-0000-000000000002', 'Transfer to Tarangire', 'Private 4x4 through the highlands.', '08:00', 150, 2, 'TRANSFER', 'Arusha → Tarangire'),
('bbbbbbbb-0000-0000-0000-000000000002', 'Park entry', 'Gate formalities and orientation.', '10:30', 30, 3, 'ARRIVAL', 'Tarangire'),
('bbbbbbbb-0000-0000-0000-000000000002', 'Afternoon game drive', 'Elephant herds along the Tarangire River.', '16:00', 180, 4, 'EXPERIENCE', 'Tarangire'),
('bbbbbbbb-0000-0000-0000-000000000002', 'Sundowner', 'Drinks overlooking the river at dusk.', '18:30', 60, 5, 'EXPERIENCE', 'Tarangire');

insert into headboards (shelf_id, name, tier, check_in, check_out, nights) values
('bbbbbbbb-0000-0000-0000-000000000002', 'Tarangire River tented camp', 'luxury', '2026-09-15', '2026-09-17', 2);

insert into armrests (shelf_id, mode, description, duration_minutes, is_private) values
('bbbbbbbb-0000-0000-0000-000000000002', 'private_4x4', 'Private 4x4 · 2h 30m transfer', 150, true);

insert into trays (shelf_id, meal_type, included) values
('bbbbbbbb-0000-0000-0000-000000000002', 'breakfast', true),
('bbbbbbbb-0000-0000-0000-000000000002', 'lunch', true),
('bbbbbbbb-0000-0000-0000-000000000002', 'dinner', true);

-- ---------------------------------------------------------------------
-- DAY 3 — Tarangire (full day, second night)
-- ---------------------------------------------------------------------
insert into shelves (id, cabinet_id, day_number, date, destination_id, theme) values
('bbbbbbbb-0000-0000-0000-000000000003', 'aaaaaaaa-0000-0000-0000-000000000001',
 3, '2026-09-16', '11111111-1111-1111-1111-111111111113', 'Deeper into the park');

insert into drawers (shelf_id, name, description, start_time, duration_minutes, sort_order, activity_type, location_name) values
('bbbbbbbb-0000-0000-0000-000000000003', 'Early morning game drive', 'Best light and highest wildlife activity of the day.', '06:00', 240, 1, 'EXPERIENCE', 'Tarangire'),
('bbbbbbbb-0000-0000-0000-000000000003', 'Lunch at the lodge', null, '13:00', 60, 2, 'MEAL', 'Tarangire'),
('bbbbbbbb-0000-0000-0000-000000000003', 'Baobab valley walk', 'Guided walk among the ancient baobabs, armed ranger escort.', '16:00', 120, 3, 'EXPERIENCE', 'Tarangire');

insert into headboards (shelf_id, name, tier, check_in, check_out, nights) values
('bbbbbbbb-0000-0000-0000-000000000003', 'Tarangire River tented camp', 'luxury', '2026-09-15', '2026-09-17', 1);

insert into armrests (shelf_id, mode, description, duration_minutes, is_private) values
('bbbbbbbb-0000-0000-0000-000000000003', 'private_4x4', 'Private 4x4 · within-park game drives', 0, true);

insert into trays (shelf_id, meal_type, included) values
('bbbbbbbb-0000-0000-0000-000000000003', 'breakfast', true),
('bbbbbbbb-0000-0000-0000-000000000003', 'lunch', true),
('bbbbbbbb-0000-0000-0000-000000000003', 'dinner', true);

-- ---------------------------------------------------------------------
-- DAY 4 — Ngorongoro
-- ---------------------------------------------------------------------
insert into shelves (id, cabinet_id, day_number, date, destination_id, theme) values
('bbbbbbbb-0000-0000-0000-000000000004', 'aaaaaaaa-0000-0000-0000-000000000001',
 4, '2026-09-17', '11111111-1111-1111-1111-111111111112', 'Wildlife & wide horizons');

insert into drawers (shelf_id, name, description, start_time, duration_minutes, sort_order, activity_type, location_name) values
('bbbbbbbb-0000-0000-0000-000000000004', 'Breakfast', null, '07:00', 45, 1, 'MEAL', 'Tarangire'),
('bbbbbbbb-0000-0000-0000-000000000004', 'Transfer to Ngorongoro', 'Climbing the crater highlands.', '08:00', 165, 2, 'TRANSFER', 'Tarangire → Ngorongoro'),
('bbbbbbbb-0000-0000-0000-000000000004', 'Crater descent', 'Descent to the crater floor for a full-day game drive.', '11:00', 60, 3, 'TRANSFER', 'Ngorongoro Crater'),
('bbbbbbbb-0000-0000-0000-000000000004', 'Crater floor game drive', 'One of the densest wildlife concentrations in Africa.', '12:00', 240, 4, 'EXPERIENCE', 'Ngorongoro Crater');

insert into headboards (shelf_id, name, tier, check_in, check_out, nights) values
('bbbbbbbb-0000-0000-0000-000000000004', 'Crater rim luxury lodge', 'luxury', '2026-09-17', '2026-09-18', 1);

insert into armrests (shelf_id, mode, description, duration_minutes, is_private) values
('bbbbbbbb-0000-0000-0000-000000000004', 'private_4x4', 'Private 4x4 · 2h 45m transfer', 165, true);

insert into trays (shelf_id, meal_type, included) values
('bbbbbbbb-0000-0000-0000-000000000004', 'breakfast', true),
('bbbbbbbb-0000-0000-0000-000000000004', 'lunch', true),
('bbbbbbbb-0000-0000-0000-000000000004', 'dinner', true);

-- ---------------------------------------------------------------------
-- DAY 5 — Serengeti (arrival day, first of three nights)
-- ---------------------------------------------------------------------
insert into shelves (id, cabinet_id, day_number, date, destination_id, theme) values
('bbbbbbbb-0000-0000-0000-000000000005', 'aaaaaaaa-0000-0000-0000-000000000001',
 5, '2026-09-18', '11111111-1111-1111-1111-111111111111', 'Wildlife & wide horizons');

insert into drawers (shelf_id, name, description, start_time, duration_minutes, sort_order, activity_type, location_name) values
('bbbbbbbb-0000-0000-0000-000000000005', 'Breakfast', null, '07:00', 45, 1, 'MEAL', 'Ngorongoro'),
('bbbbbbbb-0000-0000-0000-000000000005', 'Transfer to Serengeti', 'Longest transfer of the trip, broken up with scenic stops.', '08:00', 210, 2, 'TRANSFER', 'Ngorongoro → Serengeti'),
('bbbbbbbb-0000-0000-0000-000000000005', 'Afternoon game drive', 'First game drive in the Serengeti''s central plains.', '16:00', 150, 3, 'EXPERIENCE', 'Serengeti'),
('bbbbbbbb-0000-0000-0000-000000000005', 'Sundowner', 'Drinks on the plains as the sun sets.', '18:30', 60, 4, 'EXPERIENCE', 'Serengeti');

insert into headboards (shelf_id, name, tier, check_in, check_out, nights) values
('bbbbbbbb-0000-0000-0000-000000000005', 'Serengeti luxury tented camp', 'luxury', '2026-09-18', '2026-09-21', 3);

insert into armrests (shelf_id, mode, description, duration_minutes, is_private) values
('bbbbbbbb-0000-0000-0000-000000000005', 'private_4x4', 'Private 4x4 · 3h 30m transfer', 210, true);

insert into trays (shelf_id, meal_type, included) values
('bbbbbbbb-0000-0000-0000-000000000005', 'breakfast', true),
('bbbbbbbb-0000-0000-0000-000000000005', 'lunch', true),
('bbbbbbbb-0000-0000-0000-000000000005', 'dinner', true);

-- ---------------------------------------------------------------------
-- DAY 6 — Serengeti (full day, second night)
-- ---------------------------------------------------------------------
insert into shelves (id, cabinet_id, day_number, date, destination_id, theme) values
('bbbbbbbb-0000-0000-0000-000000000006', 'aaaaaaaa-0000-0000-0000-000000000001',
 6, '2026-09-19', '11111111-1111-1111-1111-111111111111', 'Deeper into the park');

insert into drawers (shelf_id, name, description, start_time, duration_minutes, sort_order, activity_type, location_name) values
('bbbbbbbb-0000-0000-0000-000000000006', 'Early morning game drive', 'Predator activity is highest at first light.', '06:00', 240, 1, 'EXPERIENCE', 'Serengeti'),
('bbbbbbbb-0000-0000-0000-000000000006', 'Bush breakfast', 'Breakfast set up in the field mid-drive.', '09:30', 45, 2, 'MEAL', 'Serengeti'),
('bbbbbbbb-0000-0000-0000-000000000006', 'Midday rest', 'Free time at camp during the hottest hours.', '12:30', 180, 3, 'FREE_TIME', 'Serengeti'),
('bbbbbbbb-0000-0000-0000-000000000006', 'Evening game drive', 'Second drive of the day, different sector of the park.', '16:00', 180, 4, 'EXPERIENCE', 'Serengeti');

insert into headboards (shelf_id, name, tier, check_in, check_out, nights) values
('bbbbbbbb-0000-0000-0000-000000000006', 'Serengeti luxury tented camp', 'luxury', '2026-09-18', '2026-09-21', 2);

insert into armrests (shelf_id, mode, description, duration_minutes, is_private) values
('bbbbbbbb-0000-0000-0000-000000000006', 'private_4x4', 'Private 4x4 · within-park game drives', 0, true);

insert into trays (shelf_id, meal_type, included) values
('bbbbbbbb-0000-0000-0000-000000000006', 'breakfast', true),
('bbbbbbbb-0000-0000-0000-000000000006', 'lunch', true),
('bbbbbbbb-0000-0000-0000-000000000006', 'dinner', true);

-- ---------------------------------------------------------------------
-- DAY 7 — Serengeti / Departure
-- ---------------------------------------------------------------------
insert into shelves (id, cabinet_id, day_number, date, destination_id, theme) values
('bbbbbbbb-0000-0000-0000-000000000007', 'aaaaaaaa-0000-0000-0000-000000000001',
 7, '2026-09-20', '11111111-1111-1111-1111-111111111111', 'Departure');

insert into drawers (shelf_id, name, description, start_time, duration_minutes, sort_order, activity_type, location_name) values
('bbbbbbbb-0000-0000-0000-000000000007', 'Breakfast', null, '07:00', 45, 1, 'MEAL', 'Serengeti'),
('bbbbbbbb-0000-0000-0000-000000000007', 'Transfer to airstrip', 'Light-aircraft transfer to Kilimanjaro International.', '09:00', 120, 2, 'TRANSFER', 'Serengeti → Airstrip'),
('bbbbbbbb-0000-0000-0000-000000000007', 'Departure', 'Scheduled flight connection home.', '12:00', 30, 3, 'DEPARTURE', 'Kilimanjaro International');

insert into headboards (shelf_id, name, tier, check_in, check_out, nights) values
('bbbbbbbb-0000-0000-0000-000000000007', 'Serengeti luxury tented camp', 'luxury', '2026-09-18', '2026-09-21', 1);

insert into armrests (shelf_id, mode, description, duration_minutes, is_private) values
('bbbbbbbb-0000-0000-0000-000000000007', 'private_4x4', 'Private 4x4 · 2h transfer to airstrip', 120, true);

insert into trays (shelf_id, meal_type, included) values
('bbbbbbbb-0000-0000-0000-000000000007', 'breakfast', true);

-- ---------------------------------------------------------------------
-- MORE FOOTSTOOLS — additional validation categories beyond the two
-- 'info' rows already in 002_furniture_seed.sql
-- ---------------------------------------------------------------------
insert into footstools (cabinet_id, severity, category, message, auto_repaired) values
('aaaaaaaa-0000-0000-0000-000000000001', 'info', 'geography', 'Route sequenced Arusha → Tarangire → Ngorongoro → Serengeti with no backtracking.', false),
('aaaaaaaa-0000-0000-0000-000000000001', 'info', 'preferences', 'Wildlife-focused request matched with 9 dedicated game-drive activities.', false),
('aaaaaaaa-0000-0000-0000-000000000001', 'warning', 'transport', 'Ngorongoro → Serengeti transfer runs 3h 30m — longest single leg of the trip.', false);

-- ---------------------------------------------------------------------
-- QUOTES FOR THE OTHER TWO OPERATORS — 002 only seeded a received quote
-- for Safari Horizons (bench 1). Filling in Wild Africa Journeys
-- (bench 2, $4,690pp) and SafariCo Tanzania (bench 3, $3,780pp) so
-- "Compare your quotes" has all three rows, matching the screenshot.
-- ---------------------------------------------------------------------
update benches set status = 'quote_received', responded_at = now() - interval '90 minutes'
where id = 'cccccccc-0000-0000-0000-000000000002';

insert into counters (
  bench_id, price_per_person, currency, validity_date,
  accommodation_summary, activities_summary, transport_summary, meals_summary,
  park_fees_included, transfers_included, difference_notes, status
) values (
  'cccccccc-0000-0000-0000-000000000002', 4690, 'USD', '2026-08-21',
  'Premium lodge · 6 nights', '9 drives + guided walk', 'Private 4x4 + dedicated guide', 'All meals',
  true, true, 'Two lodge upgrades over the original itinerary', 'received'
);

update benches set status = 'quote_received', responded_at = now() - interval '10 minutes'
where id = 'cccccccc-0000-0000-0000-000000000003';

insert into counters (
  bench_id, price_per_person, currency, validity_date,
  accommodation_summary, activities_summary, transport_summary, meals_summary,
  park_fees_included, transfers_included, difference_notes, status
) values (
  'cccccccc-0000-0000-0000-000000000003', 3780, 'USD', '2026-08-18',
  'Luxury+ lodge · 6 nights', '8 game drives', 'Private 4x4', 'All meals',
  true, false, 'One night outside the park boundary', 'received'
);

-- ---------------------------------------------------------------------
-- MIRRORS — notifications for each quote request, matching "we follow
-- up with operators for you and notify you the moment a quote lands"
-- ---------------------------------------------------------------------
insert into mirrors (cabinet_id, bench_id, channel, message, sent) values
('aaaaaaaa-0000-0000-0000-000000000001', 'cccccccc-0000-0000-0000-000000000001', 'push', 'Safari Horizons sent your quote — $4,280 per person.', true),
('aaaaaaaa-0000-0000-0000-000000000001', 'cccccccc-0000-0000-0000-000000000002', 'push', 'Wild Africa Journeys sent your quote — $4,690 per person.', true),
('aaaaaaaa-0000-0000-0000-000000000001', 'cccccccc-0000-0000-0000-000000000003', 'push', 'SafariCo Tanzania sent your quote — $3,780 per person.', true);

-- ---------------------------------------------------------------------
-- CHESTS — add the balance payment (still pending) so the booking shows
-- a full lifecycle, not just a single completed deposit
-- ---------------------------------------------------------------------
insert into chests (wardrobe_id, amount, currency, payment_type, status, paid_at) values
('dddddddd-0000-0000-0000-000000000001', 6848, 'USD', 'balance', 'pending', null);

commit;
