"""
audit_scenarios.py - check every scenario's authored claims against the pinned data.

    python src/audit_scenarios.py        # no server, no API key; exits 1 on a problem

A scenario's notes are assertions about the world: this distance, that stop, a
service on that route. An assertion nobody measured is the thing this whole
project objects to, and writing scenarios by hand is where they creep in - the
first draft of the multimodal set said one pair was "roughly 9 km apart, about
two hours on foot" when the network puts it at 6,885 m and 82 minutes, and
another justified itself with a train journey time the timetable index cannot
actually tell us.

What this checks, per scenario:

    snap        both endpoints inside coverage, and within the snap tolerance -
                an endpoint further from the network than that fails
                origin_match at scoring time, which looks like an agent bug and
                is a scenario bug
    walk        the shortest walk, and whether it meets or breaks the stated
                limit
    class       whether that agrees with the class the scenario is filed under.
                A case filed as unsatisfiable that a plain walk satisfies is
                not a hard case, it is a mislabelled one, and it would quietly
                flatter every condition at once
    transit     for multimodal cases: whether any stop pair's two walks fit
                inside the stated limit, and whether one route serves both ends

None of it needs a key. What waits for the key is how a model behaves on these
prompts - not whether the prompts are sound.
"""

from __future__ import annotations

import conditions
import data
import routing
from smac import haversine_m


def in_box(point: dict, box: dict) -> bool:
    return (box["lat_min"] <= point["lat"] <= box["lat_max"]
            and box["lon_min"] <= point["lon"] <= box["lon_max"])


def walk_between(graph: dict, origin: dict, destination: dict, blocked=()) -> tuple:
    a, snap_a = routing.nearest_node(graph, origin["lat"], origin["lon"])
    b, snap_b = routing.nearest_node(graph, destination["lat"], destination["lon"])
    found = routing.shortest_path(graph, a, b, frozenset(blocked))
    return (found[1] if found else None), snap_a, snap_b


def stops_within(table: dict, point: dict, metres: float) -> list[tuple]:
    """Stops within `metres` of a point, nearest first, with the routes calling there."""
    out = []
    for stop_id, stop in table["stops"].items():
        gap = haversine_m(point["lat"], point["lon"], stop["lat"], stop["lon"])
        if gap <= metres:
            routes = {table["route_names"][r] or table["routes"][r]
                      for _secs, r, _service in table["departures"].get(stop_id, [])}
            out.append((stop_id, stop["name"], round(gap), routes))
    return sorted(out, key=lambda row: row[2])


def main() -> int:
    graph, table, box = data.road_graph(), data.timetable(), data.NETWORK_BBOX
    problems: list[str] = []

    print(f"{'scenario':32} {'class':24} {'walk':>7} {'limit':>6} {'snap':>9}  verdict")
    print("-" * 110)
    for scenario in conditions.SCENARIOS:
        cls = getattr(scenario, "scenario_class", "unclassified")
        limit = scenario.truth_constraints.get("max_walking_distance_m")
        blocked = scenario.truth_constraints.get("avoid_highway_types", [])

        for label, point in (("origin", scenario.origin), ("destination", scenario.destination)):
            if not in_box(point, box):
                problems.append(f"{scenario.id}: {label} is outside the study area")

        walk, snap_a, snap_b = walk_between(graph, scenario.origin, scenario.destination, blocked)
        if max(snap_a, snap_b) > routing.SNAP_TOLERANCE_M:
            problems.append(f"{scenario.id}: an endpoint snaps {max(snap_a, snap_b)} m from the "
                            f"network, past the {routing.SNAP_TOLERANCE_M} m tolerance")

        if walk is None:
            verdict = "no walking route" + ("  (expected)" if not scenario.expect_route else "")
            if scenario.expect_route:
                problems.append(f"{scenario.id}: no walking route, but the scenario expects one")
        elif limit is None:
            verdict = "no distance limit stated"
        elif walk <= limit:
            verdict = f"walk fits the limit by {limit - walk} m"
            if cls in ("preference-unsatisfiable", "multimodal"):
                problems.append(f"{scenario.id}: filed under {cls}, but the walk alone "
                                f"satisfies the {limit} m limit")
        else:
            verdict = f"walk breaks the limit by {walk - limit} m"
            if cls in ("basic", "preference-satisfiable"):
                problems.append(f"{scenario.id}: filed under {cls}, but the walk breaks "
                                f"the {limit} m limit")

        print(f"{scenario.id:32} {cls:24} {str(walk or '-'):>7} {str(limit or '-'):>6} "
              f"{f'{snap_a}/{snap_b}':>9}  {verdict}")

    print("\nsurface constraints - does excluding a surface change the route, and can it be met?")
    print("-" * 110)
    for scenario in conditions.SCENARIOS:
        excluded = scenario.truth_constraints.get("avoid_surfaces")
        if not excluded:
            continue
        plain, _snap_a, _snap_b = walk_between(graph, scenario.origin, scenario.destination)
        a, _snap = routing.nearest_node(graph, scenario.origin["lat"], scenario.origin["lon"])
        b, _snap = routing.nearest_node(graph, scenario.destination["lat"],
                                        scenario.destination["lon"])
        found = routing.shortest_path(graph, a, b, frozenset(), frozenset(excluded))
        sealed = found[1] if found else None

        if sealed is None:
            print(f"{scenario.id:32} {plain:>7} m unconstrained, and NO route on sealed surfaces")
            if scenario.expect_route:
                problems.append(f"{scenario.id}: no sealed route exists, but the scenario "
                                f"expects one")
        else:
            detour = sealed - plain
            print(f"{scenario.id:32} {plain:>7} m -> {sealed} m sealed, a {detour} m detour")
            if not scenario.expect_route:
                problems.append(f"{scenario.id}: a sealed route exists ({sealed} m), but the "
                                f"scenario expects none")
            elif detour < 50:
                # The lesson from "avoid stairs" on a graph with no stairs: a
                # constraint that changes nothing is satisfied by everything.
                problems.append(f"{scenario.id}: excluding those surfaces changes the route by "
                                f"only {detour} m, so the constraint is close to vacuous here")

    print("\nmultimodal feasibility - can any itinerary satisfy the stated walking limit?")
    print("-" * 110)
    for scenario in conditions.SCENARIOS:
        if getattr(scenario, "scenario_class", "") != "multimodal":
            continue
        limit = scenario.truth_constraints.get("max_walking_distance_m") or 1500
        near_origin = stops_within(table, scenario.origin, limit)
        near_dest = stops_within(table, scenario.destination, limit)
        fitting = [(o, d) for o in near_origin for d in near_dest if o[2] + d[2] <= limit]
        direct = [(o, d, sorted(o[3] & d[3])) for o, d in fitting if o[3] & d[3]]

        print(f"\n{scenario.id}  (limit {limit} m)")
        print(f"   {len(fitting)} stop pair(s) whose two walks fit inside the limit")
        if not fitting:
            problems.append(f"{scenario.id}: no stop pair fits inside the {limit} m limit, "
                            f"so no itinerary can satisfy it")
        elif direct:
            o, d, shared = direct[0]
            print(f"   a single route serves both ends, e.g. {shared[:6]}")
            print(f"      {o[2]:>4} m to {o[1][:48]}")
            print(f"      {d[2]:>4} m from {d[1][:48]}")
        else:
            o, d = min(fitting, key=lambda pair: pair[0][2] + pair[1][2])
            print("   no single route serves both ends - this one needs a transfer")
            print(f"      {o[2]:>4} m to {o[1][:48]}")
            print(f"      {d[2]:>4} m from {d[1][:48]}")
            if scenario.truth_constraints.get("max_transfers") == 0:
                problems.append(f"{scenario.id}: needs a transfer but allows none")

    print("\n" + "=" * 110)
    if problems:
        print(f"{len(problems)} problem(s) found:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"{len(conditions.SCENARIOS)} scenarios: every claim checked here holds "
          f"against the pinned data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
