# Final experiment batch, 3 Oct 2026

The figures the report quotes. Every number here comes from one verifier - the
one frozen on 3 Oct, with the walking-time check (register row 13) - and one
pinned network snapshot. Earlier runs (23-24 Sep) used the verifier before that
check and are superseded; the register keeps them only where a correction to an
earlier reading is itself the point.

## Method

31 hand-authored scenarios in seven classes (`src/conditions.py`), five
conditions, the model producer writing every route:

| condition | what it does |
|---|---|
| A - no check | plan, return |
| B - model reviews itself | plan, the model reviews its own route with no tools, revises if it says so |
| C - check in code, model revises | plan, `validate()` measures it, violations go back to the model, up to 3 attempts |
| C_check_only | plan, `validate()` measures it, returned with the verdict, never revised |
| R - check, repair in code | plan, `validate()` measures it, `routing.repair()` fixes it, the model is not asked again |

B and C share the same attempt budget. Each condition makes its own first call
to the producer, so two conditions do not always judge the same first route
(see Caveats).

| batch | extraction | model | runs | W&B runs (project Network_Agent) |
|---|---|---|---|---|
| RQ2 | perfect - the scenario's own constraints | claude-haiku-4-5 | 3 | `hdtzh2mm`, `g8v5x34z`, `8kzootpv` |
| RQ1 | live - the model reads the request; one extraction shared by all five conditions | claude-haiku-4-5 | 3 | `c8wzvmw4`, `qlmtvkkb`, `vhnio0ft` |
| D1 | perfect | claude-sonnet-5, thinking off | 3 | `xumf1gug`, `n6ld3w6t`, `c9so1hwb` |
| D1 | perfect | claude-opus-5, thinking off | 1 | `d82ytsj6` |

The larger tiers are run with extended thinking off (`llm.THINKING_OFF`), because
Haiku 4.5 does not think unless asked: the comparison is of models, not of how
long each deliberates. Haiku's requests are unchanged by that setting.

Reproduce: `python conditions.py --all --perfect-extraction --producer model --json --wandb`
(drop `--perfect-extraction` for RQ1; set `LLM_MODEL` for D1), inside the network
container. Summaries: `results/summarise_final.py`, `results/analyse_rq1.py`; figures: `results/make_figures.py`.

Three runs are too few for an interval worth printing. Figures are the mean,
with the lowest and highest run in brackets, and each comparison is reported as
the number of runs it held in.

## RQ2 - does checking in code beat self-review? (Haiku, perfect extraction)

| condition | correct | honest | invented claims | model calls | latency |
|---|---|---|---|---|---|
| A - no check | 0.00 [0.00-0.00] | 0.00 | 0 | 31 | 3.0 s |
| B - model reviews itself | 0.00 [0.00-0.00] | 0.29 [0.23-0.32] | 7 [5-9] | 71 | 5.2 s |
| C - check, model revises | 0.40 [0.39-0.42] | 1.00 | 0 | 81 | 8.6 s |
| C_check_only | 0.01 [0.00-0.03] | 1.00 | 0 | 31 | 3.1 s |
| R - check, repair in code | 0.61 [0.61-0.61] | 1.00 | 0 | 31 | 3.3 s |

| comparison | runs it held in |
|---|---|
| R more correct than C | 3/3 |
| C more correct than C_check_only | 3/3 |
| C, C_check_only and R fully honest | 3/3 |
| B no more correct than A | 3/3 |
| B made invented claims | 3/3 |
| R used fewer model calls than C | 3/3 |
| no condition got wrong a scenario A got right | 3/3 |
| C_check_only exactly as correct as A | 2/3 - in the third it was right on one scenario A was not, from a different first route |

Read: **self-review buys no correctness and invents evidence in every run;
checking in code makes every answer honest; all of the correctness comes from
repair, and code repair beats model repair at under half the model calls.**

## RQ1 - how well is the request read? (Haiku, live extraction)

| run | exact | exact or only restating the request | missed | misread | invented |
|---|---|---|---|---|---|
| 1 | 0.48 (15/31) | 0.87 | 0 | 0 | 20 |
| 2 | 0.48 (15/31) | 0.87 | 0 | 0 | 20 |
| 3 | 0.45 (14/31) | 0.87 | 0 | 0 | 21 |

In 93 extractions **no stated constraint was missed and no value was misread**.
Every error is an addition, and most additions restate the request:
`allowed_modes: ["walk"]` for a walking question, or a `depart_after` equal to
the stated departure. Four scenarios a run are wrong rather than restated, not
always the same four. Two recur in every run: steps avoided on a walk where
nobody mentioned them (`basic-ml-cbd`), and a list of transit modes on a pure
walking limit (`pref-uq-800`). The others appear in one or two runs: a list of
road types to avoid (`basic-we-sb`, 2/3), transit modes on a far walk
(`multi-ip-cbd-far`, 2/3), "no steps" widened to sealed surfaces too
(`pref-kp-nosteps`, 1/3), and once a typical value substituted for a missing
one - a 5,000 m walking limit nobody stated (`edge-unreachable`, 1/3), which
the extraction prompt forbids in so many words.
An invented constraint makes the verifier stricter than the traveller, never
looser - the direction of error that fails safe.

The five conditions with live extraction:

| condition | correct | honest | invented claims | model calls |
|---|---|---|---|---|
| A | 0.02 [0.00-0.03] | 0.02 | 0 | 62 |
| B | 0.04 [0.00-0.10] | 0.25 | 6 [5-6] | 102 |
| C | 0.47 [0.45-0.48] | 1.00 | 0 | 108 |
| C_check_only | 0.04 [0.03-0.06] | 1.00 | 0 | 62 |
| R | 0.65 [0.61-0.68] | 1.00 | 0 | 62 |

Model calls include the shared extraction once per condition. Every RQ2
ordering holds under live extraction. C and R are slightly *more* correct here
than with perfect extraction; the likeliest reason is that restated
constraints such as walk-only steer the producer toward the itinerary the
scenario expects, but this batch does not isolate it and the report should not
claim it.

## D1 - does a stronger model change the conclusion? (perfect extraction)

| tier | A | B | C | C_check_only | R | B honest | B invented claims | calls C / R |
|---|---|---|---|---|---|---|---|---|
| Haiku 4.5, 3 runs | 0.00 | 0.00 | 0.40 | 0.01 | 0.61 | 0.29 | 7 | 81 / 31 |
| Sonnet 5, 3 runs | 0.09 [0.06-0.13] | 0.11 [0.10-0.13] | 0.52 [0.52-0.52] | 0.12 | 0.55 [0.52-0.58] | 0.45 | 10 [6-13] | 74 / 31 |
| Opus 5, 1 run | 0.19 | 0.23 | 0.58 | 0.16 | 0.58 | 0.48 | 7 | 64 / 31 |

What holds on every tier: **C, C_check_only and R are fully honest; checking
alone adds little correctness; B invents claims in every run and stays below
0.5 honesty; R uses the fewest model calls.**

What changes with the tier, stated as plainly as the rest:

- **A stronger producer makes better first routes** (A: 0.00, 0.09, 0.19).
- **Self-review gains a little on stronger models** (B above A by 0.02 on
  Sonnet and 0.04 on Opus; B no better than A held in 1 of 3 Sonnet runs). It
  still invents evidence every time - more of it on Sonnet than on Haiku.
- **Code repair's lead over model repair shrinks as the model strengthens**:
  +0.21 on Haiku, +0.03 on Sonnet (R ahead in 2 of 3 runs), level on Opus. A
  stronger model uses measured feedback about as well as code does. What R keeps
  on every tier is cost: 31 model calls against C's 64-81, and C takes 1.8 to
  2.6 times as long.

So the thesis claim narrows with the tier, and the report should say so:
external measurement is what makes an answer honest on every tier; who should
repair depends on the producer - code, clearly, for a small model; either, for
a large one, with code the cheaper.

## Paired significance tests (computed 28 Oct 2026)

The proposal (section 3.6) promised paired, scenario-level tests. Every condition in
a run answers the same 31 scenarios, so each comparison is a table of matched pairs;
only the discordant pairs - right under one condition and wrong under the other -
carry information, and with counts this small the exact (binomial) McNemar test is
the right form. Holm's correction is applied across the seven comparisons of each
batch. `src/mcnemar.py` reproduces every figure below from `results/final/final_*.json`.

Two readings of three runs are given. **Per run** is three independent tests and is
the one to quote. **Pooled** sums the discordant pairs over a batch's runs; the runs
share their 31 scenarios, so it counts a repeated scenario as though it were a new
one. It summarises consistency and is not additional evidence.

**RQ2 - Haiku 4.5, perfect extraction** - discordant pairs (only X right / only Y right), exact McNemar, Holm-adjusted within the batch

| comparison | outcome | run 1 | run 2 | run 3 | pooled (3 runs) |
|---|---|---|---|---|---|
| B vs A | correct | 0/0, p=1.0000 | 0/0, p=1.0000 | 0/0, p=1.0000 | 0/0, p=1.0000 |
| C-check vs A | correct | 0/0, p=1.0000 | 0/0, p=1.0000 | 1/0, p=1.0000 | 1/0, p=1.0000 |
| C vs C-check | correct | 12/0, p=0.0024 | 13/0, p=0.0012 | 11/0, p=0.0049 | 36/0, p=<0.0001 |
| R vs C | correct | 8/1, p=0.1172 | 7/1, p=0.2109 | 9/2, p=0.1963 | 24/4, p=0.0005 |
| B vs A | honest | 7/0, p=0.0625 | 10/0, p=0.0078 | 10/0, p=0.0078 | 27/0, p=<0.0001 |
| C-check vs B | honest | 24/0, p=<0.0001 | 21/0, p=<0.0001 | 21/0, p=<0.0001 | 66/0, p=<0.0001 |
| C-check vs A | honest | 31/0, p=<0.0001 | 31/0, p=<0.0001 | 31/0, p=<0.0001 | 93/0, p=<0.0001 |

**RQ1 - Haiku 4.5, live extraction** - discordant pairs (only X right / only Y right), exact McNemar, Holm-adjusted within the batch

| comparison | outcome | run 1 | run 2 | run 3 | pooled (3 runs) |
|---|---|---|---|---|---|
| B vs A | correct | 0/0, p=1.0000 | 3/1, p=1.0000 | 1/1, p=1.0000 | 4/2, p=1.0000 |
| C-check vs A | correct | 1/0, p=1.0000 | 0/0, p=1.0000 | 1/0, p=1.0000 | 2/0, p=1.0000 |
| C vs C-check | correct | 15/1, p=0.0026 | 14/0, p=0.0006 | 12/0, p=0.0024 | 41/1, p=<0.0001 |
| R vs C | correct | 5/1, p=0.6562 | 6/1, p=0.3750 | 7/0, p=0.0625 | 18/2, p=0.0012 |
| B vs A | honest | 7/0, p=0.0625 | 9/1, p=0.0859 | 7/1, p=0.2109 | 23/2, p=<0.0001 |
| C-check vs B | honest | 24/0, p=<0.0001 | 22/0, p=<0.0001 | 24/0, p=<0.0001 | 70/0, p=<0.0001 |
| C-check vs A | honest | 31/0, p=<0.0001 | 30/0, p=<0.0001 | 30/0, p=<0.0001 | 91/0, p=<0.0001 |

**D1 - Sonnet 5, perfect extraction** - discordant pairs (only X right / only Y right), exact McNemar, Holm-adjusted within the batch

| comparison | outcome | run 1 | run 2 | run 3 | pooled (3 runs) |
|---|---|---|---|---|---|
| B vs A | correct | 1/2, p=1.0000 | 1/0, p=1.0000 | 2/0, p=1.0000 | 4/2, p=1.0000 |
| C-check vs A | correct | 0/0, p=1.0000 | 2/0, p=1.0000 | 1/0, p=1.0000 | 3/0, p=0.7500 |
| C vs C-check | correct | 13/1, p=0.0092 | 13/1, p=0.0092 | 13/0, p=0.0012 | 39/2, p=<0.0001 |
| R vs C | correct | 3/2, p=1.0000 | 3/3, p=1.0000 | 3/1, p=1.0000 | 9/6, p=1.0000 |
| B vs A | honest | 11/1, p=0.0254 | 10/0, p=0.0092 | 9/0, p=0.0156 | 30/1, p=<0.0001 |
| C-check vs B | honest | 16/0, p=0.0002 | 17/0, p=<0.0001 | 18/0, p=<0.0001 | 51/0, p=<0.0001 |
| C-check vs A | honest | 26/0, p=<0.0001 | 27/0, p=<0.0001 | 27/0, p=<0.0001 | 80/0, p=<0.0001 |

**D1 - Opus 5, perfect extraction (1 run)** - discordant pairs (only X right / only Y right), exact McNemar, Holm-adjusted within the batch

| comparison | outcome | run 1 | pooled (1 runs) |
|---|---|---|---|
| B vs A | correct | 3/2, p=1.0000 | 3/2, p=1.0000 |
| C-check vs A | correct | 1/2, p=1.0000 | 1/2, p=1.0000 |
| C vs C-check | correct | 13/0, p=0.0012 | 13/0, p=0.0012 |
| R vs C | correct | 3/3, p=1.0000 | 3/3, p=1.0000 |
| B vs A | honest | 11/3, p=0.2295 | 11/3, p=0.2295 |
| C-check vs B | honest | 16/0, p=0.0002 | 16/0, p=0.0002 |
| C-check vs A | honest | 24/0, p=<0.0001 | 24/0, p=<0.0001 |

Read:

- **Checking in code makes answers honest, and that is established.** C-check is more
  honest than self-review (B) and than no check (A) in every run on every tier, each
  test p ≤ 0.0002 after correction. B is somewhat more honest than A - it sometimes
  declines to vouch for a route - significant in 2 of 3 Haiku runs and all 3 Sonnet
  runs, but it still invents claims (see RQ2 above).
- **The correctness comes from repair, and that is established.** C beats C-check in
  every run on every tier (p ≤ 0.0092 corrected). Checking alone changes correctness
  in at most two scenarios of any run.
- **Self-review adds no correctness.** On Haiku, B and A disagree on correctness in
  no scenario at all in any of the three runs. That is the absence of an effect in
  these 31 scenarios, not a proof that none exists.
- **Code repair versus model repair is consistent but not individually significant.**
  On Haiku, R is right where C is wrong in 7 to 9 scenarios a run and the reverse in
  1 to 2, in every run - but no single run reaches significance after correction
  (p = 0.12 to 0.21; live extraction 0.06 to 0.66). The pooled p = 0.0005 counts
  repeated scenarios as independent and should not be quoted as the result. On
  Sonnet 5 and Opus 5 the two are level (9/6 and 3/3 discordant pairs). The report
  should therefore claim that code repair was **more correct in every Haiku run and
  never less correct overall**, and cheaper on every tier - not that it is
  significantly more correct. A larger scenario set is what would settle it.

## Cost

From the usage log, by model, for the whole batch: Haiku 1,480 calls, $4.24
(about $0.67 a perfect-extraction run, $0.74 a live one); Sonnet 5 746 calls,
$4.81 (about $1.60 a run); Opus 5 224 calls, $3.43 for its one run. **$12.48
recorded.** The true figure is about 1.5% higher: see Caveats.

## Caveats

1. **Each condition draws its own first route.** With perfect extraction,
   C_check_only and A received the same route in about 70% of scenarios. Every
   case where C, C_check_only or R got wrong a scenario A got right was traced:
   in each, the condition never revised - its own first draw was wrong, or the
   producer returned no route - so none is verification damaging a correct
   answer (`summarise_final.py`, "harm vs A, by cause"). B's cases are its own:
   twice on Sonnet its self-review accepted or failed to replace a wrong route,
   and otherwise its first draw was wrong. A design where every condition starts from one shared first route
   would make the per-scenario comparison exact.
2. **The usage log lost 35 of 2,487 records.** The Haiku and D1 batches ran
   in two containers appending to one file over a Windows bind mount, and
   concurrent appends occasionally overwrote each other. The experiment rows are
   unaffected; only the cost log under-counts. Future concurrent runs should
   each set their own `USAGE_LOG`.
3. **A condition that returns no route is scored without its extraction
   fields** (`conditions.score`, early return). No correctness or honesty
   figure depends on them; RQ1 reads extraction from the scenario's other rows,
   which carry the same shared extraction.
4. **Opus 5 has one run.** Its figures show direction, not a range.
5. **31 scenarios.** One scenario is 0.03 of any rate.
