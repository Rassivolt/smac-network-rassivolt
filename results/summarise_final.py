"""The final batch, summarised from the saved rows - no W&B needed.

    python summarise_final.py final_rq2_haiku_*.json
    python summarise_final.py final_d1_sonnet_*.json

Metrics come from conditions.summarise(), the same function the experiment
logs with, so a number here and a number in W&B cannot mean different things.
Several runs are reported as mean [lowest-highest]; three runs are too few for
an interval worth printing. The comparisons are checked run by run.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import conditions  # noqa: E402

CONDITIONS = list(conditions.CONDITIONS)
METRICS = ("correct", "honesty", "false_disclosures", "harm_vs_A", "llm_calls", "mean_latency_ms")


def load(path: str) -> list[dict]:
    raw = Path(path).read_text(encoding="utf-8")
    rows, _ = json.JSONDecoder().raw_decode(raw[raw.index("["):])
    return rows


runs = [(p, load(p)) for p in sys.argv[1:]]
summaries = [conditions.summarise(rows) for _, rows in runs]
print(f"{len(runs)} run(s): {', '.join(Path(p).name for p, _ in runs)}")
print(f"scenarios per run: {[len({r['scenario'] for r in rows}) for _, rows in runs]}\n")

fmt = {"correct": "{:.2f}", "honesty": "{:.2f}", "harm_vs_A": "{:.2f}",
       "false_disclosures": "{:.0f}", "llm_calls": "{:.0f}", "mean_latency_ms": "{:.0f}"}
print(f"{'condition':15} " + " ".join(f"{m:>20}" for m in METRICS))
for c in CONDITIONS:
    cells = []
    for m in METRICS:
        v = [s[c][m] for s in summaries if c in s and s[c].get(m) is not None]
        if not v:
            cells.append(f"{'-':>20}")
            continue
        f = fmt[m].format
        cells.append(f"{f(sum(v) / len(v)) + ' [' + f(min(v)) + '-' + f(max(v)) + ']':>20}")
    print(f"{c:15} " + " ".join(cells))

get = lambda s, c, m: s.get(c, {}).get(m)
checks = [
    ("R correct > C correct",
     lambda s: get(s, "R_code_repair", "correct") > get(s, "C_verified", "correct")),
    ("C correct > C_check_only correct",
     lambda s: get(s, "C_verified", "correct") > get(s, "C_check_only", "correct")),
    ("C_check_only correct == A correct",
     lambda s: get(s, "C_check_only", "correct") == get(s, "A_no_check", "correct")),
    ("C, C_check_only and R honesty == 1.0",
     lambda s: all(get(s, c, "honesty") == 1.0
                   for c in ("C_verified", "C_check_only", "R_code_repair"))),
    ("B correct <= A correct",
     lambda s: get(s, "B_self_review", "correct") <= get(s, "A_no_check", "correct")),
    ("B made false claims",
     lambda s: get(s, "B_self_review", "false_disclosures") > 0),
    ("R used fewer model calls than C",
     lambda s: get(s, "R_code_repair", "llm_calls") < get(s, "C_verified", "llm_calls")),
    ("no condition harmed a scenario A got right",
     lambda s: all((get(s, c, "harm_vs_A") or 0) == 0 for c in CONDITIONS)),
]
print("\ncomparisons, run by run:")
for label, test in checks:
    held = [bool(test(s)) for s in summaries]
    print(f"  {label:48} {sum(held)}/{len(held)}")

# RQ1, when extraction was live: shared by every condition, so one value per run.
exact = [next(iter(s.values()))["extraction_exact"] for s in summaries]
# Only meaningful when extraction was live; with perfect extraction it is 1.0 by construction.
if any("rq1" in Path(p).name for p, _ in runs):
    acceptable = [next(iter(s.values()))["extraction_acceptable"] for s in summaries]
    print(f"\nRQ1 extraction exact      {[round(x, 2) for x in exact]}")
    print(f"RQ1 extraction acceptable {[round(x, 2) for x in acceptable]}")

# Every scenario A got right and a condition got wrong, split by cause. A
# condition that never revised its route returned its own first draw - the
# producer's sampling, not the check. One that revised and ended wrong is the
# case that would mean verification did harm.
print("\nharm vs A, by cause:")
any_harm = False
for p, rows in runs:
    a = {r["scenario"]: r for r in rows if r["condition"] == "A_no_check"}
    for r in rows:
        if r["condition"] == "A_no_check" or not a[r["scenario"]]["correct"] or r["correct"]:
            continue
        any_harm = True
        reviser = "self-review" if r["condition"] == "B_self_review" else "verification"
        cause = f"after revision ({reviser})" if r.get("revised") else "first draw (producer sampling)"
        last = (r.get("trace") or [""])[-1][:90]
        print(f"  {Path(p).name:26} {r['condition']:15} {r['scenario']:32} {cause}; {last}")
if not any_harm:
    print("  none")
