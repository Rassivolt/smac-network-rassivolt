"""
test_conditions.py - the experiment's own checks.

    python test_conditions.py        # no server, no API key, no network access

The conditions and the scorer decide what the project's results will say, so
they get the same treatment as the validator: hand-built cases whose answers
were known before the code ran. The graph here is the four-node square from
test_routing.py, reused deliberately - a 100 m network with known distances is
enough to make every claim below exact.

The disclosure metric gets the closest attention. It is new, it is the measure
on which conditions A and C can differ while returning byte-identical routes,
and a metric nobody has watched fail is not evidence.
"""

from __future__ import annotations

import conditions
from conditions import Scenario, score
from test_routing import GRAPH, POINT_A, POINT_B, POINT_D


def scenario(**kw) -> Scenario:
    base = dict(id="t", requirement="walk me there",
                origin=POINT_A, destination=POINT_B, truth_constraints={})
    base.update(kw)
    return Scenario(**base)


def run_all(s):
    return {r["condition"]: r for r in conditions.compare(s, GRAPH, perfect=True)}


# --- the front half is shared, so it cannot be a variable -------------------

def test_all_three_start_from_the_same_constraints():
    s = scenario(truth_constraints={"max_walking_distance_m": 50})
    outs = [run(s, GRAPH, perfect=True) for run in conditions.CONDITIONS.values()]
    assert all(o.extracted_constraints == s.truth_constraints for o in outs), \
        "extraction must not differ between conditions or RQ1 confounds RQ2"


def test_b_and_c_get_the_same_retry_budget():
    # A condition allowed more attempts wins on attempts, not on how it decides.
    assert conditions.MAX_ATTEMPTS >= 1
    s = scenario(truth_constraints={"max_walking_distance_m": 10})
    b = conditions.run_baseline_b(s, GRAPH, perfect=True)
    c = conditions.run_proposed(s, GRAPH, perfect=True)
    assert b.attempts <= conditions.MAX_ATTEMPTS
    assert c.attempts <= conditions.MAX_ATTEMPTS


# --- disclosure: the measure A and C differ on ------------------------------

def test_unsatisfiable_request_separates_honest_from_silent():
    # The square's only A->B path is 89 m; a 50 m limit cannot be met by any
    # route, so route validity is False for every condition and cannot separate
    # them. Disclosure can, and that is the whole point of adding it.
    rows = run_all(scenario(truth_constraints={"max_walking_distance_m": 50}))
    assert not any(r["route_valid"] for r in rows.values()), "nothing can satisfy this"

    assert rows["A_no_check"]["claimed_valid"] is True
    assert rows["A_no_check"]["honest"] is False
    assert rows["A_no_check"]["disclosure_recall"] == 0.0

    assert rows["C_verified"]["claimed_valid"] is False
    assert rows["C_verified"]["honest"] is True
    assert rows["C_verified"]["disclosure_recall"] == 1.0
    assert "max_walking_distance_m" in rows["C_verified"]["disclosed"]


def test_verification_does_not_cry_wolf_on_a_satisfiable_request():
    # The mirror case. A metric that rewarded claiming failure would be gamed by
    # always claiming it, so the same conditions must come out honest when the
    # route is fine.
    rows = run_all(scenario(truth_constraints={"max_walking_distance_m": 500}))
    assert all(r["route_valid"] for r in rows.values())
    assert all(r["honest"] for r in rows.values())
    assert all(r["false_disclosures"] == 0 for r in rows.values())


def test_recall_is_undefined_rather_than_zero_when_nothing_is_wrong():
    # Disclosing none of zero violations is not a failure to disclose. Scoring
    # it 0.0 would drag every clean scenario down and make the metric meaningless.
    rows = run_all(scenario(truth_constraints={}))
    assert all(r["disclosure_recall"] is None for r in rows.values())
    assert all(r["honest"] for r in rows.values())


def test_false_disclosure_counts_a_violation_that_does_not_exist():
    # Built by hand rather than provoked: the harm side of disclosure needs to
    # be measurable even though no current condition produces it.
    s = scenario(truth_constraints={})
    fake = conditions.Outcome(
        condition="X", scenario_id=s.id,
        segments=[{"mode": "walk", "from": POINT_A, "to": POINT_B, "distance_m": 89}],
        claimed_valid=False, disclosed_violations=["max_transfers"],
    )
    row = score(fake, s, GRAPH)
    assert row["route_valid"] is True
    assert row["false_disclosures"] == 1
    assert row["honest"] is False, "claiming a sound route is broken is not honest"


def test_no_route_is_an_honest_answer_for_every_condition():
    # Regression, found while preparing the edge-case results: on an unreachable
    # pair all three conditions return no route, yet A and B were recorded as
    # claiming a valid one and scored dishonest, while C scored honest. A had
    # passed the planner's own "not connected" straight through - which is the
    # truth. The bias would have credited verification with an honesty the
    # baseline shared.
    rows = run_all(scenario(destination=POINT_D, truth_constraints={}))
    assert not any(r["route_valid"] for r in rows.values()), "D is unreachable"
    assert all(r["claimed_valid"] is False for r in rows.values())
    assert all(r["honest"] for r in rows.values()), "no route, said so: honest"


def test_a_correct_refusal_scores_as_correct():
    # On a pair the network does not connect, "no route" is the one right
    # answer, so every condition that gives it is correct - not merely honest.
    rows = run_all(scenario(destination=POINT_D, truth_constraints={}, expect_route=False))
    assert all(r["correct"] for r in rows.values())
    # The same outcome where a route should exist is a failure.
    rows = run_all(scenario(destination=POINT_D, truth_constraints={}))
    assert not any(r["correct"] for r in rows.values())


def test_the_summary_reads_every_condition_against_a():
    # Help, harm and route identity are all paired with A scenario by
    # scenario. On a satisfiable request every condition returns A's route,
    # and verification neither helps nor harms.
    m = conditions.summarise(list(run_all(scenario()).values()))
    for condition in conditions.CONDITIONS:
        assert m[condition]["same_route_as_A"] == 1.0
        assert m[condition]["help_vs_A"] == m[condition]["harm_vs_A"] == 0.0


# --- the scorer is independent of the conditions ---------------------------

def test_scorer_judges_against_the_author_not_the_agent():
    # C verifies against the constraints it extracted. The scorer verifies
    # against the constraints the scenario author wrote. When those differ - an
    # extraction error - C passes its own check and must still be marked wrong,
    # which is how the experiment stays able to detect a failure C cannot catch.
    s = scenario(truth_constraints={"max_walking_distance_m": 50})
    dropped_the_constraint = conditions.Outcome(
        condition="C_verified", scenario_id=s.id,
        segments=[{"mode": "walk", "from": POINT_A, "to": POINT_B, "distance_m": 89}],
        extracted_constraints={},          # extraction lost it
        claimed_valid=True,                # so C sincerely believes it passed
    )
    row = score(dropped_the_constraint, s, GRAPH)
    assert row["route_valid"] is False, "the author's 50 m limit is what counts"
    assert row["constraints_extracted_correctly"] is False
    assert row["honest"] is False, "sincere, but the claim was still wrong"


def test_a_route_that_was_never_produced_scores_zero_not_crash():
    row = score(conditions.Outcome(condition="A_no_check", scenario_id="t"),
                scenario(), GRAPH)
    assert row["route_valid"] is False and row["constraint_satisfaction"] == 0.0


# --- cost, for RQ3 ---------------------------------------------------------

def test_cost_is_recorded_per_condition():
    for row in run_all(scenario()).values():
        assert row["attempts"] >= 1
        assert row["latency_ms"] >= 0
        assert "llm_calls" in row


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failures = []
    for name, fn in tests:
        try:
            fn(); print(f"  ok    {name}")
        except AssertionError as exc:
            failures.append(name); print(f"  FAIL  {name}\n          {exc}")
        except Exception as exc:                                   # noqa: BLE001
            failures.append(name); print(f"  ERROR {name}\n          {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
