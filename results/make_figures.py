"""Thesis figures for the REIT7820 report, computed from the raw experiment rows.

Static figures for a printed thesis (PNG 300 dpi + SVG). Colours are the dataviz
reference palette's validated light-mode categorical slots, in fixed order.
"""
import glob
import json
import statistics as st
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

REPO = Path(sys.argv[1])
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)

SLOT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]   # categorical 1-5
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e2e1dc"
CONDS = ["A_no_check", "B_self_review", "C_check_only", "C_verified", "R_code_repair"]
LABEL = {"A_no_check": "A  no check", "B_self_review": "B  self-review",
         "C_check_only": "C-check  code checks", "C_verified": "C  code checks,\nmodel repairs",
         "R_code_repair": "R  code checks,\ncode repairs"}
SHORT = {"A_no_check": "A", "B_self_review": "B", "C_check_only": "C-check",
         "C_verified": "C", "R_code_repair": "R"}
CM = 1 / 2.54

plt.rcParams.update({
    "font.family": "Arial", "font.size": 9, "axes.edgecolor": INK2, "axes.labelcolor": INK,
    "xtick.color": INK2, "ytick.color": INK2, "axes.linewidth": 0.6,
    "xtick.major.width": 0.6, "ytick.major.width": 0.6, "svg.fonttype": "none",
})


def load(pattern):
    runs = []
    for f in sorted(glob.glob(str(REPO / "results" / "final" / pattern))):
        text = Path(f).read_text(encoding="utf-8")
        runs.append(json.JSONDecoder().raw_decode(text)[0])
    return runs


def rate(runs, cond, field):
    vals = [sum(r[field] for r in run if r["condition"] == cond) / 31 for run in runs]
    return st.mean(vals), min(vals), max(vals)


def tidy(ax, grid_axis="y"):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def save(fig, name):
    fig.savefig(OUT / f"{name}.png", dpi=300, bbox_inches="tight", facecolor="white")
    fig.savefig(OUT / f"{name}.svg", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("saved", name)


RQ2, RQ1 = load("final_rq2_haiku_*.json"), load("final_rq1_haiku_*.json")
SON, OPUS = load("final_d1_sonnet_*.json"), load("final_d1_opus_*.json")

# ---------------------------------------------------------------- Fig: RQ2 correct & honest
fig, ax = plt.subplots(figsize=(16 * CM, 7.2 * CM))
width, gap = 0.36, 0.04
for k, (field, name) in enumerate((("correct", "Correct"), ("honest", "Honest"))):
    for i, cond in enumerate(CONDS):
        mean, lo, hi = rate(RQ2, cond, field)
        x = i + (k - 0.5) * (width + gap)
        ax.bar(x, mean, width, color=SLOT[k], label=name if i == 0 else None, zorder=2)
        if hi > lo:
            ax.errorbar(x, mean, yerr=[[mean - lo], [hi - mean]], color=INK, lw=0.8,
                        capsize=2.5, capthick=0.8, zorder=3)
        ax.text(x, max(mean, hi) + 0.025, f"{mean:.2f}", ha="center", va="bottom",
                fontsize=8, color=INK)
TICK = {"A_no_check": "A\nno check", "B_self_review": "B\nself-review",
        "C_check_only": "C-check\ncheck only", "C_verified": "C\nmodel repairs",
        "R_code_repair": "R\ncode repairs"}
ax.set_xticks(range(len(CONDS)), [TICK[c] for c in CONDS])
ax.set_ylim(0, 1.12)
ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0], ["0", "0.25", "0.50", "0.75", "1.00"])
ax.set_ylabel("Share of 31 scenarios")
tidy(ax)
ax.legend(frameon=False, loc="upper left", ncol=2, fontsize=8.5)
save(fig, "fig_rq2_correct_honest")

# ---------------------------------------------------------------- Fig: cost (calls, latency)
fig, axes = plt.subplots(1, 2, figsize=(16 * CM, 5.6 * CM), sharey=True)
calls = [st.mean(sum(r["llm_calls"] for r in run if r["condition"] == c) for run in RQ2) for c in CONDS]
lat = [st.mean(st.median(r["latency_ms"] for r in run if r["condition"] == c) / 1000 for run in RQ2)
       for c in CONDS]
ys = list(range(len(CONDS)))[::-1]
for ax, values, title, fmt in ((axes[0], calls, "Model calls per run (31 scenarios)", "{:.0f}"),
                               (axes[1], lat, "Median latency per scenario (s)", "{:.1f}")):
    ax.barh(ys, values, 0.55, color=SLOT[0], zorder=2)
    for y, v in zip(ys, values):
        ax.text(v + max(values) * 0.02, y, fmt.format(v), va="center", fontsize=8, color=INK)
    ax.set_title(title, fontsize=9, color=INK, loc="left")
    ax.set_xlim(0, max(values) * 1.18)
    tidy(ax, "x")
axes[0].set_yticks(ys, [LABEL[c].replace("\n", " ") for c in CONDS])
save(fig, "fig_rq3_cost")

# ---------------------------------------------------------------- Fig: model tiers
tiers = [("Haiku 4.5", RQ2), ("Sonnet 5", SON), ("Opus 5\n(1 run)", OPUS)]
fig, axes = plt.subplots(1, 2, figsize=(16 * CM, 7.0 * CM), sharey=True)
for ax, field, title in ((axes[0], "correct", "Correct"), (axes[1], "honest", "Honest")):
    for i, cond in enumerate(CONDS):
        ys_ = [rate(runs, cond, field)[0] for _, runs in tiers]
        ax.plot(range(3), ys_, color=SLOT[i], lw=2, marker="o", ms=4.5, zorder=3,
                label=SHORT[cond], markeredgecolor="white", markeredgewidth=0.8)
    if field == "honest":
        ax.annotate("C, C-check and R:\n1.00 on every tier", xy=(1, 1.0), xytext=(1, 0.80),
                    ha="center", fontsize=8, color=INK,
                    arrowprops=dict(arrowstyle="-", color=INK2, lw=0.6))
    ax.set_xticks(range(3), [t for t, _ in tiers])
    ax.set_xlim(-0.25, 2.25)
    ax.set_title(title, fontsize=9.5, color=INK, loc="left")
    tidy(ax)
axes[0].set_ylim(-0.03, 1.08)
axes[0].set_yticks([0, 0.25, 0.5, 0.75, 1.0], ["0", "0.25", "0.50", "0.75", "1.00"])
axes[0].set_ylabel("Share of 31 scenarios")
handles, labels = axes[0].get_legend_handles_labels()
fig.legend(handles, labels, frameon=False, ncol=5, loc="upper center",
           bbox_to_anchor=(0.5, 1.06), fontsize=8.5)
save(fig, "fig_rq4_tiers")

# ---------------------------------------------------------------- Fig: RQ1 extraction
fig, ax = plt.subplots(figsize=(16 * CM, 4.0 * CM))
kinds = [("exact", "Exact"), ("restated", "Restates the request"), ("wrong", "Wrong (added a constraint)")]
for j, run in enumerate(RQ1):
    rows = [r for r in run if r["condition"] == "A_no_check"]
    left = 0
    for k, (kind, name) in enumerate(kinds):
        n = sum(r["extraction"] == kind for r in rows)
        ax.barh(2 - j, n, 0.6, left=left, color=SLOT[k], edgecolor="white", linewidth=1.5,
                label=name if j == 0 else None, zorder=2)
        ax.text(left + n / 2, 2 - j, str(n), ha="center", va="center", fontsize=8,
                color="white" if k == 0 else INK)
        left += n
ax.set_yticks([2, 1, 0], ["Run 1", "Run 2", "Run 3"])
ax.set_xlim(0, 31)
ax.set_xticks([0, 5, 10, 15, 20, 25, 31])
ax.set_xlabel("Scenarios (of 31)")
tidy(ax, "x")
ax.legend(frameon=False, ncol=3, loc="lower center", bbox_to_anchor=(0.5, 1.0), fontsize=8.5)
save(fig, "fig_rq1_extraction")
