"""
routing.py - the routing and verification layers for the network agent.

Deliberately free of MCP, of the Claude API and of any tool wiring: everything
here is a pure function over a graph, a list of segments and a constraint set.
That separation is not tidiness. The project's central claim is that a route is
checked against the network rather than against a model's opinion, and a
verifier you cannot run on its own - on hand-built cases whose answers you knew
before you ran them - is a verifier you are asking people to take on trust.
`test_routing.py` exercises this module with no server and no API key.

Three layers live here, in order:

  1. Constraints  - a strict whitelist that turns loose JSON into a known shape.
  2. Routing      - a spatial index and Dijkstra over the cached OSM graph.
  3. Verification - deterministic checks producing per-constraint verdicts.

Nothing in this file calls a model, and nothing in it invents a number.
"""

from __future__ import annotations

import bisect
import heapq
from collections import deque
import math
from datetime import date, datetime, timedelta
from typing import Any

from graph import GRID_CELL_DEG, as_graph
from smac import BRISBANE_TZ, haversine_m, metres, seconds

# ---------------------------------------------------------------------------
# 1. Constraints
# ---------------------------------------------------------------------------

# Travel modes this agent recognises. A mode outside this set is not "wrong",
# it is simply not something the verifier can reason about, so it is dropped
# rather than silently treated as walking.
MODES = ("walk", "bus", "train", "ferry", "tram")

# Way types the walking graph carries (data._NETWORK_HIGHWAYS). The whitelist
# for avoid_highway_types is drawn from this list so a constraint can only name
# a type that exists in the network. That was not always true: `steps` was on
# this list while the graph fetched none, so "avoid stairs" was satisfied by
# every route without anything being checked.
HIGHWAY_TYPES = (
    "trunk", "primary", "secondary", "tertiary", "residential", "unclassified",
    "living_street", "service", "track", "footway", "path", "pedestrian",
    "steps", "cycleway",
)

# Preferred walking speed, metres per second. 1.4 m/s is the figure used in
# pedestrian planning for an unhurried adult on the flat (~5 km/h). Declared
# here as a constant rather than buried in an expression, because every walking
# duration this agent reports is derived from it and a reader of the results is
# entitled to know which assumption produced them.
WALKING_SPEED_MPS = 1.4

# The fastest a CLAIMED walking time may imply, metres per second. Not a
# planning figure: 2.5 m/s is 9 km/h, a pace no one keeps up on foot, so a walk
# claimed faster than this is impossible rather than brisk. Generous on
# purpose - the check exists to catch "a 10-minute walk" for a 21-minute one,
# not to argue with a fast walker.
MAX_WALKING_SPEED_MPS = 2.5

# How far apart two consecutive segments' endpoints may be before the route is
# treated as discontinuous. Generous enough to absorb a stop coordinate sitting
# a few metres off the kerb it serves, tight enough that a genuine gap - a
# missing leg between two places - still fails.
CONTINUITY_TOLERANCE_M = 30

# How far a point may sit from the nearest graph node and still be treated as
# being "on" the network. Roughly a street width plus a footpath.
SNAP_TOLERANCE_M = 60

# How far the planning tools let a planned route start from the point asked
# for. They begin at the nearest mapped node by construction, so their endpoint
# check measures the nearest path, not a wrong claim: a stadium's geocoded
# centre is 103 m from one. 400 m is a five-minute walk; past it, a plan does
# not start where it was asked to, and that is still reported as a failure.
PLANNING_ENDPOINT_TOLERANCE_M = 400

# How far a transit segment's endpoint may sit from the GTFS stop it claims to
# use. Larger than the road tolerance on purpose: a station is a building, its
# published coordinate is one point in it, and a platform can be a long way from
# that point. Tight enough that a leg boarding somewhere with no stop at all
# still fails.
TRANSIT_STOP_TOLERANCE_M = 200

# How far a claimed departure may sit from a real one in the timetable. Five
# minutes absorbs rounding and a caller quoting a scheduled time loosely; a
# fabricated service is not out by five minutes, it is absent entirely.
DEPARTURE_TOLERANCE_S = 300

# Every constraint this agent understands, and the type it must arrive as.
# Anything not on this list is dropped by coerce_constraints(). The whitelist
# is the security boundary as much as the schema: constraint sets reach this
# agent from a model's extraction and from other agents, neither of which is
# trusted input.
# Surface values this agent can reason about, and the subset a wheelchair,
# pram or mobility scooter has trouble with. Both lists are the values that
# actually occur in the study area, in the proportions measured when walk-v4
# was built: asphalt 38.2% of edges, concrete 24.0%, paved 4.8%, and the
# unpaved family below about 4.9% between them. 27.2% of edges state no
# surface at all, which is why the check on this constraint reports its own
# coverage rather than claiming a clean route is a proven one.
SURFACE_TYPES = (
    "asphalt", "concrete", "concrete:plates", "paved", "paving_stones", "sett",
    "cobblestone", "wood", "metal", "rubber",
    "unpaved", "ground", "dirt", "earth", "gravel", "fine_gravel", "compacted",
    "grass", "sand", "mud", "pebblestone", "woodchips", "rock",
)
UNPAVED_SURFACES = (
    "unpaved", "ground", "dirt", "earth", "gravel", "fine_gravel", "compacted",
    "grass", "sand", "mud", "pebblestone", "woodchips", "rock",
)

CONSTRAINT_TYPES: dict[str, type] = {
    "max_walking_distance_m": int,
    "max_transfers": int,
    "max_duration_s": int,
    "min_transfer_seconds": int,
    "allowed_modes": list,
    "avoid_highway_types": list,
    "avoid_surfaces": list,
    "arrive_before": str,
    "depart_after": str,
}

# Upper bounds on the numeric constraints. A negative walking limit or a
# thousand-transfer journey is not a preference, it is a malformed value, and
# accepting it would produce a verdict that looks computed but means nothing.
# Which vocabulary each list-valued constraint is checked against.
_LIST_VALUES = {
    "allowed_modes": MODES,
    "avoid_highway_types": HIGHWAY_TYPES,
    "avoid_surfaces": SURFACE_TYPES,
}

_NUMERIC_LIMITS = {
    "max_walking_distance_m": (0, 100_000),
    "max_transfers": (0, 20),
    "max_duration_s": (0, 86_400),
    "min_transfer_seconds": (0, 7_200),
}


# Spellings of the same constraint a calling model plausibly emits. Only
# unambiguous ones: "arrive_by" can only mean arrive_before, while "depart_at"
# could mean either a departure time or a lower bound on one, and is therefore
# not here. An alias renames the key and nothing else - the value still faces
# the same type and range checks below, so a wrong guess about what a caller
# meant is dropped rather than acted on.
_CONSTRAINT_ALIASES = {
    "max_walking_distance": "max_walking_distance_m",
    "max_walk_distance_m": "max_walking_distance_m",
    "max_walking_m": "max_walking_distance_m",
    "max_walk_m": "max_walking_distance_m",
    "walking_limit_m": "max_walking_distance_m",
    "max_transfer_count": "max_transfers",
    "max_duration_seconds": "max_duration_s",
    "min_transfer_s": "min_transfer_seconds",
    "modes": "allowed_modes",
    "avoid": "avoid_highway_types",
    "avoid_highways": "avoid_highway_types",
    "avoid_way_types": "avoid_highway_types",
    "arrive_by": "arrive_before",
}


def coerce_constraints(raw: Any) -> tuple[dict, dict]:
    """Turn loose JSON into a constraint set this agent can actually check.

    Returns `(constraints, report)`. Unrecognised keys, wrong types and
    out-of-range values are dropped, never guessed at, and the report says how
    many went and which were kept.

    The report deliberately names only the keys that were ACCEPTED. Rejected
    key names came from the caller, and this response is read by another
    model - echoing caller-supplied text back into it is an indirect prompt
    injection path between agents, and a count carries the same diagnostic
    weight without opening one.
    """
    if not isinstance(raw, dict):
        return {}, {"accepted": [], "dropped_count": 0,
                    "note": "Constraint set was not an object; treated as no constraints."}

    clean: dict[str, Any] = {}
    dropped = 0

    renamed = 0
    for key, value in raw.items():
        canonical = key if key in CONSTRAINT_TYPES else _CONSTRAINT_ALIASES.get(key)
        if canonical is None:
            dropped += 1
            continue
        renamed += canonical != key
        key, expected = canonical, CONSTRAINT_TYPES[canonical]

        if expected is int:
            # bool is an int subclass in Python; True as a distance is a bug.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                dropped += 1
                continue
            number = int(value)
            low, high = _NUMERIC_LIMITS[key]
            if not low <= number <= high:
                dropped += 1
                continue
            clean[key] = number

        elif expected is list:
            if not isinstance(value, list):
                dropped += 1
                continue
            permitted = _LIST_VALUES[key]
            kept = [v for v in value if isinstance(v, str) and v.lower() in permitted]
            if not kept:
                dropped += 1
                continue
            clean[key] = sorted({v.lower() for v in kept})

        elif expected is str:
            if not isinstance(value, str) or parse_clock(value) is None:
                dropped += 1
                continue
            clean[key] = value.strip()

    return clean, {
        "accepted": sorted(clean),
        "dropped_count": dropped,
        "renamed_count": renamed,
        "note": ("Unrecognised or out-of-range constraints were ignored rather than guessed at."
                 if dropped else "Every supplied constraint was recognised."),
    }


def parse_clock(value: str) -> tuple[str, Any] | None:
    """Read a time constraint as either a time of day or a full instant.

    People say "before 9am", which pins a time of day and no date; a machine
    passing a computed deadline says "2026-08-26T09:00:00+10:00", which pins an
    instant. Both are legitimate, they compare differently, and collapsing them
    into one would silently invent a date the caller never gave.

    Returns ("time", seconds_since_midnight) or ("instant", datetime), or None
    if the string is neither.
    """
    text = (value or "").strip()
    if not text:
        return None

    # Full ISO 8601. Python 3.10's fromisoformat does not accept a trailing Z.
    try:
        return "instant", datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass

    # Bare time of day, "09:00" or "09:00:00".
    parts = text.split(":")
    if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        if 0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60:
            return "time", hour * 3600 + minute * 60 + second
    return None


def read_stamp(value: Any) -> datetime | None:
    """A segment's timestamp as an aware datetime, or None if unusable."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    # A timestamp with no offset is assumed local, because the conventions
    # require an offset and the only sensible reading of a feed that omits one
    # in this region is Brisbane time.
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=BRISBANE_TZ)


def read_clock(value: Any, on: date) -> datetime | None:
    """A time of day with no date ("08:12", "8:12am", "3pm") read on `on`, or None.

    Callers quote services the way people do - "the 740 at 8:12" - and
    read_stamp, rightly, refuses that: a departure is only checkable on a date.
    The caller (the validate_route tool) chooses the date and says which it
    chose; this only reads the clock, with the same parse_clock the time
    constraints use. Anything already a full timestamp, or not a time of day
    at all, is None here and left to read_stamp.
    """
    if not isinstance(value, str) or read_stamp(value) is not None:
        return None
    text = value.strip().lower().replace(".", "")
    meridiem = next((m for m in ("am", "pm") if text.endswith(m)), None)
    if meridiem:
        text = text[:-2].strip()
        text = text + ":00" if text.isdigit() else text
    parsed = parse_clock(text)
    if parsed is None or parsed[0] != "time":
        return None
    secs = parsed[1]
    if meridiem:
        if not 1 <= secs // 3600 <= 12:
            return None
        secs = secs % (12 * 3600) + (12 * 3600 if meridiem == "pm" else 0)
    return datetime(on.year, on.month, on.day, tzinfo=BRISBANE_TZ) + timedelta(seconds=secs)


# ---------------------------------------------------------------------------
# 2. Routing
# ---------------------------------------------------------------------------

# Spatial index cell size in degrees. ~0.005 deg is roughly 500 m here, which
# puts a few dozen nodes in a cell - small enough that a lookup scans a handful
# of cells instead of all 1,428,640 nodes.
_GRID_CELL_DEG = GRID_CELL_DEG     # defined with the graph, which builds the grid

# Metres per degree on the 6,371 km sphere haversine_m uses, for the flat local
# projection nearest_node compares candidates on.
_METRES_PER_DEGREE = 6_371_000.0 * math.pi / 180.0

def spatial_index(graph) -> dict:
    """The nearest-node grid: (row, column) -> a range of node indices.

    Built with the graph (graph.Graph), so it costs nothing here. Kept as a
    function because the agent's startup and measure_surface_impact.py call it
    to make sure the grid is ready before the first lookup.
    """
    return as_graph(graph).cells


def nearest_node(graph, lat: float, lon: float, max_rings: int = 8) -> tuple[int | None, int]:
    """The closest graph node to a point, and the snap distance in metres.

    Searches the point's own grid cell, then rings of cells outward, and stops
    only when the best candidate is closer than the nearest edge of the region
    already searched - the point at which nothing unsearched can beat it. The
    earlier rule, "one more ring after the first hit", could miss a nearer node
    two rings out where the network is sparse.

    Cheap as well as exact, because validate() calls this twice per walking
    segment and it was 97% of a route tool call: a node at distance zero - the
    usual case, since a planned segment starts and ends on nodes - ends the
    search at once, and candidates are compared by squared distance on a local
    flat projection, with the exact great-circle distance computed only for
    the winner. At snapping distances the two orderings agree.

    Returns a node index into the graph (graph.Graph), or None.
    """
    g = as_graph(graph)
    lats, lons, cell_nodes = g.lat, g.lon, g.cell_nodes
    cy, cx = math.floor(lat / _GRID_CELL_DEG), math.floor(lon / _GRID_CELL_DEG)
    ky = _METRES_PER_DEGREE
    kx = ky * math.cos(math.radians(lat))

    best, best_sq = None, float("inf")
    for ring in range(max_rings + 1):
        for dy in range(-ring, ring + 1):
            for dx in range(-ring, ring + 1):
                # Only the perimeter of this ring; the interior was searched already.
                if ring and max(abs(dy), abs(dx)) != ring:
                    continue
                for j in g.cell((cy + dy, cx + dx)):
                    i = cell_nodes[j]
                    north = (lats[i] - lat) * ky
                    east = (lons[i] - lon) * kx
                    sq = north * north + east * east
                    if sq < best_sq:
                        best, best_sq = i, sq
                        if sq == 0.0:
                            return best, 0
        if best is not None:
            # Everything not yet searched lies outside this square of cells, so
            # it is at least as far away as the square's nearest edge.
            margin = min(
                (lat - (cy - ring) * _GRID_CELL_DEG) * ky,
                ((cy + ring + 1) * _GRID_CELL_DEG - lat) * ky,
                (lon - (cx - ring) * _GRID_CELL_DEG) * kx,
                ((cx + ring + 1) * _GRID_CELL_DEG - lon) * kx,
            )
            if best_sq <= margin * margin:
                break

    if best is None:
        return None, 0
    return best, haversine_m(lat, lon, lats[best], lons[best])


# How many nodes a probe visits before deciding an end is on the main network.
# Every fragment found so far is far smaller - the North Ipswich point a router
# once matched for "Powerhouse Brisbane" sits on one of 32 nodes - and the
# probe costs ~20 ms at the cap, against Dijkstra's ~5 s to exhaust the
# 1.4-million-node main network before it can say "not connected".
FRAGMENT_PROBE_NODES = 10_000


def _cut_off(graph, start: int, goal: int) -> bool:
    """True when `start` and `goal` provably lie on different parts of the network.

    Breadth-first from each end in turn, stopping when it meets the other end
    (connected, as far as this can tell), runs out of nodes (a fragment that
    does not contain the other end: proven cut off), or reaches the cap
    (inconclusive - the caller searches as before). The search from the goal
    relies on the walking graph storing every way in both directions, which
    holds for the pinned graph (no one-way edge in a 20,000-node sample) and
    is pinned by a test; the search from the start needs no such assumption.
    """
    g = as_graph(graph)
    offs, tgt = g.offs, g.tgt
    for here, there in ((goal, start), (start, goal)):
        seen, queue = {here}, deque([here])
        while queue and len(seen) < FRAGMENT_PROBE_NODES:
            node = queue.popleft()
            for k in range(offs[node], offs[node + 1]):
                neighbour = tgt[k]
                if neighbour == there:
                    return False
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        if not queue:
            return True
    return False


def shortest_path(graph, start: int, goal: int,
                  blocked_highways: frozenset[str] = frozenset(),
                  blocked_surfaces: frozenset[str] = frozenset()) -> tuple[list[int], int] | None:
    """Dijkstra's algorithm over the road/footpath graph, minimising metres.

    `blocked_highways` removes way types from consideration entirely - the
    mechanism behind an accessibility constraint such as "no steps", and the
    lever the re-planning loop pulls when a verdict says the route used
    something it should not have. `blocked_surfaces` does the same for surface
    types, for "keep me on sealed paths". A way whose surface OpenStreetMap
    does not state is NOT excluded: excluding the unknown would refuse most of
    the network on a suspicion, and the verdict says what share of the route
    could actually be checked.

    Returns (ordered node indices, total metres), or None when no path exists
    under those restrictions. None is a real answer: it can mean the network
    genuinely does not connect the two points, or that the restrictions made
    an otherwise-reachable destination unreachable. `plan_route` distinguishes
    the two by trying again without restrictions.

    Ties between equally distant nodes are broken by node index, which orders
    as the OpenStreetMap ids did as strings (graph.py), so a route is the same
    one the dictionary graph found, node for node.
    """
    g = as_graph(graph)
    # An end on a disconnected fragment would otherwise cost a search of the
    # whole main network - per variant, in compare_route_options - to learn
    # nothing: on 3 Oct that took the tool past its 40 s budget.
    if start != goal and _cut_off(g, start, goal):
        return None
    # Resolve the restrictions to integer codes once, so the inner loop is an
    # integer membership test three million times over rather than a lookup.
    blocked_hw = frozenset(c for c, name in enumerate(g.highways) if name in blocked_highways)
    blocked_attrs = frozenset(
        ix for ix, attrs in enumerate(g.way_attrs or ())
        if attrs.get("surface") in blocked_surfaces
    ) if blocked_surfaces else frozenset()

    offs, tgt, wt, hw, at = g.offs, g.tgt, g.wt, g.hw, g.at
    dist: dict[int, float] = {start: 0.0}
    prev: dict[int, int] = {}
    queue: list[tuple[float, int]] = [(0.0, start)]
    push, pop, inf, known = heapq.heappush, heapq.heappop, float("inf"), dist.get
    restricted = bool(blocked_hw or blocked_attrs)

    while queue:
        d, node = pop(queue)
        # A node is pushed again only with a strictly shorter distance, so an
        # entry longer than the node's best is stale. That is the visited-set
        # test without the set: the first pop of a node is its final distance.
        if d > dist[node]:
            continue
        if node == goal:
            break
        if restricted:
            for k in range(offs[node], offs[node + 1]):
                if hw[k] in blocked_hw or at[k] in blocked_attrs:
                    continue
                neighbour = tgt[k]
                nd = d + wt[k]
                if nd < known(neighbour, inf):
                    dist[neighbour] = nd
                    prev[neighbour] = node
                    push(queue, (nd, neighbour))
        else:
            for k in range(offs[node], offs[node + 1]):
                neighbour = tgt[k]
                nd = d + wt[k]
                if nd < known(neighbour, inf):
                    dist[neighbour] = nd
                    prev[neighbour] = node
                    push(queue, (nd, neighbour))

    if goal not in dist:
        return None

    path = [goal]
    while path[-1] != start:
        path.append(prev[path[-1]])
    path.reverse()
    return path, metres(dist[goal])


def _edge_highway(graph, a: int, b: int) -> str:
    g = as_graph(graph)
    k = g.first_edge(a, b)
    return g.highway(k) if k is not None else "unclassified"


def edge_attrs(graph, a: int, b: int) -> dict:
    """The accessibility tags on the way carrying the edge a->b, or {}.

    Edges built before walk-v4 carry no attribute index, and the hand-built
    graphs in the tests carry none either, so a missing one is a normal state
    and not a fault.
    """
    g = as_graph(graph)
    k = g.first_edge(a, b)
    return g.attrs(k) if k is not None else {}


def path_to_segments(graph, path: list[int], depart: datetime | None = None) -> list[dict]:
    """Turn a node path into walking segments, one per run of the same way type.

    A 5 km path is ~280 graph edges. Returning 280 segments would be true and
    useless - the caller wants "1.2 km along footways, then 300 m of steps",
    which is also the granularity an `avoid_highway_types` constraint is
    written at. Consecutive edges sharing a way type AND a surface are
    therefore merged: surface is the granularity the other accessibility
    constraint is written at, and a run that changes from concrete to gravel
    is exactly the transition a traveller asked about.
    """
    if not path:
        return []
    g = as_graph(graph)
    lats, lons = g.lat, g.lon
    if len(path) == 1:
        # Origin and destination on the same node - "South Bank to South Bank".
        # One walk of nothing, not no walk: every check downstream reads the
        # route's first and last segment, and an empty route crashed the tool.
        here = {"lat": lats[path[0]], "lon": lons[path[0]]}
        segment = {"mode": "walk", "highway_type": None, "surface": None,
                   "from": dict(here), "to": dict(here), "distance_m": 0, "duration_s": 0}
        if depart is not None:
            segment["depart"] = segment["arrive"] = depart.isoformat(timespec="seconds")
        return [segment]

    segments: list[dict] = []
    run_start = 0
    clock = depart

    def flush(start_index: int, end_index: int, kind: tuple[str, str | None]) -> None:
        highway, surface = kind
        nonlocal clock
        distance = sum(
            haversine_m(lats[path[i]], lons[path[i]],
                        lats[path[i + 1]], lons[path[i + 1]])
            for i in range(start_index, end_index)
        )
        duration = seconds(distance / WALKING_SPEED_MPS)
        segment = {
            "mode": "walk",
            "highway_type": highway,
            # None where OpenStreetMap does not say. Absent and "unknown" are
            # different facts, and the verdict below reports the difference.
            "surface": surface,
            "from": {"lat": lats[path[start_index]], "lon": lons[path[start_index]]},
            "to": {"lat": lats[path[end_index]], "lon": lons[path[end_index]]},
            "distance_m": metres(distance),
            "duration_s": duration,
        }
        if clock is not None:
            arrive = clock.fromtimestamp(clock.timestamp() + duration, tz=clock.tzinfo)
            segment["depart"] = clock.isoformat(timespec="seconds")
            segment["arrive"] = arrive.isoformat(timespec="seconds")
            clock = arrive
        segments.append(segment)

    def kind_of(a: str, b: str) -> tuple[str, str | None]:
        return _edge_highway(g, a, b), edge_attrs(g, a, b).get("surface")

    current = kind_of(path[0], path[1])
    for i in range(1, len(path) - 1):
        kind = kind_of(path[i], path[i + 1])
        if kind != current:
            flush(run_start, i, current)
            run_start, current = i, kind
    flush(run_start, len(path) - 1, current)
    return segments


def plan_route(graph: dict, origin: dict, destination: dict, constraints: dict,
               depart: datetime | None = None) -> dict:
    """Plan a walking route honouring what the planner can honour up front.

    Mode and way-type constraints are things a planner can respect while
    planning. Distance and duration limits are not - the shortest path is
    already the shortest, so if it breaches a walking limit no re-plan will
    fix it, and saying so is the correct answer rather than a failure. The
    verifier checks all of them again independently either way; nothing here
    is trusted downstream.
    """
    blocked = frozenset(constraints.get("avoid_highway_types", ()))
    blocked_surfaces = frozenset(constraints.get("avoid_surfaces", ()))

    start_id, start_snap = nearest_node(graph, origin["lat"], origin["lon"])
    goal_id, goal_snap = nearest_node(graph, destination["lat"], destination["lon"])
    if start_id is None or goal_id is None:
        return {"found": False, "reason": "no_network_nearby",
                "detail": "No mapped road or footpath lies near one of the two points."}

    found = shortest_path(graph, start_id, goal_id, blocked, blocked_surfaces)
    if found is None:
        # Separate "the restrictions made this unreachable" from "these points
        # are not connected at all". The caller can act on the first and not
        # on the second, so a single "no route" would be the less useful answer.
        if (blocked or blocked_surfaces) and shortest_path(graph, start_id, goal_id) is not None:
            return {"found": False, "reason": "blocked_by_constraints",
                    "detail": ("A route exists, but not one avoiding every excluded way type "
                               "and surface."),
                    "blocked_highway_types": sorted(blocked),
                    "blocked_surfaces": sorted(blocked_surfaces)}
        return {"found": False, "reason": "not_connected",
                "detail": ("These points are not connected on the loaded network. They may sit on "
                           "fragments OpenStreetMap has not mapped as joined, rather than there "
                           "being no real-world way through.")}

    path, total_m = found
    return {
        "found": True,
        "segments": path_to_segments(graph, path, depart),
        "distance_m": total_m,
        "origin_snap_m": start_snap,
        "destination_snap_m": goal_snap,
        "blocked_highway_types": sorted(blocked),
        "blocked_surfaces": sorted(blocked_surfaces),
    }


# ---------------------------------------------------------------------------
# Route options: the trade-off, measured.
#
# `plan_route` answers "here is the route, and here is what it violates".
# That is a verdict, and on a request that cannot be met it is where the
# conversation stops. A traveller who cannot manage stairs does not want to
# hear that the route has stairs; they want to know what the way round costs
# them. Enumerating the plannable alternatives and measuring each one is the
# difference between checking an answer and supporting a decision.
#
# Every option is built by the same planner and judged by the same verifier as
# any other route here, against the constraints the traveller actually stated.
# Nothing is ranked by opinion: the order is "satisfies everything" first, then
# fewest violations, then shortest.
# ---------------------------------------------------------------------------

# The accessibility restrictions worth offering as alternatives, and the words
# for them a person would recognise. Distance and time limits are deliberately
# absent: they cannot be planned around - the shortest path is already the
# shortest - so an option that "relaxes" them would be the same route again.
OPTION_VARIANTS = (
    ("shortest", "the shortest walk, ignoring every stated preference", (), ()),
    ("step_free", "avoiding steps", ("steps",), ()),
    ("sealed", "avoiding unsealed ground", (), UNPAVED_SURFACES),
    ("step_free_sealed", "avoiding both steps and unsealed ground", ("steps",), UNPAVED_SURFACES),
)


def path_accessibility(graph, path: list[int]) -> dict:
    """What a traveller with a mobility need needs to know about a path.

    Read off the edges rather than the merged segments, because the handrail
    and step-count tags live on the way and are lost in the merge - and "there
    is a flight of stairs, and it has a handrail" is a different fact from
    "there is a flight of stairs".
    """
    g = as_graph(graph)
    lats, lons = g.lat, g.lon
    unpaved = set(UNPAVED_SURFACES)
    facts = {"distance_m": 0.0, "steps_m": 0.0, "steps_flights": 0,
             "steps_with_handrail": 0, "unpaved_m": 0.0, "surface_stated_m": 0.0,
             "lowered_kerb_crossings": 0, "kerb_crossings": 0}
    previous_was_steps = False
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        metres = haversine_m(lats[a], lons[a], lats[b], lons[b])
        attrs = edge_attrs(g, a, b)
        highway = _edge_highway(g, a, b)
        facts["distance_m"] += metres
        if attrs.get("surface"):
            facts["surface_stated_m"] += metres
            if attrs["surface"] in unpaved:
                facts["unpaved_m"] += metres
        if highway == "steps":
            facts["steps_m"] += metres
            if not previous_was_steps:
                facts["steps_flights"] += 1
                if attrs.get("handrail") in ("yes", "both", "left", "right"):
                    facts["steps_with_handrail"] += 1
            previous_was_steps = True
        else:
            previous_was_steps = False
        # Kerbs sit on the nodes, and a crossing with a raised kerb is where a
        # wheelchair journey actually ends. Counted, not judged: about three
        # quarters of this network's crossings state a kerb at all.
        kerb = g.tags(b).get("kerb")
        if kerb:
            facts["kerb_crossings"] += 1
            if kerb in ("lowered", "flush"):
                facts["lowered_kerb_crossings"] += 1

    total = facts["distance_m"] or 1
    return {
        "distance_m": metres_of(facts["distance_m"]),
        "steps_m": metres_of(facts["steps_m"]),
        "steps_flights": facts["steps_flights"],
        "steps_flights_with_handrail": facts["steps_with_handrail"],
        "unpaved_m": metres_of(facts["unpaved_m"]),
        "surface_stated_share": round(facts["surface_stated_m"] / total * 100),
        "kerb_crossings": facts["kerb_crossings"],
        "lowered_kerb_crossings": facts["lowered_kerb_crossings"],
    }


def metres_of(value: float) -> int:
    return int(round(value))


def route_options(graph: dict, origin: dict, destination: dict, constraints: dict,
                  depart: datetime | None = None,
                  endpoint_tolerance_m: int = SNAP_TOLERANCE_M) -> dict:
    """Plannable alternatives between two points, each measured and judged.

    Returns `{"options": [...], "recommended": <id or None>, "reason": str,
    "satisfiable": bool}`. Each option carries what it costs against the
    shortest walk, what it does to the ground underfoot, and the verdict the
    verifier gives it under the traveller's own constraints. Options that come
    out as the same route are merged, so "step-free" and "shortest" appear once
    when the shortest walk already has no steps - a distinction with no
    difference is not an option.
    """
    start_id, start_snap = nearest_node(graph, origin["lat"], origin["lon"])
    goal_id, goal_snap = nearest_node(graph, destination["lat"], destination["lon"])
    if start_id is None or goal_id is None:
        return {"options": [], "recommended": None, "satisfiable": False,
                "reason": "No mapped road or footpath lies near one of the two points."}

    stated_ways = tuple(constraints.get("avoid_highway_types", ()))
    stated_surfaces = tuple(constraints.get("avoid_surfaces", ()))
    variants = list(OPTION_VARIANTS)
    if (stated_ways or stated_surfaces) and not any(
            set(ways) == set(stated_ways) and set(surfaces) == set(stated_surfaces)
            for _id, _label, ways, surfaces in variants):
        variants.append(("as_requested", "honouring exactly what was asked for",
                         stated_ways, stated_surfaces))

    found_by_id: dict[str, dict] = {}
    seen_routes: dict[tuple, str] = {}
    shortest_m: int | None = None

    for option_id, label, ways, surfaces in variants:
        found = shortest_path(graph, start_id, goal_id, frozenset(ways), frozenset(surfaces))
        if found is None:
            found_by_id[option_id] = {
                "option": option_id, "description": label, "possible": False,
                "reason": "No route exists between these points under that restriction.",
            }
            continue
        path, total_m = found
        if option_id == "shortest":
            shortest_m = total_m

        fingerprint = (total_m, len(path), path[len(path) // 2])
        if fingerprint in seen_routes:
            same_as = seen_routes[fingerprint]
            found_by_id[same_as].setdefault("also_satisfies", []).append(label)
            continue
        seen_routes[fingerprint] = option_id

        segments = path_to_segments(graph, path, depart)
        verdict = validate(segments, constraints, graph, expected_origin=origin,
                           expected_destination=destination,
                           endpoint_tolerance_m=endpoint_tolerance_m)
        found_by_id[option_id] = {
            "option": option_id,
            "description": label,
            "possible": True,
            "distance_m": total_m,
            "duration_s": sum(int(s.get("duration_s") or 0) for s in segments),
            "extra_m_vs_shortest": None,
            "satisfies_everything_stated": verdict["valid"],
            "violations": [v["check"] for v in verdict["violations"]],
            "accessibility": path_accessibility(graph, path),
            "segments": segments,
        }

    for option in found_by_id.values():
        if option.get("possible") and shortest_m is not None:
            option["extra_m_vs_shortest"] = option["distance_m"] - shortest_m

    # Satisfying everything comes first, then fewest violations, then shortest.
    # No weighting, no scoring: a preference order this agent invented would be
    # exactly the unjustified design choice the project argues against.
    usable = [o for o in found_by_id.values() if o.get("possible")]
    usable.sort(key=lambda o: (not o["satisfies_everything_stated"],
                               len(o["violations"]), o["distance_m"]))
    best = usable[0] if usable else None
    satisfiable = bool(best and best["satisfies_everything_stated"])

    if best is None:
        reason = ("No route exists between these points at all, under any restriction "
                  "or none.")
    elif satisfiable:
        reason = (f"{best['description'].capitalize()} meets everything that was stated"
                  + (f", at {best['extra_m_vs_shortest']} m more than the shortest walk."
                     if best["extra_m_vs_shortest"] else ", and it is also the shortest walk."))
    else:
        reason = ("Nothing satisfies every stated requirement. The options are listed with "
                  "what each one gives up, measured, so the traveller can choose rather "
                  "than be told no.")

    return {
        "options": usable + [o for o in found_by_id.values() if not o.get("possible")],
        "recommended": best["option"] if best else None,
        "satisfiable": satisfiable,
        "reason": reason,
        "origin_snap_m": start_snap,
        "destination_snap_m": goal_snap,
    }


# ---------------------------------------------------------------------------
# 3. Verification
# ---------------------------------------------------------------------------

def summarise(verdict: dict) -> str:
    """One line stating the verdict, built from the checks rather than written.

    A machine-readable verdict is the right thing to return and the wrong thing
    to be the ONLY thing returned: anything reading this response - the
    Orchestrator's model, a console, a person - has to reconstruct what happened
    from a list of check records before it can act. The sentence below is
    assembled from those same records in code, so it carries no claim the checks
    did not already make, and no model is asked to phrase it.

    Violations are named by constraint and quantified. Nothing from the caller's
    own input is repeated back, for the same reason the rest of this module
    avoids it.
    """
    # A pass that left a transit leg unchecked is not the same as a pass that
    # checked it, and a one-line summary is all some callers read.
    unchecked = next((len(c["observed"]["not_checked"]) for c in verdict["checks"]
                      if c["check"] == "transit_services_exist"
                      and isinstance(c["observed"], dict) and c["observed"].get("not_checked")), 0)
    caveat = (f" {unchecked} transit leg(s) had no readable departure time, so the timetable "
              "was NOT checked for them." if unchecked else "")
    if verdict["valid"]:
        n = verdict["constraints_checked"]
        return ("Route is valid: every network check passed"
                + (f" and all {n} stated constraint(s) are satisfied." if n
                   else ", and no constraints were stated to check.") + caveat)

    structural = [v for v in verdict["violations"] if v["kind"] == "structural"]
    constraint = [v for v in verdict["violations"] if v["kind"] == "constraint"]
    parts = []
    for v in constraint:
        parts.append(f"{v['check']} is {v['observed']} against a limit of {v['threshold']}")
    for v in structural:
        parts.append(f"{v['check']} failed")
    return ("Route REJECTED: " + "; ".join(parts) + "."
            + f" Constraint satisfaction {verdict['constraint_satisfaction_score']:.2f}." + caveat)


def nearest_stop(table: dict, lat: float, lon: float) -> tuple[str | None, int]:
    """The closest GTFS stop to a point, and the distance in metres.

    Linear over the stops inside the study area - about five thousand of them,
    which is a few milliseconds. The road graph needed a spatial index at
    1,428,640 nodes; this does not.
    """
    best_id, best = None, float("inf")
    for stop_id, s in table["stops"].items():
        d = haversine_m(lat, lon, s["lat"], s["lon"])
        if d < best:
            best_id, best = stop_id, d
    return best_id, metres(best) if best_id else 0


def service_runs_on(table: dict, service_ix: int, when: datetime) -> bool:
    """Whether a GTFS service operates on the given date.

    Two conditions, both of which a fabricated journey tends to miss: the
    service's weekday pattern must include that day, and the date must fall
    inside the feed's own validity window. A timetable is not a statement about
    all time - this archive declares which dates it describes, and a claim
    outside that range is unverifiable rather than true.
    """
    pattern = table["service_days"].get(str(service_ix))
    if not pattern or len(pattern) != 7:
        return False
    if pattern[when.weekday()] != "1":
        return False
    stamp = when.strftime("%Y%m%d")
    feed = table.get("feed") or {}
    if feed.get("start") and stamp < feed["start"]:
        return False
    if feed.get("end") and stamp > feed["end"]:
        return False
    return True


def find_departure(table: dict, stop_id: str, when: datetime,
                   route_hint: str = "") -> dict | None:
    """A scheduled departure from `stop_id` near `when`, or None if none exists.

    Returns the closest match within DEPARTURE_TOLERANCE_S that also runs on
    that date. `route_hint` matches either the GTFS route_id or the short name
    a passenger would say ("412"), because a route arriving from another agent
    could reasonably be either.

    None is the interesting answer. It means the timetable has no service
    leaving that stop at that time - which is what a confidently invented
    connection looks like from the outside.
    """
    entries = table["departures"].get(stop_id)
    if not entries:
        return None

    target = when.hour * 3600 + when.minute * 60 + when.second
    hint = (route_hint or "").strip().lower()
    best = None
    # A service day runs past midnight, so a 00:30 claim may match a departure
    # listed at 24:30 on the previous service day. Both framings are checked.
    for base in (target, target + 86_400):
        # `Departures` exposes its times directly; a hand-built table is a
        # plain list of rows, so take the column from it the slow way. Either
        # way the search is over times alone, which is what the -1 sentinels
        # in the old row comparison were standing in for.
        times = getattr(entries, "times", None)
        if times is None:
            times = [row[0] for row in entries]
        lo = bisect.bisect_left(times, base - DEPARTURE_TOLERANCE_S)
        for i in range(lo, len(entries)):
            secs, route_ix, service_ix = entries[i]
            if secs > base + DEPARTURE_TOLERANCE_S:
                break
            if hint and hint not in (table["routes"][route_ix].lower(),
                                     (table["route_names"][route_ix] or "").lower()):
                continue
            if not service_runs_on(table, service_ix, when):
                continue
            gap = abs(secs - base)
            if best is None or gap < best["off_by_s"]:
                best = {"off_by_s": gap,
                        "scheduled": f"{secs // 3600 % 24:02d}:{secs % 3600 // 60:02d}",
                        "route": table["route_names"][route_ix] or table["routes"][route_ix],
                        "route_id": table["routes"][route_ix]}
    return best


def _record(check: str, kind: str, satisfied: bool, observed: Any,
            threshold: Any, evidence: str) -> dict:
    return {"check": check, "kind": kind, "satisfied": satisfied,
            "observed": observed, "threshold": threshold, "evidence": evidence}


# Routes arrive from other agents and from models, and they do not all spell a
# route the same way. At the Showcase the Orchestrator gets one call per agent
# with no retry, so a route rejected for saying "origin" where this agent says
# "from" is a call spent on nothing. Key spellings are therefore read
# liberally. Values never are: a coordinate that is not a coordinate, or a mode
# this agent cannot reason about, is still an error, because guessing there
# would put a made-up number into a verdict.
_SEGMENT_KEYS = {
    "from": ("from", "origin", "start", "from_point", "start_point"),
    "to": ("to", "destination", "dest", "end", "to_point", "end_point"),
    "mode": ("mode", "travel_mode", "transport_mode", "type"),
    "distance_m": ("distance_m", "distance", "length_m", "metres", "meters"),
    # Not a bare "duration": its unit is a guess, and ten minutes read as ten
    # seconds would turn a true claim into an impossible one.
    "duration_s": ("duration_s", "duration_seconds"),
    "depart": ("depart", "departure", "depart_time", "departure_time"),
    "arrive": ("arrive", "arrival", "arrive_time", "arrival_time"),
    "route_id": ("route_id", "route", "service_id", "line"),
}
_SEGMENT_ALIASES = {name for names in _SEGMENT_KEYS.values() for name in names}


def _pick(segment: dict, canonical: str) -> Any:
    """The first spelling of `canonical` the caller actually used."""
    for name in _SEGMENT_KEYS[canonical]:
        if name in segment:
            return segment[name]
    return None


def read_point(value: Any) -> dict | None:
    """A coordinate pair in the shapes callers actually send, or None.

    Accepts {"lat", "lon"}, {"latitude", "longitude"}, [lat, lon] and
    "lat, lon". A pair is read in this agent's own order, [lat, lon], unless
    the first number cannot be a latitude and the second can - that is the
    GeoJSON [lon, lat] order, and the two are unambiguous anywhere this agent
    operates, where longitude is about 153 and latitude about -27. Anything
    still out of range comes back as None rather than clamped into range.
    """
    if isinstance(value, dict):
        lat = value.get("lat", value.get("latitude"))
        lon = value.get("lon", value.get("lng", value.get("longitude")))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        lat, lon = value
    elif isinstance(value, str) and value.count(",") == 1:
        lat, lon = value.split(",")
    else:
        return None
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if abs(lat) > 90 >= abs(lon):
        lat, lon = lon, lat
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"lat": lat, "lon": lon}


def read_segments(raw: Any) -> tuple[list[dict], str | None]:
    """Validate the shape of an incoming route. Returns (segments, error).

    Spelling is read liberally (`_SEGMENT_KEYS`, `read_point`) and normalised
    to this agent's own key names; every other key the caller sent is carried
    through untouched. The error names the field and the position, never the
    value: positions are ours, values are the caller's.
    """
    if not isinstance(raw, list) or not raw:
        return [], "Route must be a non-empty list of segments."

    clean: list[dict] = []
    for i, segment in enumerate(raw):
        if not isinstance(segment, dict):
            return [], f"Segment {i} is not an object."

        normalised = {k: v for k, v in segment.items() if k not in _SEGMENT_ALIASES}
        for end in ("from", "to"):
            point = read_point(_pick(segment, end))
            if point is None:
                return [], (f"Segment {i} needs a '{end}' point: an object with numeric "
                            f"'lat' and 'lon', or a [lat, lon] pair.")
            normalised[end] = point

        mode = _pick(segment, "mode") or "walk"
        if not isinstance(mode, str) or mode.lower() not in MODES:
            return [], (f"Segment {i} has an unrecognised mode. "
                        f"Recognised modes: {', '.join(MODES)}.")
        normalised["mode"] = mode.lower()

        distance = _pick(segment, "distance_m")
        if distance is not None:
            # A claimed distance is evidence the verifier weighs against the
            # network, so it has to be a number - but "626" from a model that
            # quoted it is the same claim as 626, and refusing the route over
            # the quotes would lose the check altogether.
            if isinstance(distance, bool):
                return [], f"Segment {i}'s distance is not a number."
            try:
                normalised["distance_m"] = float(distance)
            except (TypeError, ValueError):
                return [], f"Segment {i}'s distance is not a number."

        duration = _pick(segment, "duration_s")
        if duration is not None:
            # A claimed time is checked against the distance, so it has to be
            # a number, read as leniently as distance_m is.
            if isinstance(duration, bool):
                return [], f"Segment {i}'s duration is not a number."
            try:
                normalised["duration_s"] = float(duration)
            except (TypeError, ValueError):
                return [], f"Segment {i}'s duration is not a number."

        for field in ("depart", "arrive", "route_id"):
            value = _pick(segment, field)
            if value is not None:
                normalised[field] = value

        clean.append(normalised)
    return clean, None


_TIME_KEYS = frozenset(_SEGMENT_KEYS["depart"]) | frozenset(_SEGMENT_KEYS["arrive"])


def date_clock_times(segments: Any, today: date) -> tuple[Any, list[dict]]:
    """Times of day with no date ("03:00", "8:12am") put on a date. Returns (segments, notes).

    A service only runs or not on a date, and read_stamp rightly refuses a bare
    clock time - so until 22 Oct 2026 "the 412 at 03:00" left the timetable
    check with nothing to read, and the leg passed unchecked. The date is the
    one the route already states (its first full timestamp) or, failing that,
    `today`, and each reading is reported so the caller can see the assumption.
    Full timestamps, and anything that is not a time of day, pass through
    untouched for read_segments to judge. The caller's segments are not mutated.

    Used by the validate_route tool only. The experiment's producers are told to
    write full ISO 8601 timestamps and go through read_segments directly, so the
    frozen 3 Oct figures do not depend on it.
    """
    if not isinstance(segments, list):
        return segments, []
    stamps = [read_stamp(seg.get(key)) for seg in segments if isinstance(seg, dict)
              for key in sorted(_TIME_KEYS) if key in seg]
    stated = next((s for s in stamps if s is not None), None)
    on = stated.astimezone(BRISBANE_TZ).date() if stated else today
    date_from = "the date the route states" if stated else "today in Brisbane, as no date was given"
    dated, notes = [], []
    for i, seg in enumerate(segments):
        if isinstance(seg, dict):
            seg = dict(seg)
            for key in sorted(_TIME_KEYS & seg.keys()):
                when = read_clock(seg[key], on)
                if when is not None:
                    seg[key] = when.isoformat(timespec="seconds")
                    notes.append({"segment": i, "field": key, "read_as": seg[key],
                                  "date": on.isoformat(), "date_from": date_from})
        dated.append(seg)
    return dated, notes


def transit_legs(segments: list[dict]) -> list[list[dict]]:
    """Group segments into transit legs, so transfers can be counted.

    A leg is a maximal run of consecutive non-walking segments on the same
    service. `route_id` identifies the service when it is supplied - two
    consecutive bus segments on the same route are one leg, not two - and the
    mode stands in for it when it is not. Transfers are then legs minus one:
    boarding the first vehicle is not a transfer.
    """
    legs: list[list[dict]] = []
    for segment in segments:
        if segment["mode"] == "walk":
            continue
        key = segment.get("route_id") or segment["mode"]
        if legs and (legs[-1][-1].get("route_id") or legs[-1][-1]["mode"]) == key:
            legs[-1].append(segment)
        else:
            legs.append([segment])
    return legs


def validate(segments: list[dict], constraints: dict, graph: dict | None = None, *,
             expected_origin: dict | None = None,
             expected_destination: dict | None = None,
             coverage: dict | None = None,
             transit_coverage: dict | None = None,
             table: dict | None = None,
             endpoint_tolerance_m: int = SNAP_TOLERANCE_M) -> dict:
    """Check a route against the network and against a constraint set.

    Two kinds of check, kept apart because they answer different questions and
    the project reports them separately:

      * structural - is this a route at all? Do its segments join up, do they
        exist on the network, do its times run forwards?
      * constraint - does it satisfy what the traveller actually asked for?

    A route is `valid` only when every structural check and every constraint
    check passes. `constraint_satisfaction_score` is S(r,C): the fraction of
    constraint checks satisfied, reported alongside the verdict rather than
    instead of it, so a per-class analysis can see which constraint types fail
    and how often instead of only that something did.

    `graph` and `table` are both optional, and both say so when absent. Without
    the graph, walking legs are not checked against the network; without the
    timetable, transit legs are not checked against the schedule. A verdict that
    quietly drops a check it could not run is worse than one that admits to it.
    """
    checks: list[dict] = []

    # -- structural: continuity ------------------------------------------
    gaps = []
    for i in range(len(segments) - 1):
        gap = haversine_m(segments[i]["to"]["lat"], segments[i]["to"]["lon"],
                          segments[i + 1]["from"]["lat"], segments[i + 1]["from"]["lon"])
        if gap > CONTINUITY_TOLERANCE_M:
            gaps.append({"between_segments": [i, i + 1], "gap_m": metres(gap)})
    checks.append(_record(
        "segment_continuity", "structural", not gaps,
        {"discontinuities": gaps} if gaps else 0, CONTINUITY_TOLERANCE_M,
        ("Each segment starts where the previous one ended."
         if not gaps else
         f"{len(gaps)} place(s) where one segment ends more than "
         f"{CONTINUITY_TOLERANCE_M} m from where the next begins."),
    ))

    # -- structural: coverage --------------------------------------------
    if coverage:
        # Two areas, because there are two datasets. A walking leg can only be
        # measured where the graph reaches; a transit leg only needs the
        # timetable, which covers far more ground. Defaults to the old
        # behaviour - one area for everything - when no transit area is given.
        transit_area = transit_coverage or coverage

        def inside(point: dict, area: dict) -> bool:
            return (area["lat_min"] <= point["lat"] <= area["lat_max"]
                    and area["lon_min"] <= point["lon"] <= area["lon_max"])

        unknown, unmeasurable = 0, 0
        for segment in segments:
            area = coverage if segment["mode"] == "walk" else transit_area
            for end in ("from", "to"):
                point = segment[end]
                if inside(point, area):
                    continue
                # Outside everything this agent holds: it cannot say whether a
                # way or a service exists there at all, and that is a failure.
                if not inside(point, transit_area):
                    unknown += 1
                # A walking leg beyond the graph but inside the transit area is
                # not a false claim, only an unmeasured one. Saying so beats
                # failing an itinerary whose transit legs were just verified.
                else:
                    unmeasurable += 1

        observed = unknown if not unmeasurable else {"outside_all_coverage": unknown,
                                                     "walking_legs_not_measurable": unmeasurable}
        if unknown:
            evidence = (f"{unknown} segment endpoint(s) fall outside every area this agent "
                        "holds data for, where it cannot confirm that a way or a service "
                        "exists at all.")
        elif unmeasurable:
            evidence = (f"Every leg is inside the timetable's area, so transit legs were "
                        f"checked; {unmeasurable} walking endpoint(s) lie beyond the road "
                        "graph and could not be measured, which the network check below "
                        "reports as not run rather than as passed.")
        else:
            evidence = "Every segment endpoint lies inside the covered area."
        checks.append(_record("within_coverage", "structural", unknown == 0, observed, 0,
                              evidence))

    # -- structural: walking segments exist on the network ----------------
    #
    # `measured` holds, per walk segment index, the shortest distance the
    # network actually allows between its endpoints. Everything downstream
    # measures against that rather than against the segment's own claim: a
    # route arriving here is a claim about the world, and the entire point of
    # this agent is to check claims rather than restate them.
    measured: dict[int, int] = {}
    if graph is not None:
        off_network, unreachable = [], []
        for i, segment in enumerate(segments):
            if segment["mode"] != "walk":
                continue                       # transit legs run on rails and roads this graph does not model
            start_id, start_snap = nearest_node(graph, segment["from"]["lat"], segment["from"]["lon"])
            goal_id, goal_snap = nearest_node(graph, segment["to"]["lat"], segment["to"]["lon"])
            if start_id is None or goal_id is None or max(start_snap, goal_snap) > SNAP_TOLERANCE_M:
                off_network.append({"segment": i, "snap_m": max(start_snap, goal_snap)})
                continue
            found = shortest_path(graph, start_id, goal_id)
            if found is None:
                unreachable.append({"segment": i})
            else:
                measured[i] = found[1]
        problems = off_network + unreachable
        checks.append(_record(
            "walk_segments_on_network", "structural", not problems,
            {"off_network": off_network, "unreachable": unreachable} if problems else 0,
            SNAP_TOLERANCE_M,
            ("Every walking segment runs between points on the mapped network."
             if not problems else
             f"{len(off_network)} walking segment(s) do not lie on the network and "
             f"{len(unreachable)} connect points with no path between them."),
        ))

        # A segment may claim to be LONGER than the shortest path - a scenic or
        # shadier route is a real thing and not an error. It cannot claim to be
        # shorter: no walk between two points beats the network's own shortest
        # path. An under-claim is therefore provably impossible rather than
        # merely suspicious, and catching it matters because every distance
        # constraint downstream would otherwise be checked against a number the
        # caller made up. A fluent answer is not a valid route.
        understated = [
            {"segment": i, "claimed_m": int(segments[i].get("distance_m") or 0),
             "shortest_possible_m": measured[i]}
            for i in measured
            if segments[i].get("distance_m") is not None
            and int(segments[i]["distance_m"]) < measured[i] - SNAP_TOLERANCE_M
        ]
        checks.append(_record(
            "claimed_distances_possible", "structural", not understated,
            {"understated": understated} if understated else 0, SNAP_TOLERANCE_M,
            ("Every walking segment's stated distance is at least the shortest the network allows."
             if not understated else
             f"{len(understated)} walking segment(s) claim a distance shorter than the network "
             "permits between those points, which no route can achieve. Constraint checks below "
             "use the measured distance, not the claimed one."),
        ))
    else:
        for label in ("walk_segments_on_network", "claimed_distances_possible"):
            checks.append(_record(
                label, "structural", True, "skipped", None,
                "No network graph was supplied, so this was not verified. "
                "The check did not pass; it did not run.",
            ))

    # -- structural: claimed walking times are possible --------------------
    #
    # The twin of claimed_distances_possible, missing until 3 Oct: asked about
    # "a 10-minute walk" from South Bank to Roma Street - 1,816 m - this
    # function said valid, because it judged the distance and never the time.
    # A walk may be claimed slower than anyone needs; it may not be claimed
    # faster than MAX_WALKING_SPEED_MPS over the distance it covers - the
    # longer of claimed and measured, as for every check below, less the
    # snapping tolerance the distance check allows. Both forms of a time are
    # read: duration_s, and arrive minus depart.
    fastest_s: dict[int, int] = {}
    too_fast = []
    for i, segment in enumerate(segments):
        if segment["mode"] != "walk":
            continue
        distance = max(int(segment.get("distance_m") or 0), measured.get(i, 0))
        fastest_s[i] = seconds(max(0, distance - SNAP_TOLERANCE_M) / MAX_WALKING_SPEED_MPS)
        claimed = []
        if segment.get("duration_s") is not None:
            claimed.append(seconds(segment["duration_s"]))
        depart, arrive = read_stamp(segment.get("depart")), read_stamp(segment.get("arrive"))
        if depart and arrive and arrive >= depart:      # reversed legs are reported below
            claimed.append(seconds((arrive - depart).total_seconds()))
        if claimed and min(claimed) < fastest_s[i]:
            too_fast.append({"segment": i, "claimed_s": min(claimed), "distance_m": distance,
                             "fastest_possible_s": fastest_s[i],
                             "at_planning_pace_s": seconds(distance / WALKING_SPEED_MPS)})
    checks.append(_record(
        "claimed_walk_times_possible", "structural", not too_fast,
        {"too_fast": too_fast} if too_fast else 0, MAX_WALKING_SPEED_MPS,
        ("No walking segment claims to take less time than its distance allows at "
         f"{MAX_WALKING_SPEED_MPS} m/s, a pace faster than anyone walks."
         if not too_fast else
         f"{len(too_fast)} walking segment(s) claim a time that would need a pace faster than "
         f"{MAX_WALKING_SPEED_MPS} m/s over the distance walked, which no one walks. The "
         f"distance is the longer of the claimed and the measured one"
         + ("" if graph is not None else
            "; with no network supplied, the claimed one")
         + f". at_planning_pace_s is the time at {WALKING_SPEED_MPS} m/s, the pace every "
           "walking time this agent reports uses."),
    ))

    # -- structural: transit legs exist in the timetable --------------------
    #
    # Until this existed, a bus leg was taken entirely on trust: continuity and
    # timing were checked against numbers the caller supplied, and whether the
    # service existed at all was never asked. A journey can be perfectly
    # self-consistent and still be built on a bus that does not run.
    transit = [(i, s) for i, s in enumerate(segments) if s["mode"] != "walk"]
    if table is not None and transit:
        off_stop, no_service, not_checked = [], [], []
        for i, segment in transit:
            stop_id, distance = nearest_stop(table, segment["from"]["lat"], segment["from"]["lon"])
            if stop_id is None or distance > TRANSIT_STOP_TOLERANCE_M:
                off_stop.append({"segment": i, "nearest_stop_m": distance})
                continue
            when = read_stamp(segment.get("depart"))
            if when is None:
                # No departure time is not a false claim, just an uncheckable
                # one. Recorded on the check's evidence rather than failed -
                # and the evidence must then say so: before 22 Oct 2026 it
                # read "every transit leg ... with a service scheduled at that
                # time" even for a leg whose time ("03:00", no date) could not
                # be read, so a bus that never runs was passed as checked.
                not_checked.append({
                    "segment": i,
                    "reason": ("no departure time given" if segment.get("depart") in (None, "")
                               else "departure time is not an ISO 8601 timestamp"),
                })
                continue
            found = find_departure(table, stop_id, when.astimezone(BRISBANE_TZ),
                                   str(segment.get("route_id") or ""))
            if found is None:
                no_service.append({
                    "segment": i,
                    "stop": table["stops"][stop_id]["name"],
                    "claimed_departure": when.astimezone(BRISBANE_TZ).isoformat(timespec="minutes"),
                })
        problems = off_stop + no_service
        if problems:
            observed = {"not_at_a_stop": off_stop, "no_scheduled_service": no_service}
            if not_checked:
                observed["not_checked"] = not_checked
            evidence = (f"{len(off_stop)} transit leg(s) board nowhere near a known stop and "
                        f"{len(no_service)} claim a departure the timetable does not contain.")
        elif not_checked:
            observed = {"not_checked": not_checked}
            evidence = (f"{len(not_checked)} of {len(transit)} transit leg(s) gave no readable "
                        "departure time, so whether their service runs was NOT checked. Give "
                        "depart as ISO 8601, e.g. 2026-10-23T08:12:00+10:00. Every leg that "
                        "was checked boards at a real stop with a service scheduled at that time.")
        else:
            observed = 0
            evidence = "Every transit leg boards at a real stop with a service scheduled at that time."
        checks.append(_record(
            "transit_services_exist", "structural", not problems, observed,
            DEPARTURE_TOLERANCE_S, evidence,
        ))
    elif transit:
        checks.append(_record(
            "transit_services_exist", "structural", True, "skipped", None,
            "No timetable was supplied, so transit legs were not checked against the "
            "schedule. This check did not pass; it did not run.",
        ))

    # -- structural: time runs forwards -----------------------------------
    stamped = [(i, read_stamp(s.get("depart")), read_stamp(s.get("arrive")))
               for i, s in enumerate(segments)]
    reversed_legs = [i for i, depart, arrive in stamped
                     if depart and arrive and arrive < depart]
    tight = []
    for (i, _d1, arrive), (j, depart, _a2) in zip(stamped, stamped[1:]):
        if arrive and depart:
            slack = seconds((depart - arrive).total_seconds())
            floor = constraints.get("min_transfer_seconds", 0)
            if slack < floor:
                tight.append({"between_segments": [i, j], "slack_s": slack, "required_s": floor})
    checks.append(_record(
        "temporal_feasibility", "structural", not reversed_legs and not tight,
        {"segments_arriving_before_departing": reversed_legs, "infeasible_connections": tight}
        if (reversed_legs or tight) else 0,
        constraints.get("min_transfer_seconds", 0),
        ("Times run forwards and every connection can be made."
         if not reversed_legs and not tight else
         f"{len(reversed_legs)} segment(s) arrive before they depart and "
         f"{len(tight)} connection(s) leave too little time."),
    ))

    # -- structural: the route goes where it was asked to go ---------------
    #
    # Measured against SNAP_TOLERANCE_M, not the tighter continuity tolerance.
    # A route can only begin and end on a mapped network node, so a request
    # aimed at a building entrance or the middle of a lawn is legitimately
    # served by a route starting at the nearest footpath - tens of metres away
    # by definition. Holding that to the continuity tolerance would fail
    # correct routes for being correct, which is over-strict verification
    # doing harm rather than work. Genuinely wrong endpoints are hundreds or
    # thousands of metres out and still fail.
    for label, expected, end, index, word in (
        ("origin_match", expected_origin, "from", 0, "start"),
        ("destination_match", expected_destination, "to", -1, "end"),
    ):
        if not expected:
            continue
        point = segments[index][end]
        off_by = metres(haversine_m(expected["lat"], expected["lon"], point["lat"], point["lon"]))
        # SNAP_TOLERANCE_M when judging someone else's route. The planning tools
        # pass a wider figure: they start at the nearest node by construction, so
        # for them this measures how far the nearest path is, not a wrong claim.
        checks.append(_record(
            label, "structural", off_by <= endpoint_tolerance_m, off_by, endpoint_tolerance_m,
            f"The route's {word} lies {off_by} m from the point that was requested; a route "
            f"can only {word} at a mapped network point, so up to {endpoint_tolerance_m} m of "
            "offset is accepted here rather than read as a wrong endpoint.",
        ))

    # -- constraints -------------------------------------------------------
    #
    # Each walking segment counts for the longer of what it claimed and what
    # the network says is possible. Over-claiming is honoured because walking
    # further than necessary is a real choice; under-claiming is overridden
    # because it is not.
    walked = sum(
        max(int(s.get("distance_m") or 0), measured.get(i, 0))
        for i, s in enumerate(segments) if s["mode"] == "walk"
    )
    if "max_walking_distance_m" in constraints:
        limit = constraints["max_walking_distance_m"]
        checks.append(_record(
            "max_walking_distance_m", "constraint", walked <= limit, walked, limit,
            f"Walking segments total {walked} m against a limit of {limit} m.",
        ))

    if "max_transfers" in constraints:
        transfers = max(0, len(transit_legs(segments)) - 1)
        limit = constraints["max_transfers"]
        checks.append(_record(
            "max_transfers", "constraint", transfers <= limit, transfers, limit,
            f"The route uses {transfers} transfer(s) against a limit of {limit}.",
        ))

    if "allowed_modes" in constraints:
        allowed = set(constraints["allowed_modes"])
        used = sorted({s["mode"] for s in segments})
        offending = sorted(set(used) - allowed)
        checks.append(_record(
            "allowed_modes", "constraint", not offending, used, sorted(allowed),
            ("Every segment uses a permitted mode."
             if not offending else
             f"The route uses {', '.join(offending)}, which the request excluded."),
        ))

    if "avoid_highway_types" in constraints:
        avoid = set(constraints["avoid_highway_types"])
        hit = sorted({s["highway_type"] for s in segments
                      if isinstance(s.get("highway_type"), str) and s["highway_type"] in avoid})
        checks.append(_record(
            "avoid_highway_types", "constraint", not hit, hit, sorted(avoid),
            ("The route avoids every excluded way type."
             if not hit else
             f"The route uses {', '.join(hit)}, which the request asked to avoid."),
        ))

    if "avoid_surfaces" in constraints:
        avoid = set(constraints["avoid_surfaces"])
        # NOT `walked`: that name already holds the walking distance in metres
        # and is reported as walking_distance_m at the end of this function.
        # Rebinding it here returned a list of segments where a number belongs.
        walk_segments = [s for s in segments if s["mode"] == "walk"]
        total = sum(float(s.get("distance_m") or 0) for s in walk_segments)
        stated = sum(float(s.get("distance_m") or 0) for s in walk_segments if s.get("surface"))
        hit = sorted({s["surface"] for s in walk_segments if s.get("surface") in avoid})
        share = round(stated / total * 100) if total else 0
        # A pass here is weaker than the other constraint checks and says so.
        # OpenStreetMap states a surface for about three quarters of this
        # network; on the rest, "no offending surface was found" means nobody
        # recorded one, not that there is none. Reporting the share is the
        # difference between a verdict and a reassurance.
        checks.append(_record(
            "avoid_surfaces", "constraint", not hit, hit, sorted(avoid),
            (f"No segment states an excluded surface. {share}% of the walking distance "
             f"states a surface at all; the rest is untagged in OpenStreetMap and could "
             f"not be checked either way."
             if not hit else
             f"The route crosses {', '.join(hit)}, which the request asked to avoid. "
             f"{share}% of the walking distance states a surface."),
        ))

    if "max_duration_s" in constraints:
        # A walking leg counts for no less than it could possibly take, as a
        # walking distance counts for no less than the network allows.
        total = sum(max(seconds(s.get("duration_s") or 0), fastest_s.get(i, 0))
                    for i, s in enumerate(segments))
        first, last = read_stamp(segments[0].get("depart")), read_stamp(segments[-1].get("arrive"))
        if first and last:
            total = seconds((last - first).total_seconds())
        limit = constraints["max_duration_s"]
        checks.append(_record(
            "max_duration_s", "constraint", total <= limit, total, limit,
            f"The journey takes {total} s against a limit of {limit} s.",
        ))

    for key, end, comparison in (("arrive_before", "arrive", "before"),
                                 ("depart_after", "depart", "after")):
        if key not in constraints:
            continue
        wanted = parse_clock(constraints[key])
        stamp = read_stamp(segments[-1 if end == "arrive" else 0].get(end))
        if stamp is None or wanted is None:
            checks.append(_record(
                key, "constraint", False, "no_timestamp", constraints[key],
                f"The route carries no {end} time, so this constraint cannot be shown to hold. "
                "An unverifiable constraint is reported as unsatisfied rather than assumed.",
            ))
            continue
        local = stamp.astimezone(BRISBANE_TZ)
        if wanted[0] == "time":
            observed = local.hour * 3600 + local.minute * 60 + local.second
            satisfied = observed <= wanted[1] if comparison == "before" else observed >= wanted[1]
        else:
            observed = local.isoformat(timespec="seconds")
            satisfied = stamp <= wanted[1] if comparison == "before" else stamp >= wanted[1]
        checks.append(_record(
            key, "constraint", satisfied, local.isoformat(timespec="seconds"), constraints[key],
            f"The route {end}s at {local.isoformat(timespec='seconds')}, which is "
            f"{'within' if satisfied else 'outside'} the requested window.",
        ))

    constraint_checks = [c for c in checks if c["kind"] == "constraint"]
    violations = [c for c in checks if not c["satisfied"]]

    return {
        "valid": not violations,
        "checks": checks,
        "violations": violations,
        # S(r,C) from the project's equation (2). One when nothing was asked:
        # a route with no constraints on it satisfies all zero of them, and
        # reporting 0.0 there would read as total failure.
        "constraint_satisfaction_score": (
            round(sum(c["satisfied"] for c in constraint_checks) / len(constraint_checks), 4)
            if constraint_checks else 1.0
        ),
        "constraints_checked": len(constraint_checks),
        "walking_distance_m": walked,
        "transfers": max(0, len(transit_legs(segments)) - 1),
    }


# ---------------------------------------------------------------------------
# Repair: the smallest change that makes a failing route pass - then proof.
#
# validate() says what is wrong with a route. For a caller holding someone
# else's itinerary the next question is what would make it right, and the
# answer is usually small: one walking leg nobody measured, one bus that does
# not run at the time claimed. Replanning the whole journey would answer a
# question nobody asked and throw away the parts that were fine.
#
# Every repair is judged by the same validate() as the route it repairs, and
# is only reported as repaired if it passes. This module trusts no producer of
# routes, and a repair is a producer like any other.
#
# What it will not do is invent a journey. Changing a mode, the number of
# transfers or a boarding stop is planning a different itinerary, and the
# timetable index holds departures per stop rather than trip sequences, so a
# different route from the same stop cannot be shown to reach the same place.
# Those violations come back with the reason they could not be repaired.
# ---------------------------------------------------------------------------

MAX_WAIT_S = 3600          # how far ahead a replacement departure is looked for
MAX_REPAIR_LEGS = 12       # walking legs replanned at most, to stay inside the budget

_UNREPAIRABLE = {
    "max_walking_distance_m": (
        "Every walking leg is already the shortest the network allows under the stated "
        "restrictions, so getting under the limit needs a different itinerary - other stops "
        "or other services - which is planning, not repair."),
    "max_transfers": "Fewer transfers means a different itinerary, which is planning, not repair.",
    "max_duration_s": (
        "The journey is still too long with every leg on its earliest feasible service. "
        "A faster journey needs a different itinerary."),
    "allowed_modes": (
        "A leg uses a mode that was not allowed. Replacing a mode is planning a different "
        "journey, not repairing this one."),
    "arrive_before": (
        "The earliest feasible arrival for this itinerary is still too late. An earlier "
        "arrival needs an earlier start or a different itinerary."),
    "depart_after": (
        "The itinerary leaves before the stated time, and moving its start would move every "
        "service in it - which is replanning the journey."),
    "within_coverage": (
        "Part of the route lies outside the area this agent holds data for, so it can be "
        "neither measured nor repaired there."),
    "claimed_walk_times_possible": (
        "Correcting a walking time means replanning the leg on the walking network, and "
        "this leg could not be replanned."),
    "segment_continuity": (
        "The legs do not join up, and which leg is the wrong one cannot be decided from the "
        "route alone."),
}


def _shifted(leg: dict, delta: timedelta) -> dict:
    """A copy of a leg with its times moved by `delta`."""
    moved = dict(leg)
    for key in ("depart", "arrive"):
        stamp = read_stamp(leg.get(key))
        if stamp is not None:
            moved[key] = (stamp + delta).isoformat(timespec="seconds")
    return moved


def _restamp(legs: list[dict], start: datetime) -> list[dict]:
    """Give consecutive walking legs times, starting at `start`."""
    clock, out = start, []
    for leg in legs:
        leg = dict(leg)
        end = clock + timedelta(seconds=int(leg.get("duration_s") or 0))
        leg["depart"] = clock.isoformat(timespec="seconds")
        leg["arrive"] = end.isoformat(timespec="seconds")
        out.append(leg)
        clock = end
    return out


def _walk_between(graph: dict, start: dict, end: dict,
                  restrictions: dict) -> tuple[list[dict] | None, str]:
    """Walking legs from `start` to `end` under the traveller's restrictions.

    The first and last leg are pinned to the exact points given. The planner
    starts and ends at the nearest network node, up to SNAP_TOLERANCE_M away,
    while legs must meet within CONTINUITY_TOLERANCE_M - so an unpinned repair
    could break continuity with its neighbours and fail for a reason it
    introduced itself.
    """
    plan = plan_route(graph, start, end, restrictions)
    if not plan["found"]:
        return None, plan.get("detail") or "no walking route could be found"
    legs = [dict(s) for s in plan["segments"]] or [{
        "mode": "walk", "highway_type": None, "surface": None,
        "from": dict(start), "to": dict(end), "distance_m": 0, "duration_s": 0,
    }]
    legs[0]["from"] = {"lat": start["lat"], "lon": start["lon"]}
    legs[-1]["to"] = {"lat": end["lat"], "lon": end["lon"]}
    return legs, ""


def next_departure(table: dict, stop_id: str, after: datetime, route_hint: str,
                   window_s: int = MAX_WAIT_S) -> dict | None:
    """The first scheduled departure of a route from a stop at or after `after`.

    Unlike find_departure, which asks whether a claimed departure exists, this
    asks what the traveller could actually catch. The route is required: with
    no route named, the next bus from a stop could be going anywhere.
    """
    entries = table["departures"].get(stop_id)
    hint = (route_hint or "").strip().lower()
    if not entries or not hint:
        return None
    times = getattr(entries, "times", None)
    if times is None:
        times = [row[0] for row in entries]
    local = after.astimezone(BRISBANE_TZ)
    target = local.hour * 3600 + local.minute * 60 + local.second
    best = None
    # A service day runs past midnight, as in find_departure.
    for base in (target, target + 86_400):
        for i in range(bisect.bisect_left(times, base), len(entries)):
            secs, route_ix, service_ix = entries[i]
            if secs > base + window_s:
                break
            if hint not in (table["routes"][route_ix].lower(),
                            (table["route_names"][route_ix] or "").lower()):
                continue
            if not service_runs_on(table, service_ix, local):
                continue
            if best is None or secs - base < best["wait_s"]:
                best = {"wait_s": secs - base,
                        "route": table["route_names"][route_ix] or table["routes"][route_ix],
                        "route_id": table["routes"][route_ix]}
            break
    if best is None:
        return None
    best["when"] = local + timedelta(seconds=best["wait_s"])
    return best


def repair(segments: list[dict], constraints: dict, verdict: dict, revalidate, *,
           graph: dict | None = None, table: dict | None = None,
           expected_origin: dict | None = None,
           expected_destination: dict | None = None) -> dict:
    """Propose the smallest change that makes a failing route pass, and prove it.

    Three passes, each a repair a person would make by hand:

      1. Endpoints. A route that starts or ends away from the requested point
         gets a walking leg to connect it - an itinerary that begins at the bus
         stop rather than the traveller's door is the commonest gap of all.
      2. Walking legs. A leg whose stated distance is impossible, or which uses
         a way type or surface the traveller asked to avoid, is replanned
         between its own two ends under the traveller's restrictions.
      3. Timeline. A forward pass puts every leg on the earliest time it can
         actually start: a walk after the previous leg arrives, a transit leg
         on a departure the timetable contains, of the route the itinerary
         named, after the traveller can be at the stop.

    `revalidate(segments)` must run validate() with the same constraints,
    graph, coverage and endpoints as the original check. The result is only
    `repaired` if the repaired route passes it.
    """
    restrictions = {k: constraints[k] for k in ("avoid_highway_types", "avoid_surfaces")
                    if k in constraints}
    failed = {v["check"]: v for v in verdict["violations"]}
    working = [dict(s) for s in segments]
    source = list(range(len(segments)))        # original index behind each working leg
    changes: list[dict] = []
    unrepairable: list[dict] = []

    # -- 1. endpoints --------------------------------------------------------
    for check, expected, at_start in (("origin_match", expected_origin, True),
                                      ("destination_match", expected_destination, False)):
        if check not in failed or not expected:
            continue
        if graph is None:
            unrepairable.append({"check": check, "why": (
                "Connecting the route to the requested point needs the walking network, "
                "and that point is outside it.")})
            continue
        edge = working[0] if at_start else working[-1]
        a, b = (expected, edge["from"]) if at_start else (edge["to"], expected)
        legs, why = _walk_between(graph, a, b, restrictions)
        if legs is None:
            unrepairable.append({"check": check, "why": f"No walking connection: {why}"})
            continue
        anchor = read_stamp(edge.get("depart" if at_start else "arrive"))
        if anchor is not None:
            total = sum(int(leg.get("duration_s") or 0) for leg in legs)
            legs = _restamp(legs, anchor - timedelta(seconds=total) if at_start else anchor)
        added = sum(int(leg.get("distance_m") or 0) for leg in legs)
        if at_start:
            working, source = legs + working, [None] * len(legs) + source
        else:
            working, source = working + legs, source + [None] * len(legs)
        gap = failed[check]["observed"]
        changes.append({
            "segment": "start" if at_start else "end",
            "change": (f"added a {metres(added)} m walk from the requested origin to where "
                       "the route begins" if at_start else
                       f"added a {metres(added)} m walk from where the route ends to the "
                       "requested destination"),
            "why": (f"the route {'starts' if at_start else 'ends'} {gap} m from the requested "
                    f"{'origin' if at_start else 'destination'}, in a straight line; the walk "
                    "follows the network, so it is longer"),
        })

    # -- 2. walking legs -----------------------------------------------------
    reasons: dict[int, str] = {}
    network = failed.get("walk_segments_on_network")
    if network and isinstance(network["observed"], dict):
        for item in network["observed"].get("off_network", []):
            unrepairable.append({"check": "walk_segments_on_network", "segment": item["segment"],
                                 "why": (f"An end of this leg is {item['snap_m']} m from any mapped "
                                         "way. The coordinate itself is probably wrong, and "
                                         "moving it is the caller's decision.")})
        for item in network["observed"].get("unreachable", []):
            unrepairable.append({"check": "walk_segments_on_network", "segment": item["segment"],
                                 "why": ("No path joins the two ends of this leg on the mapped "
                                         "network.")})
    impossible = failed.get("claimed_distances_possible")
    if impossible and isinstance(impossible["observed"], dict):
        for item in impossible["observed"].get("understated", []):
            reasons[item["segment"]] = (f"it claimed {item['claimed_m']} m where the network's "
                                        f"shortest is {item['shortest_possible_m']} m")
    too_fast = failed.get("claimed_walk_times_possible")
    if too_fast and isinstance(too_fast["observed"], dict):
        for item in too_fast["observed"].get("too_fast", []):
            reasons.setdefault(item["segment"], (
                f"it claimed {item['claimed_s']} s for {item['distance_m']} m, faster than "
                "anyone walks"))
    for check, field in (("avoid_highway_types", "highway_type"), ("avoid_surfaces", "surface")):
        if check in failed:
            avoid = set(constraints.get(check, ()))
            for i, leg in enumerate(segments):
                if leg["mode"] == "walk" and leg.get(field) in avoid:
                    reasons.setdefault(i, f"it uses {str(leg[field]).replace('_', ' ')}, "
                                          "which the traveller asked to avoid")
    over_limit = "max_walking_distance_m" in failed

    if graph is not None:
        rebuilt, rebuilt_source, replanned = [], [], 0
        for leg, origin_ix in zip(working, source):
            reason = reasons.get(origin_ix) if origin_ix is not None else None
            if leg["mode"] != "walk" or replanned >= MAX_REPAIR_LEGS or (
                    reason is None and not over_limit):
                rebuilt.append(leg)
                rebuilt_source.append(origin_ix)
                continue
            legs, why = _walk_between(graph, leg["from"], leg["to"], restrictions)
            planned = sum(int(x.get("distance_m") or 0) for x in (legs or []))
            claimed = leg.get("distance_m")
            # Under the walking limit alone, a leg is only replaced when the
            # replan is genuinely shorter. Replacing a leg with itself would
            # report a change that changed nothing.
            if reason is None and (legs is None or claimed is None
                                   or planned >= int(claimed)):
                rebuilt.append(leg)
                rebuilt_source.append(origin_ix)
                continue
            if legs is None:
                unrepairable.append({"check": "claimed_distances_possible", "segment": origin_ix,
                                     "why": f"The leg could not be replanned: {why}"})
                rebuilt.append(leg)
                rebuilt_source.append(origin_ix)
                continue
            start = read_stamp(leg.get("depart"))
            if start is not None:
                legs = _restamp(legs, start)
            replanned += 1
            rebuilt.extend(legs)
            rebuilt_source.extend([origin_ix] * len(legs))
            by_type: dict[str, int] = {}
            for x in legs:
                kind = str(x.get("highway_type") or "unnamed way").replace("_", " ")
                by_type[kind] = by_type.get(kind, 0) + int(x.get("distance_m") or 0)
            dominant = max(by_type, key=by_type.get)
            changes.append({
                "segment": origin_ix,
                "change": (f"replanned the walking leg: {metres(planned)} m on the mapped "
                           f"network, mostly {dominant}"
                           + (f", {by_type['steps']} m of it on steps" if "steps" in by_type
                              else "")),
                "why": reason or "the walking total was over the limit, and this leg can be shorter",
                "before": {"distance_m": claimed},
                "after": {"distance_m": metres(planned), "legs": len(legs)},
            })
        working, source = rebuilt, rebuilt_source

    # -- 3. timeline ---------------------------------------------------------
    floor = int(constraints.get("min_transfer_seconds") or 0)
    ready = None
    for k, leg in enumerate(working):
        depart = read_stamp(leg.get("depart"))
        if depart is None:
            ready = None            # nothing after an untimed leg can be reasoned from it
            continue
        earliest = ready + timedelta(seconds=floor) if ready is not None else None
        if leg["mode"] == "walk":
            if earliest is not None and depart < earliest:
                working[k] = _shifted(leg, earliest - depart)
                changes.append({
                    "segment": source[k],
                    "change": f"walk starts at {earliest.astimezone(BRISBANE_TZ):%H:%M}",
                    "why": "the leg before it does not arrive in time for the original start",
                })
        elif table is not None:
            stop_id, distance = nearest_stop(table, leg["from"]["lat"], leg["from"]["lon"])
            hint = str(leg.get("route_id") or "")
            at_stop = stop_id is not None and distance <= TRANSIT_STOP_TOLERANCE_M
            runs = at_stop and find_departure(
                table, stop_id, depart.astimezone(BRISBANE_TZ), hint) is not None
            catchable = earliest is None or depart >= earliest
            if not (runs and catchable):
                if not at_stop:
                    unrepairable.append({"check": "transit_services_exist", "segment": source[k],
                                         "why": (f"This leg boards {distance} m from the nearest "
                                                 "stop, so there is no departure to move it to.")})
                elif not hint:
                    unrepairable.append({"check": "transit_services_exist", "segment": source[k],
                                         "why": ("This leg names no route, so a replacement "
                                                 "departure could be a bus going anywhere.")})
                else:
                    look_from = earliest if earliest is not None else depart
                    found = next_departure(table, stop_id, look_from, hint)
                    if found is None:
                        unrepairable.append({
                            "check": "transit_services_exist", "segment": source[k],
                            "why": (f"No {hint} leaves {table['stops'][stop_id]['name']} within "
                                    f"{MAX_WAIT_S // 60} minutes of "
                                    f"{look_from.astimezone(BRISBANE_TZ):%H:%M}.")})
                    else:
                        working[k] = _shifted(leg, found["when"] - depart)
                        changes.append({
                            "segment": source[k],
                            "change": (f"moved to the {found['when']:%H:%M} {found['route']} "
                                       f"from {table['stops'][stop_id]['name']}"),
                            "why": ("the claimed departure is not in the timetable" if not runs
                                    else "the traveller cannot reach the stop in time for it"),
                            "before": {"depart": leg.get("depart")},
                            "after": {"depart": working[k].get("depart")},
                            # Said out loud because it is the one number here the
                            # data does not vouch for.
                            "assumption": ("The arrival moved by the same amount. The "
                                           "timetable index holds departures per stop, not "
                                           "trip sequences, so the ride time is the "
                                           "itinerary's own claim, carried over unverified."),
                        })
        ready = read_stamp(working[k].get("arrive"))

    # -- the proof -----------------------------------------------------------
    final = revalidate(working)
    still = {v["check"]: v for v in final["violations"]}
    kept = [u for u in unrepairable if u["check"] in still]
    named = {u["check"] for u in kept}
    for check, v in still.items():
        if check not in named:
            kept.append({"check": check,
                         "why": _UNREPAIRABLE.get(check, "Not a kind of violation this tool "
                                                         "repairs."),
                         "evidence": v["evidence"]})
    return {
        "attempted": True,
        "repaired": final["valid"],
        "changes": changes,
        "unrepairable": kept,
        "segments": working,
        "verdict": final,
    }
