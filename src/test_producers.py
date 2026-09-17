"""
test_producers.py - the route producer, which is now the experiment's variable.

    python test_producers.py        # no server, no API key, no network access

Until a producer could write a wrong route, conditions A and C returned the
identical one every time and RQ2 had nothing to measure. These tests are about
the two properties that restores, and both of them are properties of the
*experiment* rather than of the agent:

  1. The stub's faults are the kind the verifier can catch. A fake that failed
     in ways nothing checks would make condition C look useless; a fake that
     failed in ways only C's own checks recognise would make it look infallible.
     Each fault below is asserted against the specific check that catches it.

  2. What the producer is told stays fixed. It is offered stops and routes and
     never their times, so a departure time is always the producer's own claim.
     If that ever leaks, the experiment quietly stops measuring anything.
"""

from __future__ import annotations

import conditions
import producers
import routing
from smac import haversine_m
from test_routing import GRAPH, POINT_A, POINT_B, TIMETABLE, check

# A second toy graph, one 2 km edge. The four-node square in test_routing is
# 100 m to a side, and an understated distance there cannot be caught by
# anyone: the verifier allows a 60 m snap tolerance, which on a 89 m route
# swallows any understatement a producer could make. That is the checker being
# right rather than lax - on the real network every scenario is kilometres -
# but it means the fault needs a route long enough to show it.
LONG_A = {"lat": -27.4800, "lon": 153.0100}
LONG_B = {"lat": -27.4800, "lon": 153.0300}
LONG_M = round(haversine_m(LONG_A["lat"], LONG_A["lon"], LONG_B["lat"], LONG_B["lon"]))
LONG_GRAPH = {
    "nodes": {"P": dict(LONG_A), "Q": dict(LONG_B)},
    "edges": {"P": [["Q", LONG_M, "footway"]], "Q": [["P", LONG_M, "footway"]]},
}


def scenario(**kw):
    base = dict(id="p", requirement="get me there", origin=POINT_A, destination=POINT_B,
                truth_constraints={})
    base.update(kw)
    return conditions.Scenario(**base)


def verdict_for(plan, table=None, graph=GRAPH, origin=POINT_A, destination=POINT_B):
    return routing.validate(plan["segments"], {}, graph,
                            expected_origin=origin, expected_destination=destination, table=table)


def long_walk():
    """A scenario whose route is long enough for an understatement to be visible."""
    return scenario(origin=LONG_A, destination=LONG_B)


def long_verdict(plan):
    return verdict_for(plan, graph=LONG_GRAPH, origin=LONG_A, destination=LONG_B)


# --- what the producer is allowed to know ----------------------------------

def test_stops_are_offered_but_their_times_never_are():
    # The experiment rests on this asymmetry: a producer can name a real stop
    # and a real route, and still has to invent the departure time - which is
    # the claim the timetable can then settle. Leak the times here and every
    # transit verdict becomes a formality.
    offered = producers.nearby_stops(TIMETABLE, POINT_A)
    assert offered and offered[0]["name"] == "Test Station"
    assert offered[0]["routes"] == ["412"], "the route that calls there, by name"
    leaked = {"departures", "times", "depart", "schedule", "timetable"} & set(offered[0])
    assert not leaked, f"the producer must not be told when services run: {leaked}"


# --- the stub fails in ways the verifier recognises ------------------------

def test_an_understated_walk_is_caught_as_an_impossible_claim():
    plan = producers.stub(long_walk(), LONG_GRAPH, {}, TIMETABLE, fault="understated_walk")
    assert plan["found"], "the fault is in the numbers, not in whether a route came back"
    assert plan["distance_m"] < LONG_M, "the stub is supposed to understate"
    assert not check(long_verdict(plan), "claimed_distances_possible")["satisfied"]


def test_an_invented_service_is_caught_against_the_timetable():
    plan = producers.stub(scenario(), GRAPH, {}, TIMETABLE, fault="invented_departure")
    if len(plan["segments"]) < 2:
        return                       # no stop near this toy destination; nothing to assert
    assert not check(verdict_for(plan, TIMETABLE), "transit_services_exist")["satisfied"]


def test_a_one_minute_connection_is_caught_as_unmakeable():
    plan = producers.stub(scenario(truth_constraints={"min_transfer_seconds": 180}),
                          GRAPH, {"min_transfer_seconds": 180}, TIMETABLE,
                          fault="impossible_transfer")
    if len(plan["segments"]) < 2:
        return
    verdict = routing.validate(plan["segments"], {"min_transfer_seconds": 180}, GRAPH,
                               expected_origin=POINT_A, expected_destination=POINT_B,
                               table=TIMETABLE)
    assert not check(verdict, "temporal_feasibility")["satisfied"]


def test_the_stub_repairs_when_it_is_shown_what_was_measured():
    # Condition C's loop depends on a producer that can act on evidence. This
    # is the offline stand-in for that, and the reason C's repair path can be
    # developed and tested before a key exists.
    broken = producers.stub(long_walk(), LONG_GRAPH, {}, TIMETABLE, fault="understated_walk")
    repaired = producers.stub(long_walk(), LONG_GRAPH, {}, TIMETABLE,
                              feedback=f"claimed_distances_possible: measured {LONG_M} "
                                       f"against {broken['distance_m']}")
    assert not check(long_verdict(broken), "claimed_distances_possible")["satisfied"]
    assert check(long_verdict(repaired), "claimed_distances_possible")["satisfied"]


# --- the seam itself --------------------------------------------------------

def test_every_producer_returns_the_shape_the_conditions_read():
    for name, make in producers.PRODUCERS.items():
        produced = make(scenario(), GRAPH, {}, TIMETABLE)
        plan = produced[0] if isinstance(produced, tuple) else produced
        assert set(plan) >= {"found", "segments", "distance_m", "producer"}, name
        assert plan["producer"] == name or name == "model"


def test_the_model_producer_without_a_key_declines_rather_than_crashing():
    # The Showcase machine may have no key at all. A producer that raised here
    # would take the whole experiment down; one that returns "no route" is a
    # result the scorer already knows how to judge.
    import os

    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        plan, calls = producers.model(scenario(), GRAPH, {}, TIMETABLE)
        assert plan["found"] is False and calls == 0
        assert "no API key" in plan["detail"]
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
