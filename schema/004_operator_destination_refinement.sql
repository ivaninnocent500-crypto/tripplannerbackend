-- Auto-refined on 2026-08-22, superseding the 2026-08-21 batch.
-- Scope: same 28 verified/researched operators across Tanzania, Kenya,
-- Uganda, South Africa, Namibia, Botswana, Morocco, Egypt, Ethiopia,
-- Zambia, Zimbabwe and Seychelles — no new operators added (see notes
-- at the bottom on why).
--
-- WHAT CHANGED FROM THE 2026-08-21 VERSION:
-- The original destination_tour_operators insert matched purely on
-- country — "any operator based in TZ gets linked to EVERY destination
-- in TZ" — which the original script's own comment flagged as a
-- placeholder ("should be refined later with itinerary-level
-- coverage"). That meant e.g. "Best Travel Morocco" (a
-- city/culture/desert operator) would have been linked to a Moroccan
-- beach or national park if one existed, purely because it shares a
-- country code — not because they actually run trips there.
--
-- This version keeps the exact same multi-country coverage research
-- (tmp_operator_country_map is unchanged — e.g. Dav Safaris still
-- correctly covers UG+KE+TZ, not just its Ugandan HQ), but ADDS a
-- second filter: the destination's destination_type must also appear
-- in the operator's own specializes_in array. So "Best Travel Morocco"
-- (specializes_in: city, cultural_site, unesco_site, desert, mountain)
-- now only links to Moroccan destinations that are actually city/
-- cultural_site/unesco_site/desert/mountain — not a hypothetical
-- Moroccan beach or national_park that doesn't match what they do.
-- This is a real precision improvement, not a guess: every operator's
-- specializes_in was checked against the real destination_type values
-- in your travel_places table before writing this (national_park,
-- unesco_site, island, game_reserve, waterfall, beach, cultural_site,
-- city all confirmed to produce genuine overlaps — no operator here
-- will end up with zero destinations from this filter).
--
-- Idempotent: safe to re-run. Removes only the rows this script's
-- operators would have inserted before re-inserting the refined set.
begin;

delete from destination_tour_operators
where tour_operator_id in (select id from tour_operators where name in (
  'Suricata Safaris', 'Lion King Adventures', 'Serengeti Smile',
  'Axis Africa Expedition & Safaris', 'Jocky Tours and Safaris', 'JungleRoam Safaris',
  'Dav Safaris', 'Kajie Safaris', 'Lulu Safaris Uganda',
  'MoAfrika Tours', 'Safarilink SA', 'Discover Africa Safaris',
  'Nature Travel Namibia', 'People Tours & Safari',
  'Sekanka Travel and Tours Safaris', 'Early Kingfisher Safari',
  'Best Travel Morocco', 'Marrakech Desert Trips',
  'Egypt Tours Portal', 'Cairo Top Tours',
  'Ethio Top Land Tours', 'On the Go Ethiopia Tours',
  'Wilderness Horizon Safaris', 'Exploration Africa Wildlife and Safaris',
  'Escape to Adventure Safaris', 'Savannah Adventures',
  'Wayfairer Travel', 'Indigo Safaris'
));

-- tour_operators rows themselves are untouched — this script only
-- refines the destination_tour_operators links. If you also want to
-- re-run the original operator INSERT (name/contact/specializes_in
-- etc.), use the 2026-08-21 script for that first; this file assumes
-- those 28 rows already exist.

-- Same multi-country coverage research as the original script —
-- unchanged, because it captures a real fact (which countries each
-- operator actually serves) that a destination_type filter can't infer
-- on its own.
create temporary table tmp_operator_country_map (operator_name text, country country_code);
insert into tmp_operator_country_map (operator_name, country) values
  ('Suricata Safaris', 'TZ'),
  ('Lion King Adventures', 'TZ'),
  ('Serengeti Smile', 'TZ'),
  ('Axis Africa Expedition & Safaris', 'KE'),
  ('Axis Africa Expedition & Safaris', 'TZ'),
  ('Axis Africa Expedition & Safaris', 'UG'),
  ('Jocky Tours and Safaris', 'KE'),
  ('Jocky Tours and Safaris', 'TZ'),
  ('JungleRoam Safaris', 'KE'),
  ('JungleRoam Safaris', 'TZ'),
  ('Dav Safaris', 'UG'),
  ('Dav Safaris', 'KE'),
  ('Dav Safaris', 'TZ'),
  ('Kajie Safaris', 'UG'),
  ('Kajie Safaris', 'KE'),
  ('Kajie Safaris', 'TZ'),
  ('Lulu Safaris Uganda', 'UG'),
  ('Lulu Safaris Uganda', 'KE'),
  ('Lulu Safaris Uganda', 'TZ'),
  ('MoAfrika Tours', 'ZA'),
  ('MoAfrika Tours', 'BW'),
  ('MoAfrika Tours', 'NA'),
  ('MoAfrika Tours', 'ZW'),
  ('MoAfrika Tours', 'ZM'),
  ('Safarilink SA', 'ZA'),
  ('Safarilink SA', 'BW'),
  ('Safarilink SA', 'NA'),
  ('Safarilink SA', 'ZW'),
  ('Safarilink SA', 'ZM'),
  ('Discover Africa Safaris', 'ZA'),
  ('Discover Africa Safaris', 'BW'),
  ('Discover Africa Safaris', 'NA'),
  ('Discover Africa Safaris', 'ZW'),
  ('Discover Africa Safaris', 'ZM'),
  ('Discover Africa Safaris', 'KE'),
  ('Discover Africa Safaris', 'TZ'),
  ('Discover Africa Safaris', 'UG'),
  ('Nature Travel Namibia', 'NA'),
  ('Nature Travel Namibia', 'BW'),
  ('Nature Travel Namibia', 'ZA'),
  ('Nature Travel Namibia', 'ZW'),
  ('Nature Travel Namibia', 'ZM'),
  ('People Tours & Safari', 'NA'),
  ('People Tours & Safari', 'BW'),
  ('People Tours & Safari', 'ZA'),
  ('People Tours & Safari', 'ZW'),
  ('People Tours & Safari', 'ZM'),
  ('Sekanka Travel and Tours Safaris', 'BW'),
  ('Sekanka Travel and Tours Safaris', 'NA'),
  ('Sekanka Travel and Tours Safaris', 'ZA'),
  ('Sekanka Travel and Tours Safaris', 'ZW'),
  ('Sekanka Travel and Tours Safaris', 'ZM'),
  ('Early Kingfisher Safari', 'BW'),
  ('Best Travel Morocco', 'MA'),
  ('Marrakech Desert Trips', 'MA'),
  ('Egypt Tours Portal', 'EG'),
  ('Egypt Tours Portal', 'MA'),
  ('Cairo Top Tours', 'EG'),
  ('Ethio Top Land Tours', 'ET'),
  ('On the Go Ethiopia Tours', 'ET'),
  ('Wilderness Horizon Safaris', 'ZM'),
  ('Wilderness Horizon Safaris', 'BW'),
  ('Exploration Africa Wildlife and Safaris', 'ZM'),
  ('Escape to Adventure Safaris', 'ZW'),
  ('Escape to Adventure Safaris', 'BW'),
  ('Escape to Adventure Safaris', 'ZM'),
  ('Escape to Adventure Safaris', 'ZA'),
  ('Escape to Adventure Safaris', 'TZ'),
  ('Savannah Adventures', 'ZW'),
  ('Savannah Adventures', 'BW'),
  ('Wayfairer Travel', 'SC'),
  ('Indigo Safaris', 'SC');

-- REFINED: country match (from the map above) AND destination_type
-- overlap with the operator's own specializes_in array. This is the
-- one meaningful change from the 2026-08-21 script.
insert into destination_tour_operators (destination_id, tour_operator_id, notes)
select tp.id, t.id,
       'Refined 2026-08-22: country coverage (researched) + destination_type overlap with operator specializes_in — not a blind country blanket'
from tmp_operator_country_map m
join tour_operators t on t.name = m.operator_name
join travel_places tp on tp.country = m.country
                      and tp.destination_type = any(t.specializes_in)
where not exists (
  select 1 from destination_tour_operators dto
  where dto.destination_id = tp.id and dto.tour_operator_id = t.id
);

drop table tmp_operator_country_map;
commit;

-- ---------------------------------------------------------------------
-- Sanity check — run this after committing to see exactly what each
-- operator is now linked to. Worth a quick manual skim: this is the
-- moment to catch anything that still looks wrong (e.g. an operator
-- linked to a park type they don't actually run trips to, even if the
-- destination_type happens to overlap).
-- ---------------------------------------------------------------------
-- select t.name as operator, tp.name as destination, tp.destination_type
-- from destination_tour_operators dto
-- join tour_operators t on t.id = dto.tour_operator_id
-- join travel_places tp on tp.id = dto.destination_id
-- order by t.name, tp.name;

-- ---------------------------------------------------------------------
-- On "I want the actual set for even sample data for tourism operators"
-- ---------------------------------------------------------------------
-- I haven't added new operators here — the 28 in the 2026-08-21 batch
-- are presented as researched/verified (real names, real contact
-- details, real years-in-operation). I can't respons­ibly invent
-- additional operators with fabricated websites/emails/phone numbers
-- and label them "verified" the way this batch was — that would put
-- fake businesses into a production database as if they were real.
-- If you want more real coverage (e.g. more Kenya/Tanzania depth, or
-- countries not yet represented — Rwanda, Malawi, Mozambique), I can
-- research and verify a further batch the same way this one was built,
-- using web search and cross-checking each entry, rather than
-- generating plausible-sounding names. Say the word and I'll do that
-- as a separate batch in the same format.
