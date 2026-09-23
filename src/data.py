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
import array
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import csv
import io
import zipfile

import httpx

from smac import SEQ_BBOX, haversine_m

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

# The study area: the Brisbane metropolitan area, not all of South East
# Queensland (smac.SEQ_BBOX). Widened from inner Brisbane (-27.60..-27.35,
# 152.90..153.15) once that box was working end to end. The reason is the
# Showcase: the orchestrator makes one call per agent with no retry, and a
# request landing outside the box can only be declined - an honest answer that
# demonstrates nothing. This box reaches Ipswich, Redcliffe, Logan and the
# Redlands, where a walking route is something a person might actually take.
#
# It deliberately stops short of the Gold Coast, the Sunshine Coast and
# Toowoomba. A walking route from Brisbane to any of them is not a route anyone
# would walk, and the graph would pay for the coverage on every call. Density
# falls away from the centre, so the wider box costs far less than its area
# suggests: measured at the rebuild, 6.2x the area for 2.5x the nodes (573,140
# -> 1,428,640), built in about a minute and loaded in a few seconds.
# Out-of-scope requests are still declined rather than guessed at.
NETWORK_BBOX = {"lat_min": -27.75, "lat_max": -27.15, "lon_min": 152.70, "lon_max": 153.35}

# Way types kept: the walkable network, matching the OSMnx "walk" network the
# proposal cites. An earlier version kept only arterials, residential streets
# and footpaths, and so silently omitted steps, service lanes, laneways,
# unclassified roads, living streets, tracks and shared cycleways - about
# 34,000 ways, 27% of the walkable ways in the study area. That cost more than
# coverage: distances came out up to 261 m longer than the network allows, and
# 72% of the graph's disconnected fragments were artefacts of the omission
# rather than places a walker genuinely cannot reach.
_NETWORK_HIGHWAYS = ("trunk|primary|secondary|tertiary|residential|unclassified|"
                     "living_street|service|track|footway|path|pedestrian|steps|cycleway")

# Way tags kept on every edge, and node tags kept on the nodes that carry
# them. Chosen by measuring what the study area actually holds rather than by
# what would be nice to have - see the coverage figures in this module's own
# history and in descriptions.md. `surface` is the one dense enough (67.6%) to
# carry a constraint; the rest are recorded because they cost one interned
# integer per edge and a rebuild costs a re-derivation of every ground-truth
# value, so the time to collect them is now.
#
# Deliberately NOT kept: incline (1.0% of ways), wheelchair (0.4%), width
# (4.0%). A constraint on a tag that thin would be satisfied by nearly every
# route while checking nearly nothing.
_KEPT_WAY_TAGS = ("surface", "smoothness", "lit", "handrail", "ramp", "step_count")
_KEPT_NODE_TAGS = ("kerb", "tactile_paving")

_ONEWAY_FORWARD = {"yes", "true", "1"}
_ONEWAY_BACKWARD = {"-1", "reverse"}

# Explicit permission that overrides a general access restriction for walkers.
_FOOT_ALLOWED = {"yes", "designated", "permissive"}

# Transit coverage is deliberately wider than walking coverage. A walking
# route from the Gold Coast to Brisbane is not a route anyone takes, so the
# graph stays metropolitan - but a train from the Gold Coast is an ordinary
# journey, and this agent's job includes saying whether the service someone
# claims to be catching actually runs. The GTFS feed covers the whole of South
# East Queensland; indexing only the walking box meant a leg outside it could
# not be checked at all, which is the wrong answer to a question the data can
# settle. Walking legs outside NETWORK_BBOX are still reported as unverifiable.
TIMETABLE_BBOX = SEQ_BBOX

# Bumped whenever the rules below change what these files contain, so a cache
# or a pin built under old rules - on this machine or in a Docker volume - is
# never silently reused. The graph and the timetable index carry separate
# versions because they are cut to separate boxes: widening one must not force
# a rebuild, and a re-derivation of every ground-truth value, for the other.
_SCHEMA = "walk-v4"
_TIMETABLE_SCHEMA = "seq-v1"

_ROAD_GRAPH_CACHE: dict | None = None

# A pinned snapshot, when one is present, is what the network agent answers
# from - ahead of the cache, whatever its age. The cache refreshes on a timer,
# and ground truth measured on one network cannot be judged against another: a
# refreshed graph or timetable moves the answers with no change to the code.
# Pinning holds the local agent, the container and any deployment to the data
# the ground truth was derived from. See pin_snapshot.py.
SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "snapshot"

# dataset -> (file it was loaded from, "pinned" or "cache"), for snapshot_info().
_LOADED_FROM: dict[str, tuple[Path, str]] = {}


class Departures:
    """One stop's departures as three typed arrays, read like a list of rows.

    Stored column-wise because that is where the saving is: a row of three
    small integers costs 88 bytes as a Python list and eight bytes as three
    array slots. Across 3.79 million departures that is the difference between
    528 MB and about 35 MB.

    It behaves like the list of `[seconds, route_ix, service_ix]` rows it
    replaces - indexing, slicing, iteration and len - so callers that read it
    as one need no changes. `times` is exposed because a departure lookup is a
    bisection over the times alone, and searching a plain array beats
    reconstructing rows to compare them.
    """

    __slots__ = ("times", "routes", "services")

    def __init__(self, rows):
        # 'i' takes seconds past midnight past the 24-hour mark; 'H' takes the
        # route and service indices, which the SEQ feed keeps well under 2,000.
        self.times = array.array("i", (int(r[0]) for r in rows))
        self.routes = array.array("H", (int(r[1]) for r in rows))
        self.services = array.array("H", (int(r[2]) for r in rows))

    def __len__(self):
        return len(self.times)

    def __getitem__(self, i):
        if isinstance(i, slice):
            return [(self.times[k], self.routes[k], self.services[k])
                    for k in range(*i.indices(len(self.times)))]
        return (self.times[i], self.routes[i], self.services[i])

    def __iter__(self):
        return zip(self.times, self.routes, self.services)


def _compact_timetable(table: dict) -> dict:
    """Turn each stop's departure rows into typed arrays. Idempotent."""
    departures = table.get("departures")
    if not isinstance(departures, dict):
        return table
    for stop_id, rows in departures.items():
        if not isinstance(rows, Departures):
            departures[stop_id] = Departures(rows)
    return table


def _compact_graph(graph: dict) -> dict:
    """Collapse the duplicate strings json.load leaves behind.

    A node id appears once as a key in `nodes`, again as a key in `edges`, and
    once more for every edge that points at it - and json gives each occurrence
    its own string object. Interning makes them one object; the graph is a
    graph, so ids are referenced far more often than they are distinct.

    The same applies with more force to way types. There are fourteen of them
    and three million edges.

    Measured effect on the pinned walk-v4 graph: 1.25 GB to about 0.9 GB, at a
    cost of a few seconds once at startup. The structure is unchanged - edges
    become tuples rather than lists, which unpack identically - so nothing
    downstream has to know this ran.
    """
    nodes, edges = graph.get("nodes"), graph.get("edges")
    if not nodes or not edges:
        return graph

    # Register the nodes dict's OWN key objects first, so every id interned
    # afterwards resolves to the object the nodes dict is already holding
    # rather than to a second copy of it.
    for node_id in nodes:
        sys.intern(node_id)

    # Rebuild bucket by bucket, taking from the old dict as we go: building a
    # second complete copy first would double the peak, which on a small
    # instance is the one moment that cannot afford it.
    old, new = edges, {}
    while old:
        key, bucket = old.popitem()
        new[sys.intern(key)] = [
            (sys.intern(entry[0]), entry[1], sys.intern(entry[2]), *entry[3:])
            for entry in bucket
        ]
    graph["edges"] = new
    return graph


def _load_json(path: Path, dataset: str, source: str) -> Any:
    _LOADED_FROM[dataset] = (path, source)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    # Whether it came from the pin or the cache, it arrives with json's
    # duplicate strings. Compacting here covers both paths at once.
    if dataset == "road_graph":
        return _compact_graph(loaded)
    if dataset == "timetable":
        return _compact_timetable(loaded)
    return loaded


def _pinned(filename: str) -> Path | None:
    path = SNAPSHOT_DIR / filename
    return path if path.is_file() else None


def _walkable(tags: dict) -> bool:
    """Whether a pedestrian may use this way at all.

    An explicit foot=yes/designated/permissive wins. Otherwise a way tagged
    foot=no, or access=private/no, is closed to walkers and left out - private
    driveways and gated service lanes are most of these. The same rule OSMnx's
    walk filter applies.
    """
    foot = tags.get("foot")
    if foot in _FOOT_ALLOWED:
        return True
    return foot != "no" and tags.get("access") not in ("private", "no")


def _way_direction(tags: dict, highway: str) -> str:
    """Which way a pedestrian may traverse a way: "forward", "backward" or "both".

    This graph routes walkers, and a vehicle one-way does not bind a walker: the
    footpath beside a one-way street runs both ways, and nobody walks round a
    roundabout in the direction of traffic. So `oneway` and `junction=roundabout`
    are deliberately ignored, and the only thing that restricts direction is an
    explicit `oneway:foot` - how a genuinely one-way footbridge, stair or
    turnstile is tagged. yes/true/1 means forward along the way's node order,
    -1/reverse means backward.

    An earlier version applied vehicle one-ways to every non-footpath way. That
    is right for a driving router and wrong for this one: it imposed 73,801
    one-way edges on pedestrians and, once service lanes were in the graph,
    trapped routes in car-park circulation and made some of them longer than
    the network allows. `highway` stays in the signature for callers that pass
    it; direction no longer depends on it.
    """
    foot = tags.get("oneway:foot")
    if foot in _ONEWAY_FORWARD:
        return "forward"
    if foot in _ONEWAY_BACKWARD:
        return "backward"
    return "both"


def road_graph(ttl_seconds: int = 30 * 24 * 3600) -> dict:
    """The bounded SEQ walking network as a graph, built once and cached.

    Returns `{"nodes": {node_id: {"lat", "lon", ...}}, "edges": {node_id:
    [[neighbour_id, metres, highway_type, attrs_ix], ...]}, "way_attrs": [...]}`.

    `attrs_ix` indexes `way_attrs`, a list of the distinct accessibility tag
    sets found (`_KEPT_WAY_TAGS`). There are a few hundred distinct sets across
    three million edges, so interning them costs one integer an edge instead of
    a dictionary an edge. A node carries `kerb` or `tactile_paving` only when
    OpenStreetMap says so. Call this once at startup (or let the first
    tool call pay for it) and hold the result - like `gtfs_table`, this must
    never be re-fetched or re-parsed inside a tool call.

    Ways closed to walkers are dropped (`_walkable`), and so are the nodes only
    they used - otherwise a point could snap to a node with no edges and look
    stranded. Direction follows pedestrian rules (`_way_direction`): every way
    is two-way unless `oneway:foot` says otherwise.
    """
    global _ROAD_GRAPH_CACHE
    if _ROAD_GRAPH_CACHE is not None:
        return _ROAD_GRAPH_CACHE

    pinned = _pinned(f"network_graph_{_SCHEMA}.json")
    if pinned:
        _ROAD_GRAPH_CACHE = _load_json(pinned, "road_graph", "pinned")
        return _ROAD_GRAPH_CACHE

    cache_path = CACHE_DIR / f"network_graph_{_SCHEMA}.json"
    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < ttl_seconds:
        _ROAD_GRAPH_CACHE = _load_json(cache_path, "road_graph", "cache")
        return _ROAD_GRAPH_CACHE

    b = NETWORK_BBOX
    query = (
        "[out:json][timeout:180];"
        f'way["highway"~"^({_NETWORK_HIGHWAYS})$"]'
        f"({b['lat_min']},{b['lon_min']},{b['lat_max']},{b['lon_max']});"
        "(._;>;);"
        "out body;"
    )

    def fetch():
        # Overpass's Apache front-end 406s a request with no Accept header and
        # httpx's default User-Agent - send both explicitly, same as geocode().
        # The public instance also returns 504 under load, often on a first
        # attempt and fine on the second, so a cold build retries before giving
        # up. A cache built under an older schema is never an acceptable
        # fallback - it lives under a different filename for that reason.
        last: Exception | None = None
        for attempt in range(3):
            try:
                response = httpx.post(
                    OVERPASS_URL,
                    data={"data": query},
                    headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                    timeout=240.0,
                )
                response.raise_for_status()
                return response.json()["elements"]
            except Exception as exc:  # noqa: BLE001 - retried, then re-raised
                last = exc
                time.sleep(5 * (attempt + 1))
        raise last  # type: ignore[misc]

    try:
        elements = fetch()
    except Exception:
        if cache_path.exists():
            _ROAD_GRAPH_CACHE = _load_json(cache_path, "road_graph", "cache")
            return _ROAD_GRAPH_CACHE
        raise

    # Node ids are kept as strings throughout, on both the first parse and every
    # cache reload - JSON object keys are always strings, and a graph that used
    # int ids fresh but str ids from cache would break equality checks in the
    # pathfinder depending on which path built it.
    coords: dict[str, dict] = {}
    ways: list[dict] = []
    for el in elements:
        if el["type"] == "node":
            point = {"lat": el["lat"], "lon": el["lon"]}
            # Kerbs and tactile paving sit on nodes, not ways: they describe a
            # crossing point rather than a stretch of footpath, which is also
            # why they cannot be folded into the edge attributes below.
            tags = el.get("tags") or {}
            for tag in _KEPT_NODE_TAGS:
                if tag in tags:
                    point[tag] = str(tags[tag])
            coords[str(el["id"])] = point
        elif el["type"] == "way" and _walkable(el.get("tags", {})):
            ways.append(el)
    del elements

    nodes: dict[str, dict] = {}
    edges: dict[str, list] = {}
    way_attrs: list[dict] = []
    attrs_index: dict[tuple, int] = {}
    for way in ways:
        refs = [str(r) for r in way.get("nodes", [])]
        tags = way.get("tags", {})
        highway = tags.get("highway", "unclassified")
        direction = _way_direction(tags, highway)

        kept = {tag: str(tags[tag]) for tag in _KEPT_WAY_TAGS if tag in tags}
        key = tuple(sorted(kept.items()))
        attrs_ix = attrs_index.get(key)
        if attrs_ix is None:
            attrs_ix = attrs_index[key] = len(way_attrs)
            way_attrs.append(kept)

        for a, b_id in zip(refs, refs[1:]):
            if a not in coords or b_id not in coords:
                continue  # a way can reference a node outside the query box
            nodes[a], nodes[b_id] = coords[a], coords[b_id]
            dist = haversine_m(coords[a]["lat"], coords[a]["lon"],
                               coords[b_id]["lat"], coords[b_id]["lon"])
            if direction in ("forward", "both"):
                edges.setdefault(a, []).append([b_id, dist, highway, attrs_ix])
            if direction in ("backward", "both"):
                edges.setdefault(b_id, []).append([a, dist, highway, attrs_ix])

    graph = {"nodes": nodes, "edges": edges, "way_attrs": way_attrs}
    cache_path.write_text(json.dumps(graph), encoding="utf-8")
    _LOADED_FROM["road_graph"] = (cache_path, "cache")
    _ROAD_GRAPH_CACHE = graph
    return graph

# ---------------------------------------------------------------------------
# Translink GTFS as a timetable index, used by the network agent's transit
# checks. Separate from gtfs_table() above, which hands back raw rows: this
# builds the derived structure the verifier actually queries, and pays for it
# once.
# ---------------------------------------------------------------------------

# Cut to TIMETABLE_BBOX, which is all of South East Queensland - not to the
# walking box. A stop this agent cannot walk to is still a stop it can be asked
# about: "is the 8:15 from Nerang real?" is a question with an answer whether or
# not a footpath route to Nerang would be meaningful.
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

    pinned = _pinned(f"gtfs_timetable_{_TIMETABLE_SCHEMA}.json")
    if pinned:
        _TIMETABLE_CACHE = _load_json(pinned, "timetable", "pinned")
        return _TIMETABLE_CACHE

    cache_path = CACHE_DIR / f"gtfs_timetable_{_TIMETABLE_SCHEMA}.json"
    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < ttl_seconds:
        _TIMETABLE_CACHE = _load_json(cache_path, "timetable", "cache")
        return _TIMETABLE_CACHE

    # Read through the archive at the snapshot TTL, not the daily one.
    archive_path = CACHE_DIR / "seq_gtfs.zip"
    b = TIMETABLE_BBOX
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
    _LOADED_FROM["timetable"] = (cache_path, "cache")
    _TIMETABLE_CACHE = table
    return table


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_info() -> dict:
    """Which files this process loaded its network and timetable from, fingerprinted.

    Printed at agent startup and recorded beside experiment results, so every
    number can be traced to the data that produced it. Hashing the files takes
    a moment, which is why it happens here, on request, and never on a tool
    call. A pinned file that no longer matches the committed manifest is
    reported rather than refused: the answers are still honest about their
    source, and the mismatch is visible wherever this is printed.
    """
    manifest_path = SNAPSHOT_DIR / "MANIFEST.json"
    manifest = (json.loads(manifest_path.read_text(encoding="utf-8")).get("files", {})
                if manifest_path.is_file() else {})
    info = {}
    for dataset, (path, source) in _LOADED_FROM.items():
        digest = _file_sha256(path)
        entry = {"source": source, "file": path.name, "sha256": digest[:16]}
        if source == "pinned":
            entry["matches_manifest"] = manifest.get(path.name, {}).get("sha256") == digest
        info[dataset] = entry
    return info
