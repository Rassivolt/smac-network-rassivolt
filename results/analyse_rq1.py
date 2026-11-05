"""RQ1 from a live-extraction run: how well is the traveller's request read?

    python analyse_rq1.py rq1_rows.json

One extraction per scenario is shared by every condition, so each scenario is
counted once. An extraction is judged against the scenario author's constraint
set key by key, because "wrong" covers three different failures with different
consequences downstream:

    missed    a constraint the traveller stated was not extracted - the
              verifier then checks against less than was asked, and passes
              routes it should fail
    misread   the key is right and the value is not
    invented  a constraint nobody stated - the verifier then fails routes
              for a requirement the traveller never had
"""
import collections
import json
import sys

raw = open(sys.argv[1], encoding="utf-8").read()
rows, _ = json.JSONDecoder().raw_decode(raw[raw.index("["):])

by_scenario = collections.defaultdict(list)
for r in rows:
    # A condition that returned no route is scored on an early path that omits
    # the extraction fields (conditions.score). Extraction is shared, so the
    # scenario's other rows carry it; those are the ones read here.
    if "extracted" in r:
        by_scenario[r["scenario"]].append(r)

# The fix of 25 Sep: every condition must have seen the same constraints.
unshared = [s for s, rs in by_scenario.items()
            if len({json.dumps(r["extracted"], sort_keys=True) for r in rs}) > 1]
print(f"scenarios: {len(by_scenario)}; conditions per scenario: "
      f"{len(next(iter(by_scenario.values())))}; "
      f"scenarios where conditions saw different constraints: {len(unshared)}")
print()


def same(a, b):
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(str, a)) == sorted(map(str, b))
    return a == b


tally = collections.Counter()
per_class = collections.defaultdict(collections.Counter)
details = []
for scenario, rs in by_scenario.items():
    r = rs[0]
    truth, got, cls = r["truth"], r["extracted"], r["scenario_class"]
    missed = sorted(k for k in truth if k not in got)
    invented = sorted(k for k in got if k not in truth)
    misread = sorted(k for k in truth if k in got and not same(truth[k], got[k]))
    exact = not (missed or invented or misread)
    tally["exact" if exact else "wrong"] += 1
    per_class[cls]["n"] += 1
    per_class[cls]["exact"] += exact
    for kind, keys in (("missed", missed), ("misread", misread), ("invented", invented)):
        tally[kind] += len(keys)
        per_class[cls][kind] += len(keys)
    if not exact:
        details.append((scenario, cls, missed, misread, invented, truth, got))

n = len(by_scenario)
print(f"exact extraction: {tally['exact']}/{n} = {tally['exact'] / n:.2f}")
print(f"errors by kind  : missed {tally['missed']}, misread {tally['misread']}, "
      f"invented {tally['invented']}")
print()
print(f"{'class':26} {'n':>3} {'exact':>6} {'missed':>7} {'misread':>8} {'invented':>9}")
for cls, c in per_class.items():
    print(f"{cls:26} {c['n']:>3} {c['exact'] / c['n']:>6.2f} {c['missed']:>7} "
          f"{c['misread']:>8} {c['invented']:>9}")

print("\nthe wrong ones:")
for scenario, cls, missed, misread, invented, truth, got in details:
    print(f"  {scenario} ({cls})")
    if missed:
        print(f"      missed   {missed}")
    for k in misread:
        print(f"      misread  {k}: truth {truth[k]!r}, read {got[k]!r}")
    for k in invented:
        print(f"      invented {k}: {got[k]!r}")

# The five conditions under live extraction, for set-side comparison with the
# perfect-extraction runs.
print("\nconditions under live extraction:")
conds = collections.defaultdict(list)
for r in rows:
    conds[r["condition"]].append(r)
for c, rs in conds.items():
    k = len(rs)
    correct = sum(bool(r["correct"]) for r in rs) / k
    honest = sum(bool(r["honest"]) for r in rs) / k
    false = sum(r["false_disclosures"] for r in rs)
    calls = sum(r["llm_calls"] for r in rs)
    print(f"  {c:15} correct {correct:.2f}  honest {honest:.2f}  "
          f"false claims {false:>2}  model calls {calls}")
