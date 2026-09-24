"""
smoke_console.py - the console's model-routed path, end to end, as a check.

    python smoke_console.py                             # console at localhost:8080
    python smoke_console.py --url http://host:8080

Why this exists. On 23 Sep a ceiling on prompt length in llm.ask refused the
console's router prompt, every model-routed query quietly fell back to the
scripted plan, and nothing noticed for two days: the unit tests, the evaluation
and the conformance check all call tools directly, and none of them goes
through a model choosing tools from their descriptions. This does. It is the
nearest thing in the repository to the week 12 Orchestrator, and on 25 Sep it
is also what showed "Brisbane CBD" resolving to an office in West End.

It needs the stack running with an API key, and costs a few cents a run.

Assertions are made on what the TOOLS returned, never on the model's prose,
which varies from run to run. A router may pick a different tool on a
different day - plan_verified_route instead of validate_route, say - and that
is not a failure; returning the wrong fact is.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

# One expectation per network query, derived from the scenario ground truth.
# Each is (what should be true, test over the list of tool results).
EXPECT = {
    "net-named-places": (
        "a route of 5.0-5.7 km to the real city centre - 4,452 m meant 'Brisbane CBD' "
        "had resolved to an office in West End",
        lambda results: any(r.get("reachable") and 5000 <= (r.get("distance_m") or 0) <= 5700
                            for r in results)),
    "net-tradeoff": (
        "a network tool reported that nothing meets both the step-free and 7.25 km limits",
        lambda results: any(r.get("satisfiable") is False or r.get("valid") is False
                            for r in results)),
    "net-out-of-scope": (
        "no route was produced for Sydney",
        lambda results: not any(r.get("reachable") or r.get("valid") for r in results)),
    "net-verified-unsatisfiable": (
        "a network tool found the 800 m limit broken, and none called it met",
        lambda results: any(r.get("valid") is False or r.get("satisfiable") is False
                            for r in results)
        and not any(r.get("valid") is True for r in results)),
    "net-catch-a-false-route": (
        "a network tool measured the walk at over 4 km, exposing the 300 m claim",
        lambda results: any((r.get("walking_distance_m") or r.get("distance_m") or 0) >= 4000
                            for r in results)),
}


def post(url: str, payload: dict, timeout: float = 180) -> dict:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def check(run: dict) -> tuple[list[str], list[dict], list[str]]:
    """(problems, tool results, tools called) for one console run."""
    problems, results, called = [], [], []
    if run.get("mode") != "model":
        reason = next((s.get("text", "") for s in run["trace"] if s.get("kind") == "error"), "")
        problems.append(f"fell back to the scripted plan: {reason[:160]}")
    for step in run["trace"]:
        kind = step.get("kind")
        if kind == "error" and run.get("mode") == "model":
            problems.append(f"error step: {str(step.get('text'))[:160]}")
        if kind == "call":
            called.append(f"{step.get('agent')}.{step.get('tool')}")
            result = step.get("result")
            if isinstance(result, dict):
                results.append(result)
                # Specification section 5: every tool response cites its sources.
                if not result.get("sources"):
                    problems.append(f"{step.get('tool')} returned no sources")
    answers = [s.get("text") or "" for s in run["trace"] if s.get("kind") == "answer"]
    if not answers or not answers[-1].strip():
        problems.append("no final answer")
    elif answers[-1].lstrip().startswith("{"):
        problems.append("the final answer is raw JSON - the router's reply was not parsed")
    return problems, results, called


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end check of the console's model path.")
    parser.add_argument("--url", default="http://localhost:8080")
    args = parser.parse_args()

    with urllib.request.urlopen(f"{args.url}/api/catalogue", timeout=30) as response:
        catalogue = json.loads(response.read())
    queries = [q for q in catalogue["queries"] if q.get("domain") == "network"]
    if catalogue.get("model") in (None, "unconfigured"):
        print("No model is configured for the console; this check needs an API key.")
        return 1

    failed = flaky = 0
    started = time.time()
    for query in queries:
        # One retry. The failures this exists for - a prompt the router cannot
        # send, a place resolved to the wrong spot - happen every time. A router
        # that now and then answers a routing question without calling any tool
        # (about one run in six, on 25 Sep) is the router's noise, not this
        # agent's regression, and a check that went red on it would soon be
        # ignored. So a failure that clears on retry is reported - visibly - as
        # flaky, and only a failure that repeats fails the check.
        attempts = []
        for _ in range(2):
            attempts.append(attempt(args.url, query))
            if not attempts[-1][1]:
                break
        run, problems, called = attempts[-1]
        retried = bool(attempts[0][1])
        label = "FAIL" if problems else ("flaky" if retried else "ok")
        failed += bool(problems)
        flaky += retried and not problems
        print(f"{label:5} {query['id']:28} {run.get('mode'):8} "
              f"{run.get('ms', 0):>6} ms  {', '.join(called) or '(no tool called)'}")
        for shown_run, shown_problems, _called in attempts:
            for problem in shown_problems:
                print(f"        {problem}")
            if shown_problems:
                # What the router actually said, so a failure can be diagnosed
                # from this output alone.
                print(f"        answered: {final_answer(shown_run)[:240]!r}")

    print()
    print(f"{len(queries) - failed}/{len(queries)} network queries passed through "
          f"model routing in {time.time() - started:.0f}s"
          + (f"; {flaky} passed only on retry" if flaky else ""))
    return 1 if failed else 0


def final_answer(run: dict) -> str:
    answers = [s.get("text") or "" for s in run["trace"] if s.get("kind") == "answer"]
    return answers[-1] if answers else ""


def attempt(url: str, query: dict) -> tuple[dict, list[str], list[str]]:
    """Run one query through the console and judge it."""
    run = post(f"{url}/api/run", {"query_id": query["id"]})
    problems, results, called = check(run)
    expectation = EXPECT.get(query["id"])
    if expectation is None:
        problems.append("no expectation written for this query - add one to EXPECT")
    else:
        note, holds = expectation
        if not holds(results):
            problems.append(f"expected {note}")
    return run, problems, called


if __name__ == "__main__":
    sys.exit(main())
