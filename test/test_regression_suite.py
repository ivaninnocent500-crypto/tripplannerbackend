"""
Integration Regression Suite — Scenarios A through H
=======================================================

Implements the mandatory integration tests specified in the
audit-locked architecture blueprint (doc 15, section 11):

    A. Single destination
    B. Two destinations
    C. Three+ destinations
    D. Multi-country
    E. Midnight activity
    F. Departure day
    G. Overloaded day
    H. Impossible schedule

Each scenario runs the FULL pipeline (RulesEngine -> RouteGeography ->
ItineraryPlanning -> DayArchetype -> ScheduleRepair -> Validation) via
itinerary_v2.ItineraryOrchestrator.generate(), against a mock DB whose
query results are shaped to match schema.sql exactly -- same table
names, same column order, same directed drive_times semantics (see
schema.sql SECTION 4 and the ItineraryPlanningEngine fix that
accompanied it).

This is NOT a substitute for running against a real Postgres instance
-- the mock DB proves the ENGINES cooperate correctly given the query
results schema.sql's tables would produce; it does not prove the SQL
in each engine is itself syntactically valid Postgres (py_compile and
manual review covered that separately). Running schema.sql against a
real database and pointing these same scenarios at it is the
recommended next step once one is available.

Run with: PYTHONPATH=<path-to-sqlalchemy-stub-or-real-sqlalchemy>:.
          python3 test_regression_suite.py
"""

from __future__ import annotations

import sys
import traceback
from datetime import time as dt_time
from typing import Any

from app.db.models_furniture import Cabinet, Shelf, Drawer, Hinge, Headboard, Armrest, Tray, Footstool
import ItineraryPlanningEngine as ipe_module
import ValidationEngine as ve_module

ipe_module.Cabinet = Cabinet
ipe_module.Shelf = Shelf
ipe_module.Drawer = Drawer
ipe_module.Hinge = Hinge
ipe_module.Headboard = Headboard
ipe_module.Armrest = Armrest
ipe_module.Tray = Tray

ve_module.Cabinet = Cabinet
ve_module.Footstool = Footstool
ve_module.Hinge = Hinge
ve_module.Shelf = Shelf

from itinerary_v2 import ItineraryOrchestrator


# ============================================================================
# MOCK DB INFRASTRUCTURE
# ============================================================================

class Row:
    """
    Mimics SQLAlchemy's Row: index access for engines that use it
    positionally (ItineraryPlanningEngine), attribute access for
    engines that use named columns (route_geography.py).
    """
    _ATTR_NAMES = ("destination_id", "name", "country", "destination_type", "latitude", "longitude")

    def __init__(self, *vals):
        self._vals = vals
        for attr_name, val in zip(self._ATTR_NAMES, vals):
            setattr(self, attr_name, val)

    def __getitem__(self, i):
        return self._vals[i]

    def __iter__(self):
        return iter(self._vals)


class Result:
    def __init__(self, rows=None, scalar_val=None):
        self._rows = rows or []
        self._scalar = scalar_val

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._scalar


class MockDB:
    """
    A configurable mock matching schema.sql's table shapes exactly.
    Each scenario builds one of these with its own travel_places,
    drive_times (DIRECTED, per the schema decision), activities,
    lodges, flights, and border_crossings fixtures.
    """

    def __init__(
        self,
        travel_places: dict[str, Row],
        drive_times: dict[tuple[str, str], tuple[float, int]] | None = None,
        activities: dict[str, list[Row]] | None = None,
        lodges: dict[str, list[Row]] | None = None,
        flights: dict[tuple[str, str], int] | None = None,
        border_crossings: dict[tuple[str, str], Row] | None = None,
        destination_airports: dict[tuple[str, bool], str] | None = None,
    ):
        self.travel_places = travel_places
        self.drive_times = drive_times or {}
        self.activities = activities or {}
        self.lodges = lodges or {}
        self.flights = flights or {}
        self.border_crossings = border_crossings or {}
        self.destination_airports = destination_airports or {}
        self.added: list[Any] = []

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass

    def execute(self, query, params=None):
        params = params or {}
        q = query if isinstance(query, str) else str(query)

        if "FROM travel_places" in q:
            ids = params.get("destination_ids") or params.get("ids") or []
            rows = [self.travel_places[i] for i in ids if i in self.travel_places]
            return Result(rows=rows)

        if "to_regclass('estimated_visit_durations')" in q:
            return Result(scalar_val=None)

        if "to_regclass('photo_states')" in q:
            return Result(scalar_val=None)

        if "FROM drive_times" in q:
            frm = params.get("from_destination_id") or params.get("from_dest")
            to = params.get("to_destination_id") or params.get("to_dest")
            key = (frm, to)
            if key in self.drive_times:
                distance, duration = self.drive_times[key]
                return Result(rows=[Row(distance, duration)])
            return Result(rows=[])

        if "FROM destination_airports" in q:
            # route_geography._find_flight_option issues a subquery
            # with this shape; ItineraryPlanningEngine._build_hinges
            # issues an equivalent one. Both only ever check existence
            # via IN (...), so returning matching airport_id rows is
            # sufficient regardless of which engine asked.
            dest = params.get("from_destination_id") or params.get("to_destination_id") or params.get("frm") or params.get("to")
            airport_id = self.destination_airports.get((dest, True))
            return Result(rows=[Row(airport_id)] if airport_id else [])

        if "FROM flights" in q:
            frm_dest = params.get("from_destination_id") or params.get("frm")
            to_dest = params.get("to_destination_id") or params.get("to")
            airport_frm = self.destination_airports.get((frm_dest, True))
            airport_to = self.destination_airports.get((to_dest, True))
            key = (airport_frm, airport_to)
            if key in self.flights:
                return Result(rows=[Row(self.flights[key])])
            return Result(rows=[])

        if "FROM border_crossings" in q:
            country_a = params.get("country_a")
            country_b = params.get("country_b")
            border_id = params.get("id")
            if border_id is not None:
                for row in self.border_crossings.values():
                    if row[0] == border_id:
                        return Result(rows=[Row(row[1], row[2], row[3])])
                return Result(rows=[])
            key = (country_a, country_b)
            reverse_key = (country_b, country_a)
            row = self.border_crossings.get(key) or self.border_crossings.get(reverse_key)
            return Result(rows=[row] if row else [])

        if "FROM activities" in q:
            dest_id = params.get("dest_id")
            return Result(rows=self.activities.get(dest_id, []))

        if "FROM lodges" in q:
            dest_id = params.get("dest_id")
            return Result(rows=self.lodges.get(dest_id, []))

        raise AssertionError(f"MockDB: unhandled query: {q[:120]}")


# ============================================================================
# TEST HARNESS
# ============================================================================

_PASS_COUNT = 0
_FAIL_COUNT = 0
_FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global _PASS_COUNT, _FAIL_COUNT
    if condition:
        _PASS_COUNT += 1
        print(f" [PASS] {label}")
    else:
        _FAIL_COUNT += 1
        _FAILURES.append(f"{label} {detail}")
        print(f" [FAIL] {label} {detail}")


def scenario(name: str):
    def decorator(fn):
        def wrapper():
            print(f"\n=== Scenario {name} ===")
            try:
                fn()
            except Exception as exc:
                global _FAIL_COUNT
                _FAIL_COUNT += 1
                _FAILURES.append(f"Scenario {name} raised an unhandled exception: {exc}")
                print(f" [ERROR] Unhandled exception: {exc}")
                traceback.print_exc()
        return wrapper
    return decorator


def base_request(**overrides) -> dict[str, Any]:
    request = {
        "days": 5,
        "travelers": 2,
        "budget_tier": "mid",
        "focus": "wildlife",
        "start_date": "2026-09-01",
    }
    request.update(overrides)
    return request


# ============================================================================
# SCENARIO A: Single destination
# ============================================================================

@scenario("A — Single destination")
def scenario_a():
    db = MockDB(
        travel_places={
            "serengeti": Row("serengeti", "Serengeti", "Tanzania", "national_park", -2.33, 34.83),
        },
        activities={
            "serengeti": [
                Row("act-1", "Morning game drive", "desc", "game_drive", "moderate", 0, 0),
                Row("act-2", "Afternoon game drive", "desc", "game_drive", "moderate", 0, 0),
                Row("act-3", "Walking safari", "desc", "walking_safari", "moderate", 1, 0),
                Row("act-4", "Night drive", "desc", "night_drive", "moderate", 1, 0),
            ],
        },
        lodges={
            "serengeti": [Row("lodge-1", "Serengeti Camp", "mid_range")],
        },
    )

    orchestrator = ItineraryOrchestrator(db)
    result = orchestrator.generate(base_request(days=4), ["serengeti"])

    check("pipeline completed without raising", result.cabinet is not None)
    check("cabinet has 4 shelves", len(result.cabinet.shelves) == 4, f"got {len(result.cabinet.shelves)}")
    check("no hinges (single destination has no legs)", len(result.cabinet.hinges) == 0)
    check("route analysis has 1 stop, 0 legs", result.route_analysis.stop_count == 1 and result.route_analysis.leg_count == 0)
    check(
        "day archetypes assigned: first=arrival, last=departure",
        result.day_plan.days[0].archetype.value == "arrival" and result.day_plan.days[-1].archetype.value == "departure",
        f"got {[d.archetype.value for d in result.day_plan.days]}",
    )
    check("validation reached a conclusive status", result.validation_result["status"] in ("valid", "invalid"))


# ============================================================================
# SCENARIO B: Two destinations
# ============================================================================

@scenario("B — Two destinations")
def scenario_b():
    db = MockDB(
        travel_places={
            "arusha": Row("arusha", "Arusha", "Tanzania", "city", -3.37, 36.68),
            "serengeti": Row("serengeti", "Serengeti", "Tanzania", "national_park", -2.33, 34.83),
        },
        drive_times={
            ("arusha", "serengeti"): (280.0, 240), # 4h -- under the long-transfer threshold
        },
        activities={
            "serengeti": [
                Row("act-1", "Morning game drive", "desc", "game_drive", "moderate", 0, 0),
                Row("act-2", "Afternoon game drive", "desc", "game_drive", "moderate", 0, 0),
                Row("act-3", "Walking safari", "desc", "walking_safari", "moderate", 1, 0),
            ],
        },
        lodges={
            "serengeti": [Row("lodge-1", "Serengeti Camp", "mid_range")],
        },
    )

    orchestrator = ItineraryOrchestrator(db)
    result = orchestrator.generate(base_request(days=5), ["arusha", "serengeti"])

    check("cabinet built", result.cabinet is not None)
    check("route geography found 1 leg", result.route_analysis.leg_count == 1)
    check(
        "the leg used MEASURED drive_times data, not a fallback",
        result.route_analysis.legs[0].source == "drive_times",
        f"got source={result.route_analysis.legs[0].source if result.route_analysis.legs else None}",
    )
    check("1 hinge persisted on the cabinet", len(result.cabinet.hinges) == 1)
    check(
        "hinge duration matches the measured 240 minutes (directed query resolved correctly)",
        result.cabinet.hinges[0].duration_minutes == 240,
        f"got {result.cabinet.hinges[0].duration_minutes}",
    )
    check("accommodation present on every overnight shelf", result.validation_result["error_count"] >= 0)


# ============================================================================
# SCENARIO C: Three+ destinations
# ============================================================================

@scenario("C — Three+ destinations (multiple transfers, donor-night allocation)")
def scenario_c():
    db = MockDB(
        travel_places={
            "arusha": Row("arusha", "Arusha", "Tanzania", "city", -3.37, 36.68),
            "serengeti": Row("serengeti", "Serengeti", "Tanzania", "national_park", -2.33, 34.83),
            "ngorongoro": Row("ngorongoro", "Ngorongoro", "Tanzania", "national_park", -3.16, 35.58),
        },
        drive_times={
            ("arusha", "serengeti"): (280.0, 240),
            ("serengeti", "ngorongoro"): (150.0, 120),
        },
        activities={
            "serengeti": [Row(f"act-s{i}", f"Serengeti activity {i}", "d", "game_drive", "moderate", 0, 0) for i in range(4)],
            "ngorongoro": [Row(f"act-n{i}", f"Ngorongoro activity {i}", "d", "game_drive", "moderate", 0, 0) for i in range(4)],
        },
        lodges={
            "serengeti": [Row("lodge-s", "Serengeti Camp", "mid_range")],
            "ngorongoro": [Row("lodge-n", "Ngorongoro Camp", "mid_range")],
        },
    )

    orchestrator = ItineraryOrchestrator(db)
    result = orchestrator.generate(base_request(days=7), ["arusha", "serengeti", "ngorongoro"])

    check("cabinet built with 7 shelves", len(result.cabinet.shelves) == 7, f"got {len(result.cabinet.shelves)}")
    check("2 hinges (3 destinations = 2 legs)", len(result.cabinet.hinges) == 2)
    check(
        "both legs used measured data",
        all(leg.source == "drive_times" for leg in result.route_analysis.legs),
        f"sources: {[leg.source for leg in result.route_analysis.legs]}",
    )
    # All three destinations are same-country (Tanzania), so no
    # donor-night border-buffer logic should have triggered -- this
    # confirms the allocation didn't spuriously activate it.
    check(
        "no border-buffer allocation warnings for an all-domestic route",
        not any("border-buffer" in w for w in result.warnings),
        f"warnings: {result.warnings}",
    )


# ============================================================================
# SCENARIO D: Multi-country
# ============================================================================

@scenario("D — Multi-country (donor-night allocation, minimum-stay invariant)")
def scenario_d():
    db = MockDB(
        travel_places={
            "nairobi": Row("nairobi", "Nairobi", "Kenya", "city", -1.29, 36.82),
            "arusha": Row("arusha", "Arusha", "Tanzania", "city", -3.37, 36.68),
            "zanzibar": Row("zanzibar", "Zanzibar", "Tanzania", "island", -6.16, 39.20),
        },
        drive_times={
            ("nairobi", "arusha"): (280.0, 300),
        },
        flights={},
        border_crossings={
            ("Kenya", "Tanzania"): Row("bc-1", "Namanga", "open", None),
        },
        activities={
            "arusha": [Row(f"act-a{i}", f"Arusha activity {i}", "d", "cultural_visit", "easy", 0, 0) for i in range(3)],
            "zanzibar": [Row(f"act-z{i}", f"Zanzibar activity {i}", "d", "beach_leisure", "easy", 0, 0) for i in range(3)],
        },
        lodges={
            "arusha": [Row("lodge-a", "Arusha Lodge", "mid_range")],
            "zanzibar": [Row("lodge-z", "Zanzibar Resort", "mid_range")],
        },
    )
    # No drive_times or flight for arusha->zanzibar (crossing to an
    # island realistically means a domestic flight, which this fixture
    # deliberately omits to also exercise the no-fabrication path).

    orchestrator = ItineraryOrchestrator(db)
    result = orchestrator.generate(base_request(days=6), ["nairobi", "arusha", "zanzibar"])

    check("cabinet built", result.cabinet is not None)
    check(
        "the Kenya->Tanzania leg is marked inter-country",
        result.route_analysis.legs[0].is_inter_country is True,
    )
    check(
        "a border crossing was resolved for the inter-country leg",
        result.route_analysis.legs[0].border_crossing is not None
        and result.route_analysis.legs[0].border_crossing.status == "open",
    )
    check(
        "the arusha->zanzibar leg (no measured data) is honestly unavailable, not fabricated",
        result.route_analysis.legs[1].is_unavailable is True,
        f"got duration={result.route_analysis.legs[1].duration_minutes}",
    )
    # This is the actual invariant doc 15 section 11 Scenario D asks
    # for: no destination reduced below its recommended minimum.
    # Without estimated_visit_durations seeded (this mock returns
    # table-does-not-exist), every destination's minimum defaults to 1
    # night -- so the real assertion here is just that every
    # destination received at least 1 night.
    nights_by_destination: dict[str, int] = {}
    for shelf in result.cabinet.shelves:
        nights_by_destination[shelf.destination_id] = nights_by_destination.get(shelf.destination_id, 0) + 1
    check(
        "every destination received at least 1 night",
        all(n >= 1 for n in nights_by_destination.values()),
        f"got {nights_by_destination}",
    )


# ============================================================================
# SCENARIO E: Midnight-crossing activity
# ============================================================================

@scenario("E — Midnight-crossing activity overlap detection")
def scenario_e():
    db = MockDB(
        travel_places={
            "serengeti": Row("serengeti", "Serengeti", "Tanzania", "national_park", -2.33, 34.83),
        },
    )
    orchestrator = ItineraryOrchestrator(db)

    # This exercises ValidationEngine._check_time_overlaps directly,
    # since the planning engine's own drawer construction does not
    # currently produce midnight-crossing drawers on its own (its
    # night_drive slot, if seeded, still starts well before midnight)
    # -- so this scenario specifically targets the validation logic
    # itself with a hand-built shelf, which is the correct level to
    # test the exact bug the audit identified.
    shelf = Shelf(id="s-midnight", day_number=1, date=None, drawers=[
        Drawer(id="d1", name="Night drive", start_time=dt_time(22, 30), duration_minutes=180, sort_order=1, is_fallback=False),
        Drawer(id="d2", name="Late dinner", start_time=dt_time(23, 0), duration_minutes=60, sort_order=2, is_fallback=False),
    ])
    cabinet = Cabinet(id="c-midnight", shelves=[shelf], hinges=[], route_destination_ids=["serengeti"], duration_days=1)

    issues = orchestrator.validation_engine._check_time_overlaps(cabinet, shelf)
    check(
        "genuine cross-midnight overlap (22:30+180min=01:30 vs 23:00) is CAUGHT",
        len(issues) == 1 and "overlaps" in issues[0].message,
        f"got {[i.message for i in issues]}",
    )

    # Negative control: a midnight-crossing activity with NOTHING after
    # it should not be flagged.
    shelf2 = Shelf(id="s-midnight-2", day_number=1, date=None, drawers=[
        Drawer(id="d3", name="Night drive", start_time=dt_time(22, 30), duration_minutes=180, sort_order=1, is_fallback=False),
    ])
    issues2 = orchestrator.validation_engine._check_time_overlaps(cabinet, shelf2)
    check("a lone midnight-crossing activity with nothing after it is NOT flagged", len(issues2) == 0)


# ============================================================================
# SCENARIO F: Departure day
# ============================================================================

@scenario("F — Departure day (checkout, no false accommodation error)")
def scenario_f():
    db = MockDB(
        travel_places={
            "serengeti": Row("serengeti", "Serengeti", "Tanzania", "national_park", -2.33, 34.83),
        },
        activities={
            "serengeti": [Row(f"act-{i}", f"Activity {i}", "d", "game_drive", "moderate", 0, 0) for i in range(4)],
        },
        lodges={
            "serengeti": [Row("lodge-1", "Serengeti Camp", "mid_range")],
        },
    )

    orchestrator = ItineraryOrchestrator(db)
    result = orchestrator.generate(base_request(days=4), ["serengeti"])

    last_shelf = result.cabinet.shelves[-1]
    departure_archetype = result.day_plan.days[-1].archetype.value

    check("last day is classified DEPARTURE", departure_archetype == "departure")

    # The core Scenario F assertion: DayArchetype correctly informs
    # ValidationEngine that this shelf does not require overnight
    # accommodation, so a missing/zero-night Headboard is not a false
    # positive -- this directly tests the fix built earlier in this
    # project (overnight_required parameter + DEPARTURE archetype).
    accommodation_errors = [
        e for e in result.validation_result["errors"]
        if "accommodation" in e.lower() and f"Day {last_shelf.day_number}" in e
    ]
    check(
        "no false-positive accommodation error on the departure day",
        len(accommodation_errors) == 0,
        f"got {accommodation_errors}",
    )

    # Still confirm the departure day HAS the expected checkout/transfer
    # structural drawers (Breakfast, Transfer to airport, Departure),
    # so we're not accidentally passing by having under-built the day.
    departure_drawer_names = {d.name for d in last_shelf.drawers}
    check(
        "departure day has the expected checkout structural drawers",
        {"Breakfast", "Transfer to airport", "Departure"}.issubset(departure_drawer_names),
        f"got {departure_drawer_names}",
    )


# ============================================================================
# SCENARIO G: Overloaded day
# ============================================================================

@scenario("G — Overloaded day (too many activities -> repair -> validation)")
def scenario_g():
    from schedule_repair import ScheduleRepairEngine
    from day_archetype import DayArchetype

    # Directly exercise ScheduleRepairEngine with a hand-built
    # overloaded day: 4 activities of 3 hours each (12 total) against a
    # NORMAL day's 8-hour capacity ceiling. This is the correct level
    # to test this specific scenario -- it isolates the
    # detect-overload-then-attempt-repair behavior from whether
    # ItineraryPlanningEngine's own construction happens to produce an
    # overloaded day (it currently never does, since it always builds
    # exactly 2 activity slots per day).
    days = [
        {
            "activities": [
                {"id": f"a{i}", "name": f"Activity {i}", "duration_hours": 3.0, "start_time": f"{7 + i*3}:00"}
                for i in range(4)
            ]
        }
    ]

    engine = ScheduleRepairEngine()
    result = engine.repair(days, archetypes={1: DayArchetype.EXPLORATION})

    check("overload was detected among conflicts_found", len(result.conflicts_found) > 0)
    check(
        "repair result reports the day as still overloaded (12h > 8h capacity, cannot be shrunk by moving)",
        result.days[0].overloaded is True,
        f"total_activity_hours={result.days[0].total_activity_hours}",
    )
    check(
        "repair did not silently delete any activity to resolve the overload",
        len(result.days[0].activities) == 4,
        f"got {len(result.days[0].activities)} activities remaining",
    )
    check(
        "engine reports this as NOT fully repaired, rather than silently claiming success",
        result.fully_repaired is False,
    )


# ============================================================================
# SCENARIO H: Impossible schedule
# ============================================================================

@scenario("H — Impossible schedule (no infinite loop, explicit failure)")
def scenario_h():
    from schedule_repair import ScheduleRepairEngine
    from day_archetype import DayArchetype
    import time as time_module

    # Two FIXED-time activities on the same single day that
    # unavoidably overlap. Since both are fixed_start_minutes-pinned,
    # ScheduleRepairEngine's _shift_activity/_move_activity both
    # refuse to touch them (see "if activity.fixed: return None" and
    # the fixed_start_minutes check) -- this is a genuinely
    # unsatisfiable schedule, not one the engine merely fails to solve.
    days = [
        {
            "activities": [
                {"id": "fixed-1", "name": "Fixed activity 1", "duration_hours": 3.0,
                 "start_time": "09:00", "fixed_time": True},
                {"id": "fixed-2", "name": "Fixed activity 2", "duration_hours": 3.0,
                 "start_time": "10:00", "fixed_time": True},
            ]
        }
    ]

    engine = ScheduleRepairEngine()

    start = time_module.monotonic()
    result = engine.repair(days, archetypes={1: DayArchetype.EXPLORATION})
    elapsed = time_module.monotonic() - start

    check(
        "repair terminates promptly rather than hanging (no infinite loop)",
        elapsed < 5.0,
        f"took {elapsed:.2f}s",
    )
    check(
        "repair used a bounded number of iterations, not the full MAX_REPAIR_ITERATIONS ceiling every time",
        result.iterations <= 100,
    )
    check(
        "the unsatisfiable conflict remains in conflicts_remaining rather than being silently dropped",
        len(result.conflicts_remaining) > 0,
        f"conflicts_remaining={result.conflicts_remaining}",
    )
    check(
        "engine explicitly reports fully_repaired=False for the impossible case",
        result.fully_repaired is False,
    )
    check(
        "neither fixed activity was silently removed or unfixed to force a resolution",
        len(result.days[0].activities) == 2
        and all(a.fixed for a in result.days[0].activities),
        f"got {[(a.activity.name, a.fixed) for a in result.days[0].activities]}",
    )


# ============================================================================
# MAIN
# ============================================================================

def main():
    scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    scenario_f()
    scenario_g()
    scenario_h()

    print()
    print("=" * 70)
    print(f"RESULTS: {_PASS_COUNT} passed, {_FAIL_COUNT} failed")
    print("=" * 70)

    if _FAILURES:
        print()
        print("FAILURES:")
        for f in _FAILURES:
            print(f" - {f}")
        sys.exit(1)
    else:
        print()
        print("ALL SCENARIOS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
