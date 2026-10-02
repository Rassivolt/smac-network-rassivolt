"""The robustness runner's judging, without a stack or a key.

    python test_robustness.py
"""

from __future__ import annotations

import json
from pathlib import Path

from robustness import CASES, holds, judge, wilson


def test_a_range_needs_a_number_and_reads_the_first_field_present():
    assert holds({"walking_distance_m": 1816}, {"distance_m|walking_distance_m": {"min": 1600}})
    assert not holds({"distance_m": 900}, {"distance_m|walking_distance_m": {"min": 1600}})
    # A boolean is not a distance, and a missing field is not zero.
    assert not holds({"distance_m": True}, {"distance_m": {"max": 2000}})
    assert not holds({}, {"distance_m": {"max": 2000}})


def test_every_field_of_a_condition_must_hold():
    plan = {"valid": True, "constraints_checked": 0, "distance_m": 1975}
    # A pram route planned WITHOUT the step restriction is not the step-free one.
    assert not holds(plan, {"valid": True, "constraints_checked": {"min": 1}})
    assert holds({**plan, "constraints_checked": 1}, {"valid": True, "constraints_checked": {"min": 1}})


def test_any_none_and_answer_excludes_each_report_their_own_problem():
    expect = {"any": [{"valid": False}], "none": [{"satisfiable": True}],
              "answer_excludes": ["AVAILABLE TOOLS"]}
    assert judge(expect, [{"valid": False}], "No.") == []
    problems = judge(expect, [{"valid": True}, {"satisfiable": True}], "AVAILABLE TOOLS: ...")
    assert len(problems) == 3


def test_an_out_of_scope_question_passes_with_no_tool_called():
    assert judge({"none": [{"reachable": True}]}, [], "I can't help with weather.") == []


def test_the_interval_is_honest_about_thirty_runs():
    low, high = wilson(24, 30)
    assert 0.6 < low < 0.65 and 0.9 < high < 0.92
    assert wilson(0, 0) == (0.0, 0.0)


def test_the_question_set_is_well_formed():
    data = json.loads(Path(CASES).read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) == 30
    assert len({c["id"] for c in cases}) == 30, "case ids must be unique"
    for case in cases:
        assert case["prompt"].strip() and case["why"].strip() and case["reference"]
        assert set(case["expect"]) <= {"any", "none", "answer_excludes"}, case["id"]
        assert case["expect"].get("any") or case["expect"].get("none"), case["id"]


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}\n          {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
