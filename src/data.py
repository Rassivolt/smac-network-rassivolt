"""
data.py - fetching open data, with a cache.

Two sources, both open, both keyless:

  * Queensland Government charging stations, via the data.qld.gov.au CKAN
    datastore API. Returns clean JSON - use this rather than the published CSV,
    which contains embedded newlines inside quoted fields.
  * OpenStreetMap place lookup, via Nominatim (ODbL). The specification requires openly
    licensed sources, which rules out Google Maps geocoding.

Everything is cached on disk. That is not an optimisation: a tool call must
return within 45 seconds, and Nominatim's public endpoint allows only one
request per second. Fifty-five agents calling it live would get the cohort
blocked.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

import csv
import io
import zipfile

import httpx

from smac import haversine_m

CACHE_DIR = Path(__file__).with_name(".cache")
CACHE_DIR.mkdir(exist_ok=True)

# data.qld.gov.au - "Find a charging station - Electric vehicle", CC BY 4.0
QLD_API = "https://www.data.qld.gov.au/api/3/action/datastore_search"
QLD_RESOURCE_ID = "a34d4b5f-8e3c-4995-8950-2e84fd7bb4d5"
QLD_LICENCE = "Queensland Government (TMR) via data.qld.gov.au, CC BY 4.0"

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSM_LICENCE = "© OpenStreetMap contributors, ODbL 1.0"
USER_AGENT = "REIT7820-SMAC-agent/0.1 (UQ teaching project)"

_nominatim_lock = threading.Lock()
_nominatim_last = 0.0


def _cached(key: str, ttl_seconds: int, fetch) -> Any:
    """Return cached JSON if fresh, else fetch and store. Falls back to a stale
    copy if the fetch fails - a publisher outage must not fail your tool call."""
    path = CACHE_DIR / (hashlib.sha256(key.encode()).hexdigest()[:16] + ".json")
    if path.exists() and time.time() - path.stat().st_mtime < ttl_seconds:
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        payload = fetch()
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload
    except Exception:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        raise


def charging_stations(ttl_seconds: int = 6 * 3600) -> list[dict]:
    """All Queensland Electric Super Highway stations, as clean records.

    Fields kept: name, lat, lon, address, host, status, onward_stations.
    The source publishes a `Charging plugs available` column that is empty for
    every row, so it is left out rather than reported as unknown
    detail your caller might mistake for real.
    """
    def fetch():
        response = httpx.get(
            QLD_API,
            params={"resource_id": QLD_RESOURCE_ID, "limit": 1000},
            timeout=20.0,
        )
        response.raise_for_status()
        return response.json()["result"]["records"]

    stations = []
    for row in _cached(f"qld:{QLD_RESOURCE_ID}", ttl_seconds, fetch):
        try:
            lat, lon = float(row["Latitude"]), float(row["Longitude"])
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed rows rather than crashing
        stations.append(
            {
                "name": str(row.get("Location Name", "")).strip(),
                "lat": lat,
                "lon": lon,
                "address": " ".join(str(row.get("Address", "")).split()),
                "host": str(row.get("Host", "")).strip() or None,
                "status": str(row.get("Status", "")).strip() or None,
                "onward_stations": " ".join(str(row.get("Nearest QESH charging station", "")).split()) or None,
            }
        )
    return stations


def geocode(place: str, ttl_seconds: int = 30 * 24 * 3600) -> dict | None:
    """Look up a place name on OpenStreetMap. Returns None if there is no match.

    Cached for a month and rate-limited to Nominatim's published 1 req/sec.
    """
    query = (place or "").strip()
    if not query:
        return None

    def fetch():
        global _nominatim_last
        with _nominatim_lock:
            wait = 1.1 - (time.time() - _nominatim_last)
            if wait > 0:
                time.sleep(wait)
            _nominatim_last = time.time()
        response = httpx.get(
            NOMINATIM,
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "au"},
            headers={"User-Agent": USER_AGENT},
            timeout=20.0,
        )
        response.raise_for_status()
        return response.json()

    results = _cached(f"osm:{query.lower()}", ttl_seconds, fetch)
    if not results:
        return None
    hit = results[0]
    return {
        "lat": float(hit["lat"]),
        "lon": float(hit["lon"]),
        "matched_name": hit.get("display_name"),
        # building vs suburb vs administrative area - precision varies hugely,
        # and the caller deserves to know which it got.
        "match_kind": f"{hit.get('class', '?')}/{hit.get('type', '?')}",
    }


# Translink GTFS, used by the public transport agent.
# data.qld.gov.au dataset: general-transit-feed-specification-gtfs-translink
GTFS_URL = "https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip"
GTFS_LICENCE = "Translink SEQ GTFS via data.qld.gov.au, CC BY 4.0"

_GTFS_CACHE: dict[str, list[dict]] = {}


def gtfs_table(filename: str, ttl_seconds: int = 24 * 3600) -> list[dict]:
    """Read one file out of the SEQ GTFS archive.

    The archive is 28 MB compressed. Only ask for the small members:
    stops.txt is 1.6 MB and routes.txt is 0.13 MB, but **stop_times.txt is
    164 MB uncompressed** and must never be parsed inside a tool call - you
    would blow the 45-second budget many times over. If you need
    timetables, index that file once at startup into your own structure.
    """
    if filename in _GTFS_CACHE:
        return _GTFS_CACHE[filename]

    archive = CACHE_DIR / "seq_gtfs.zip"
    if not archive.exists() or time.time() - archive.stat().st_mtime > ttl_seconds:
        with httpx.stream("GET", GTFS_URL, timeout=180.0, follow_redirects=True) as response:
            response.raise_for_status()
            with archive.open("wb") as fh:
                for chunk in response.iter_bytes():
                    fh.write(chunk)

    with zipfile.ZipFile(archive) as z, z.open(filename) as member:
        rows = list(csv.DictReader(io.TextIOWrapper(member, encoding="utf-8-sig")))

    # GTFS values arrive space-padded in this feed: ' -27.467834'.
    _GTFS_CACHE[filename] = [{k: (v.strip() if isinstance(v, str) else v) for k, v in r.items()} for r in rows]
    return _GTFS_CACHE[filename]


# Translink patronage, used by the policy agent.
PATRONAGE_SEQ_ID = "c49df919-5c0d-4bd2-9e43-776509b95ef6"
PATRONAGE_LICENCE = "Translink monthly performance data via data.qld.gov.au, CC BY 3.0"


def patronage_seq(ttl_seconds: int = 24 * 3600) -> list[dict]:
    """Monthly SEQ passenger trips by mode.

    Returns records with `month` (ISO 8601 date), `mode` and `trips` (int).
    The published fields are 'Month-Year' ("Dec-2023") and 'Passenger trips'
    (a string) - both normalised here so tools never repeat the parsing.
    """
    def fetch():
        response = httpx.get(
            QLD_API,
            params={"resource_id": PATRONAGE_SEQ_ID, "limit": 1000},
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()["result"]["records"]

    from datetime import datetime

    records = []
    for row in _cached(f"patronage:{PATRONAGE_SEQ_ID}", ttl_seconds, fetch):
        try:
            month = datetime.strptime(str(row["Month-Year"]).strip(), "%b-%Y")
            trips = int(float(row["Passenger trips"]))
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed rows rather than crashing
        records.append({"month": month.strftime("%Y-%m-01"), "mode": str(row["Mode"]).strip(), "trips": trips})
    return sorted(records, key=lambda r: (r["month"], r["mode"]))


# OpenStreetMap road and footpath network via Overpass, used by the network agent.
# Same data as geocode()'s Nominatim above (both OpenStreetMap, both ODbL) but a
# different endpoint and a different shape - a graph, not a point lookup.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Deliberately bounded to inner-to-middle Brisbane, not all of South East
# Queensland (smac.SEQ_BBOX). Two reasons: the Overpass query and the resulting
# graph stay small enough to build once at startup inside the 45s budget, and a
# routing agent that is honest about a smaller footprint beats one that silently
# thins out coverage across a huge one. Widen this once the smaller box is
# working end to end - it is a v1 scope decision, not a technical ceiling.
NETWORK_BBOX = {"lat_min": -27.60, "lat_max": -27.35, "lon_min": 152.90, "lon_max": 153.15}

# Way types kept. Matches the proposal's "principal road network" framing:
# arterials and local streets plus footpaths, not every laneway and driveway.
_NETWORK_HIGHWAYS = "trunk|primary|secondary|tertiary|residential|footway|path|pedestrian"

# Highways a pedestrian, not a vehicle, is assumed to be using. A vehicle
# `oneway` restriction on one of these does not stop someone walking it
# backwards in the real world, so it is ignored for these unless the way
# carries an explicit `oneway:foot` override - see _way_direction().
_FOOT_HIGHWAYS = {"footway", "path", "pedestrian", "steps"}

_ONEWAY_FORWARD = {"yes", "true", "1"}
_ONEWAY_BACKWARD = {"-1", "reverse"}

_ROAD_GRAPH_CACHE: dict | None = None


def _way_direction(tags: dict, highway: str) -> str:
    """Which way a way's node order may be traversed: "forward", "backward" or "both".

    Three OSM conventions, layered in the order they take effect:

    1. Foot-accessible ways (footway/path/pedestrian/steps) ignore a vehicle
       `oneway` tag by default - it restricts cars, not pedestrians - unless
       the way carries `oneway:foot` itself, which is how a genuinely
       one-way footbridge or turnstile is tagged.
    2. `oneway` on the way: yes/true/1 = forward only (the direction the
       way's nodes are listed in), -1/reverse = backward only (the way was
       digitised against the direction traffic may flow - a real and common
       case, not a rare one), no = explicitly both.
    3. `junction=roundabout` (or `circular`) is *implicitly* one-way in the
       node order even with no `oneway` tag at all. Missing this is a common
       routing bug, not a corner case - roundabouts are common on arterials.

    Anything not caught by the above defaults to both directions.

    Not modelled: turn restrictions (OSM `type=restriction` relations - "no
    left turn" and similar). Those live on separate relation elements this
    query does not fetch, and would need their own Overpass query and a
    directed-turn model rather than a plain edge weight. Declared as a gap
    rather than silently assumed away.
    """
    if highway in _FOOT_HIGHWAYS:
        foot = tags.get("oneway:foot")
        if foot in _ONEWAY_FORWARD:
            return "forward"
        if foot in _ONEWAY_BACKWARD:
            return "backward"
        return "both"

    oneway = tags.get("oneway")
    if oneway in _ONEWAY_FORWARD:
        return "forward"
    if oneway in _ONEWAY_BACKWARD:
        return "backward"
    if oneway == "no":
        return "both"
    if tags.get("junction") in ("roundabout", "circular"):
        return "forward"
    return "both"


def road_graph(ttl_seconds: int = 30 * 24 * 3600) -> dict:
    """The bounded SEQ road/footpath network as a graph, built once and cached.

    Returns `{"nodes": {node_id: {"lat", "lon"}}, "edges": {node_id: [[neighbour_id,
    metres, highway_type], ...]}}`. Call this once at startup (or let the first
    tool call pay for it) and hold the result - like `gtfs_table`, this must
    never be re-fetched or re-parsed inside a tool call.

    Edge direction follows `_way_direction()`: forward-only, backward-only or
    both, depending on `oneway`, reversed one-ways (`oneway=-1`), roundabouts
    and whether the way is foot-accessible. See that function for the exact
    rules and what is still not modelled (turn restrictions).
    """
    global _ROAD_GRAPH_CACHE
    if _ROAD_GRAPH_CACHE is not None:
        return _ROAD_GRAPH_CACHE

    cache_path = CACHE_DIR / "network_graph.json"
    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < ttl_seconds:
        _ROAD_GRAPH_CACHE = json.loads(cache_path.read_text(encoding="utf-8"))
        return _ROAD_GRAPH_CACHE

    b = NETWORK_BBOX
    query = (
        "[out:json][timeout:120];"
        f'way["highway"~"^({_NETWORK_HIGHWAYS})$"]'
        f"({b['lat_min']},{b['lon_min']},{b['lat_max']},{b['lon_max']});"
        "(._;>;);"
        "out body;"
    )

    def fetch():
        # Overpass's Apache front-end 406s a request with no Accept header and
        # httpx's default User-Agent - send both explicitly, same as geocode().
        response = httpx.post(
            OVERPASS_URL,
            data={"data": query},
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            timeout=150.0,
        )
        response.raise_for_status()
        return response.json()["elements"]

    try:
        elements = fetch()
    except Exception:
        if cache_path.exists():
            _ROAD_GRAPH_CACHE = json.loads(cache_path.read_text(encoding="utf-8"))
            return _ROAD_GRAPH_CACHE
        raise

    # Node ids are kept as strings throughout, on both the first parse and every
    # cache reload - JSON object keys are always strings, and a graph that used
    # int ids fresh but str ids from cache would break equality checks in the
    # pathfinder depending on which path built it.
    nodes: dict[str, dict] = {}
    ways: list[dict] = []
    for el in elements:
        if el["type"] == "node":
            nodes[str(el["id"])] = {"lat": el["lat"], "lon": el["lon"]}
        elif el["type"] == "way":
            ways.append(el)

    edges: dict[str, list] = {}
    for way in ways:
        refs = [str(r) for r in way.get("nodes", [])]
        tags = way.get("tags", {})
        highway = tags.get("highway", "unclassified")
        direction = _way_direction(tags, highway)
        for a, b_id in zip(refs, refs[1:]):
            if a not in nodes or b_id not in nodes:
                continue  # a way can reference a node outside the query box
            dist = haversine_m(nodes[a]["lat"], nodes[a]["lon"], nodes[b_id]["lat"], nodes[b_id]["lon"])
            if direction in ("forward", "both"):
                edges.setdefault(a, []).append([b_id, dist, highway])
            if direction in ("backward", "both"):
                edges.setdefault(b_id, []).append([a, dist, highway])

    graph = {"nodes": nodes, "edges": edges}
    cache_path.write_text(json.dumps(graph), encoding="utf-8")
    _ROAD_GRAPH_CACHE = graph
    return graph


# ---------------------------------------------------------------------------
# Translink GTFS as a timetable index, used by the network agent's transit
# checks. Separate from gtfs_table() above, which hands back raw rows: this
# builds the derived structure the verifier actually queries, and pays for it
# once.
# ---------------------------------------------------------------------------

# Same bounded study area as the road graph. 42% of SEQ's stops and 64% of its
# stop_times fall inside it, and a stop this agent can never route to is a stop
# it has no business indexing.
_TIMETABLE_CACHE: dict | None = None

# How long the timetable treats its GTFS archive as current. Deliberately far
# longer than gtfs_table()'s 24-hour default: an experiment comparing three
# conditions has to judge all of them against the same transport state, and a
# feed that rolls over mid-run silently breaks that. Translink does republish -
# the archive gained 800,000 stop_times rows between two runs a week apart
# during development - so this is a real hazard, not a theoretical one. Every
# index records the fingerprint of the archive it came from, so a result can
# always be traced back to the snapshot that produced it.
SNAPSHOT_TTL_SECONDS = 365 * 24 * 3600


def _gtfs_seconds(clock: str) -> int | None:
    """GTFS time of day as seconds past the service day's midnight.

    Hours may exceed 23: a trip listed at "25:30:00" departs at 01:30 on the
    following calendar day but still belongs to the previous service day. That
    is the specification's own convention, not bad data, and truncating it to
    01:30 would silently move a late-night service twenty-four hours.
    """
    parts = (clock or "").strip().split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    h, m, s = (int(p) for p in parts)
    return h * 3600 + m * 60 + s


def timetable(ttl_seconds: int = 30 * 24 * 3600) -> dict:
    """The SEQ timetable as a per-stop departure index, built once and cached.

    Returns:
        stops       {stop_id: {"name", "lat", "lon"}} inside the study area
        departures  {stop_id: [[seconds_past_midnight, route_ix, service_ix], ...]}
                    sorted by time, so a lookup is a bisection
        routes      [route_id, ...]        - route_ix indexes this
        route_names [short_name, ...]      - parallel to routes
        services    [service_id, ...]      - service_ix indexes this
        service_days {service_ix: "MTWTFSS" as seven 0/1 characters}
        feed        {"start", "end"} from the archive's own calendar range

    Route and service identifiers are interned to integers rather than repeated
    on every one of two million rows; there are only ~1,700 routes and ~170
    services, so this is the difference between a structure that fits in memory
    and one that does not.
    """
    global _TIMETABLE_CACHE
    if _TIMETABLE_CACHE is not None:
        return _TIMETABLE_CACHE

    cache_path = CACHE_DIR / "gtfs_timetable.json"
    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < ttl_seconds:
        _TIMETABLE_CACHE = json.loads(cache_path.read_text(encoding="utf-8"))
        return _TIMETABLE_CACHE

    # Read through the archive at the snapshot TTL, not the daily one.
    archive_path = CACHE_DIR / "seq_gtfs.zip"
    b = NETWORK_BBOX
    stops: dict[str, dict] = {}
    for row in gtfs_table("stops.txt", SNAPSHOT_TTL_SECONDS):
        try:
            lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if b["lat_min"] <= lat <= b["lat_max"] and b["lon_min"] <= lon <= b["lon_max"]:
            stops[row["stop_id"]] = {"name": row.get("stop_name", ""), "lat": lat, "lon": lon}

    # trip_id -> (route_ix, service_ix), interned.
    route_ix: dict[str, int] = {}
    service_ix: dict[str, int] = {}
    trips: dict[str, tuple[int, int]] = {}
    for row in gtfs_table("trips.txt", SNAPSHOT_TTL_SECONDS):
        r = row.get("route_id", "")
        s = row.get("service_id", "")
        trips[row["trip_id"]] = (route_ix.setdefault(r, len(route_ix)),
                                 service_ix.setdefault(s, len(service_ix)))

    routes = [""] * len(route_ix)
    for r, i in route_ix.items():
        routes[i] = r
    names = {row["route_id"]: row.get("route_short_name", "")
             for row in gtfs_table("routes.txt", SNAPSHOT_TTL_SECONDS)}
    route_names = [names.get(r, "") for r in routes]

    services = [""] * len(service_ix)
    for s, i in service_ix.items():
        services[i] = s
    service_days: dict[str, str] = {}
    starts, ends = [], []
    for row in gtfs_table("calendar.txt", SNAPSHOT_TTL_SECONDS):
        ix = service_ix.get(row.get("service_id", ""))
        if ix is None:
            continue
        service_days[str(ix)] = "".join(
            row.get(d, "0") or "0" for d in
            ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))
        if row.get("start_date"):
            starts.append(row["start_date"])
        if row.get("end_date"):
            ends.append(row["end_date"])

    # The expensive pass. Streamed rather than held: stop_times is the largest
    # member of the archive and only the in-area rows survive into the index.
    departures: dict[str, list] = {}
    archive = CACHE_DIR / "seq_gtfs.zip"
    with zipfile.ZipFile(archive) as z, z.open("stop_times.txt") as member:
        for row in csv.DictReader(io.TextIOWrapper(member, encoding="utf-8-sig")):
            stop = (row.get("stop_id") or "").strip()
            if stop not in stops:
                continue
            secs = _gtfs_seconds(row.get("departure_time", ""))
            if secs is None:
                continue
            trip = trips.get((row.get("trip_id") or "").strip())
            if trip is None:
                continue
            departures.setdefault(stop, []).append([secs, trip[0], trip[1]])
    for entries in departures.values():
        entries.sort()

    table = {
        "stops": stops,
        "departures": departures,
        "routes": routes,
        "route_names": route_names,
        "services": services,
        "service_days": service_days,
        "feed": {"start": min(starts) if starts else "", "end": max(ends) if ends else ""},
        # Which archive this index was built from. Two runs quoting the same
        # fingerprint were judged against the same timetable; two quoting
        # different ones were not, however similar their numbers look.
        "snapshot": {
            "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest()[:16],
            "bytes": archive_path.stat().st_size,
            "retrieved": time.strftime("%Y-%m-%dT%H:%M:%S+10:00",
                                       time.localtime(archive_path.stat().st_mtime)),
        },
    }
    cache_path.write_text(json.dumps(table), encoding="utf-8")
    _TIMETABLE_CACHE = table
    return table
