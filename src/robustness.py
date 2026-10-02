"""
robustness.py - thirty unseen questions through the console's model router.

    python robustness.py                          # each question once, console at localhost:8080
    python robustness.py --repeats 3 --out ../usage/robustness.json
    python robustness.py --only edge-injection --only claim-gabba-kp-1km

What it measures. smoke_console.py checks the five catalogue questions the
console was built around. This asks the question those cannot answer: does the
agent hold up on questions nobody wrote for it - typed in lower case, in
Chinese or German, with typos, as a claim to check, as an injection attempt,
or about somewhere it does not cover? The questions are in
eval/unseen.network.json, with what each must and must not produce and the
reference figure behind it.

A run passes when the router reached a model decision, every tool result
cites its sources, there is a readable final answer, and the expectation holds
over what the TOOLS returned. The model's prose is never judged, apart from
answer_excludes for the injection case; it varies run to run, and the claim
this supports is about the facts the agent surfaces.

Not a regression check: no retry, because a router that sometimes fails is the
finding. A run that never reached the model - the console fell back to its
scripted plan, or the request itself failed - is reported as infrastructure and
kept out of the rate, since it measures the network between here and the API,
not the agent.

Needs the stack running with an API key. One pass costs well under a dollar.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
from pathlib import Path

from smoke_console import check, final_answer, post

CASES = Path(__file__).resolve().parent / "eval" / "unseen.network.json"


def field(result: dict, name: str):
    """The first of 'a|b' that the result carries, or None."""
    for key in name.split("|"):
        if key in result:
            return result[key]
    return None


def holds(result: dict, condition: dict) -> bool:
    """Whether every field of `condition` holds for one tool result."""
    for name, want in condition.items():
        value = field(result, name)
        if isinstance(want, dict) and ("min" in want or "max" in want):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            if value < want.get("min", -math.inf) or value > want.get("max", math.inf):
                return False
        elif isinstance(want, dict) and "contains" in want:
            if not isinstance(value, str) or want["contains"].lower() not in value.lower():
                return False
        elif value != want:
            return False
    return True


def judge(expect: dict, results: list[dict], answer: str) -> list[str]:
    """What the expectation found wrong, empty when it holds."""
    problems = []
    wanted = expect.get("any")
    if wanted and not any(holds(r, c) for r in results for c in wanted):
        problems.append(f"no tool result matched any of {json.dumps(wanted)}")
    for condition in expect.get("none", []):
        hit = next((r for r in results if holds(r, condition)), None)
        if hit is not None:
            shown = {k: hit[k] for k in ("reachable", "valid", "satisfiable", "distance_m",
                                         "walking_distance_m", "duration_s") if k in hit}
            problems.append(f"a tool result matched the forbidden {json.dumps(condition)}: "
                            f"{json.dumps(shown)}")
    for text in expect.get("answer_excludes", []):
        if text.lower() in answer.lower():
            problems.append(f"the final answer contains {text!r}")
    return problems


def run_case(url: str, case: dict) -> dict:
    """One question, one run, judged."""
    started = time.time()
    try:
        run = post(f"{url}/api/run", {"text": case["prompt"]})
    except Exception as exc:                                        # noqa: BLE001
        return {"id": case["id"], "outcome": "infrastructure",
                "problems": [f"request failed: {type(exc).__name__}: {exc}"],
                "tools": [], "answer": "", "seconds": round(time.time() - started, 1)}
    problems, results, called = check(run)
    answer = final_answer(run)
    if run.get("mode") != "model":
        outcome = "infrastructure"
    else:
        problems += judge(case["expect"], results, answer)
        outcome = "fail" if problems else "pass"
    routes = [{"tool": f"{s.get('agent')}.{s.get('tool')}", "arguments": s.get("arguments")}
              for s in run.get("trace", []) if s.get("kind") == "route"]
    return {"id": case["id"], "outcome": outcome, "problems": problems, "tools": called,
            "routes": routes, "answer": answer, "seconds": round(time.time() - started, 1)}


def wilson(passed: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval for a pass rate. Thirty runs is not many."""
    if total == 0:
        return 0.0, 0.0
    p = passed / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return max(0.0, centre - half), min(1.0, centre + half)


def main() -> int:
    parser = argparse.ArgumentParser(description="Unseen questions through the model router.")
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--only", action="append", help="run just this case id (repeatable)")
    parser.add_argument("--out", help="write every run, routes and answers included, as JSON")
    args = parser.parse_args()

    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    with urllib.request.urlopen(f"{args.url}/api/catalogue", timeout=30) as response:
        model = json.loads(response.read()).get("model")
    if model in (None, "unconfigured"):
        print("No model is configured for the console; this needs an API key.")
        return 1
    print(f"router model: {model}; {len(cases)} questions x {args.repeats}\n")

    runs: list[dict] = []
    started = time.time()
    for case in cases:
        for _ in range(args.repeats):
            outcome = run_case(args.url, case)
            outcome["category"] = case["category"]
            runs.append(outcome)
            label = {"pass": "ok", "fail": "FAIL", "infrastructure": "infra"}[outcome["outcome"]]
            print(f"{label:5} {case['id']:32} {outcome['seconds']:>5}s  "
                  f"{', '.join(outcome['tools']) or '(no tool called)'}")
            for problem in outcome["problems"]:
                print(f"        {problem}")
            if outcome["outcome"] != "pass":
                print(f"        answered: {outcome['answer'][:240]!r}")

    counted = [r for r in runs if r["outcome"] != "infrastructure"]
    passed = sum(r["outcome"] == "pass" for r in counted)
    print("\nby category (runs that reached the model):")
    for category in dict.fromkeys(c["category"] for c in cases):
        mine = [r for r in counted if r["category"] == category]
        print(f"  {category:14} {sum(r['outcome'] == 'pass' for r in mine)}/{len(mine)}")
    # Which side failed. A router that answers without calling a tool, or says
    # no to the whole question, is the router's failure; a tool result that
    # breaks the expectation is the agent's. The two need different fixes.
    failed = [r for r in counted if r["outcome"] == "fail"]
    silent = sum(not r["tools"] for r in failed)
    if failed:
        print(f"\nof {len(failed)} failure(s): {silent} with no tool called (the router), "
              f"{len(failed) - silent} after tools were called (look at what they returned)")
    low, high = wilson(passed, len(counted))
    infra = len(runs) - len(counted)
    print(f"\nrobustness {passed}/{len(counted)} = "
          f"{passed / len(counted) if counted else 0:.2f} [95% {low:.2f}-{high:.2f}]"
          + (f"; {infra} run(s) never reached the model and are not counted" if infra else "")
          + f"; {time.time() - started:.0f}s")

    if args.out:
        Path(args.out).write_text(json.dumps({"model": model, "repeats": args.repeats,
                                              "runs": runs}, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
