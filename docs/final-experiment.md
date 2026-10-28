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
container. Summaries: `usage/summarise_final.py`, `usage/analyse_rq1.py`.

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
