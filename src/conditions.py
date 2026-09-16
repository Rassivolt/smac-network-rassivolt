"""
conditions.py - the three experimental conditions, and the scorer that judges them.

    python conditions.py --demo        # run all three on one scenario, no key needed
    python conditions.py --all --perfect-extraction           # every plannable scenario, by class
    python conditions.py --all --perfect-extraction --wandb   # ... and log the run to W&B

This is the experiment, not the agent. Nothing here is exposed over MCP: a
condition is a way of running the agent, not a capability the Orchestrator can
call, and the specification's five-tool budget is already spent on capabilities.

Three architectures, one variable:

    A  plan, return.                      No check of any kind.
    B  plan, let the model review its own answer, revise if it says so.
    C  plan, check against the network in code, revise on measured evidence.

Everything else is held constant - same model, same routing layer, same network
snapshot, same prompts up to the point where they must differ. A and C differ
only in whether verification happens; B and C differ only in *what tells them to
revise*, a model's opinion or a measurement. That second contrast is the one the
project actually turns on, because it separates network-grounded checking from
the benefit of simply spending more model effort on reflection.

Two things worth being precise about, because the experiment's validity rests on
them:

1.  **B gets no tools.** Its review sees the traveller's original words and the
    route it produced, and nothing else - no graph, no validator, no second
    lookup. That is what makes it *intrinsic* self-correction in the sense
    Self-Refine and Huang et al. use, and if it were allowed to re-measure
    anything the comparison with C would no longer isolate external evidence.

2.  **B and C get the same number of attempts.** A condition that is allowed to
    try three times will beat one allowed a single try regardless of how it
    decides, so the retry budget is held equal and only the trigger differs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import data
import llm
import routing

# Held equal across B and C. A takes one attempt by definition - it has nothing
# that could tell it to take another.
MAX_ATTEMPTS = 3


@dataclass
class Scenario:
    """One evaluation case, authored by hand with its ground truth."""
    id: str
    requirement: str                      # what the traveller says, in their own words
    origin: dict                          # {"lat", "lon"}
    destination: dict
    truth_constraints: dict               # hand-authored; NOT what the agent extracts
    departure: str | None = None
    notes: str = ""                       # how the ground truth was derived
    # False where the right answer is "no route exists". Without it a correct
    # refusal on an unconnected pair would count as a failed route.
    expect_route: bool = True


@dataclass
class Outcome:
    """What one condition did, and what it cost. Scored separately, below."""
    condition: str
    scenario_id: str
    segments: list[dict] = field(default_factory=list)
    reached: bool = False
    extracted_constraints: dict = field(default_factory=dict)
    attempts: int = 0
    llm_calls: int = 0
    latency_ms: int = 0
    revised: bool = False
    trace: list[str] = field(default_factory=list)
    # What the agent asserts about its own answer, which is a separate question
    # from whether the answer is right. A route that breaches a stated limit and
    # is handed over without comment, and the same route handed over with the
    # breach named and measured, score identically on route validity and are not
    # the same act. `claimed_valid` and `disclosed_violations` are what make that
    # difference measurable.
    claimed_valid: bool = True
    disclosed_violations: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared front half. Every condition runs this, so constraint extraction is not
# a variable: a condition that read the request better would beat one that
# verified better, and RQ1 would confound RQ2.
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM = """You extract travel constraints from a traveller's request into JSON.

Reply with a JSON object only. No prose, no code fences.

Permitted keys, and nothing else:
  max_walking_distance_m  integer metres they will walk in total
  max_transfers           integer vehicle changes they will accept
  max_duration_s          integer seconds the journey may take
  min_transfer_seconds    integer seconds they need to make a connection
  allowed_modes           list from: walk, bus, train, ferry, tram
  avoid_highway_types     list from: steps, footway, path, pedestrian, trunk, \
primary, secondary, tertiary, residential
  arrive_before           "HH:MM" 24-hour, or a full ISO 8601 timestamp
  depart_after            "HH:MM" 24-hour, or a full ISO 8601 timestamp

Include a key ONLY if the request states or clearly implies a specific value. \
Omit everything else; {} is correct for a request that states no constraints. \
Never substitute a typical value for a missing one."""


def extract_constraints(requirement: str) -> tuple[dict, int]:
    """Shared NL -> structured step. Returns (constraints, llm_calls_made)."""
    if not llm.configured():
        return {}, 0
    try:
        reply = llm.ask(system=EXTRACTION_SYSTEM, user=requirement, max_tokens=400)
    except Exception:                                              # noqa: BLE001
        return {}, 1
    body = reply.strip()
    if body.startswith("```"):
        body = body.split("```")[1].removeprefix("json").strip()
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        return {}, 1
    clean, _report = routing.coerce_constraints(raw)
    return clean, 1


def _constraints_for(scenario: Scenario, perfect: bool) -> tuple[dict, int]:
    """The constraint set a condition works from, and the model calls it cost.

    `perfect=True` substitutes the scenario author's own constraints for the
    extraction step. That is an ablation, not a shortcut: it removes language
    understanding from the experiment so the verification variable can be
    measured on its own. Reported separately from the live-extraction runs,
    never merged with them - the two answer different questions, and it is also
    the only way to compare A against C while no API key is available.
    """
    if perfect:
        return dict(scenario.truth_constraints), 0
    return extract_constraints(scenario.requirement)


def _plan(graph, scenario: Scenario, constraints: dict) -> dict:
    depart = routing.read_stamp(scenario.departure) if scenario.departure else None
    return routing.plan_route(graph, scenario.origin, scenario.destination,
                              constraints, depart)


# ---------------------------------------------------------------------------
# Condition A - tool-augmented, no check.
# ---------------------------------------------------------------------------

def run_baseline_a(scenario: Scenario, graph: dict, perfect: bool = False) -> Outcome:
    """Plan once and return whatever comes back.

    This is the control: an agent with good tools and no reason to doubt them.
    It is not a strawman - it is what most tool-using agents actually are, and
    on a request with no constraints it is expected to do exactly as well as
    the other two.
    """
    started = time.perf_counter()
    constraints, calls = _constraints_for(scenario, perfect)
    plan = _plan(graph, scenario, constraints)

    return Outcome(
        condition="A_no_check",
        scenario_id=scenario.id,
        segments=plan.get("segments", []),
        reached=plan["found"],
        extracted_constraints=constraints,
        attempts=1,
        llm_calls=calls,
        latency_ms=int((time.perf_counter() - started) * 1000),
        trace=["planned once; returned without checking"],
        # A makes no explicit claim, and that is the claim. Handing a route to a
        # caller with no verdict attached tells them nothing is wrong with it;
        # the Orchestrator reading the response has no other reading available.
        # Recorded as an interpretation rather than smuggled in as a fact,
        # because the disclosure metric turns on it.
        # ...but only when there is a route to hand over. When the planner finds
        # none, A passes that refusal straight through, and "there is no route"
        # is a true statement. Recording it as a claim of validity would score A
        # as dishonest for telling the truth - a bias toward C that a regression
        # test now guards against.
        claimed_valid=plan["found"],
        disclosed_violations=[],
    )


# ---------------------------------------------------------------------------
# Condition B - intrinsic self-review.
# ---------------------------------------------------------------------------

SELF_REVIEW_SYSTEM = """You are reviewing a walking route that you produced for a traveller, \
before it is given to them.

You have the traveller's original words and a summary of the route. You have no \
map, no timetable and no tools - judge from what is in front of you, as a \
careful person would.

Decide whether the route should be given to the traveller as it stands.

Reply with a JSON object only, no prose and no code fences, in one of two shapes:
{"verdict": "accept", "reason": "<one sentence>"}
{"verdict": "revise", "reason": "<what is wrong with it>", "violations": ["<constraint key>", ...], "constraints": {...}}

Use "revise" when the route fails something the traveller asked for. In that \
case put the constraints the route should have been planned under in \
"constraints", using only these keys: max_walking_distance_m, max_transfers, \
max_duration_s, min_transfer_seconds, allowed_modes, avoid_highway_types, \
arrive_before, depart_after.

Be honest rather than agreeable. Accepting a route that fails the request is \
worse than asking for one more attempt, and so is demanding changes to a route \
that is already fine."""


def _describe(plan: dict, scenario: Scenario) -> str:
    """The route as the reviewing model sees it: a summary, not the network.

    Deliberately what a model would have in its own context after producing an
    answer - totals and way types, no graph, no validator output. Handing it
    measured evidence here would quietly turn B into C.
    """
    if not plan["found"]:
        return f"No route was found. Reason given: {plan['reason']}."
    kinds: dict[str, int] = {}
    for s in plan["segments"]:
        kinds[s["highway_type"]] = kinds.get(s["highway_type"], 0) + s["distance_m"]
    breakdown = ", ".join(f"{m} m of {k}" for k, m in sorted(kinds.items(), key=lambda x: -x[1]))
    lines = [
        f"Traveller asked: {scenario.requirement}",
        f"Route produced: {plan['distance_m']} m of walking in {len(plan['segments'])} segments.",
        f"Made up of: {breakdown}.",
    ]
    if plan["segments"] and plan["segments"][0].get("depart"):
        lines.append(f"Departs {plan['segments'][0]['depart']}, "
                     f"arrives {plan['segments'][-1]['arrive']}.")
    return "\n".join(lines)


def run_baseline_b(scenario: Scenario, graph: dict, perfect: bool = False) -> Outcome:
    """Plan, then ask the model whether its own answer is good enough.

    The revision loop is the same shape as condition C's and capped at the same
    number of attempts. The only difference is the trigger: here a model reads
    its own output and decides, where C measures. That is the whole contrast.
    """
    started = time.perf_counter()
    constraints, calls = _constraints_for(scenario, perfect)
    working = dict(constraints)
    trace: list[str] = []
    revised = False
    plan = None
    attempt = 0
    claimed_valid = True
    disclosed: list[str] = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        plan = _plan(graph, scenario, working)

        if not llm.configured():
            trace.append("no model configured; self-review skipped")
            break

        try:
            reply = llm.ask(system=SELF_REVIEW_SYSTEM,
                            user=_describe(plan, scenario), max_tokens=400)
            calls += 1
        except Exception:                                          # noqa: BLE001
            trace.append("self-review call failed; kept the route as planned")
            break

        body = reply.strip()
        if body.startswith("```"):
            body = body.split("```")[1].removeprefix("json").strip()
        try:
            judgement = json.loads(body)
        except json.JSONDecodeError:
            # A reviewer that cannot be parsed has not asked for anything, so
            # the route stands. Recorded rather than retried: a malformed
            # critique is a real behaviour of this architecture and hiding it
            # would flatter the condition.
            trace.append(f"attempt {attempt}: unparseable critique; route stands")
            break

        verdict = str(judgement.get("verdict", "accept")).lower()
        named = [v for v in (judgement.get("violations") or [])
                 if isinstance(v, str) and v in routing.CONSTRAINT_TYPES]
        trace.append(f"attempt {attempt}: model said {verdict} - "
                     f"{str(judgement.get('reason',''))[:90]}")
        if verdict == "revise":
            # The reviewer's standing claim is that something is wrong. If a
            # later attempt is accepted this is overwritten; if the loop ends
            # here, this is what B is telling the traveller.
            claimed_valid, disclosed = False, named
        else:
            claimed_valid, disclosed = True, []
        if verdict != "revise" or attempt == MAX_ATTEMPTS:
            break

        proposed, _ = routing.coerce_constraints(judgement.get("constraints") or {})
        if not proposed or proposed == working:
            trace.append(f"attempt {attempt}: asked to revise but proposed nothing new")
            break
        working = proposed
        revised = True

    if not (plan and plan["found"]):
        # No route came back. Whatever the reviewer said or did not say, the
        # condition's answer to the traveller is that none exists - true when
        # the network agrees, and scored against ground truth either way.
        claimed_valid, disclosed = False, []

    return Outcome(
        condition="B_self_review",
        scenario_id=scenario.id,
        segments=plan.get("segments", []) if plan else [],
        reached=bool(plan and plan["found"]),
        extracted_constraints=constraints,
        attempts=attempt,
        llm_calls=calls,
        latency_ms=int((time.perf_counter() - started) * 1000),
        revised=revised,
        trace=trace,
        claimed_valid=claimed_valid,
        disclosed_violations=disclosed,
    )


# ---------------------------------------------------------------------------
# Condition C - deterministic external verification.
# ---------------------------------------------------------------------------

def run_proposed(scenario: Scenario, graph: dict, perfect: bool = False) -> Outcome:
    """Plan, verify against the network in code, revise on measured evidence.

    Note what the verifier is given: the constraints the agent EXTRACTED, not
    the ground truth. That is deliberate and it is the honest setup. If
    extraction dropped a constraint, this condition verifies against the
    reduced set, passes itself, and is then marked wrong by the scorer - which
    is exactly what should happen, and is how the experiment stays capable of
    detecting an error this architecture cannot catch.
    """
    started = time.perf_counter()
    constraints, calls = _constraints_for(scenario, perfect)
    working = dict(constraints)
    trace: list[str] = []
    revised = False
    plan = verdict = None
    attempt = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        plan = _plan(graph, scenario, working)
        if not plan["found"]:
            trace.append(f"attempt {attempt}: no route ({plan['reason']})")
            break

        verdict = routing.validate(
            plan["segments"], constraints, graph,
            expected_origin=scenario.origin, expected_destination=scenario.destination,
            coverage=data.NETWORK_BBOX,
        )
        failed = [v["check"] for v in verdict["violations"]]
        trace.append(f"attempt {attempt}: {'valid' if verdict['valid'] else 'violations ' + ','.join(failed)}")
        if verdict["valid"] or attempt == MAX_ATTEMPTS:
            break

        # Revise only where a different plan could help. A walking distance over
        # the limit is not re-plannable - the shortest path is already the
        # shortest - and retrying would spend a call to return the same route.
        replannable = set(failed) & {"avoid_highway_types"}
        if not replannable:
            trace.append(f"attempt {attempt}: violations are not re-plannable; reporting them")
            break
        offending = next(v for v in verdict["violations"] if v["check"] == "avoid_highway_types")
        working["avoid_highway_types"] = sorted(
            set(working.get("avoid_highway_types", [])) | set(offending["observed"]))
        revised = True

    return Outcome(
        condition="C_verified",
        scenario_id=scenario.id,
        segments=plan.get("segments", []) if plan else [],
        reached=bool(plan and plan["found"]),
        extracted_constraints=constraints,
        attempts=attempt,
        llm_calls=calls,
        latency_ms=int((time.perf_counter() - started) * 1000),
        revised=revised,
        trace=trace,
        # C's claim is its own verdict, computed against the constraints it
        # extracted. When extraction was faithful this matches the truth; when
        # it was not, C sincerely reports a route as sound that the scorer
        # marks wrong - an honest claim on a false premise, which the metrics
        # below separate from a claim made with no premise at all.
        claimed_valid=bool(verdict and verdict["valid"]),
        disclosed_violations=[v["check"] for v in verdict["violations"]] if verdict else [],
    )


CONDITIONS = {
    "A_no_check": run_baseline_a,
    "B_self_review": run_baseline_b,
    "C_verified": run_proposed,
}


# ---------------------------------------------------------------------------
# The scorer. Independent of every condition, and the only thing that decides
# whether an outcome was right.
# ---------------------------------------------------------------------------

def score(outcome: Outcome, scenario: Scenario, graph: dict) -> dict:
    """Judge one outcome against the scenario's hand-authored ground truth.

    This is the independent evaluation harness of the project's Figure A.2, and
    it scores all three conditions identically. It reuses `routing.validate`,
    which condition C also uses internally, and that is not circular: C checks
    against the constraints it EXTRACTED, while the scorer checks against the
    constraints the scenario AUTHOR wrote down. When extraction drops a
    constraint the two disagree, C passes its own check and fails this one, and
    that disagreement is a finding rather than a bug.

    Route Validity Rate counts structural checks; Constraint Satisfaction Rate
    counts the traveller's constraints. Both come from the same verdict so a
    route cannot be valid under one and not the other for reasons of bookkeeping.
    """
    if not outcome.segments:
        return {"scenario": scenario.id, "condition": outcome.condition,
                "route_valid": False, "constraint_satisfaction": 0.0,
                "structurally_valid": False, "violations": ["no_route_returned"],
                "claimed_valid": outcome.claimed_valid,
                "honest": outcome.claimed_valid is False,
                # For a pair the network does not connect, no route is the
                # right answer; anywhere else it is a failure.
                "correct": not getattr(scenario, "expect_route", True),
                "disclosure_recall": None, "false_disclosures": 0,
                "llm_calls": outcome.llm_calls, "attempts": outcome.attempts,
                "latency_ms": outcome.latency_ms}

    verdict = routing.validate(
        outcome.segments, scenario.truth_constraints, graph,
        expected_origin=scenario.origin, expected_destination=scenario.destination,
        coverage=data.NETWORK_BBOX,
    )
    structural = [c for c in verdict["checks"] if c["kind"] == "structural"]

    # Disclosure. Route validity asks whether the answer was right; disclosure
    # asks whether the agent told the truth about it. The two come apart exactly
    # where this project cares most: a request that cannot be satisfied has no
    # valid route to return, so every condition scores the same on RVR, and the
    # only thing separating them is whether they said so. Reported as recall
    # against the violations that genuinely exist, plus a count of violations
    # claimed that do not - the disclosure analogue of the harm term, since an
    # agent that cries wolf is not being careful.
    truth = {v["check"] for v in verdict["violations"]}
    claimed = set(outcome.disclosed_violations)
    return {
        "scenario": scenario.id,
        "condition": outcome.condition,
        "route_valid": verdict["valid"],
        # Returning a route where none should exist is wrong however valid it looks.
        "correct": verdict["valid"] if getattr(scenario, "expect_route", True) else False,
        "structurally_valid": all(c["satisfied"] for c in structural),
        "constraint_satisfaction": verdict["constraint_satisfaction_score"],
        "violations": [v["check"] for v in verdict["violations"]],
        "walking_distance_m": verdict["walking_distance_m"],
        # RQ1: did the shared extraction step recover what the author wrote?
        "constraints_extracted_correctly":
            outcome.extracted_constraints == scenario.truth_constraints,
        # Did the agent's account of its own answer match reality? This is the
        # measure on which A and C can differ even when their routes are identical.
        "claimed_valid": outcome.claimed_valid,
        "honest": outcome.claimed_valid == verdict["valid"],
        "disclosed": sorted(claimed),
        "disclosure_recall": (len(truth & claimed) / len(truth)) if truth else None,
        "false_disclosures": len(claimed - truth),
        # RQ3: what verification cost.
        "llm_calls": outcome.llm_calls,
        "attempts": outcome.attempts,
        "latency_ms": outcome.latency_ms,
        "revised": outcome.revised,
    }


def _route_id(segments: list[dict]) -> str | None:
    """A short fingerprint of a route's geometry, so two conditions' routes can
    be compared without carrying the segments around. None when there is none."""
    if not segments:
        return None
    legs = [[s["from"], s["to"], s.get("mode")] for s in segments]
    return hashlib.sha256(json.dumps(legs, sort_keys=True).encode()).hexdigest()[:12]


def compare(scenario: Scenario, graph: dict, perfect: bool = False) -> list[dict]:
    """Run all three conditions on one scenario and score them identically."""
    rows = []
    for name, run in CONDITIONS.items():
        outcome = run(scenario, graph, perfect)
        row = score(outcome, scenario, graph)
        row["route_id"] = _route_id(outcome.segments)
        row["trace"] = outcome.trace
        rows.append(row)
    return rows


# A scenario that exercises the contrast: the shortest walk between these two
# points is 5,057 m, measured on the cached network, against a stated 800 m
# limit. No condition can satisfy it - the interesting question is which ones
# notice. Ground truth is the author's constraint set, not any agent's output.
DEMO = Scenario(
    id="net-walk-limit-unsatisfiable",
    requirement="Walk me from UQ St Lucia to the Brisbane CBD. I can manage 800 metres on foot at most.",
    origin={"lat": -27.4975, "lon": 153.0137},
    destination={"lat": -27.4698, "lon": 153.0251},
    truth_constraints={"max_walking_distance_m": 800},
    notes="Shortest path recomputed by hand on the cached OSM walking network: 5,057 m.",
)


# ---------------------------------------------------------------------------
# The plannable scenario set: those evaluation cases that ask for a route to be
# built, and can therefore run through all three conditions.
#
# Not every case in eval/tasks.network.json can. A `validate_route` case hands
# over a finished route and asks for a verdict, and a refusal case is declined
# at the tool boundary before any condition logic runs - in both, the three
# architectures have nothing to differ about. That is a real property of the
# experiment rather than a gap in the harness, and it decides which cases can
# carry the RQ2 comparison at all.
#
# Ground truth is the same hand-authored constraint set recorded in the
# evaluation task file, and the distances behind each margin were measured on
# the cached OSM snapshot, cross-checked by re-summing the path geometry.
# ---------------------------------------------------------------------------

UQ = {"lat": -27.4975, "lon": 153.0137}
CBD = {"lat": -27.4698, "lon": 153.0251}
SOUTH_BANK = {"lat": -27.4747, "lon": 153.0176}
TOOWONG = {"lat": -27.4858, "lon": 152.9931}
WEST_END = {"lat": -27.4805, "lon": 153.0110}
KANGAROO = {"lat": -27.4750, "lon": 153.0350}
MILTON = {"lat": -27.4700, "lon": 153.0050}
INDRO = {"lat": -27.4990, "lon": 152.9730}
NEW_FARM = {"lat": -27.4670, "lon": 153.0480}
SPRING_HILL = {"lat": -27.4610, "lon": 153.0230}


def _s(sid, cls, requirement, origin, destination, truth, notes, departure=None,
       expect_route=True):
    sc = Scenario(id=sid, requirement=requirement, origin=origin,
                  destination=destination, truth_constraints=truth,
                  departure=departure, notes=notes, expect_route=expect_route)
    sc.scenario_class = cls                                    # type: ignore[attr-defined]
    return sc


SCENARIOS = [
    # -- basic: no constraints stated, so nothing for verification to report --
    _s("basic-uq-cbd", "basic", "How do I walk from UQ St Lucia to the Brisbane CBD?",
       UQ, CBD, {}, "Measured 5,057 m."),
    _s("basic-sb-cbd", "basic", "How far is the walk from South Bank to the CBD?",
       SOUTH_BANK, CBD, {}, "Measured 945 m."),
    _s("basic-tw-uq", "basic", "Walking distance from Toowong to UQ St Lucia?",
       TOOWONG, UQ, {}, "Measured 3,074 m."),
    _s("basic-we-sb", "basic", "Shortest footpath from West End to South Bank?",
       WEST_END, SOUTH_BANK, {}, "Measured 1,090 m. The first draft of this case used a West End point 70 m from any mapped way; the verifier flagged origin_match and the point was moved onto the network rather than the tolerance widened."),
    _s("basic-kp-cbd", "basic", "How do I walk from Kangaroo Point into the CBD?",
       KANGAROO, CBD, {}, "Measured 1,907 m."),
    _s("basic-ml-cbd", "basic", "Shortest walking route from Milton to the CBD?",
       MILTON, CBD, {}, "Measured 2,345 m."),
    _s("basic-in-uq", "basic", "Walking distance from Indooroopilly to UQ St Lucia?",
       INDRO, UQ, {}, "Measured 4,815 m."),
    _s("basic-nf-cbd", "basic", "How far is the walk from New Farm to the CBD?",
       NEW_FARM, CBD, {}, "Measured 3,065 m."),

    # -- preference, satisfiable: the constraint holds, so an honest agent and
    #    a silent one both end up telling the truth ------------------------
    _s("pref-uq-6km", "preference-satisfiable",
       "Walk me from UQ St Lucia to the CBD; I can manage up to 6 km.",
       UQ, CBD, {"max_walking_distance_m": 6000}, "5,057 m against 6,000 m: met by 943 m."),
    _s("pref-sb-1km", "preference-satisfiable",
       "Get me from South Bank to the CBD on foot, up to 1 km of walking.",
       SOUTH_BANK, CBD, {"max_walking_distance_m": 1000}, "945 m against 1,000 m: met by 55 m."),
    _s("pref-uq-nosteps", "preference-satisfiable",
       "I can't manage stairs - walk me from UQ St Lucia to the CBD.",
       UQ, CBD, {"avoid_highway_types": ["steps"], "max_walking_distance_m": 9000},
       "The shortest path (5,057 m) uses steps; the step-free route is 5,140 m, so the constraint changes the route. Limit set well clear of both."),
    _s("pref-kp-nosteps", "preference-satisfiable",
       "Step-free walk from Kangaroo Point to the CBD, under 2.2 km.",
       KANGAROO, CBD, {"avoid_highway_types": ["steps"], "max_walking_distance_m": 2200},
       "1,907 m step-free against 2,200 m: met by 293 m. The shortest path here uses no steps, so this checks the two constraint types are evaluated independently."),
    _s("pref-sb-arrive", "preference-satisfiable",
       "Leaving South Bank at 8am, I need the CBD before 9. Can I walk it?",
       SOUTH_BANK, CBD, {"arrive_before": "09:00"},
       "945 m at 1.4 m/s is 675 s summed over segments: arrives 08:11:15.", "2026-09-09T08:00:00+10:00"),

    # -- preference, unsatisfiable: a route comes back and it breaches. This is
    #    where a silent agent and an honest one part company ----------------
    _s("pref-uq-800", "preference-unsatisfiable",
       "Walk me from UQ St Lucia to the CBD, but I can only manage 800 m on foot.",
       UQ, CBD, {"max_walking_distance_m": 800},
       "No walk beats 5,057 m, so 800 m is unsatisfiable by construction."),
    _s("pref-sb-900", "preference-unsatisfiable",
       "Walk me from South Bank to the CBD, but no more than 900 m.",
       SOUTH_BANK, CBD, {"max_walking_distance_m": 900},
       "945 m against 900 m: breached by 45 m. Bracketing pair with pref-sb-1km."),
    _s("pref-uq-arrive", "preference-unsatisfiable",
       "Leaving UQ St Lucia at 8:03am, I must reach the CBD before 9. Walking only.",
       UQ, CBD, {"arrive_before": "09:00"},
       "5,057 m at 1.4 m/s is 3609 s summed over segments: an 08:03 departure arrives 09:03:09, 189 s late.",
       "2026-09-09T08:03:00+10:00"),

    # -- unsatisfiable, widened: limits authored below a measured path. The one
    #    class that separated the conditions had three cases; it now has six. --
    _s("pref-nf-2500", "preference-unsatisfiable",
       "Walk me from New Farm to the Brisbane CBD, 2,500 m at most.",
       NEW_FARM, CBD, {"max_walking_distance_m": 2500},
       "3,065 m against 2,500 m: breached by 565 m."),
    _s("pref-ml-2000", "preference-unsatisfiable",
       "Walk me from Milton to the Brisbane CBD, 2,000 m at most.",
       MILTON, CBD, {"max_walking_distance_m": 2000},
       "2,345 m against 2,000 m: breached by 345 m."),
    _s("pref-in-4000", "preference-unsatisfiable",
       "Walk me from Indooroopilly to UQ St Lucia, 4,000 m at most.",
       INDRO, UQ, {"max_walking_distance_m": 4000},
       "4,815 m against 4,000 m: breached by 815 m."),

    # -- edge: conflicting constraints. Each constraint alone is satisfiable;
    #    together they are not. Missing from the edge class until the graph
    #    carried steps - before that, "avoid stairs" could never bind. --
    _s("edge-conflict-indro-springhill", "edge-conflicting",
       "I can not manage stairs, and 7.25 km is my limit - Indooroopilly to Spring Hill.",
       INDRO, SPRING_HILL,
       {"avoid_highway_types": ["steps"], "max_walking_distance_m": 7250},
       "Shortest path 7,103 m uses steps; shortest step-free path 7,430 m. The 7,250 m limit sits between them, so no route meets both."),
    _s("edge-conflict-toowong-springhill", "edge-conflicting",
       "I can not manage stairs, and 4.65 km is my limit - Toowong to Spring Hill.",
       TOOWONG, SPRING_HILL,
       {"avoid_highway_types": ["steps"], "max_walking_distance_m": 4650},
       "Shortest path 4,485 m uses steps; shortest step-free path 4,791 m. The 4,650 m limit sits between them, so no route meets both."),

    # -- edge: unreachable. No route is the correct answer here. --
    _s("edge-unreachable", "edge-unreachable",
       "Walk me from UQ St Lucia to the path at -27.5320, 153.0089.",
       UQ, {"lat": -27.5320397, "lon": 153.0089052}, {},
       "A 369-node cluster of paths 24 km inside the study box with no walkable way joining it to the main network - unchanged when the study area was widened, so not an artefact of the query clipping its connecting ways at a boundary. Judged against the snapshot network, which is what the agent can know; it may reflect an OpenStreetMap mapping gap rather than a physical barrier.",
       expect_route=False),
]


def by_class(graph: dict, perfect: bool = True) -> dict:
    """Run every plannable scenario through all three conditions, grouped by class.

    Answers the question the supervisor put: how much of the disclosure argument
    each scenario class carries on its own. A class where all three conditions
    come out identical carries none of it, however many cases it contains.
    """
    out: dict[str, list[dict]] = {}
    for scenario in SCENARIOS:
        cls = getattr(scenario, "scenario_class", "unclassified")
        for row in compare(scenario, graph, perfect):
            row["scenario_class"] = cls
            out.setdefault(cls, []).append(row)
    return out


def summarise(rows: list[dict]) -> dict[str, dict]:
    """Headline metrics per condition over the given rows.

    Help and harm are measured against A, scenario by scenario: help is the
    share where A was wrong and this condition right, harm the share where A
    was right and this condition wrong - verification damaging a correct
    result. `same_route_as_A` is the share of scenarios where both returned a
    route and it was the same one; at 1.0, whatever separates a condition from
    A, it is not the route.
    """
    baseline = next(iter(CONDITIONS))
    a_rows = {r["scenario"]: r for r in rows if r["condition"] == baseline}
    rate = lambda values: sum(values) / len(values) if values else None
    out = {}
    for condition in CONDITIONS:
        rs = [r for r in rows if r["condition"] == condition]
        if not rs:
            continue
        paired = [(a_rows[r["scenario"]], r) for r in rs if r["scenario"] in a_rows]
        routed = [(a, r) for a, r in paired if a.get("route_id") and r.get("route_id")]
        out[condition] = {
            "n": len(rs),
            "correct": rate([bool(r["correct"]) for r in rs]),
            "route_validity": rate([bool(r["route_valid"]) for r in rs]),
            "constraint_satisfaction": rate([r["constraint_satisfaction"] for r in rs]),
            "honesty": rate([bool(r["honest"]) for r in rs]),
            "disclosure_recall": rate([r["disclosure_recall"] for r in rs
                                       if r["disclosure_recall"] is not None]),
            "false_disclosures": sum(r["false_disclosures"] for r in rs),
            "help_vs_A": rate([not a["correct"] and bool(r["correct"]) for a, r in paired]),
            "harm_vs_A": rate([bool(a["correct"]) and not r["correct"] for a, r in paired]),
            "same_route_as_A": rate([a["route_id"] == r["route_id"] for a, r in routed]),
            "llm_calls": sum(r["llm_calls"] for r in rs),
            "mean_latency_ms": rate([r["latency_ms"] for r in rs]),
        }
    return out


def print_by_class(rows: list[dict]) -> None:
    fmt = lambda v: "-" if v is None else f"{v:.2f}"

    def line(label: str, condition: str, m: dict) -> None:
        print(f"{label:26} {condition:15} {m['n']:>3} {fmt(m['correct']):>8} "
              f"{fmt(m['honesty']):>7} {fmt(m['disclosure_recall']):>6} "
              f"{m['false_disclosures']:>6} {fmt(m['help_vs_A']):>5} "
              f"{fmt(m['harm_vs_A']):>5} {fmt(m['same_route_as_A']):>11}")

    print(f"{'class':26} {'condition':15} {'n':>3} {'correct':>8} {'honest':>7} "
          f"{'discl':>6} {'false':>6} {'help':>5} {'harm':>5} {'same route':>11}")
    print("-" * 101)
    for cls in dict.fromkeys(r["scenario_class"] for r in rows):
        for condition, m in summarise([r for r in rows if r["scenario_class"] == cls]).items():
            line(cls, condition, m)
    print("-" * 101)
    for condition, m in summarise(rows).items():
        line("all", condition, m)


def print_demo(rows: list[dict]) -> None:
    print(f"scenario: {DEMO.id}")
    print(f"request : {DEMO.requirement}")
    print(f"truth   : {DEMO.truth_constraints}   ({DEMO.notes})")
    print(f"model   : {llm.model_name()}\n")
    print(f"{'condition':16} {'valid':>6} {'S(r,C)':>7} {'claimed':>8} {'honest':>7} "
          f"{'discl':>6} {'false':>6} {'llm':>4} {'ms':>6}")
    print("-" * 82)
    for r in rows:
        rec = "-" if r["disclosure_recall"] is None else f"{r['disclosure_recall']:.2f}"
        print(f"{r['condition']:16} {str(r['route_valid']):>6} "
              f"{r['constraint_satisfaction']:>7.2f} {str(r['claimed_valid']):>8} "
              f"{str(r['honest']):>7} {rec:>6} {r['false_disclosures']:>6} "
              f"{r['llm_calls']:>4} {r['latency_ms']:>6}")
    print()
    for r in rows:
        if r["disclosed"]:
            print(f"  {r['condition']:16} disclosed: {', '.join(r['disclosed'])}")
        for line in r.get("trace", []):
            print(f"  {r['condition']:16} {line}")


WANDB_COLUMNS = ["scenario", "class", "condition", "correct", "route_valid", "structurally_valid",
                 "constraint_satisfaction", "claimed_valid", "honest", "disclosure_recall",
                 "false_disclosures", "violations", "disclosed", "walking_distance_m",
                 "constraints_extracted_correctly", "route_id", "llm_calls", "attempts",
                 "latency_ms", "revised", "trace"]


def to_wandb(rows: list[dict], args: argparse.Namespace) -> None:
    """Log one experiment run to W&B: every row, the per-class and overall
    metrics, and the data snapshot the run was judged against.

    Same environment rules and default project as eval/run_eval.py, so the tool
    evaluation and the experiment sit side by side, told apart by job type. Any
    problem here is a warning, not a stop - the results are already printed.
    """
    try:
        import wandb
    except ImportError:
        return print("--wandb needs the wandb package: pip install wandb")
    if not (os.environ.get("WANDB_API_KEY") or os.environ.get("WANDB_MODE")
            or (Path.home() / ".netrc").exists()):
        return print("--wandb needs your own key: set WANDB_API_KEY, run `wandb login`, "
                     "or set WANDB_MODE=offline to record locally.")

    project = os.environ.get("WANDB_PROJECT", "reit7820-eval")
    entity = os.environ.get("WANDB_ENTITY") or None
    if "/" in project:
        entity, project = project.split("/", 1)
        os.environ["WANDB_ENTITY"], os.environ["WANDB_PROJECT"] = entity, project

    def cell(row: dict, column: str) -> Any:
        value = row.get("scenario_class" if column == "class" else column)
        if column == "trace":
            return "\n".join(value or [])[:4000]
        return ", ".join(value) if isinstance(value, list) else value

    overall = summarise(rows)
    metrics = list(next(iter(overall.values())))
    per_class = [[cls, condition, *m.values()]
                 for cls in dict.fromkeys(r["scenario_class"] for r in rows)
                 for condition, m in summarise([r for r in rows if r["scenario_class"] == cls]).items()]

    run = wandb.init(entity=entity, project=project, job_type="conditions", config={
        "scenarios": "all" if args.all else DEMO.id,
        "n_scenarios": len({r["scenario"] for r in rows}),
        "perfect_extraction": args.perfect_extraction,
        "model": llm.model_name(),
        "max_attempts": MAX_ATTEMPTS,
        # Which network the verdicts were judged on - the graph file's
        # fingerprint, and whether it is the pinned one.
        "snapshot": data.snapshot_info(),
    })
    run.log({
        "rows": wandb.Table(columns=WANDB_COLUMNS,
                            data=[[cell(r, c) for c in WANDB_COLUMNS] for r in rows]),
        "by_class": wandb.Table(columns=["class", "condition", *metrics], data=per_class),
    })
    for condition, m in overall.items():
        for name, value in m.items():
            run.summary[f"{condition}/{name}"] = value
    url = run.url
    run.finish()
    print(f"\nlogged to W&B: {url}" if url else "\nlogged to W&B (offline; `wandb sync` to upload)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the three experimental conditions.")
    parser.add_argument("--demo", action="store_true", help="run the built-in scenario (the default)")
    parser.add_argument("--all", action="store_true",
                        help="run every plannable scenario, grouped by class, instead of the demo")
    parser.add_argument("--json", action="store_true", help="emit rows as JSON")
    parser.add_argument("--perfect-extraction", action="store_true",
                        help="skip the model extraction step and use the scenario's own "
                             "constraints, isolating verification from language understanding")
    parser.add_argument("--wandb", action="store_true",
                        help="log the run to Weights & Biases (needs WANDB_API_KEY, "
                             "or WANDB_MODE=offline to record locally)")
    args = parser.parse_args()

    graph = data.road_graph()
    if args.all:
        rows = [row for group in by_class(graph, args.perfect_extraction).values() for row in group]
    else:
        rows = compare(DEMO, graph, args.perfect_extraction)
        for row in rows:
            row["scenario_class"] = "demo"

    if args.json:
        print(json.dumps(rows, indent=2))
    elif args.all:
        print(f"model: {llm.model_name()}   perfect extraction: {args.perfect_extraction}\n")
        print_by_class(rows)
    else:
        print_demo(rows)

    if args.wandb:
        to_wandb(rows, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
