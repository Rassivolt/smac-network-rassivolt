"""
producers.py - who writes the route the three conditions then argue about.

    python producers.py --demo                 # one itinerary from each producer

Until now the route came from `routing.plan_route`, which is correct by
construction: it cannot return a discontinuous journey, a walk that is not on
the network, or a bus that does not run. That made the experiment's central
comparison vacuous - conditions A and C returned the identical route in all 22
scenarios, so verification could change what the agent *said* and never what it
*did*. `same_route_as_A` was 1.00 everywhere, which is the honest way of saying
RQ2 had nothing to measure.

This module supplies the missing variable. A producer takes a scenario and
returns a route in the same shape `plan_route` does, and the conditions do not
care which one made it:

    deterministic  the shortest-path planner. Correct by construction; the
                   control, and what the MCP tools keep serving.
    model          a model assembles a multimodal itinerary - which stops to
                   walk to, which service to board, at what time. It is given
                   the stops that exist and NOT the times they run, so the
                   times it states are its own claim, which is exactly the
                   claim the timetable can settle.
    stub           a deliberately fallible fake, seeded and offline, used to
                   develop and test this machinery without a key and without
                   spending one. It is NOT evidence about any real model and
                   must never be reported as though it were.

The point of the multimodal framing: this agent already verifies transit legs
against the Translink timetable - that the stop exists, that a service departs
it at the claimed time, on that service day, on the claimed route, with a
makeable connection - and the deterministic planner, which only ever walks,
never once exercised those checks. A model writing itineraries produces errors
of precisely the kind they were built to catch.
"""

from __future__ import annotations

import argparse
import json
import random
from typing import Any

import data
import llm
import routing
from smac import haversine_m

# How far from an endpoint a stop is worth offering. Far enough to give a real
# choice of boarding points, short enough that the walk to one is plausible.
STOP_SEARCH_RADIUS_M = 1200
STOPS_OFFERED = 8


def nearby_stops(table: dict, point: dict, limit: int = STOPS_OFFERED) -> list[dict]:
    """The stops a traveller could reasonably reach from a point, nearest first.

    Each carries the routes that call there, read from the timetable index, but
    NOT their departure times. That asymmetry is deliberate and it is what makes
    the experiment work: a producer can name a real stop and a real route, and
    still has to state a departure time from its own head. Whether that time
    exists is then a question with an answer.
    """
    found = []
    for stop_id, stop in table["stops"].items():
        metres = haversine_m(point["lat"], point["lon"], stop["lat"], stop["lon"])
        if metres <= STOP_SEARCH_RADIUS_M:
            found.append((metres, stop_id, stop))
    found.sort(key=lambda row: row[0])

    offered = []
    for metres, stop_id, stop in found[:limit]:
        routes = sorted({table["route_names"][route_ix] or table["routes"][route_ix]
                         for _secs, route_ix, _service_ix in table["departures"].get(stop_id, [])})
        offered.append({
            "stop_id": stop_id,
            "name": stop["name"],
            "lat": round(stop["lat"], 6),
            "lon": round(stop["lon"], 6),
            "walk_m": round(metres),
            "routes": routes[:12],
        })
    return offered


def _as_plan(segments: list[dict], producer: str, note: str = "") -> dict:
    """Wrap a produced route in the shape every condition already reads."""
    if not segments:
        return {"found": False, "segments": [], "distance_m": 0,
                "reason": "no_route", "detail": note or "The producer returned no route.",
                "producer": producer}
    walking = sum(s.get("distance_m", 0) for s in segments if s["mode"] == "walk")
    return {"found": True, "segments": segments, "distance_m": round(walking),
            "reason": None, "detail": note, "producer": producer,
            # Claimed, not measured. Every number in here came from whoever
            # wrote the route; the verifier's job is to disagree with it.
            "claimed": True}


# ---------------------------------------------------------------------------
# 1. Deterministic - the control, and what the MCP tools serve.
# ---------------------------------------------------------------------------

def deterministic(scenario, graph: dict, constraints: dict, table: dict | None = None) -> dict:
    depart = routing.read_stamp(scenario.departure) if scenario.departure else None
    plan = routing.plan_route(graph, scenario.origin, scenario.destination, constraints, depart)
    plan["producer"] = "deterministic"
    return plan


# ---------------------------------------------------------------------------
# 2. Model - a model assembles the itinerary.
# ---------------------------------------------------------------------------

ITINERARY_SYSTEM = """You are a journey planner for Brisbane. Produce ONE itinerary \
for the traveller, as JSON only - no prose, no code fences.

Shape:
{"segments": [
  {"mode": "walk", "from": {"lat": .., "lon": ..}, "to": {"lat": .., "lon": ..}, \
"distance_m": <integer metres>, "depart": "<ISO 8601>", "arrive": "<ISO 8601>"},
  {"mode": "bus", "route_id": "<route as shown>", "from": {"lat": .., "lon": ..}, \
"to": {"lat": .., "lon": ..}, "depart": "<ISO 8601>", "arrive": "<ISO 8601>"}
]}

Rules:
- Modes: walk, bus, train, ferry, tram. Every journey starts and ends with a walk \
segment unless it starts or ends at a stop.
- Each segment must start where the previous one ended, exactly.
- Use the stops given to you. Board only at a stop's own coordinates.
- Timestamps are ISO 8601 with the +10:00 offset. Times must run forwards, and a \
connection needs enough time to make it.
- Walking is about 1.4 metres per second. State distance_m for every walk segment.
- Honour the traveller's stated constraints.
- Give the itinerary you believe is right. Do not explain it."""


def _itinerary_request(scenario, constraints: dict, table: dict) -> str:
    """Everything the model is told. Stops and routes, deliberately no times."""
    lines = [
        f"Traveller asked: {scenario.requirement}",
        f"Origin: {scenario.origin['lat']}, {scenario.origin['lon']}",
        f"Destination: {scenario.destination['lat']}, {scenario.destination['lon']}",
        f"Constraints to honour: {json.dumps(constraints) if constraints else 'none stated'}",
        f"Departure time: {scenario.departure or 'not stated - assume 2026-09-09T08:00:00+10:00'}",
        "",
        "Stops near the origin:",
        json.dumps(nearby_stops(table, scenario.origin), indent=1),
        "",
        "Stops near the destination:",
        json.dumps(nearby_stops(table, scenario.destination), indent=1),
    ]
    return "\n".join(lines)


def _read_itinerary(reply: str) -> tuple[list[dict], str]:
    """Parse a model's itinerary. Returns (segments, note); segments empty on failure."""
    body = reply.strip()
    if body.startswith("```"):
        body = body.split("```")[1].removeprefix("json").strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return [], "the itinerary was not readable JSON"
    raw = payload.get("segments") if isinstance(payload, dict) else payload
    # The same liberal reader the MCP tool uses, so a producer that spells a
    # segment its own way is not scored as having failed to produce one.
    segments, error = routing.read_segments(raw)
    return (segments, "") if error is None else ([], error)


def model(scenario, graph: dict, constraints: dict, table: dict | None = None,
          feedback: str = "") -> tuple[dict, int]:
    """Ask a model for an itinerary. Returns (plan, llm_calls).

    `feedback` carries measured violations back for a second attempt - what
    condition C does with a verdict, and the reason this takes an argument the
    self-review condition cannot supply.
    """
    if not llm.configured():
        return _as_plan([], "model", "no API key configured, so no itinerary was produced"), 0
    table = table if table is not None else data.timetable()
    user = _itinerary_request(scenario, constraints, table)
    if feedback:
        user += f"\n\nYour previous itinerary was checked against the map and the timetable " \
                f"and failed:\n{feedback}\nProduce a corrected itinerary."
    try:
        reply = llm.ask(system=ITINERARY_SYSTEM, user=user, max_tokens=1500)
    except Exception as exc:                                       # noqa: BLE001
        return _as_plan([], "model", f"the itinerary call failed ({type(exc).__name__})"), 1
    segments, note = _read_itinerary(reply)
    return _as_plan(segments, "model", note), 1


# ---------------------------------------------------------------------------
# 3. Stub - a fallible fake, for building this without a key.
# ---------------------------------------------------------------------------
#
# Every fault below is one the verifier has a check for, and one a real model
# plausibly commits: a shortened walk it did not measure, a service invented at
# a round number of minutes, a connection too tight to make. The point is to
# prove the machinery reacts, not to claim anything about how often a model
# would do this. Seeded by scenario id, so a run is reproducible and a test can
# assert on it.
# ---------------------------------------------------------------------------

STUB_FAULTS = ("understated_walk", "invented_departure", "impossible_transfer", "none")


def stub(scenario, graph: dict, constraints: dict, table: dict | None = None,
         fault: str | None = None, feedback: str = "") -> dict:
    """A fake model's itinerary: a real walk, then a plausible-looking lie.

    Told what it got wrong, it produces the route it should have produced -
    standing in for a model that repairs when it is handed evidence. That is
    what lets condition C's repair path be exercised offline; whether a real
    model repairs this readily is a question only the real one can answer.
    """
    plan = deterministic(scenario, graph, constraints, table)
    if not plan["found"]:
        return _as_plan([], "stub", plan.get("detail", ""))
    if feedback:
        plan["producer"] = "stub"
        plan["detail"] = "repaired after being shown the measured violations"
        return plan

    rng = random.Random(scenario.id)
    fault = fault or rng.choice(STUB_FAULTS)
    first, last = plan["segments"][0], plan["segments"][-1]
    walk = {
        "mode": "walk",
        "from": {"lat": first["from"]["lat"], "lon": first["from"]["lon"]},
        "to": {"lat": last["to"]["lat"], "lon": last["to"]["lon"]},
        "distance_m": plan["distance_m"],
        "depart": first.get("depart"),
        "arrive": last.get("arrive"),
    }

    if fault == "understated_walk":
        # The failure this project exists to catch: a number nobody measured.
        walk["distance_m"] = max(50, round(plan["distance_m"] * 0.4))
        return _as_plan([walk], "stub", "walk distance understated by 60%")

    if fault in ("invented_departure", "impossible_transfer"):
        table = table if table is not None else data.timetable()
        stops = nearby_stops(table, scenario.destination, limit=1)
        if not stops or not stops[0]["routes"]:
            return _as_plan([walk], "stub", "no stop nearby; returned the walk alone")
        stop, route = stops[0], stops[0]["routes"][0]
        walk["to"] = {"lat": stop["lat"], "lon": stop["lon"]}
        walk["arrive"] = "2026-09-09T08:14:00+10:00"
        walk["depart"] = "2026-09-09T08:00:00+10:00"
        # 03:17 is the hour no Brisbane service runs, so the invented departure
        # is impossible rather than merely unlikely.
        board = "2026-09-09T08:15:00+10:00" if fault == "impossible_transfer" \
            else "2026-09-09T03:17:00+10:00"
        ride = {
            "mode": "bus", "route_id": route,
            "from": {"lat": stop["lat"], "lon": stop["lon"]},
            "to": {"lat": scenario.destination["lat"], "lon": scenario.destination["lon"]},
            "depart": board, "arrive": "2026-09-09T08:45:00+10:00",
        }
        note = ("boarding one minute after arriving" if fault == "impossible_transfer"
                else "a service invented at 03:17")
        return _as_plan([walk, ride], "stub", note)

    return _as_plan([walk], "stub", "no fault injected")


PRODUCERS = {"deterministic": deterministic, "model": model, "stub": stub}

# Producers that can act on a verdict. The deterministic planner cannot: it
# already returns the best route it has, so handing it violations changes
# nothing. This is what condition C sends feedback to.
REPAIRS_ON_FEEDBACK = frozenset({"model", "stub"})


def main() -> int:
    parser = argparse.ArgumentParser(description="Show what each producer returns.")
    parser.add_argument("--demo", action="store_true")
    parser.parse_args()

    import conditions
    graph, table = data.road_graph(), data.timetable()
    scenario = conditions.SCENARIOS[0]
    print(f"scenario: {scenario.id} - {scenario.requirement}\n")
    for name in PRODUCERS:
        produced = PRODUCERS[name](scenario, graph, dict(scenario.truth_constraints), table)
        plan = produced[0] if isinstance(produced, tuple) else produced
        print(f"{name:14} found={plan['found']} segments={len(plan['segments'])} "
              f"claimed_walk={plan['distance_m']} m  {plan.get('detail') or ''}")
        if plan["found"]:
            verdict = routing.validate(
                plan["segments"], dict(scenario.truth_constraints), graph,
                expected_origin=scenario.origin, expected_destination=scenario.destination,
                coverage=data.NETWORK_BBOX,
                table=table if any(s["mode"] != "walk" for s in plan["segments"]) else None)
            print(f"{'':14} verifier: {routing.summarise(verdict)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
