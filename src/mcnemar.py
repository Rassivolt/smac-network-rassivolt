"""Paired significance tests for the final experiment batch (3 Oct 2026).

The proposal (section 3.6) promised paired, scenario-level comparisons:
McNemar's test for binary outcomes. Every condition in a run answers the same
31 scenarios, so each comparison is a 2x2 table of matched pairs, and only the
discordant pairs - right under one condition, wrong under the other - carry
information. The exact (binomial) form is used because those counts are small.

    python mcnemar.py                      # all batches, from ../results/final/final_*.json
    python mcnemar.py --markdown           # the tables for docs/final-experiment.md

Two ways of reading three runs are reported, and neither is hidden:
  * per run - three independent tests, the conservative reading;
  * pooled - discordant pairs summed over the runs of a batch. The runs share
    their 31 scenarios, so the pooled test treats repeats of a scenario as if
    they were new ones; it is a summary of consistency, not extra evidence, and
    is labelled as such.
Holm's correction is applied across the comparisons within each batch.
"""
from __future__ import annotations

import argparse
import glob
import json
from math import comb
from pathlib import Path

RESULTS = Path(__file__).resolve().parent.parent / "results" / "final"

BATCHES = {
    "RQ2 - Haiku 4.5, perfect extraction": "final_rq2_haiku_*.json",
    "RQ1 - Haiku 4.5, live extraction": "final_rq1_haiku_*.json",
    "D1 - Sonnet 5, perfect extraction": "final_d1_sonnet_*.json",
    "D1 - Opus 5, perfect extraction (1 run)": "final_d1_opus_*.json",
}

# (outcome, condition X, condition Y, what the comparison answers)
COMPARISONS = [
    ("correct", "B_self_review", "A_no_check", "RQ2: does self-review add correctness?"),
    ("correct", "C_check_only", "A_no_check", "RQ2: does checking alone add correctness?"),
    ("correct", "C_verified", "C_check_only", "RQ2: does repair (by the model) add correctness?"),
    ("correct", "R_code_repair", "C_verified", "RQ3: code repair vs model repair"),
    ("honest", "B_self_review", "A_no_check", "RQ2: does self-review add honesty?"),
    ("honest", "C_check_only", "B_self_review", "RQ2: checking in code vs self-review, honesty"),
    ("honest", "C_check_only", "A_no_check", "RQ2: does checking alone add honesty?"),
]

SHORT = {"A_no_check": "A", "B_self_review": "B", "C_verified": "C",
         "C_check_only": "C-check", "R_code_repair": "R"}


def load(path: str) -> list[dict]:
    """The rows of one run. The file ends with the W&B line after the JSON array."""
    text = Path(path).read_text(encoding="utf-8")
    rows, _ = json.JSONDecoder().raw_decode(text)
    return rows


def exact_mcnemar(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the two discordant counts."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(comb(n, k) for k in range(0, min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def discordant(rows: list[dict], outcome: str, x: str, y: str) -> tuple[int, int]:
    """(pairs where only X succeeded, pairs where only Y succeeded)."""
    by = {(r["scenario"], r["condition"]): bool(r[outcome]) for r in rows}
    scenarios = sorted({r["scenario"] for r in rows})
    only_x = sum(by[(s, x)] and not by[(s, y)] for s in scenarios)
    only_y = sum(by[(s, y)] and not by[(s, x)] for s in scenarios)
    return only_x, only_y


def holm(pvalues: list[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values, in the original order."""
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted, running = [0.0] * len(pvalues), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(pvalues) - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted


def fmt(p: float) -> str:
    return "<0.0001" if p < 1e-4 else f"{p:.4f}"


def analyse() -> dict[str, list[dict]]:
    results = {}
    for batch, pattern in BATCHES.items():
        runs = [load(f) for f in sorted(glob.glob(str(RESULTS / pattern)))]
        if not runs:
            continue
        lines = []
        for outcome, x, y, question in COMPARISONS:
            per_run = [discordant(rows, outcome, x, y) for rows in runs]
            pooled = (sum(b for b, _ in per_run), sum(c for _, c in per_run))
            lines.append({
                "question": question, "outcome": outcome,
                "pair": f"{SHORT[x]} vs {SHORT[y]}",
                "per_run": [(b, c, exact_mcnemar(b, c)) for b, c in per_run],
                "pooled": (*pooled, exact_mcnemar(*pooled)),
            })
        # Holm across this batch's comparisons, on the pooled p-values and on
        # each run's own p-values separately.
        for key in range(len(runs)):
            adj = holm([line["per_run"][key][2] for line in lines])
            for line, a in zip(lines, adj):
                line.setdefault("per_run_holm", []).append(a)
        adj = holm([line["pooled"][2] for line in lines])
        for line, a in zip(lines, adj):
            line["pooled_holm"] = a
        results[batch] = lines
    return results


def print_text(results: dict[str, list[dict]]) -> None:
    for batch, lines in results.items():
        print(f"\n== {batch}")
        print(f"{'comparison':<16}{'outcome':<9}{'per run: only X / only Y, p (Holm)':<58}"
              f"pooled: X / Y, p (Holm)")
        for line in lines:
            runs = "  ".join(f"{b}/{c} p={fmt(h)}" for (b, c, _), h
                             in zip(line["per_run"], line["per_run_holm"]))
            b, c, _ = line["pooled"]
            print(f"{line['pair']:<16}{line['outcome']:<9}{runs:<58}{b}/{c} p={fmt(line['pooled_holm'])}")


def print_markdown(results: dict[str, list[dict]]) -> None:
    for batch, lines in results.items():
        print(f"\n**{batch}** - discordant pairs (only X right / only Y right), exact McNemar, "
              "Holm-adjusted within the batch\n")
        n = len(lines[0]["per_run"])
        heads = " | ".join(f"run {i + 1}" for i in range(n))
        print(f"| comparison | outcome | {heads} | pooled ({n} runs) |")
        print("|---|---|" + "---|" * n + "---|")
        for line in lines:
            runs = " | ".join(f"{b}/{c}, p={fmt(h)}" for (b, c, _), h
                              in zip(line["per_run"], line["per_run_holm"]))
            b, c, _ = line["pooled"]
            print(f"| {line['pair']} | {line['outcome']} | {runs} | "
                  f"{b}/{c}, p={fmt(line['pooled_holm'])} |")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--markdown", action="store_true", help="print tables for the docs")
    args = parser.parse_args()
    results = analyse()
    (print_markdown if args.markdown else print_text)(results)
