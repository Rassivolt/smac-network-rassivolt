"""
test_smoke_console.py - the console smoke check fails when it should.

    python test_smoke_console.py      # no stack, no key, no network

smoke_console.py only earns its place if it goes red on the failures it was
written for. These feed it fabricated console runs - one per failure seen in
the week 9 self-check - and assert that each one is caught, then that a good
run is not. A check that cannot fail is worse than no check, because it is
reported as a pass; that has happened in this repository twice already.

Also here: the console's salvage of a router reply whose JSON is broken, since
the smoke check is what flags an unparsed reply.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "application"))

import smoke_console

SOURCES = ["© OpenStreetMap contributors, ODbL 1.0"]


def run(mode="model", results=(), answer="The walk is 5.3 km.", error=None):
    trace = []
    if error:
        trace.append({"kind": "error", "text": error})
    for result in results:
        trace.append({"kind": "route", "agent": "network", "tool": "plan_verified_route"})
        trace.append({"kind": "call", "agent": "network", "tool": "plan_verified_route",
                      "result": result})
    trace.append({"kind": "answer", "text": answer})
    return {"mode": mode, "trace": trace, "ms": 10}


def route(distance, **extra):
    return {"reachable": True, "valid": True, "distance_m": distance, "sources": SOURCES, **extra}


def test_a_fallback_to_the_scripted_plan_is_a_failure():
    # 23 Sep: a prompt ceiling refused the router's prompt and every query fell
    # back to the scripted plan. The answers were fine; the path was broken.
    problems, _results, _called = smoke_console.check(run(
        mode="scripted", results=[route(5332)],
        error="Model routing failed, ran the scripted plan instead. prompt is 19422 "
              "characters against a limit of 8000"))
    assert any("scripted" in p for p in problems), problems


def test_a_confidently_wrong_distance_fails_the_expectation():
    # 25 Sep: "Brisbane CBD" resolved to an office in West End, and the router
    # reported 4,452 m. Every generic check passed; only the expectation catches it.
    _note, holds = smoke_console.EXPECT["net-named-places"]
    assert not holds([route(4452)])
    assert holds([route(5332)])


def test_a_raw_json_answer_is_a_failure():
    problems, _r, _c = smoke_console.check(run(
        results=[route(5332)],
        answer='{"action": "answer", "text": "It is 5.3 km.""}'))
    assert any("raw JSON" in p for p in problems), problems


def test_a_tool_response_without_sources_is_a_failure():
    unsourced = {"reachable": True, "distance_m": 5332}
    problems, _r, _c = smoke_console.check(run(results=[unsourced]))
    assert any("no sources" in p for p in problems), problems


def test_a_good_run_passes():
    problems, results, called = smoke_console.check(run(results=[route(5332)]))
    assert problems == [], problems
    assert called == ["network.plan_verified_route"]
    assert smoke_console.EXPECT["net-named-places"][1](results)


def test_the_expectations_cover_every_network_query_in_the_catalogue():
    # A query added to the console without an expectation would get only the
    # generic checks - it would pass however wrong its answer was.
    from queries import QUERIES
    network = {q["id"] for q in QUERIES if q.get("domain") == "network"}
    assert network <= set(smoke_console.EXPECT), network - set(smoke_console.EXPECT)


def test_a_broken_final_answer_is_salvaged_and_a_broken_tool_call_is_not():
    import ui
    seen = '{"action": "answer", "text": "No, the claim is false. It is 4452 m.""}'
    assert ui.parse_router_reply(seen) == {
        "action": "answer", "text": "No, the claim is false. It is 4452 m."}
    # A tool call's arguments cannot be recovered without guessing, so it is
    # not salvaged: calling a tool with guessed input is worse than an error.
    try:
        ui.parse_router_reply('{"action": "call", "tool": "x", "arguments": {"a": 1}')
    except ValueError:
        pass
    else:
        raise AssertionError("a broken tool call was accepted")


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
