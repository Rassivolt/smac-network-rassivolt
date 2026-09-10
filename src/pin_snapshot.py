"""
pin_snapshot.py - freeze the network and timetable the ground truth was measured on.

    python src/pin_snapshot.py             # pin what is in the cache now
    python src/pin_snapshot.py --verify    # check the pinned files against the manifest

Every distance, arrival time and service in eval/tasks.network.json was derived
from one road graph and one timetable index. The agent's cache refreshes those
on a timer, and a container or a deployment builds its own copies from whatever
OpenStreetMap and Translink publish that day - so without a pin the same code
gives different answers in different places and at different times, and the
evaluation starts failing for reasons that have nothing to do with the agent.

Pinning copies the two derived files into snapshot/ at the repository root and
writes snapshot/MANIFEST.json with their fingerprints. data.py prefers a pinned
file over the cache whenever one is present. The manifest is committed; the
~125 MB of data it describes is not, so anywhere else the agent runs needs the
files alongside - the Docker stack mounts snapshot/ read-only, a deployment
would copy it - and --verify says whether they are the right ones.

Re-pinning moves the ground truth under every case derived from the old
snapshot, so it refuses to overwrite a different pin without --force.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time

import data

FILES = (f"network_graph_{data._GRAPH_SCHEMA}.json", "gtfs_timetable.json")
MANIFEST = data.SNAPSHOT_DIR / "MANIFEST.json"


def _stamp(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+10:00", time.localtime(seconds))


def verify() -> int:
    if not MANIFEST.is_file():
        print(f"no manifest at {MANIFEST}")
        return 1
    bad = 0
    for name, expected in json.loads(MANIFEST.read_text(encoding="utf-8"))["files"].items():
        path = data.SNAPSHOT_DIR / name
        if not path.is_file():
            status = "MISSING"
        elif data._file_sha256(path) != expected["sha256"]:
            status = "DIFFERENT"
        else:
            status = "ok"
        bad += status != "ok"
        print(f"  {status:9} {name}  {expected['sha256'][:16]}")
    return 1 if bad else 0


def pin(force: bool) -> int:
    missing = [name for name in FILES if not (data.CACHE_DIR / name).is_file()]
    if missing:
        print(f"not in the cache yet: {', '.join(missing)} - start the network agent once to build them")
        return 1

    digests = {name: data._file_sha256(data.CACHE_DIR / name) for name in FILES}
    if MANIFEST.is_file():
        pinned = {n: f["sha256"] for n, f in
                  json.loads(MANIFEST.read_text(encoding="utf-8"))["files"].items()}
        if pinned == digests:
            print("already pinned - the cache holds exactly the pinned data")
            return verify()
        if not force:
            print("snapshot/ already pins different data. Re-pinning moves the ground truth "
                  "under every case derived from it; pass --force if that is intended.")
            return 1

    data.SNAPSHOT_DIR.mkdir(exist_ok=True)
    for name in FILES:
        shutil.copy2(data.CACHE_DIR / name, data.SNAPSHOT_DIR / name)   # keeps the build time

    graph_file, table_file = (data.SNAPSHOT_DIR / name for name in FILES)
    graph = json.loads(graph_file.read_text(encoding="utf-8"))
    table = json.loads(table_file.read_text(encoding="utf-8"))
    manifest = {
        "pinned": _stamp(time.time()),
        "files": {name: {"sha256": digests[name], "bytes": (data.SNAPSHOT_DIR / name).stat().st_size}
                  for name in FILES},
        "road_graph": {
            "schema": data._GRAPH_SCHEMA,
            "nodes": len(graph["nodes"]),
            "built": _stamp(graph_file.stat().st_mtime),
            "bbox": data.NETWORK_BBOX,
            "source": data.OSM_LICENCE,
        },
        "timetable": {
            "gtfs_archive": table["snapshot"],
            "feed": table["feed"],
            "source": data.GTFS_LICENCE,
        },
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"pinned to {data.SNAPSHOT_DIR}")
    print(f"  road graph  {manifest['road_graph']['nodes']:,} nodes, built {manifest['road_graph']['built']}")
    print(f"  timetable   GTFS archive {table['snapshot']['sha256']}, "
          f"feed {table['feed']['start']}-{table['feed']['end']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Pin the data the ground truth was measured on.")
    parser.add_argument("--verify", action="store_true", help="check snapshot/ against its manifest")
    parser.add_argument("--force", action="store_true", help="replace a different existing pin")
    args = parser.parse_args()
    return verify() if args.verify else pin(args.force)


if __name__ == "__main__":
    raise SystemExit(main())
