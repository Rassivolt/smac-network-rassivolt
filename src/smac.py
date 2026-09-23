"""
smac.py - shared plumbing for every REIT7820 agent.

You do not need to edit this file. You should read it once, though: every
function here exists because the specification requires something, and using
them is what makes your agent pass the Week 9 handshake test.
"""

from __future__ import annotations

import functools
import inspect
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FuturesTimeout
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable

# Identity

# The seven domains are fixed by the specification. The kit ships skeletons for
# the first three, but this must accept every name the spec permits - rejecting
# a conformant agent here would fail you at startup for something the spec
# explicitly allows.
DOMAINS = "charging|pt|policy|equity|network|weather|custom"
AGENT_NAME_RE = re.compile(rf"^smac-({DOMAINS})-[a-z0-9]+(-[a-z0-9]+)*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def check_identity(agent_name: str, version: str) -> None:
    """Fail loudly at startup rather than quietly at the handshake test."""
    if not AGENT_NAME_RE.match(agent_name):
        raise ValueError(
            f"AGENT_NAME {agent_name!r} does not match the required pattern:\n"
            "  smac-{domain}-{slot}, lowercase and hyphenated\n"
            "  domain must be one of: charging pt policy equity network weather custom\n"
            "  e.g. smac-charging-reliability"
        )
    if not SEMVER_RE.match(version):
        raise ValueError(f"VERSION {version!r} must be semantic, e.g. '1.0.0'")


# Response envelope

def respond(payload: dict[str, Any], sources: Iterable[str]) -> dict[str, Any]:
    """Return a tool result with the mandatory `sources` array attached.

    Every tool response MUST cite the dataset or API behind the answer.
    Returning through this function is the easiest way to never forget.

        return respond({"chargers": [...]}, sources=["OpenChargeMap (ODbL)"])
    """
    sources = [s for s in sources if s]
    if not sources:
        raise ValueError(
            "Every response needs at least one source. "
            "Cite the dataset or API that backs this answer."
        )
    return {**payload, "sources": sources}


def out_of_scope(reason: str, sources: Iterable[str] = ()) -> dict[str, Any]:
    """Say honestly that a query is outside your coverage.

    Do NOT invent an answer for a question you cannot serve. Behaviour under
    out-of-scope queries is part of the Showcase rubric, and the Orchestrator
    can route to another agent if you tell it plainly that you cannot help.
    """
    return {"status": "out_of_scope", "reason": reason, "sources": list(sources)}


# Safety wrapper

# The specification's limit is 45s and a call MUST return or error inside it.
# Timing out exactly at 45s and then building an error message returns after
# it, so the wrapper gives up at 40 and leaves five seconds to answer in.
TOOL_BUDGET_SECONDS = 40.0
SPEC_LIMIT_SECONDS = 45.0

# One line per tool call: what was asked, how long it took, how it ended.
# Beside this file by default; under Docker it points into a mounted directory,
# because a log inside a container is gone the moment the container is.
CALL_LOG = Path(os.environ.get("CALL_LOG") or Path(__file__).with_name("calls.jsonl"))


def _shape(value: Any) -> Any:
    """What an argument was, without repeating what it said.

    Numbers travel as themselves - a coordinate is the first thing worth
    knowing when a route comes back wrong, and there is nothing sensitive in
    one. Strings travel as a length: they are untrusted caller input, and a
    place name, a requirement or a prompt injection all belong in a log the
    same way, which is not at all.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return f"<str {len(value)}>"
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in list(value.items())[:12]}
    if isinstance(value, (list, tuple)):
        return f"<{type(value).__name__} {len(value)}>"
    return f"<{type(value).__name__}>"


def _log_call(tool: str, arguments: dict, seconds: float, outcome: str, detail: str) -> None:
    """Record one call. Never raises: bookkeeping must not fail a tool call.

    KNOWN GAP. This sees only calls that reach the tool. A call whose arguments
    fail the MCP layer's own schema validation - a string where a list belongs,
    a missing required field - is rejected before `guard` runs and leaves no
    line here, which is the opposite of convenient: those are exactly the calls
    worth reading after something went wrong at the Showcase. The client's
    error is still returned to the caller, so nothing is hidden from THEM;
    it is hidden from us. Loosening the schemas to catch them in our own code
    would be the wrong fix - the schema is what the Orchestrator reads.
    """
    record = {
        "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": tool,
        "ms": round(seconds * 1000),
        "outcome": outcome,
        # Only what the caller actually supplied. The MCP layer passes every
        # optional parameter explicitly, so keeping the empties would bury the
        # two values that mattered under seven that did not.
        "args": {k: _shape(v) for k, v in arguments.items()
                 if v not in (None, "", (), [], {})},
    }
    if detail:
        record["detail"] = detail
    try:
        # stdout first, so `docker compose logs -f` is useful live even when
        # the log file cannot be written.
        print(f"[call] {tool} {outcome} {record['ms']}ms"
              + (f" ({detail})" if detail else ""), flush=True)
        CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CALL_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + chr(10))
    except OSError:
        pass

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="smac-tool")


def guard(fn):
    """Wrap a tool so it can never crash the server or blow the time budget.

    Put it *under* the @mcp.tool() decorator:

        @mcp.tool()
        @guard
        def my_tool(...): ...

    Three things happen:
      1. Any exception becomes a readable MCP tool error instead of a crash.
      2. Anything still running at 40s is abandoned with a clear timeout error,
         so you stay inside the 45s budget.
      3. Your docstring is cleaned up, so the indentation of a multi-line
         docstring does not leak into the description the Orchestrator reads.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        started, outcome, detail = time.monotonic(), "ok", ""
        try:
            result = _POOL.submit(fn, *args, **kwargs).result(timeout=TOOL_BUDGET_SECONDS)
            # A refusal is not a failure, and reading the logs after a Showcase
            # the difference is the whole question: a tool that refused told
            # the Orchestrator something true about its coverage.
            if isinstance(result, dict) and result.get("status") == "out_of_scope":
                outcome = "refused"
            return result
        except _FuturesTimeout:
            outcome, detail = "timeout", f"{TOOL_BUDGET_SECONDS:.0f}s"
            raise RuntimeError(
                f"{fn.__name__} did not finish within {TOOL_BUDGET_SECONDS:.0f}s. "
                f"Cache your data or narrow the query - the spec limit is "
                f"{SPEC_LIMIT_SECONDS:.0f}s."
            ) from None
        except Exception as exc:  # noqa: BLE001 - deliberate: never crash
            outcome, detail = "error", type(exc).__name__
            raise RuntimeError(f"{fn.__name__} failed: {type(exc).__name__}: {exc}") from None
        finally:
            # Bind rather than read kwargs: a caller may pass positionally, and
            # an argument log that silently empties itself is worse than none.
            try:
                bound = inspect.signature(fn).bind_partial(*args, **kwargs).arguments
            except TypeError:
                bound = dict(kwargs)
            _log_call(fn.__name__, bound, time.monotonic() - started, outcome, detail)

    if wrapper.__doc__:
        wrapper.__doc__ = inspect.cleandoc(wrapper.__doc__)
    return wrapper


# Data conventions. Use these rather than hand-rolling them.

# South East Queensland, generously bounded: Noosa in the north, the NSW
# border in the south, Toowoomba in the west, the coast in the east.
SEQ_BBOX = {"lat_min": -28.30, "lat_max": -26.00, "lon_min": 151.50, "lon_max": 153.60}

BRISBANE_TZ = timezone(timedelta(hours=10))  # AEST year-round, no daylight saving


def in_seq(lat: float, lon: float) -> bool:
    """True if a WGS84 point is inside the theme's SEQ coverage area."""
    return (
        SEQ_BBOX["lat_min"] <= lat <= SEQ_BBOX["lat_max"]
        and SEQ_BBOX["lon_min"] <= lon <= SEQ_BBOX["lon_max"]
    )


def now_brisbane() -> str:
    """Current time as ISO 8601 with timezone, e.g. 2026-08-08T15:04:05+10:00."""
    return datetime.now(BRISBANE_TZ).isoformat(timespec="seconds")


def money(amount: float | int | str | Decimal) -> str:
    """AUD as a decimal string: money(0.5) -> '0.50'.

    This is the convention students most often get wrong. The spec wants a
    string, not a float - floats cannot represent cents exactly.
    """
    return str(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def metres(value: float) -> int:
    """Distances are integer metres."""
    return int(round(value))


def seconds(value: float) -> int:
    """Durations are integer seconds."""
    return int(round(value))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Great-circle distance between two WGS84 points, in metres."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return metres(2 * r * math.asin(math.sqrt(a)))
