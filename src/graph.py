"""
graph.py - the walking network as columnar arrays.

The network is 1.43 million nodes and 3.1 million edges. Held as the
dictionaries json.load returns - a string id per node, a dict per coordinate
pair, a tuple per edge - it took 1.6 GB, which a 1 GB UQCloud zone cannot hold.
The same graph as arrays takes 83 MB: coordinates in two float arrays, edges
in compressed sparse row form (`offs[i]:offs[i+1]` indexes node i's edges in
`tgt`, `wt`, `hw` and `at`), and the nearest-node grid as ranges into one
array of node indices.

Nodes are numbered by their OpenStreetMap id in string order, so comparing two
indices gives the same answer as comparing the two id strings did. Dijkstra
breaks ties between equally distant nodes by that comparison, and keeping it
is what keeps every route identical to the dictionary graph's, node for node.
Edges keep their order within each node, and the grid keeps nodes in their
original order within each cell, for the same reason: both decide which of two
equal candidates wins.

`Graph.from_dict` converts the dictionary form - the JSON the snapshot was
pinned as, and the small hand-built graphs the tests use. `save` and `load`
read and write one binary file, so a deployment never parses the JSON at all.
"""

from __future__ import annotations

import array
import json
import math
import sys
from pathlib import Path
from typing import Any

# The nearest-node grid. 0.005 degrees is about 550 m north-south.
GRID_CELL_DEG = 0.005

_MAGIC = b"SMACGRAPH1\n"

# (name, typecode) of every array a Graph holds, in file order.
_ARRAYS = (("lat", "d"), ("lon", "d"), ("offs", "I"), ("tgt", "I"), ("wt", "I"),
           ("hw", "B"), ("at", "h"), ("cell_y", "i"), ("cell_x", "i"),
           ("cell_start", "I"), ("cell_end", "I"), ("cell_nodes", "I"), ("order", "I"))


def write_columns(path: Path, magic: bytes, header: dict, columns: list) -> None:
    """Write `magic`, a JSON header and named arrays to one file.

    Deterministic: the same content writes the same bytes on any little-endian
    machine, so a file rebuilt from the pinned JSON on a laptop and on the
    deployment zone has the same sha256.
    """
    header = dict(header, byteorder=sys.byteorder, arrays=[])
    blobs, offset = [], 0
    for name, arr in columns:
        raw = arr.tobytes()
        header["arrays"].append({"name": name, "typecode": arr.typecode, "itemsize": arr.itemsize,
                                 "length": len(arr), "offset": offset})
        pad = (-len(raw)) % 8
        blobs.append(raw + bytes(1) * pad)
        offset += len(raw) + pad
    # Key order kept, not sorted: a timetable's stops are searched in the order
    # they were stored, and the first of two equally near stops wins.
    head = json.dumps(header, separators=(",", ":")).encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(magic)
        fh.write(len(head).to_bytes(8, "little"))
        fh.write(head)
        for blob in blobs:
            fh.write(blob)


def read_columns(path: Path, magic: bytes) -> tuple[dict, dict]:
    """The header and the named arrays of a file written by write_columns."""
    raw = memoryview(Path(path).read_bytes())
    if bytes(raw[:len(magic)]) != magic:
        raise ValueError(f"{path} is not a {magic.decode().strip()} file")
    at = len(magic)
    size = int.from_bytes(raw[at:at + 8], "little")
    header = json.loads(bytes(raw[at + 8:at + 8 + size]))
    base = at + 8 + size
    columns = {}
    for spec in header["arrays"]:
        arr = array.array(spec["typecode"])
        if arr.itemsize != spec["itemsize"]:
            raise ValueError(f"{spec['name']}: item size {arr.itemsize} here, "
                             f"{spec['itemsize']} in {path}")
        start = base + spec["offset"]
        arr.frombytes(raw[start:start + spec["length"] * spec["itemsize"]])
        if header["byteorder"] != sys.byteorder:
            arr.byteswap()
        columns[spec["name"]] = arr
    return header, columns


class Graph:
    """A walking graph as arrays. Nodes are integer indices 0..len-1."""

    __slots__ = ("ids", "order", "lat", "lon", "offs", "tgt", "wt", "hw", "at", "highways",
                 "way_attrs", "node_tags", "cells", "cell_nodes", "cell_y", "cell_x",
                 "cell_start", "cell_end", "source")

    def __len__(self) -> int:
        return len(self.lat)

    # -- identity --------------------------------------------------------------
    def node_id(self, i: int) -> str:
        """The OpenStreetMap id (or a test graph's name) of node i."""
        return str(self.ids[i])

    def index(self, node_id: str) -> int | None:
        """The index of a node id, or None. A binary search over ids in string
        order; for tests and diagnostics, never a tool call's hot path."""
        key = str(node_id)
        lo, hi = 0, len(self.ids)
        while lo < hi:
            mid = (lo + hi) // 2
            if str(self.ids[mid]) < key:
                lo = mid + 1
            else:
                hi = mid
        return lo if lo < len(self.ids) and str(self.ids[lo]) == key else None

    # -- edges -----------------------------------------------------------------
    def edges(self, i: int) -> range:
        """Positions of node i's edges in tgt / wt / hw / at, in stored order."""
        return range(self.offs[i], self.offs[i + 1])

    def first_edge(self, a: int, b: int) -> int | None:
        """The first edge a->b in stored order, as the dictionary graph's lookups found it."""
        tgt = self.tgt
        for k in range(self.offs[a], self.offs[a + 1]):
            if tgt[k] == b:
                return k
        return None

    def highway(self, k: int) -> str:
        return self.highways[self.hw[k]]

    def attrs(self, k: int) -> dict:
        ix = self.at[k]
        return self.way_attrs[ix] if ix >= 0 and self.way_attrs else {}

    def tags(self, i: int) -> dict:
        """kerb / tactile_paving on node i, where OpenStreetMap states them."""
        return self.node_tags.get(i, {})

    def cell(self, key: tuple[int, int]) -> range:
        found = self.cells.get(key)
        return range(found[0], found[1]) if found else range(0)

    # -- building --------------------------------------------------------------
    @classmethod
    def from_dict(cls, graph: dict) -> "Graph":
        nodes, edges = graph["nodes"], graph["edges"]
        ids = sorted(nodes)
        ix = {node_id: i for i, node_id in enumerate(ids)}
        n = len(ids)
        g = cls()
        # Real ids are numeric and stored as 64-bit integers; a test graph's
        # names ("A", "B") stay strings. Only exact round-trips become numbers,
        # so node_id() always gives back the id it was given.
        numeric = all(isinstance(x, str) and x.isdigit() and str(int(x)) == x for x in ids)
        g.ids = array.array("q", (int(x) for x in ids)) if numeric else list(ids)
        # The nodes in the order the dictionary held them, for anything that
        # samples or iterates nodes and must do so exactly as before.
        g.order = array.array("I", (ix[node_id] for node_id in nodes))
        g.lat = array.array("d", bytes(8 * n))
        g.lon = array.array("d", bytes(8 * n))
        g.node_tags = {}
        for node_id, pos in nodes.items():
            i = ix[node_id]
            g.lat[i], g.lon[i] = pos["lat"], pos["lon"]
            extra = {k: v for k, v in pos.items() if k not in ("lat", "lon")}
            if extra:
                g.node_tags[i] = extra
        g.highways = sorted({str(e[2]) for bucket in edges.values() for e in bucket})
        code = {h: c for c, h in enumerate(g.highways)}
        if len(g.highways) > 255:
            raise ValueError("more than 255 way types - widen hw beyond one byte")
        g.offs = array.array("I", [0]) * (n + 1)
        g.tgt, g.wt = array.array("I"), array.array("I")
        g.hw, g.at = array.array("B"), array.array("h")
        for i, node_id in enumerate(ids):
            for e in edges.get(node_id, ()):
                g.tgt.append(ix[e[0]])
                g.wt.append(int(e[1]))
                g.hw.append(code[str(e[2])])
                g.at.append(int(e[3]) if len(e) > 3 and e[3] is not None else -1)
            g.offs[i + 1] = len(g.tgt)
        g.way_attrs = list(graph.get("way_attrs") or [])
        members: dict[tuple[int, int], list[int]] = {}
        for node_id, pos in nodes.items():                 # the dict's own order
            key = (math.floor(pos["lat"] / GRID_CELL_DEG), math.floor(pos["lon"] / GRID_CELL_DEG))
            members.setdefault(key, []).append(ix[node_id])
        g.cell_nodes = array.array("I")
        g.cell_y, g.cell_x = array.array("i"), array.array("i")
        g.cell_start, g.cell_end = array.array("I"), array.array("I")
        for (cy, cx), nodes_in_cell in members.items():
            g.cell_y.append(cy); g.cell_x.append(cx)
            g.cell_start.append(len(g.cell_nodes))
            g.cell_nodes.extend(nodes_in_cell)
            g.cell_end.append(len(g.cell_nodes))
        g._index_cells()
        g.source = {}
        return g

    def _index_cells(self) -> None:
        self.cells = {(self.cell_y[c], self.cell_x[c]): (self.cell_start[c], self.cell_end[c])
                      for c in range(len(self.cell_y))}

    # -- file ------------------------------------------------------------------
    def save(self, path: Path, source: dict | None = None) -> None:
        """One binary file (write_columns), with `source` naming the JSON it came from."""
        numeric = isinstance(self.ids, array.array)
        header = {
            "ids": "int64" if numeric else list(self.ids),
            "highways": self.highways,
            "way_attrs": self.way_attrs,
            "node_tags": [[i, self.node_tags[i]] for i in sorted(self.node_tags)],
            "source": source or {},
        }
        columns = [(name, getattr(self, name)) for name, _code in _ARRAYS]
        if numeric:
            columns.append(("ids", self.ids))
        write_columns(path, _MAGIC, header, columns)

    @classmethod
    def load(cls, path: Path) -> "Graph":
        header, columns = read_columns(path, _MAGIC)
        g = cls()
        for name, arr in columns.items():
            setattr(g, name, arr)
        if header["ids"] != "int64":
            g.ids = header["ids"]
        g.highways = header["highways"]
        g.way_attrs = header["way_attrs"]
        g.node_tags = {int(i): tags for i, tags in header["node_tags"]}
        g.source = header.get("source", {})
        g._index_cells()
        return g


_FROM_DICT: dict[int, tuple[dict, Graph]] = {}


def as_graph(graph: Any) -> Graph | None:
    """A Graph from either form, converting a dictionary graph once.

    The agent loads a Graph directly. The tests and older callers pass the
    dictionary form; converting each on every call would be wasteful, so the
    last few conversions are kept, with the dictionary held so its id cannot
    be reused by another object.
    """
    if graph is None or isinstance(graph, Graph):
        return graph
    cached = _FROM_DICT.get(id(graph))
    if cached is not None and cached[0] is graph:
        return cached[1]
    converted = Graph.from_dict(graph)
    if len(_FROM_DICT) >= 16:
        _FROM_DICT.pop(next(iter(_FROM_DICT)))
    _FROM_DICT[id(graph)] = (graph, converted)
    return converted
