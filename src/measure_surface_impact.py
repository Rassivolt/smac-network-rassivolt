"""
measure_surface_impact.py - what does "keep me on sealed paths" actually cost?

    python measure_surface_impact.py                 # 150 pairs, fixed seed
    python measure_surface_impact.py --pairs 400     # tighter intervals, slower
    python measure_surface_impact.py --json          # machine-readable

The claim this agent's fifth tool rests on is that avoiding unpaved ground is
sometimes free, sometimes a detour, and sometimes impossible - and that which
of the three you get depends on where you are. That is an empirical claim, and
until this script existed it was a number in a chat transcript, which is not
evidence. This re-derives it from the pinned graph, with the sampling written
down, so the figure in the design rationale has something behind it.

WHAT IS MEASURED. Origin-destination pairs are drawn at random from the walking
graph. Each pair is planned twice by the production planner - once with no
constraints, once with every unpaved surface excluded - and the outcome is one
of three:

    unchanged     the sealed route is the same length as the unrestricted one
    detour        a sealed route exists but is longer
    no sealed     no route avoiding unpaved ground could be found at all
                  (routing.plan_route's `blocked_by_constraints`)

SETTING. Pairs are split by a property of the UNRESTRICTED route: the share of
its metres carried by `path`, `track` or `bridleway`, the OpenStreetMap way
types typical of parkland and bush tracks. At or above PARKLAND_SHARE the pair
is "parkland", otherwise "street". The split deliberately does not look at
surface tags - classifying by surface and then measuring a surface effect would
be circular.

TAGGING COVERAGE. Every figure is reported next to the share of route metres
whose surface OpenStreetMap actually states. A route with no unpaved ground
found on 40% coverage is a weaker claim than the same route at 95%, and the
difference between them is mapping effort, not pavement. Reporting the two
together is the same honesty the tool itself practises.

A NOTE ON METHOD. An earlier interactive measurement reported 36 park pairs and
153 arterial pairs. That sampling was never written down and cannot be
reproduced, so this is a new measurement, not a replication - the numbers are
expected to differ, and the earlier ones should not be quoted.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import data
import routing

# Sampling. Both ends are drawn from the graph itself, so the sample follows
# the mapped network rather than a rectangle of empty land.
SEED = 20260923
MIN_SEPARATION_M = 400      # below this the route is too short to detour
MAX_SEPARATION_M = 2500     # above this it is not a walk most people take
MAX_SNAP_M = 150            # a target that lands this far from any way is skipped

# The way types that mark a route as running through parkland rather than
# streets. `footway` is deliberately absent: it is the ordinary urban pavement.
PARKLAND_HIGHWAYS = frozenset({"path", "track", "bridleway"})
PARKLAND_SHARE = 0.25

UNPAVED = list(routing.UNPAVED_SURFACES)


def _offset(lat: float, lon: float, bearing_deg: float, metres: float) -> tuple[float, float]:
    """A point `metres` away on a given bearing, flat-earth over a few km."""
    dlat = metres * math.cos(math.radians(bearing_deg)) / 111_320.0
    dlon = metres * math.sin(math.radians(bearing_deg)) / (111_320.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def _profile(graph, path: list[int]) -> dict:
    """Metres by way type and by surface along one route."""
    total = parkland = stated = unpaved = 0
    for a, b in zip(path, path[1:]):
        k = graph.first_edge(a, b)
        if k is None:
            continue
        weight = graph.wt[k]
        total += weight
        if graph.highway(k) in PARKLAND_HIGHWAYS:
            parkland += weight
        surface = graph.attrs(k).get("surface")
        if surface:
            stated += weight
            if surface in routing.UNPAVED_SURFACES:
                unpaved += weight
    return {"total_m": total, "parkland_m": parkland,
            "surface_stated_m": stated, "unpaved_m": unpaved}


def _stranded(graph, node: int) -> bool:
    """True when every way out of this node is unpaved.

    "No sealed route" means two very different things and counting them
    together hides both. A node in the middle of a bush track has no sealed
    edge to leave by, so the search fails at the first step and the finding is
    about that one point - partly an artefact of sampling arbitrary graph
    nodes. A node on a sealed street whose destination sits beyond an unsealed
    corridor is a finding about the network between them, which is the thing
    the tool exists to report.
    """
    for k in graph.edges(node):
        if graph.attrs(k).get("surface") not in routing.UNPAVED_SURFACES:
            return False
    return True



def sample_pairs(graph, count: int, rng: random.Random) -> list[tuple[int, int]]:
    """`count` origin-destination node pairs, each a plausible walk apart."""
    # The graph's original node order, so a seed draws the same pairs it did
    # when the graph was a dictionary.
    node_ids = list(graph.order)
    pairs, attempts = [], 0
    while len(pairs) < count and attempts < count * 40:
        attempts += 1
        start = rng.choice(node_ids)
        separation = rng.uniform(MIN_SEPARATION_M, MAX_SEPARATION_M)
        lat, lon = _offset(graph.lat[start], graph.lon[start], rng.uniform(0, 360), separation)
        goal, snap = routing.nearest_node(graph, lat, lon)
        if goal is None or goal == start or snap > MAX_SNAP_M:
            continue
        pairs.append((start, goal))
    return pairs


def measure(graph: dict, pairs: list[tuple[str, str]], verbose: bool = True) -> list[dict]:
    """Plan each pair twice and classify the difference.

    `routing.shortest_path` rather than `routing.plan_route`, because the node
    path is needed to profile the route and plan_route returns coalesced
    segments carrying only coordinates. This is the same pathfinder the agent
    serves from; what plan_route adds on top - snapping and telling
    "the restrictions blocked it" apart from "these points do not connect" -
    is done here explicitly, by the same test it uses.
    """
    blocked = frozenset(UNPAVED)
    rows = []
    for n, (start, goal) in enumerate(pairs, 1):
        base = routing.shortest_path(graph, start, goal)
        if base is None:
            rows.append({"outcome": "not_connected", "setting": None})
            continue
        path, baseline_m = base
        profile = _profile(graph, path)

        sealed = routing.shortest_path(graph, start, goal, frozenset(), blocked)
        if sealed is not None:
            extra = sealed[1] - baseline_m
            outcome, sealed_m = ("unchanged" if extra <= 0 else "detour"), sealed[1]
        else:
            # An unrestricted route exists and a sealed one does not, so it was
            # the surfaces that blocked it - plan_route's own distinction.
            extra, sealed_m = None, None
            outcome = ("stranded_endpoint"
                       if _stranded(graph, start) or _stranded(graph, goal)
                       else "no_sealed_route")

        total = profile["total_m"] or baseline_m or 1
        parkland_share = profile["parkland_m"] / total
        rows.append({
            "outcome": outcome,
            "setting": "parkland" if parkland_share >= PARKLAND_SHARE else "street",
            "baseline_m": baseline_m,
            "sealed_m": sealed_m,
            "extra_m": extra,
            "parkland_share": round(parkland_share, 3),
            "surface_coverage": round(profile["surface_stated_m"] / total, 3),
            "unpaved_share": round(profile["unpaved_m"] / total, 3),
        })
        if verbose and n % 10 == 0:
            print(f"  {n}/{len(pairs)} planned", flush=True)
    return rows


def summarise(rows: list[dict]) -> dict:
    out = {}
    for setting in ("parkland", "street"):
        group = [r for r in rows if r["setting"] == setting]
        if not group:
            continue
        detours = [r["extra_m"] for r in group if r["outcome"] == "detour"]
        detours.sort()
        counts = {o: sum(1 for r in group if r["outcome"] == o)
                  for o in ("unchanged", "detour", "no_sealed_route",
                            "stranded_endpoint")}
        out[setting] = {
            "pairs": len(group),
            **counts,
            "no_sealed_pct": round(100 * counts["no_sealed_route"] / len(group), 1),
            "median_detour_m": detours[len(detours) // 2] if detours else None,
            "max_detour_m": detours[-1] if detours else None,
            # The honesty column: how much of these routes OSM actually tags.
            "median_surface_coverage": round(
                sorted(r["surface_coverage"] for r in group)[len(group) // 2], 3),
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--pairs", type=int, default=150)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    graph = data.road_graph()
    routing.spatial_index(graph)
    snapshot = data.snapshot_info().get("road_graph", {})

    if not args.json:
        print(f"graph   : {len(graph)} nodes, "
              f"{snapshot.get('source', '?')} {snapshot.get('file', '?')} "
              f"sha256 {snapshot.get('sha256', '?')}")
        print(f"sampling: {args.pairs} pairs, seed {args.seed}, "
              f"{MIN_SEPARATION_M}-{MAX_SEPARATION_M} m apart")
        print("planning each pair twice (unrestricted, then sealed only)...", flush=True)

    started = time.time()
    rng = random.Random(args.seed)
    pairs = sample_pairs(graph, args.pairs, rng)
    rows = measure(graph, pairs, verbose=not args.json)
    summary = summarise(rows)

    result = {
        "graph_sha256": snapshot.get("sha256"),
        "graph_source": snapshot.get("source"),
        "seed": args.seed,
        "pairs_requested": args.pairs,
        "pairs_routed": sum(1 for r in rows if r["setting"]),
        "not_connected": sum(1 for r in rows if r["outcome"] == "not_connected"),
        "parkland_threshold": PARKLAND_SHARE,
        "seconds": round(time.time() - started, 1),
        "by_setting": summary,
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return

    print(f"\nrouted {result['pairs_routed']} pairs "
          f"({result['not_connected']} unconnected, excluded) in {result['seconds']}s\n")
    head = f"{'setting':<10} {'pairs':>6} {'same':>6} {'detour':>7} {'none':>6} {'none%':>7} {'strand':>7} " \
           f"{'med.detour':>11} {'max':>7} {'surf.cov':>9}"
    print(head)
    print("-" * len(head))
    for setting, m in summary.items():
        print(f"{setting:<10} {m['pairs']:>6} {m['unchanged']:>6} {m['detour']:>7} "
              f"{m['no_sealed_route']:>6} {m['no_sealed_pct']:>6}% "
              f"{m['stranded_endpoint']:>7} "
              f"{str(m['median_detour_m'] or '-'):>11} {str(m['max_detour_m'] or '-'):>7} "
              f"{m['median_surface_coverage']:>9}")

    print()
    print("'none' is a pair with no sealed route through the network. 'strand' is one")
    print("where an endpoint itself has no sealed way out, which is a fact about that")
    print("point rather than about the network between the two.")
    print("surf.cov is the median share of route metres whose surface OSM states; a low")
    print("figure means 'no unsealed ground found' is a weaker claim than it looks.")


if __name__ == "__main__":
    main()
