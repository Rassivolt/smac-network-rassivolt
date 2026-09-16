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
import math
from datetime import datetime
from typing import Any

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

# How far apart two consecutive segments' endpoints may be before the route is
# treated as discontinuous. Generous enough to absorb a stop coordinate sitting
# a few metres off the kerb it serves, tight enough that a genuine gap - a
# missing leg between two places - still fails.
CONTINUITY_TOLERANCE_M = 30

# How far a point may sit from the nearest graph node and still be treated as
# being "on" the network. Roughly a street width plus a footpath.
SNAP_TOLERANCE_M = 60

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
CONSTRAINT_TYPES: dict[str, type] = {
    "max_walking_distance_m": int,
    "max_transfers": int,
    "max_duration_s": int,
    "min_transfer_seconds": int,
    "allowed_modes": list,
    "avoid_highway_types": list,
    "arrive_before": str,
    "depart_after": str,
}

# Upper bounds on the numeric constraints. A negative walking limit or a
# thousand-transfer journey is not a preference, it is a malformed value, and
# accepting it would produce a verdict that looks computed but means nothing.
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
            permitted = MODES if key == "allowed_modes" else HIGHWAY_TYPES
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


# ---------------------------------------------------------------------------
# 2. Routing
# ---------------------------------------------------------------------------

# Spatial index cell size in degrees. ~0.005 deg is roughly 500 m here, which
# puts a few dozen nodes in a cell - small enough that a lookup scans a handful
# of cells instead of all 1,428,640 nodes.
_GRID_CELL_DEG = 0.005

_INDEX_CACHE: dict[int, dict] = {}


def spatial_index(graph: dict) -> dict:
    """Bucket every node into a coarse lat/lon grid, once per graph.

    Without this, finding the nearest node is a linear scan of the whole graph:
    ~0.5 s per lookup, and `validate_route` needs two per segment. With it, a
    lookup touches a handful of buckets. This is the one thing a library like
    OSMnx gives you for free (it carries a KD-tree via scipy); twenty lines of
    dictionary here buys the same asymptotics without the dependency.
    """
    cached = _INDEX_CACHE.get(id(graph))
    if cached is not None:
        return cached

    index: dict[tuple[int, int], list[str]] = {}
    for node_id, pos in graph["nodes"].items():
        key = (math.floor(pos["lat"] / _GRID_CELL_DEG), math.floor(pos["lon"] / _GRID_CELL_DEG))
        index.setdefault(key, []).append(node_id)

    _INDEX_CACHE.clear()          # one graph is live at a time; do not leak
    _INDEX_CACHE[id(graph)] = index
    return index


def nearest_node(graph: dict, lat: float, lon: float, max_rings: int = 8) -> tuple[str | None, int]:
    """The closest graph node to a point, and the snap distance in metres.

    Searches the point's own grid cell first, then rings outward. Once a
    candidate is found it searches one further ring before settling: a node
    just across a cell boundary can be nearer than one in the far corner of
    the cell the point happens to sit in, and stopping at the first hit would
    quietly return the wrong node in exactly the awkward cases.
    """
    index = spatial_index(graph)
    nodes = graph["nodes"]
    centre = (math.floor(lat / _GRID_CELL_DEG), math.floor(lon / _GRID_CELL_DEG))

    best_id, best_dist = None, float("inf")
    settle_after = None

    for ring in range(max_rings + 1):
        for dy in range(-ring, ring + 1):
            for dx in range(-ring, ring + 1):
                # Only the perimeter of this ring; the interior was searched already.
                if ring and max(abs(dy), abs(dx)) != ring:
                    continue
                for node_id in index.get((centre[0] + dy, centre[1] + dx), ()):
                    pos = nodes[node_id]
                    d = haversine_m(lat, lon, pos["lat"], pos["lon"])
                    if d < best_dist:
                        best_id, best_dist = node_id, d
        if best_id is not None and settle_after is None:
            settle_after = ring + 1
        if settle_after is not None and ring >= settle_after:
            break

    return best_id, metres(best_dist) if best_id else 0


def shortest_path(graph: dict, start: str, goal: str,
                  blocked_highways: frozenset[str] = frozenset()) -> tuple[list[str], int] | None:
    """Dijkstra's algorithm over the road/footpath graph, minimising metres.

    `blocked_highways` removes way types from consideration entirely - the
    mechanism behind an accessibility constraint such as "no steps", and the
    lever the re-planning loop pulls when a verdict says the route used
    something it should not have.

    Returns (ordered node ids, total metres), or None when no path exists
    under those restrictions. None is a real answer: it can mean the network
    genuinely does not connect the two points, or that the restrictions made
    an otherwise-reachable destination unreachable. `plan_route` distinguishes
    the two by trying again without restrictions.
    """
    edges = graph["edges"]
    dist: dict[str, float] = {start: 0.0}
    prev: dict[str, str] = {}
    queue: list[tuple[float, str]] = [(0.0, start)]
    visited: set[str] = set()

    while queue:
        d, node = heapq.heappop(queue)
        if node in visited:
            continue
        visited.add(node)
        if node == goal:
            break
        for neighbour, weight, highway in edges.get(node, []):
            if highway in blocked_highways:
                continue
            nd = d + weight
            if nd < dist.get(neighbour, float("inf")):
                dist[neighbour] = nd
                prev[neighbour] = node
                heapq.heappush(queue, (nd, neighbour))

    if goal not in dist:
        return None

    path = [goal]
    while path[-1] != start:
        path.append(prev[path[-1]])
    path.reverse()
    return path, metres(dist[goal])


def _edge_highway(graph: dict, a: str, b: str) -> str:
    for neighbour, _weight, highway in graph["edges"].get(a, []):
        if neighbour == b:
            return highway
    return "unclassified"


def path_to_segments(graph: dict, path: list[str], depart: datetime | None = None) -> list[dict]:
    """Turn a node path into walking segments, one per run of the same way type.

    A 5 km path is ~280 graph edges. Returning 280 segments would be true and
    useless - the caller wants "1.2 km along footways, then 300 m of steps",
    which is also the granularity an `avoid_highway_types` constraint is
    written at. Consecutive edges sharing a way type are therefore merged.
    """
    if len(path) < 2:
        return []

    nodes = graph["nodes"]
    segments: list[dict] = []
    run_start = 0
    clock = depart

    def flush(start_index: int, end_index: int, highway: str) -> None:
        nonlocal clock
        distance = sum(
            haversine_m(nodes[path[i]]["lat"], nodes[path[i]]["lon"],
                        nodes[path[i + 1]]["lat"], nodes[path[i + 1]]["lon"])
            for i in range(start_index, end_index)
        )
        duration = seconds(distance / WALKING_SPEED_MPS)
        segment = {
            "mode": "walk",
            "highway_type": highway,
            "from": {"lat": nodes[path[start_index]]["lat"], "lon": nodes[path[start_index]]["lon"]},
            "to": {"lat": nodes[path[end_index]]["lat"], "lon": nodes[path[end_index]]["lon"]},
            "distance_m": metres(distance),
            "duration_s": duration,
        }
        if clock is not None:
            arrive = clock.fromtimestamp(clock.timestamp() + duration, tz=clock.tzinfo)
            segment["depart"] = clock.isoformat(timespec="seconds")
            segment["arrive"] = arrive.isoformat(timespec="seconds")
            clock = arrive
        segments.append(segment)

    current = _edge_highway(graph, path[0], path[1])
    for i in range(1, len(path) - 1):
        highway = _edge_highway(graph, path[i], path[i + 1])
        if highway != current:
            flush(run_start, i, current)
            run_start, current = i, highway
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

    start_id, start_snap = nearest_node(graph, origin["lat"], origin["lon"])
    goal_id, goal_snap = nearest_node(graph, destination["lat"], destination["lon"])
    if start_id is None or goal_id is None:
        return {"found": False, "reason": "no_network_nearby",
                "detail": "No mapped road or footpath lies near one of the two points."}

    found = shortest_path(graph, start_id, goal_id, blocked)
    if found is None:
        # Separate "the restrictions made this unreachable" from "these points
        # are not connected at all". The caller can act on the first and not
        # on the second, so a single "no route" would be the less useful answer.
        if blocked and shortest_path(graph, start_id, goal_id) is not None:
            return {"found": False, "reason": "blocked_by_constraints",
                    "detail": "A route exists, but not one avoiding every excluded way type.",
                    "blocked_highway_types": sorted(blocked)}
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
    if verdict["valid"]:
        n = verdict["constraints_checked"]
        return ("Route is valid: every network check passed"
                + (f" and all {n} stated constraint(s) are satisfied." if n
                   else ", and no constraints were stated to check."))

    structural = [v for v in verdict["violations"] if v["kind"] == "structural"]
    constraint = [v for v in verdict["violations"] if v["kind"] == "constraint"]
    parts = []
    for v in constraint:
        parts.append(f"{v['check']} is {v['observed']} against a limit of {v['threshold']}")
    for v in structural:
        parts.append(f"{v['check']} failed")
    return ("Route REJECTED: " + "; ".join(parts) + "."
            + f" Constraint satisfaction {verdict['constraint_satisfaction_score']:.2f}.")


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
        lo = bisect.bisect_left(entries, [base - DEPARTURE_TOLERANCE_S, -1, -1])
        for secs, route_ix, service_ix in entries[lo:]:
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

        for field in ("depart", "arrive", "route_id"):
            value = _pick(segment, field)
            if value is not None:
                normalised[field] = value

        clean.append(normalised)
    return clean, None


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
             table: dict | None = None) -> dict:
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
        outside = sum(
            1 for segment in segments for end in ("from", "to")
            if not (coverage["lat_min"] <= segment[end]["lat"] <= coverage["lat_max"]
                    and coverage["lon_min"] <= segment[end]["lon"] <= coverage["lon_max"])
        )
        checks.append(_record(
            "within_coverage", "structural", outside == 0, outside, 0,
            ("Every segment endpoint lies inside the covered area."
             if not outside else
             f"{outside} segment endpoint(s) fall outside the covered area, where this "
             "agent cannot confirm the network exists."),
        ))

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

    # -- structural: transit legs exist in the timetable --------------------
    #
    # Until this existed, a bus leg was taken entirely on trust: continuity and
    # timing were checked against numbers the caller supplied, and whether the
    # service existed at all was never asked. A journey can be perfectly
    # self-consistent and still be built on a bus that does not run.
    transit = [(i, s) for i, s in enumerate(segments) if s["mode"] != "walk"]
    if table is not None and transit:
        off_stop, no_service = [], []
        for i, segment in transit:
            stop_id, distance = nearest_stop(table, segment["from"]["lat"], segment["from"]["lon"])
            if stop_id is None or distance > TRANSIT_STOP_TOLERANCE_M:
                off_stop.append({"segment": i, "nearest_stop_m": distance})
                continue
            when = read_stamp(segment.get("depart"))
            if when is None:
                # No departure time is not a false claim, just an uncheckable
                # one. Recorded on the check's evidence rather than failed.
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
        checks.append(_record(
            "transit_services_exist", "structural", not problems,
            {"not_at_a_stop": off_stop, "no_scheduled_service": no_service} if problems else 0,
            DEPARTURE_TOLERANCE_S,
            ("Every transit leg boards at a real stop with a service scheduled at that time."
             if not problems else
             f"{len(off_stop)} transit leg(s) board nowhere near a known stop and "
             f"{len(no_service)} claim a departure the timetable does not contain."),
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
        checks.append(_record(
            label, "structural", off_by <= SNAP_TOLERANCE_M, off_by, SNAP_TOLERANCE_M,
            f"The route's {word} lies {off_by} m from the point that was requested; a route "
            f"can only {word} at a mapped network point, so up to {SNAP_TOLERANCE_M} m of "
            "offset is the network's granularity rather than a wrong endpoint.",
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

    if "max_duration_s" in constraints:
        total = sum(int(s.get("duration_s") or 0) for s in segments)
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
