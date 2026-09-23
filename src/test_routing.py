"""
test_routing.py - the validator, checked against cases whose answers were known
before it ran.

    python test_routing.py          # no server, no API key, no network access

The project's claim is that route correctness is established by measurement
rather than by a model's confidence. That claim rests entirely on this file. A
verifier nobody has watched fail on a route that should fail is not evidence,
it is an assertion with more steps - so every check below is fired twice, once
on a route built to pass it and once on a route built to break it in a
specific, stated way.

The graph here is hand-built and tiny. Using the real 1.4-million-node Brisbane
network would make these tests depend on an Overpass fetch, on what
OpenStreetMap happened to say this week, and on nobody having retagged a
footpath - none of which is what is under test.

Written as plain asserts in test_* functions, so it runs under pytest if you
have it and under `python test_routing.py` if you do not.
"""

from __future__ import annotations

import routing
from routing import coerce_constraints, plan_route, read_segments, validate

# ---------------------------------------------------------------------------
# A four-node square, 100 m to a side, plus one island node nothing reaches.
#
#     A ---- B          D (island, no edges)
#     |      |
#     C ---- (B via C)
#
# Coordinates are chosen so the sides really are ~100 m apart, which is what
# lets the distance assertions below be exact rather than approximate.
# ---------------------------------------------------------------------------

_LAT, _LON = -27.4800, 153.0100
_STEP = 0.0009            # ~100 m of latitude; close enough at this longitude

GRAPH = {
    "nodes": {
        "A": {"lat": _LAT, "lon": _LON},
        "B": {"lat": _LAT, "lon": _LON + _STEP},
        "C": {"lat": _LAT - _STEP, "lon": _LON},
        "D": {"lat": _LAT - 0.05, "lon": _LON + 0.05},        # the island
    },
    "edges": {
        "A": [["B", 89, "footway"], ["C", 100, "steps"]],
        "B": [["A", 89, "footway"]],
        "C": [["A", 100, "steps"]],
        "D": [],
    },
}

POINT_A = {"lat": _LAT, "lon": _LON}
POINT_B = {"lat": _LAT, "lon": _LON + _STEP}
POINT_D = {"lat": _LAT - 0.05, "lon": _LON + 0.05}


def walk(frm, to, distance_m, **extra):
    return {"mode": "walk", "from": frm, "to": to, "distance_m": distance_m, **extra}


def check(verdict, name):
    """The single check named, so a test asserts about the one it means."""
    return next(c for c in verdict["checks"] if c["check"] == name)


# ---------------------------------------------------------------------------
# Constraint coercion. The whitelist is a security boundary, not just a schema,
# so the cases that matter most are the ones it must refuse.
# ---------------------------------------------------------------------------

def test_recognised_constraints_survive():
    clean, report = coerce_constraints({
        "max_walking_distance_m": 800,
        "max_transfers": 1,
        "allowed_modes": ["Walk", "BUS"],
        "arrive_before": "09:00",
    })
    assert clean["max_walking_distance_m"] == 800
    assert clean["allowed_modes"] == ["bus", "walk"], "modes are lowercased and sorted"
    assert report["dropped_count"] == 0


def test_unknown_and_malformed_constraints_are_dropped_not_guessed():
    clean, report = coerce_constraints({
        "max_walking_distance_m": -5,          # out of range
        "max_transfers": True,                 # bool is not a count
        "allowed_modes": ["teleport"],         # not a mode this agent models
        "arrive_before": "half past nine",     # not a time it can compare
        "ignore_previous_instructions": "x",   # not a constraint at all
    })
    assert clean == {}, "nothing malformed may survive into a verdict"
    assert report["dropped_count"] == 5


def test_report_never_echoes_caller_supplied_key_names():
    # The response is read by another model. Rejected key names came from the
    # caller, so naming them back would open an injection path between agents.
    _clean, report = coerce_constraints({"<script>alert(1)</script>": 1})
    assert report["accepted"] == []
    assert "script" not in str(report)


def test_constraint_set_that_is_not_an_object_is_survivable():
    clean, report = coerce_constraints("max_walking_distance_m=800")
    assert clean == {} and report["dropped_count"] == 0


# ---------------------------------------------------------------------------
# Segment shape. Routes arrive from other agents, so malformed ones must
# produce a readable refusal rather than an exception.
# ---------------------------------------------------------------------------

def test_well_formed_route_is_accepted():
    segments, error = read_segments([walk(POINT_A, POINT_B, 89)])
    assert error is None and segments[0]["mode"] == "walk"


def test_malformed_routes_are_named_not_raised():
    for bad, expect in (
        ([], "non-empty"),
        (["not a segment"], "not an object"),
        ([{"mode": "walk", "to": POINT_B}], "'from'"),
        ([{"mode": "walk", "from": {"lat": "x", "lon": 1}, "to": POINT_B}], "numeric"),
        ([{"mode": "hovercraft", "from": POINT_A, "to": POINT_B}], "unrecognised mode"),
    ):
        segments, error = read_segments(bad)
        assert segments == [] and error and expect in error, bad


# ---------------------------------------------------------------------------
# Liberal in what it accepts, strict in what it believes. The Showcase gives
# each agent one call with no retry, so a route refused over a key spelling is
# a call spent on nothing - but a value that is not a value must still fail,
# because guessing one would put a made-up number into a verdict.
# ---------------------------------------------------------------------------

def test_a_route_spelled_another_way_is_still_read():
    segments, error = read_segments([{
        "origin": [_LAT, _LON],                       # a pair, not an object
        "destination": {"latitude": _LAT, "longitude": _LON + _STEP},
        "travel_mode": "WALK",                        # not lower case
        "distance": "89",                             # quoted by the model that sent it
        "highway_type": "footway",                    # not an alias: carried through
    }])
    assert error is None
    assert segments[0]["from"] == POINT_A and segments[0]["to"] == POINT_B
    assert segments[0]["mode"] == "walk" and segments[0]["distance_m"] == 89.0
    assert segments[0]["highway_type"] == "footway", "unknown keys must survive"


def test_a_coordinate_pair_in_the_other_order_is_recognised():
    # GeoJSON writes [lon, lat]. Here longitude is ~153 and latitude ~-27, so
    # a first number over 90 can only be the longitude - and anything genuinely
    # ambiguous is read in this agent's own order, [lat, lon].
    assert routing.read_point([153.02, -27.47]) == {"lat": -27.47, "lon": 153.02}
    assert routing.read_point([-27.47, 153.02]) == {"lat": -27.47, "lon": 153.02}
    assert routing.read_point([10.0, 20.0]) == {"lat": 10.0, "lon": 20.0}


def test_liberal_reading_still_refuses_a_value_it_cannot_trust():
    for bad, expect in (
        ([{"origin": ["north", "east"], "destination": POINT_B}], "'from' point"),
        ([{"origin": [-27.48, 999.0], "destination": POINT_B}], "'from' point"),
        ([{"from": POINT_A, "to": POINT_B, "distance": "about 90 m"}], "not a number"),
        ([{"from": POINT_A, "to": POINT_B, "travel_mode": "hovercraft"}], "unrecognised mode"),
    ):
        segments, error = read_segments(bad)
        assert segments == [] and error and expect in error, bad


def test_constraint_keys_are_read_liberally_and_values_are_not():
    clean, report = coerce_constraints({
        "max_walking_distance": 800,      # the "_m" left off
        "arrive_by": "09:00",             # can only mean arrive_before
        "avoid": ["steps"],
        "max_walking_distance_m": 900,    # canonical form of the first key
    })
    assert clean["arrive_before"] == "09:00" and clean["avoid_highway_types"] == ["steps"]
    assert clean["max_walking_distance_m"] in (800, 900), "one of the two spellings wins"
    assert report["renamed_count"] == 3

    # An alias renames the key and vouches for nothing else: the value still
    # faces the same checks, so a wrong guess about the caller is dropped here.
    clean, report = coerce_constraints({"max_walking_distance": "quite far", "avoid": ["lifts"]})
    assert clean == {} and report["dropped_count"] == 2


# ---------------------------------------------------------------------------
# Structural checks. Each one fired both ways.
# ---------------------------------------------------------------------------

def test_continuity_passes_when_segments_join():
    verdict = validate([walk(POINT_A, POINT_B, 89), walk(POINT_B, POINT_A, 89)], {})
    assert check(verdict, "segment_continuity")["satisfied"]


def test_continuity_fails_on_a_deliberately_disconnected_route():
    # Segment 0 ends at B; segment 1 starts at D, ~6 km away. A route claiming
    # to be one journey while teleporting between its parts is the failure this
    # check exists for.
    verdict = validate([walk(POINT_A, POINT_B, 89), walk(POINT_D, POINT_A, 100)], {})
    record = check(verdict, "segment_continuity")
    assert not record["satisfied"]
    assert record["observed"]["discontinuities"][0]["between_segments"] == [0, 1]
    assert record["observed"]["discontinuities"][0]["gap_m"] > 1000
    assert verdict["valid"] is False


def test_walk_segment_off_the_network_is_caught():
    # D is a node, but nothing connects to it, and the point is far from A/B.
    verdict = validate([walk(POINT_A, POINT_D, 6000)], {}, GRAPH)
    assert not check(verdict, "walk_segments_on_network")["satisfied"]


def test_walk_segment_on_the_network_passes():
    verdict = validate([walk(POINT_A, POINT_B, 89)], {}, GRAPH)
    assert check(verdict, "walk_segments_on_network")["satisfied"]


def test_topology_check_admits_when_it_could_not_run():
    # Without a graph the check must say it was skipped, not report a pass.
    # A verdict that quietly drops a check it could not run is worse than one
    # that admits to it, because the caller cannot tell the difference.
    record = check(validate([walk(POINT_A, POINT_B, 89)], {}), "walk_segments_on_network")
    assert record["observed"] == "skipped" and "did not run" in record["evidence"]


def test_walking_distance_survives_a_surface_constraint():
    # walking_distance_m is a number, and stays one whichever constraints are
    # checked. It stopped being one when the avoid_surfaces check reused the
    # name holding the total, and no evaluation case asserted the field on a
    # surface-constrained route, so the wrong type travelled to the caller.
    segments = [{"mode": "walk", "from": {"lat": -27.5, "lon": 153.0},
                 "to": {"lat": -27.5, "lon": 153.001}, "distance_m": 100,
                 "surface": "asphalt", "highway_type": "footway"}]
    plain = validate(segments, {})
    constrained = validate(segments, {"avoid_surfaces": ["gravel"]})
    assert isinstance(plain["walking_distance_m"], (int, float))
    assert isinstance(constrained["walking_distance_m"], (int, float)), \
        "an avoid_surfaces check must not change the type of walking_distance_m"
    assert plain["walking_distance_m"] == constrained["walking_distance_m"]


def test_a_transit_leg_beyond_the_walking_area_is_still_covered():
    # The two datasets reach different distances, so the coverage check asks a
    # different question of each leg. A bus leg inside the timetable's area is
    # covered even where no footpath route could be planned - otherwise
    # widening the timetable would have bought nothing.
    walking = {"lat_min": -27.75, "lat_max": -27.15, "lon_min": 152.70, "lon_max": 153.35}
    seq = {"lat_min": -28.30, "lat_max": -26.00, "lon_min": 151.50, "lon_max": 153.60}
    nerang = {"lat": -27.9944, "lon": 153.3505}
    helensvale = {"lat": -27.9253, "lon": 153.3380}

    ride = [{"mode": "bus", "route_id": "740-5053", "from": nerang, "to": helensvale}]
    record = check(validate(ride, {}, coverage=walking, transit_coverage=seq), "within_coverage")
    assert record["satisfied"], "a transit leg inside the timetable's area is covered"

    # A walking leg out there is not a false claim, only an unmeasured one.
    stroll = [walk(nerang, helensvale, 9000)]
    record = check(validate(stroll, {}, coverage=walking, transit_coverage=seq), "within_coverage")
    assert record["satisfied"]
    assert record["observed"]["walking_legs_not_measurable"] == 2
    assert "could not be measured" in record["evidence"]

    # Outside both areas it is a failure again: the agent holds nothing there.
    sydney = [walk({"lat": -33.8568, "lon": 151.2153}, {"lat": -33.8908, "lon": 151.2743}, 7000)]
    record = check(validate(sydney, {}, coverage=walking, transit_coverage=seq), "within_coverage")
    assert not record["satisfied"] and record["observed"] == 2


def test_the_timetable_area_contains_the_walking_area():
    # The split only makes sense in one direction. A timetable narrower than
    # the graph would mean transit legs inside the walking box going unchecked,
    # which is the failure this arrangement exists to prevent.
    import data
    walking, transit = data.NETWORK_BBOX, data.TIMETABLE_BBOX
    assert transit["lat_min"] <= walking["lat_min"] and transit["lat_max"] >= walking["lat_max"]
    assert transit["lon_min"] <= walking["lon_min"] and transit["lon_max"] >= walking["lon_max"]


def test_coverage_check_flags_points_outside_the_study_area():
    box = {"lat_min": -27.60, "lat_max": -27.35, "lon_min": 152.90, "lon_max": 153.15}
    sydney = {"lat": -33.8568, "lon": 151.2153}
    verdict = validate([walk(POINT_A, sydney, 900_000)], {}, coverage=box)
    assert check(verdict, "within_coverage")["observed"] == 1


def test_origin_and_destination_must_be_where_they_were_asked_for():
    verdict = validate([walk(POINT_A, POINT_B, 89)], {},
                       expected_origin=POINT_A, expected_destination=POINT_D)
    assert check(verdict, "origin_match")["satisfied"]
    assert not check(verdict, "destination_match")["satisfied"], "ends 6 km from the request"


def test_endpoint_match_tolerates_the_networks_own_granularity():
    # Regression test for an over-strict check found in live running: a request
    # aimed at a building entrance snapped 50 m to the nearest footpath, and the
    # verifier called the resulting - correct - route invalid. A route can only
    # start on the network, so the snap distance is not an error. This is the
    # H_harm case the project measures: verification damaging a correct result.
    fifty_m_away = {"lat": _LAT + 0.00045, "lon": _LON}
    verdict = validate([walk(POINT_A, POINT_B, 89)], {}, expected_origin=fifty_m_away)
    record = check(verdict, "origin_match")
    assert record["satisfied"], "a 50 m snap onto the network is granularity, not a wrong start"
    assert 40 <= record["observed"] <= 60
    assert verdict["valid"], "an otherwise sound route must not be failed by its snap distance"


def test_endpoint_match_still_fails_a_genuinely_wrong_endpoint():
    verdict = validate([walk(POINT_A, POINT_B, 89)], {}, expected_origin=POINT_D)
    assert not check(verdict, "origin_match")["satisfied"], "6 km off is a wrong start"


# ---------------------------------------------------------------------------
# Temporal feasibility - the "transfer too tight to make" case from the plan.
# ---------------------------------------------------------------------------

def test_a_transfer_with_enough_time_is_feasible():
    route = [
        walk(POINT_A, POINT_B, 89, depart="2026-08-26T08:00:00+10:00",
             arrive="2026-08-26T08:05:00+10:00"),
        {"mode": "bus", "route_id": "412", "from": POINT_B, "to": POINT_A,
         "depart": "2026-08-26T08:09:00+10:00", "arrive": "2026-08-26T08:20:00+10:00"},
    ]
    verdict = validate(route, {"min_transfer_seconds": 180})
    assert check(verdict, "temporal_feasibility")["satisfied"], "4 min to make a 3 min connection"


def test_a_transfer_that_is_too_tight_fails_against_the_stated_gap():
    # Bus departs 60 s after the walk arrives, against a 180 s requirement.
    route = [
        walk(POINT_A, POINT_B, 89, depart="2026-08-26T08:00:00+10:00",
             arrive="2026-08-26T08:05:00+10:00"),
        {"mode": "bus", "route_id": "412", "from": POINT_B, "to": POINT_A,
         "depart": "2026-08-26T08:06:00+10:00", "arrive": "2026-08-26T08:20:00+10:00"},
    ]
    verdict = validate(route, {"min_transfer_seconds": 180})
    record = check(verdict, "temporal_feasibility")
    assert not record["satisfied"]
    assert record["observed"]["infeasible_connections"][0] == {
        "between_segments": [0, 1], "slack_s": 60, "required_s": 180,
    }, "the record must carry the observed value and the threshold, not just a verdict"


def test_a_segment_arriving_before_it_departs_is_caught():
    route = [walk(POINT_A, POINT_B, 89, depart="2026-08-26T09:00:00+10:00",
                  arrive="2026-08-26T08:00:00+10:00")]
    record = check(validate(route, {}), "temporal_feasibility")
    assert record["observed"]["segments_arriving_before_departing"] == [0]


# ---------------------------------------------------------------------------
# Transit legs against the timetable. A hand-built table, not the real feed:
# the real one changes every few weeks (it gained 800,000 rows between two runs
# a week apart during development), and a test that has to be rewritten when
# Translink publishes is testing the wrong thing.
# ---------------------------------------------------------------------------

STOP = {"lat": _LAT, "lon": _LON}

TIMETABLE = {
    "stops": {"S1": {"name": "Test Station", "lat": _LAT, "lon": _LON}},
    # 08:30:00 = 30600s, and 24:30 = 88200s for the after-midnight case
    "departures": {"S1": [[30600, 0, 0], [88200, 0, 0]]},
    "routes": ["R-412"],
    "route_names": ["412"],
    "services": ["WEEKDAYS"],
    "service_days": {"0": "1111100"},            # Mon-Fri only
    "feed": {"start": "20260901", "end": "20261231"},
}


def ride(depart, route_id="412", frm=None):
    return [{"mode": "bus", "route_id": route_id, "from": frm or STOP,
             "to": POINT_B, "depart": depart, "arrive": "2026-09-09T09:00:00+10:00"}]


def transit_check(segments):
    verdict = validate(segments, {}, None, table=TIMETABLE)
    return check(verdict, "transit_services_exist")


def test_a_real_service_at_a_real_stop_passes():
    # Wednesday 9 September 2026, inside the feed window, 08:30 on route 412.
    assert transit_check(ride("2026-09-09T08:30:00+10:00"))["satisfied"]


def test_a_service_that_does_not_run_at_that_hour_is_rejected():
    # Same stop, same route, 03:17 - the timetable has nothing there. This is
    # what a confidently invented connection looks like from the outside.
    record = transit_check(ride("2026-09-09T03:17:00+10:00"))
    assert not record["satisfied"]
    assert record["observed"]["no_scheduled_service"][0]["segment"] == 0


def test_an_invented_route_number_is_rejected():
    record = transit_check(ride("2026-09-09T08:30:00+10:00", route_id="999"))
    assert not record["satisfied"], "no route 999 departs this stop"


def test_a_service_running_only_on_weekdays_fails_on_a_weekend():
    # 12 September 2026 is a Saturday; the service pattern is 1111100.
    assert not transit_check(ride("2026-09-12T08:30:00+10:00"))["satisfied"]
    assert transit_check(ride("2026-09-11T08:30:00+10:00"))["satisfied"], "Friday runs"


def test_a_date_outside_the_feed_window_is_rejected():
    # The feed describes 1 Sep to 31 Dec 2026 and says nothing about 2027. A
    # claim outside that range is unverifiable, not true.
    assert not transit_check(ride("2027-03-03T08:30:00+10:00"))["satisfied"]


def test_after_midnight_services_are_found_on_the_previous_service_day():
    # GTFS lists a 00:30 departure as 24:30 on the day before. Truncating that
    # to 00:30 would move a late-night service twenty-four hours.
    assert transit_check(ride("2026-09-10T00:30:00+10:00"))["satisfied"]


def test_boarding_nowhere_near_a_stop_is_rejected():
    far = {"lat": _LAT - 0.05, "lon": _LON + 0.05}          # ~6 km from the only stop
    record = transit_check(ride("2026-09-09T08:30:00+10:00", frm=far))
    assert not record["satisfied"]
    assert record["observed"]["not_at_a_stop"][0]["segment"] == 0


def test_a_transit_leg_with_no_departure_time_is_not_failed_for_it():
    # An unstated time is uncheckable, not false. Failing it would punish a
    # caller for omitting an optional field.
    leg = [{"mode": "bus", "route_id": "412", "from": STOP, "to": POINT_B}]
    assert transit_check(leg)["satisfied"]


def test_timetable_check_admits_when_it_could_not_run():
    record = check(validate(ride("2026-09-09T08:30:00+10:00"), {}), "transit_services_exist")
    assert record["observed"] == "skipped" and "did not run" in record["evidence"]


def test_walk_only_routes_do_not_get_a_transit_check():
    verdict = validate([walk(POINT_A, POINT_B, 89)], {}, GRAPH, table=TIMETABLE)
    assert not any(c["check"] == "transit_services_exist" for c in verdict["checks"])


# ---------------------------------------------------------------------------
# Constraint checks. The proposal's worked example is a 1,200 m walking leg
# against an 800 m limit, so that is the case used here.
# ---------------------------------------------------------------------------

def test_walking_limit_passes_and_fails_around_its_threshold():
    under = validate([walk(POINT_A, POINT_B, 700)], {"max_walking_distance_m": 800})
    assert check(under, "max_walking_distance_m")["satisfied"]

    over = validate([walk(POINT_A, POINT_B, 1200)], {"max_walking_distance_m": 800})
    record = check(over, "max_walking_distance_m")
    assert not record["satisfied"]
    assert (record["observed"], record["threshold"]) == (1200, 800)


def test_a_segment_cannot_claim_to_be_shorter_than_the_network_allows():
    # Found in live running: a single walk segment claiming 10 m for a leg the
    # network cannot do in under 89 m passed an 800 m limit, because the limit
    # was checked against the caller's own number. Under-claiming is provably
    # impossible - no walk beats the shortest path - so the measured distance
    # wins. Checking a constraint against a figure the caller invented is not
    # verification, it is transcription.
    verdict = validate([walk(POINT_A, POINT_B, 10)], {"max_walking_distance_m": 50}, GRAPH)
    record = check(verdict, "claimed_distances_possible")
    assert not record["satisfied"]
    assert record["observed"]["understated"][0]["shortest_possible_m"] == 89
    assert verdict["walking_distance_m"] == 89, "the measured distance is what counts"
    assert not check(verdict, "max_walking_distance_m")["satisfied"]


def test_a_segment_may_legitimately_claim_to_be_longer():
    # Walking further than the shortest path is a real choice - a shadier or
    # flatter way - so an over-claim is honoured rather than corrected.
    verdict = validate([walk(POINT_A, POINT_B, 400)], {}, GRAPH)
    assert check(verdict, "claimed_distances_possible")["satisfied"]
    assert verdict["walking_distance_m"] == 400


def test_distance_plausibility_admits_when_it_could_not_run():
    record = check(validate([walk(POINT_A, POINT_B, 10)], {}), "claimed_distances_possible")
    assert record["observed"] == "skipped" and "did not run" in record["evidence"]


def test_walking_distance_sums_only_walking_segments():
    route = [
        walk(POINT_A, POINT_B, 400),
        {"mode": "train", "from": POINT_B, "to": POINT_A, "distance_m": 9000},
        walk(POINT_A, POINT_B, 300),
    ]
    verdict = validate(route, {"max_walking_distance_m": 800})
    assert check(verdict, "max_walking_distance_m")["observed"] == 700, "9 km of train is not walked"


def test_transfers_count_boardings_not_segments():
    # Two consecutive segments on route 412 are one leg, then a train: one
    # transfer, not two. Counting segments instead of legs would say three.
    route = [
        {"mode": "bus", "route_id": "412", "from": POINT_A, "to": POINT_B},
        {"mode": "bus", "route_id": "412", "from": POINT_B, "to": POINT_A},
        {"mode": "train", "route_id": "BNFG", "from": POINT_A, "to": POINT_B},
    ]
    verdict = validate(route, {"max_transfers": 1})
    assert check(verdict, "max_transfers")["observed"] == 1
    assert check(verdict, "max_transfers")["satisfied"]

    assert validate(route, {"max_transfers": 0})["valid"] is False


def test_a_walk_only_route_has_no_transfers():
    assert validate([walk(POINT_A, POINT_B, 89)], {"max_transfers": 0})["valid"]


def test_excluded_modes_are_reported_by_name():
    route = [walk(POINT_A, POINT_B, 100),
             {"mode": "ferry", "from": POINT_B, "to": POINT_A}]
    record = check(validate(route, {"allowed_modes": ["walk", "bus"]}), "allowed_modes")
    assert not record["satisfied"] and record["observed"] == ["ferry", "walk"]


def test_avoided_way_types_are_caught():
    route = [walk(POINT_A, POINT_B, 89, highway_type="steps")]
    record = check(validate(route, {"avoid_highway_types": ["steps"]}), "avoid_highway_types")
    assert not record["satisfied"] and record["observed"] == ["steps"]


def test_arrive_before_compares_time_of_day():
    early = [walk(POINT_A, POINT_B, 89, arrive="2026-08-26T08:45:00+10:00")]
    late = [walk(POINT_A, POINT_B, 89, arrive="2026-08-26T09:15:00+10:00")]
    assert check(validate(early, {"arrive_before": "09:00"}), "arrive_before")["satisfied"]
    assert not check(validate(late, {"arrive_before": "09:00"}), "arrive_before")["satisfied"]


def test_an_unverifiable_time_constraint_is_unsatisfied_not_assumed():
    # No timestamp on the route at all. The honest verdict is "cannot be shown
    # to hold", which counts as a violation - assuming it holds would be the
    # model-style guess this whole architecture exists to avoid.
    record = check(validate([walk(POINT_A, POINT_B, 89)], {"arrive_before": "09:00"}),
                   "arrive_before")
    assert not record["satisfied"] and record["observed"] == "no_timestamp"


# ---------------------------------------------------------------------------
# The verdict as a whole.
# ---------------------------------------------------------------------------

def test_score_is_the_fraction_of_constraints_met():
    route = [walk(POINT_A, POINT_B, 1200, highway_type="footway")]
    verdict = validate(route, {
        "max_walking_distance_m": 800,        # fails
        "max_transfers": 2,                   # passes
        "avoid_highway_types": ["steps"],     # passes
    })
    assert verdict["constraints_checked"] == 3
    assert verdict["constraint_satisfaction_score"] == round(2 / 3, 4)
    assert verdict["valid"] is False, "S(r,C) < 1 cannot be a valid route"


def test_a_route_with_no_constraints_scores_one():
    # Zero of zero constraints satisfied is total success, not total failure.
    verdict = validate([walk(POINT_A, POINT_B, 89)], {}, GRAPH)
    assert verdict["constraint_satisfaction_score"] == 1.0 and verdict["valid"]


def test_every_violation_carries_its_evidence():
    verdict = validate([walk(POINT_A, POINT_B, 1200)], {"max_walking_distance_m": 800})
    for violation in verdict["violations"]:
        assert violation["evidence"], "a violation without evidence cannot be acted on"
        assert "observed" in violation and "threshold" in violation


# ---------------------------------------------------------------------------
# Planning, on the hand-built graph.
# ---------------------------------------------------------------------------

def test_plan_takes_the_shorter_side_of_the_square():
    plan = plan_route(GRAPH, POINT_A, POINT_B, {})
    assert plan["found"] and plan["distance_m"] == 89


def test_plan_reports_an_unreachable_destination_rather_than_inventing_one():
    plan = plan_route(GRAPH, POINT_A, POINT_D, {})
    assert not plan["found"] and plan["reason"] == "not_connected"


def test_plan_separates_blocked_by_constraints_from_not_connected():
    # A->C exists but only via steps. Excluding steps must say so specifically,
    # because the caller can act on that and cannot act on "no route".
    point_c = {"lat": _LAT - _STEP, "lon": _LON}
    plan = plan_route(GRAPH, POINT_A, point_c, {"avoid_highway_types": ["steps"]})
    assert not plan["found"] and plan["reason"] == "blocked_by_constraints"
    assert plan_route(GRAPH, POINT_A, point_c, {})["found"], "reachable when steps are allowed"


def test_planned_routes_pass_their_own_verifier():
    # The planner and the verifier are separate code paths on purpose. If a
    # route this agent produced could not survive its own checks, one of them
    # is wrong, and this is where that shows up.
    plan = plan_route(GRAPH, POINT_A, POINT_B, {})
    verdict = validate(plan["segments"], {}, GRAPH,
                       expected_origin=POINT_A, expected_destination=POINT_B)
    assert verdict["valid"], verdict["violations"]


def test_segments_merge_consecutive_runs_of_one_way_type():
    plan = plan_route(GRAPH, POINT_A, POINT_B, {})
    assert len(plan["segments"]) == 1 and plan["segments"][0]["highway_type"] == "footway"


def test_nearest_node_finds_the_node_under_the_point():
    node_id, snap = routing.nearest_node(GRAPH, _LAT, _LON)
    assert node_id == "A" and snap == 0


# ---------------------------------------------------------------------------
# Route options: the trade-off. A verdict says what a route violates; these
# say what the alternatives cost. The properties worth pinning are the ones a
# reader would otherwise have to trust: that an option satisfying everything
# outranks a shorter one that does not, that identical routes are not dressed
# up as a choice, and that an impossible option is reported rather than hidden.
# ---------------------------------------------------------------------------

# P to Q is one flight of steps, with a handrail. The way round through R is
# longer and has none.
STEPS_GRAPH = {
    "nodes": {
        "P": {"lat": _LAT, "lon": _LON},
        "Q": {"lat": _LAT, "lon": _LON + _STEP},
        "R": {"lat": _LAT - _STEP, "lon": _LON},
    },
    "edges": {
        "P": [["Q", 89, "steps", 0], ["R", 100, "footway", 1]],
        "Q": [["P", 89, "steps", 0], ["R", 100, "footway", 1]],
        "R": [["P", 100, "footway", 1], ["Q", 100, "footway", 1]],
    },
    "way_attrs": [{"handrail": "yes"}, {"surface": "concrete"}],
}


def options_for(graph, constraints, origin=POINT_A, destination=POINT_B):
    return routing.route_options(graph, origin, destination, constraints)


def test_an_option_that_satisfies_everything_outranks_a_shorter_one_that_does_not():
    result = options_for(STEPS_GRAPH, {"avoid_highway_types": ["steps"]})
    assert result["satisfiable"] is True
    assert result["recommended"] == "step_free", result["reason"]
    best = result["options"][0]
    assert best["distance_m"] == 200 and best["extra_m_vs_shortest"] == 111
    assert best["satisfies_everything_stated"] and best["violations"] == []
    # The shorter route is still offered, with what it would cost the traveller.
    shortest = next(o for o in result["options"] if o["option"] == "shortest")
    assert shortest["distance_m"] == 89
    assert shortest["violations"] == ["avoid_highway_types"]


def test_the_flight_of_steps_is_counted_with_its_handrail():
    # The handrail tag is on the way and is lost when segments are merged, so
    # this is read off the edges. "There are stairs" and "there are stairs with
    # a handrail" are different answers to the same question.
    result = options_for(STEPS_GRAPH, {})
    shortest = next(o for o in result["options"] if o["option"] == "shortest")
    facts = shortest["accessibility"]
    assert facts["steps_flights"] == 1 and facts["steps_flights_with_handrail"] == 1
    assert facts["steps_m"] == 89


def test_identical_routes_are_not_presented_as_a_choice():
    # On the four-node square the shortest walk is already step-free and states
    # no surface, so every variant is the same route. Offering it four times
    # would look like deliberation and tell the traveller nothing.
    result = options_for(GRAPH, {})
    assert len(result["options"]) == 1
    assert len(result["options"][0]["also_satisfies"]) == 3


def test_an_impossible_option_is_reported_not_omitted():
    graph = {"nodes": {"P": POINT_A, "Q": POINT_B},
             "edges": {"P": [["Q", 89, "steps", 0]], "Q": [["P", 89, "steps", 0]]},
             "way_attrs": [{"surface": "gravel"}]}
    result = options_for(graph, {"avoid_highway_types": ["steps"]})
    assert result["satisfiable"] is False
    step_free = next(o for o in result["options"] if o["option"] == "step_free")
    assert step_free["possible"] is False and "No route exists" in step_free["reason"]
    # And the one route there is comes back anyway, with its violation named -
    # the traveller learns the stairs are unavoidable, not merely disallowed.
    assert result["recommended"] == "shortest"
    assert next(o for o in result["options"] if o["option"] == "shortest")["distance_m"] == 89


# The mirror of STEPS_GRAPH: here the short way is the footway and the way
# round is the stairs. Needed because a requested combination that happens to
# produce the same route as one of the four variants is merged into it - which
# is right, and which makes that graph useless for testing the opposite.
FOOTWAY_GRAPH = {
    "nodes": {
        "P": {"lat": _LAT, "lon": _LON},
        "Q": {"lat": _LAT, "lon": _LON + _STEP},
        "R": {"lat": _LAT - _STEP, "lon": _LON},
    },
    "edges": {
        "P": [["Q", 89, "footway", 0], ["R", 100, "steps", 0]],
        "Q": [["P", 89, "footway", 0], ["R", 100, "steps", 0]],
        "R": [["P", 100, "steps", 0], ["Q", 100, "steps", 0]],
    },
    "way_attrs": [{}],
}


def test_a_stated_combination_the_variants_do_not_cover_is_planned_too():
    # Avoiding footways is nobody's accessibility need, so it is not one of the
    # four offered variants - and the traveller's actual request still gets
    # planned, as its own option, and wins because it is the only one that
    # satisfies what was asked.
    result = options_for(FOOTWAY_GRAPH, {"avoid_highway_types": ["footway"]})
    requested = next(o for o in result["options"] if o["option"] == "as_requested")
    assert requested["distance_m"] == 200, "the stairs are the only non-footway way round"
    assert result["recommended"] == "as_requested" and result["satisfiable"] is True
    # The shortest walk is still offered, and still says what it breaches.
    shortest = next(o for o in result["options"] if o["option"] == "shortest")
    assert shortest["distance_m"] == 89 and shortest["violations"] == ["avoid_highway_types"]


# ---------------------------------------------------------------------------
# Surface. The second accessibility constraint, and the one whose evidence is
# incomplete by nature: OpenStreetMap states a surface for about three quarters
# of this network, so a clean verdict here is weaker than a clean verdict on
# way types, and has to say so rather than read as proof.
# ---------------------------------------------------------------------------

# P to Q directly is a short gravel track; the way round through R is sealed
# for one leg and untagged for the other.
SURFACE_GRAPH = {
    "nodes": {
        "P": {"lat": _LAT, "lon": _LON},
        "Q": {"lat": _LAT, "lon": _LON + _STEP},
        "R": {"lat": _LAT - _STEP, "lon": _LON},
    },
    "edges": {
        "P": [["Q", 89, "path", 0], ["R", 100, "footway", 1]],
        "Q": [["P", 89, "path", 0], ["R", 100, "footway", 2]],
        "R": [["P", 100, "footway", 1], ["Q", 100, "footway", 2]],
    },
    "way_attrs": [{"surface": "gravel"}, {"surface": "concrete"}, {}],
}


def test_the_planner_routes_around_an_excluded_surface():
    direct = routing.shortest_path(SURFACE_GRAPH, "P", "Q")
    sealed = routing.shortest_path(SURFACE_GRAPH, "P", "Q", frozenset(), frozenset(["gravel"]))
    assert direct[1] == 89, "the gravel track is the short way"
    assert sealed[1] == 200, "avoiding it costs the detour through R"


def test_an_unknown_surface_is_not_treated_as_a_bad_one():
    # The R->Q leg states no surface. Excluding the unknown would refuse most
    # of the real network on a suspicion rather than on evidence - 27% of it
    # states no surface at all.
    sealed = routing.shortest_path(SURFACE_GRAPH, "P", "Q", frozenset(), frozenset(["gravel"]))
    assert sealed is not None and sealed[1] == 200


def test_a_pair_with_no_sealed_route_is_refused_with_its_reason():
    graph = {"nodes": {"P": POINT_A, "Q": POINT_B},
             "edges": {"P": [["Q", 89, "path", 0]], "Q": [["P", 89, "path", 0]]},
             "way_attrs": [{"surface": "gravel"}]}
    plan = plan_route(graph, POINT_A, POINT_B, {"avoid_surfaces": ["gravel"]})
    assert plan["found"] is False
    assert plan["reason"] == "blocked_by_constraints", plan
    assert plan["blocked_surfaces"] == ["gravel"]
    # The same pair without the constraint is perfectly routable, which is what
    # makes the refusal a statement about the request and not about the network.
    assert plan_route(graph, POINT_A, POINT_B, {})["found"] is True


def test_the_surface_verdict_reports_how_much_it_could_check():
    segments = [walk(POINT_A, POINT_B, 100, surface="concrete"),
                walk(POINT_B, POINT_A, 100)]              # states no surface
    record = check(validate(segments, {"avoid_surfaces": ["gravel"]}), "avoid_surfaces")
    assert record["satisfied"], "nothing states an excluded surface"
    assert "50% of the walking distance states a surface" in record["evidence"]


def test_a_stated_bad_surface_still_fails_the_check():
    segments = [walk(POINT_A, POINT_B, 100, surface="gravel")]
    record = check(validate(segments, {"avoid_surfaces": ["gravel"]}), "avoid_surfaces")
    assert not record["satisfied"] and record["observed"] == ["gravel"]


def test_surface_values_outside_the_vocabulary_are_dropped():
    clean, report = coerce_constraints({"avoid_surfaces": ["gravel", "lava", "regolith"]})
    assert clean["avoid_surfaces"] == ["gravel"]
    assert report["dropped_count"] == 0, "the key was recognised; only its bad values went"


# ---------------------------------------------------------------------------
# Walking semantics of the graph builder. The router is for pedestrians, so
# vehicle rules must not leak into it - an earlier version let them, and
# omitted whole classes of walkable way while claiming to cover them.
# ---------------------------------------------------------------------------

def test_a_vehicle_one_way_does_not_bind_a_pedestrian():
    import data
    assert data._way_direction({"oneway": "yes"}, "residential") == "both"
    assert data._way_direction({"oneway": "-1"}, "primary") == "both"
    assert data._way_direction({"junction": "roundabout"}, "tertiary") == "both"


def test_an_explicit_foot_one_way_does():
    import data
    assert data._way_direction({"oneway:foot": "yes"}, "footway") == "forward"
    assert data._way_direction({"oneway:foot": "-1"}, "steps") == "backward"


def test_ways_closed_to_walkers_are_excluded_unless_foot_is_permitted():
    import data
    assert not data._walkable({"access": "private"})
    assert not data._walkable({"foot": "no"})
    assert data._walkable({"access": "private", "foot": "designated"})
    assert data._walkable({})


def test_every_way_type_a_constraint_can_name_is_one_the_graph_fetches():
    # The bug this guards: `steps` sat on the whitelist while the graph fetched
    # none, so "avoid stairs" was satisfied by every route and checked nothing.
    import data
    assert set(routing.HIGHWAY_TYPES) <= set(data._NETWORK_HIGHWAYS.split("|"))


def test_every_tool_description_states_the_current_coverage_box():
    # The study area is quoted in prose in three of the tool descriptions, and
    # the Showcase orchestrator chooses tools by reading them. When the box was
    # widened, every one of those numbers had to move; a description still
    # advertising the old box would have the agent decline work it can do, or
    # claim work it cannot. Read from the source rather than the imported
    # module, so this needs no server and no MCP runtime.
    import ast
    import pathlib
    import re

    import data
    source = (pathlib.Path(__file__).resolve().parent / "agents" / "network.py").read_text(encoding="utf-8")
    expected = set(data.NETWORK_BBOX.values())
    checked = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node) or ""
        if "lat " not in doc or "lon " not in doc:
            continue                      # this tool does not state a coverage box
        numbers = {float(n) for n in re.findall(r"-?\d+\.\d+", doc)}
        assert expected <= numbers, f"{node.name} states a stale coverage box"
        checked += 1
    assert checked >= 3, f"only {checked} descriptions stated a coverage box"


def test_loading_collapses_duplicate_strings():
    # The graph is 1.4 million nodes and 3.1 million edges, and json.load gives
    # every occurrence of an id its own string object - three copies each, plus
    # three million copies of fourteen way types. Interning them on load is
    # worth about 350 MB, which is the difference between fitting in a small
    # cloud instance and not.
    #
    # Equality would not catch a regression here: two separate strings are
    # equal. Identity is the whole point, so identity is what is asserted.
    import json
    import os
    import tempfile
    from pathlib import Path

    import data
    saved = data.SNAPSHOT_DIR, data._ROAD_GRAPH_CACHE, dict(data._LOADED_FROM)
    with tempfile.TemporaryDirectory() as tmp:
        pinned = Path(tmp) / f"network_graph_{data._SCHEMA}.json"
        pinned.write_text(json.dumps(GRAPH), encoding="utf-8")
        os.utime(pinned, (0, 0))
        try:
            data.SNAPSHOT_DIR, data._ROAD_GRAPH_CACHE = Path(tmp), None
            graph = data.road_graph()
            keys = {node_id: node_id for node_id in graph["nodes"]}
            for node_id, bucket in graph["edges"].items():
                assert node_id is keys[node_id], "an edge key is a second copy of a node id"
                for entry in bucket:
                    assert entry[0] is keys[entry[0]], "a neighbour is a second copy"
            highways = {entry[2] for bucket in graph["edges"].values() for entry in bucket}
            objects = {id(entry[2]) for bucket in graph["edges"].values() for entry in bucket}
            assert len(objects) == len(highways), "way types are not shared objects"
        finally:
            data.SNAPSHOT_DIR, data._ROAD_GRAPH_CACHE = saved[0], saved[1]
            data._LOADED_FROM.clear()
            data._LOADED_FROM.update(saved[2])


def test_departures_read_like_rows_however_they_are_stored():
    # `Departures` holds three typed arrays and must still behave like the list
    # of rows it replaced, because find_departure, the producers and the
    # hand-built tables in these tests all read it as one.
    import data
    rows = [[30600, 0, 1], [45000, 2, 1], [88200, 0, 0]]
    packed = data.Departures(rows)
    assert len(packed) == 3
    assert packed[1] == (45000, 2, 1)
    assert list(packed) == [tuple(r) for r in rows]
    assert packed[1:] == [(45000, 2, 1), (88200, 0, 0)]
    assert list(packed.times) == [30600, 45000, 88200]


def test_a_pinned_snapshot_wins_over_the_cache_however_old():
    # Ground truth is measured on one snapshot. A cache refreshed on a timer
    # would move the answers under it, so a pinned file is used whenever it is
    # present, whatever its age, and the cache is not consulted.
    import json
    import os
    import tempfile
    from pathlib import Path

    import data
    saved = data.SNAPSHOT_DIR, data._ROAD_GRAPH_CACHE, dict(data._LOADED_FROM)
    with tempfile.TemporaryDirectory() as tmp:
        pinned = Path(tmp) / f"network_graph_{data._SCHEMA}.json"
        pinned.write_text(json.dumps(GRAPH), encoding="utf-8")
        os.utime(pinned, (0, 0))                        # 1970: past any TTL
        try:
            data.SNAPSHOT_DIR, data._ROAD_GRAPH_CACHE = Path(tmp), None
            # Compare the content, not the container: loading compacts
            # each edge into a tuple, so an equality check against the raw
            # JSON would now be testing list-versus-tuple rather than which
            # file was read. Reading the wrong file still fails this.
            loaded = data.road_graph()
            assert loaded["nodes"] == GRAPH["nodes"]
            assert ({k: [list(e) for e in v] for k, v in loaded["edges"].items()}
                    == {k: [list(e) for e in v] for k, v in GRAPH["edges"].items()})
            info = data.snapshot_info()["road_graph"]
            assert info["source"] == "pinned"
            # No manifest beside it, so the file cannot be vouched for - and
            # that is reported, not hidden.
            assert info["matches_manifest"] is False
        finally:
            data.SNAPSHOT_DIR, data._ROAD_GRAPH_CACHE = saved[0], saved[1]
            data._LOADED_FROM.clear()
            data._LOADED_FROM.update(saved[2])


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures.append((name, exc))
            print(f"  FAIL  {name}\n          {exc}")
        except Exception as exc:                                   # noqa: BLE001
            failures.append((name, exc))
            print(f"  ERROR {name}\n          {type(exc).__name__}: {exc}")

    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
