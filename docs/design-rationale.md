# Design rationale register

Every non-obvious design decision in this agent, with the reason it was made, the
evidence that it was the right one, the code that implements it and the test that
would catch its removal.

It exists because the decisions and the evidence were in different places. The
measurements are in commit messages and scenario notes, the reasoning is in code
comments, and the literature is largely absent - `background-existing-tools.md`
surveys seven existing systems, of which six are products and one is a patent.
A design chapter cannot be assembled from that, and neither can an answer to
"why did you do it that way?" asked in a viva.

**How to read the status column.** `literature` means a published source stating
the principle the decision rests on. Not every row needs one - some decisions are
engineering judgement and should say so rather than acquire a citation for
decoration. Rows marked `needed` are ones where a real claim is being made about
how systems or models behave, and that claim should not rest on this project's
own say-so.

| # | Decision | Literature | Data | Code | Eval |
|---|---|---|---|---|---|
| 1 | Verification is deterministic, not the model reviewing itself | needed | ✅ | ✅ | ✅ |
| 2 | The route producer is the experiment's variable | n/a - method | ✅ | ✅ | ✅ |
| 3 | The fifth tool compares trade-offs instead of returning the shortest path | needed | ✅ | ✅ | ✅ |
| 4 | Accessibility is carried by `surface`, not `incline` or `wheelchair` | needed | ✅ | ✅ | ✅ |
| 5 | Coverage has three states, not two | needed | ✅ | ✅ | ✅ |
| 6 | The walking graph and the timetable cover different areas | n/a - domain | ✅ | ✅ | ✅ |
| 7 | Inputs are read liberally, values judged strictly | ✅ Postel | ⚠️ none | ✅ | ⚠️ none |
| 8 | Data is pinned to a pinned snapshot with a manifest | needed | ✅ | ✅ | ⚠️ indirect |
| 9 | A 429 is decided by the `retry-after` header, not the status code | n/a - spec | ⚠️ none | ✅ | ✅ |
| 10 | Out-of-scope questions get a structured refusal, not a best guess | needed | ✅ | ✅ | ✅ |
| 11 | Tool descriptions name the accessibility capability | needed | ✅ | ✅ | ⚠️ partial |
| 12 | A failing route is repaired in code, not handed back to the model | needed | ✅ | ✅ | ✅ |
| 13 | A claimed walking time is judged against its distance, at 2.5 m/s | needed | ✅ | ✅ | ✅ |

---

## 1. Verification is deterministic, not the model reviewing itself

**Decision.** Condition C checks a produced route against the graph and the
timetable with code, and feeds the measured violations back for one bounded
replan. It does not ask the model whether its own answer was good.

**Literature.** *Needed.* The claim being made is that a language model cannot
reliably find its own reasoning errors without external feedback. Leads to verify:
Huang et al. on whether LLMs can self-correct reasoning; Madaan et al., Self-Refine,
for the opposing result and the conditions under which it holds. Both must be read
before citing - the interesting question is not whether self-correction works but
*what kind of feedback* makes it work, which is exactly what conditions B and C
differ on.

**Data.** Scenario `net-walk-limit-unsatisfiable` asks for a route from UQ St Lucia
to the Brisbane CBD within 800 m of walking. The true shortest path is 5,057 m, so
no route satisfies it. Run 23 Sep 2026 on `claude-haiku-4-5`:

- **A** (no check) returned a route and claimed it was valid.
- **B** (model reviews itself) reviewed that route and *accepted* it, reporting
  "only 263 metres of walking, well within the traveller's 800 metre maximum".
  The figure is invented; the measured value is 5,057 m.
- **C** (deterministic check) failed it on `claimed_distances_possible`, returned
  the measured distances, and the second attempt was valid and honest.

B is the illustration. The model did not fail to notice the problem - it
located the right quantity and fabricated a value that cleared the threshold.
That is a different failure from inattention, and it is the one an intrinsic review
cannot fix.

**Data, repeated.** Three runs of all 31 scenarios on 24 Sep 2026, perfect
extraction, `claude-haiku-4-5` (W&B runs `txa4le9m`, `r224oov6`, `aotydubk`).
Mean, with the lowest and highest of the three in brackets:

| condition | correct | honest | invented claims | model calls |
|---|---|---|---|---|
| A - no check | 0.00 [0.00-0.00] | 0.00 | 0 | 31 |
| B - model reviews itself | 0.01 [0.00-0.03] | 0.35 | 10 [9-12] | 71 |
| C - check in code, model revises | 0.39 [0.29-0.45] | 1.00 | 0 | 81 |

B made invented claims in every run and never moved honesty above 0.35. C was
honest in every scenario of every run.

**A correction.** The first run on 23 Sep had B at 0.00 against A at 0.03, and was
read at the time as self-review making the answer *worse*. Repetition does not
support that: B was no worse than A in only two of three runs, and in the third it
got one scenario right that A did not. The claim that holds is narrower and still
strong - intrinsic review brings no gain in correctness, and it invents evidence
every time. The commit message of 23 Sep says "worse than nothing"; the report
must not.

**Code.** `routing.validate()` (`src/routing.py:978`); condition C's replan loop,
`run_proposed` (`src/conditions.py:407`).

**Evaluation.** Scenario class `preference-unsatisfiable` (6 scenarios);
evaluation class `edge-and-failure` (20 of 53 cases).

---

## 2. The route producer is the experiment's variable

**Decision.** Who writes the route - a deterministic planner, a model, or a seeded
fallible stub - is a parameter (`--producer`), not a fixed part of the pipeline.

**Literature.** Not needed. This is experimental design, and the reason is internal:
with a deterministic producer the three conditions receive identical routes, so
`same_route_as_A` is 1.00 by construction and the conditions cannot be compared.
Saying so plainly is better than attaching a citation to it.

**Data.** With the stub producer `same_route_as_A` falls from 1.00 to 0.43;
C is correct on 0.68 of scenarios against A's 0.45, with harm 0.00.

**Code.** `src/producers.py`; the `producer` parameter threaded through
`conditions._plan`.

**Evaluation.** `src/test_producers.py` (7 tests).

---

## 3. The fifth tool compares trade-offs instead of returning the shortest path

**Decision.** `find_shortest_route` was withdrawn at week 7. The slot now holds
`compare_route_options`, which plans the shortest, step-free, sealed and combined
variants, judges each against the stated constraints, and reports what each one
costs - extra metres, flights of steps and how many have a handrail, unsealed
metres, lowered kerbs. It applies no weighting of its own.

**Literature.** *Needed.* Two claims want support: that route choice is
multi-criteria rather than distance-minimising, and that presenting costed
alternatives is decision support rather than recommendation. Leads: pedestrian and
accessible routing criteria; multi-criteria shortest path formulations. The point
to establish is that "shortest" is a *choice of objective*, not a neutral default.

**Data.** Re-derived 23 Sep 2026 by `src/measure_surface_impact.py`, seed 20260923,
against pinned graph sha256 `f0ed8a50b9ba03a9`: 600 origin-destination pairs sampled
400-2500 m apart, 575 routed (25 unconnected, excluded), each planned twice - once
unrestricted, once with every unpaved surface excluded. Pairs are split by the share
of the unrestricted route carried by `path`, `track` or `bridleway`, not by surface,
which would be circular.

| setting | pairs | unchanged | detour | no sealed route | stranded endpoint | median detour | max | surface coverage |
|---|---|---|---|---|---|---|---|---|
| parkland | 58 | 12 (21%) | 16 (28%) | 2 (3.4%) | 28 (48%) | 1,103 m | 7,329 m | 0.85 |
| street | 517 | 436 (84%) | 44 (8.5%) | 4 (0.8%) | 33 (6.4%) | 174 m | 2,882 m | 0.925 |

The shape of the claim holds and the explanation changes. Avoiding unpaved ground is
free for 84% of street pairs and only 21% of parkland ones, and where it costs
something it costs six times more in parkland - a median 1,103 m against 174 m, with
one pair sent 7.3 km out of its way. But the dominant parkland effect is not a severed
network: only 3.4% of parkland pairs have no sealed corridor between them, while 48%
have an **endpoint with no sealed way out at all**. The barrier is usually at the end
of the journey, not in the middle of it.

Two honest limits. Stranded endpoints are partly an artefact of sampling arbitrary
graph nodes, some of which sit mid-track where nobody starts a journey - though a
picnic ground reachable only by dirt path is genuinely unreachable for a wheelchair,
so the effect is not only artefact. And surface coverage is 0.85 and 0.925, so between
7% and 15% of route metres state no surface at all: "no unsealed ground found" is a
weaker claim than "proven sealed", which is why the tool reports both.

*Supersedes* an earlier interactive measurement (36 park pairs, 15 with no sealed
route) that was never written down. Its headline proportion was close - 52% of
parkland pairs here are stranded or severed against 42% there - but it described the
mechanism wrongly, and it should not be quoted.

**Code.** `routing.route_options()` (`src/routing.py:617`); `OPTION_VARIANTS`
(`src/routing.py:550`); the tool in `src/agents/network.py`.

**Evaluation.** 3 evaluation cases call `compare_route_options`; scenario class
`accessibility-surface` (3 scenarios). Change history in `src/descriptions.md`,
"Tool 5".

---

## 4. Accessibility is carried by `surface`, not `incline` or `wheelchair`

**Decision.** The graph keeps `surface`, `smoothness`, `lit`, `handrail`, `ramp`
and `step_count` on ways and `kerb`, `tactile_paving` on nodes. It deliberately
drops `incline`, `wheelchair` and `width`.

**Literature.** *Needed.* Two separable claims: which physical attributes actually
determine whether a pedestrian route is passable for a given mobility need, and
the known incompleteness of volunteered geographic information. Leads: accessible
pedestrian routing criteria; OpenStreetMap data quality and completeness studies.
The second matters more than it looks - it is the reason the tool reports coverage
alongside every verdict.

**Data.** Measured on the pinned graph: `surface` is stated on **72.8%** of edges,
against `incline` **1.0%**, `wheelchair` **0.4%** and `width` **4.0%**
(`src/data.py:254`). A constraint built on a tag present on 1% of edges is
vacuous - it would pass everything while appearing to check something, which is a
bug this project has already shipped once, in an earlier "avoid steps" constraint.
Node tags: 28,978 kerbs, 22,641 tactile paving, 40,156 crossings.

**Code.** `data._KEPT_WAY_TAGS`, `data._KEPT_NODE_TAGS` (`src/data.py:257-258`);
`routing.SURFACE_TYPES` (`src/routing.py:92`); `routing.path_accessibility()`
(`src/routing.py:558`).

**Evaluation.** Scenario class `accessibility-surface` (3); the surface-constraint
non-vacuity check in `src/audit_scenarios.py`, which fails the build if a
constraint stops excluding anything.

---

## 5. Coverage has three states, not two

**Decision.** A point is inside the covered area, or outside everything this agent
holds, or *inside the timetable's area but beyond the walking graph* - in which
case the walking leg is reported as **not measurable** rather than as failed or as
passed.

**Literature.** *Needed.* The claim is that a system reporting what it does not
know is more useful than one that collapses uncertainty into a pass or a fail.
Leads: calibration and abstention in model outputs; honesty and scope in tool-using
agents. This also connects directly to the MCP specification's out-of-scope
requirement (§6), which is a rubric item, not a research finding.

**Data.** A Gold Coast itinerary - the Week 7 lecture's own Showcase example -
verifies at 08:05 and fails at 03:17 on `transit_services_exist`, with the walking
leg reported as `{"outside_all_coverage": 0, "walking_legs_not_measurable": 2}`.
Before the split it returned a coverage violation and verified nothing at all,
which is the wrong answer to a question the data can settle. A Sydney route is
still a violation.

**Code.** The `within_coverage` check inside `routing.validate()`
(`src/routing.py:970`).

**Evaluation.** 2 evaluation cases (`multimodal-and-timetable`); 2 tests in
`src/test_routing.py`, one of which asserts the timetable area contains the walking
area - the split is only sound in that direction.

---

## 6. The walking graph and the timetable cover different areas

**Decision.** The walking graph is cut to metropolitan Brisbane; the timetable
index covers all of South East Queensland. They carry separate schema versions.

**Literature.** Not needed - this is a domain judgement and should be stated as
one. A 30 km walking route is not a route anyone takes, so extending the graph
buys nothing; a train from the Gold Coast is an ordinary journey, and whether the
service someone names actually runs is a question the feed answers wherever the
stop is.

**Data.** The widened index holds 13,119 stops and 3,793,094 departures (71 MB,
30 s to build). The walking graph holds 1,428,719 nodes.

**Code.** `data.NETWORK_BBOX` (`src/data.py:233`), `data.TIMETABLE_BBOX`
(`src/data.py:274`); separate `_SCHEMA` and `_TIMETABLE_SCHEMA`.

**Evaluation.** As row 5.

---

## 7. Inputs are read liberally, values judged strictly

**Decision.** `read_point()` accepts `{"lat","lon"}`, `{"latitude","longitude"}`,
`[lat, lon]` and `"lat,lon"`, and detects GeoJSON axis order. Segment keys and
constraint names have accepted aliases. What is *accepted* is wide; what is
*judged valid* is not relaxed at all.

**Literature.** Postel's robustness principle (RFC 761 §2.10 / RFC 1122). This one
has a real source and it is short.

**Data.** ⚠️ None. The decision was made from the specification's warning that the
handshake test sends deliberately malformed calls (§6), not from a measurement.
A count of how often a caller's spelling differs would need a caller.

**Code.** `routing.read_point()` (`src/routing.py:865`); `_SEGMENT_KEYS`,
`_CONSTRAINT_ALIASES` in `src/routing.py`.

**Evaluation.** ⚠️ Covered by unit tests but no evaluation case exercises a
malformed call end to end. **Action: add malformed-input cases before the
handshake test.**

---

## 8. Data is pinned to a snapshot with a manifest

**Decision.** The graph and the timetable are pinned to files whose sha256 is
recorded in `snapshot/MANIFEST.json`, mounted read-only into the container. The
manifest is tracked; the ~272 MB it describes is not.

**Literature.** *Needed.* The claim is that an empirical result computed against a
live API is not reproducible. Leads: reproducibility in computational research;
data versioning practice. Modest claim, should be easy to source.

**Data.** Overpass and the GTFS feed both change under you: the feed in use is
valid 20260905-20261104 and will expire during this project. Ground truth
re-derived by hand against a changed graph is the failure this prevents - it has
already happened once, which is why the walking schema is at v4.

**Code.** `src/pin_snapshot.py`; `data.snapshot_info()`; the read-only mount in
`docker/compose.yml`.

**Evaluation.** ⚠️ Indirect: the agent prints each dataset's sha256 and whether it
matches the manifest at startup, and `pin_snapshot.py --verify` checks it. No
evaluation case asserts it.

---

## 9. A 429 is decided by the `retry-after` header, not the status code

**Decision.** `llm.ask` retries on the `retry-after` header first, then on the
error type in the body (`rate_limit`, `overloaded`, `too many requests`). Credit
and billing markers never retry. A 429 carrying neither signal is not retried.

**Literature.** Not needed - this follows the unit's own week 7 material and the
Anthropic API's documented behaviour. It is a specification-following decision and
should be recorded as one.

**Data.** ⚠️ None, and none is likely: provoking a real rate limit to measure it
would cost money to demonstrate something the header already states.

**Code.** `llm._worth_retrying()` (`src/llm.py:90`); `RETRYABLE_STATUS`
(`src/llm.py:65`).

**Evaluation.** `src/test_llm.py` (7 tests), which record the sleeps rather than
performing them.

---

## 10. Out-of-scope questions get a structured refusal, not a best guess

**Decision.** A place outside SEQ, a coordinate outside the network area, or a
requirement the extraction layer cannot read returns a structured out-of-scope
response naming the boundary, with sources attached. The unmatched input is never
echoed back.

**Literature.** *Needed.* Two claims: that stating a limit beats guessing past it,
and that echoing untrusted input into a response read by another model is an
indirect prompt-injection path. The second has a real and citable literature.
Leads: indirect prompt injection in tool-using and retrieval-augmented systems.

**Data.** `geocode_place` asked for UQ St Lucia by a model that guessed returned a
point 261 m from OpenStreetMap's - the measurement that put the tool in
`common.py` in the first place.

**Code.** `smac.out_of_scope()`; `_coverage_refusal()` in `src/agents/network.py`;
`_EXTRACTION_REFUSALS` (`src/agents/network.py:256`).

**Evaluation.** Evaluation class `edge-and-failure` (17 cases), including
out-of-coverage and unreadable-requirement cases; scenario `edge-unreachable`.

---

## 11. Tool descriptions name the accessibility capability

**Decision.** All four route-touching descriptions state that the agent plans and
audits step-free and sealed-surface journeys, distinguish step-free *walking* from
step-free *boarding*, and state the two coverage areas.

**Literature.** *Needed.* The claim is that in a tool-selection setting the
description is the interface - a capability the selecting model cannot see is one
it cannot choose. Leads: tool selection and retrieval in LLM agents; work on tool
documentation quality. The specification states this as an assessed criterion
(§4.2), which makes it a rubric requirement as well as a design claim.

**Data.** Found by audit, not by testing: before week 7, **not one of the four
descriptions mentioned surface, wheelchair, kerb, accessibility or pram**, while
the constraint was implemented, the graph carried the tags, and five scenarios and
evaluation cases covered it.

**Code.** Docstrings in `src/agents/network.py`; `src/common.py`.

**Evaluation.** ⚠️ Partial, since 3 Oct. `src/robustness.py` puts thirty unseen
questions through the console's router, which sees every agent's tools and picks
from their descriptions alone. In the five accessibility questions - wheelchair,
pram, "no stairs", "under 2 km, I'm slow", "is it step-free?" - and the Chinese
pram question, the router chose a network tool with the restriction applied in
18 of 18 runs. That is selection among four agents on descriptions, but by one
router model (Haiku 4.5) and through the console's own prompt, not the
Orchestrator's. **Still needed for the week 12 claim: the same questions through
the Orchestrator, or a router prompt that is not ours.**

Across all thirty, three runs each: **86/90 = 0.96 [95% 0.89-0.98]**. All four
failures are the router calling no tool - three times declining the whole
injection question rather than its legitimate half, once replying with prose
instead of a decision. In no run did a tool return a fact that broke an
expectation. The single pass before that found four faults on the agent's side,
each fixed before the three-run measurement: "Melbourne" replaced by Melbourne
Street Green in South Brisbane, a crash routing from a place to itself, a 40 s
timeout on a point on a disconnected fragment, and aliases missed when the router
added "Brisbane" to a name.

---

## 12. A failing route is repaired in code, not handed back to the model

**Decision.** `validate_route(repair=true)` returns, for a route that fails, the
smallest change that makes it pass: a walking leg with an impossible distance, or
one using steps or a surface the traveller excluded, is replanned between its own
two ends; a service that does not run, or cannot be reached in time, is moved to
the next departure of the *same route* the traveller can actually catch; a route
starting or ending away from the requested point gets the walk to it. The repaired
route is checked again by the same `validate()` and only reported as repaired if it
passes. What needs a different itinerary - a walking total no leg can shorten,
fewer transfers, another mode - is reported as unrepairable with the reason. No
new tool slot: repair is what validation is for when the answer is no.

In the experiment the same function is condition R. C sends the measured
violations back to the model and asks again; R repairs them in code and never
asks again.

**This is a deviation from the proposal**, which had verification (C) as the
proposed architecture and no repair capability. It was added in week 9 on
Chengbo's advice to extend the agent beyond validation, and it is recorded here so
the change has its reason next to it.

**Literature.** *Needed.* Two claims: that self-repair by a model is bounded by the
quality of the feedback it is given, and that a repairer with access to ground
truth outperforms one that is only told what went wrong. Leads to verify before
citing: work on LLM self-repair in code generation, which reports gains limited by
feedback quality; tool-interactive critiquing, where a model corrects itself with
external tools. Neither has been read yet, so neither is a reference.

**Data.** The same three runs as row 1, with C taken apart into its two parts:

| condition | correct | honest | model calls | latency |
|---|---|---|---|---|
| C_check_only - check and disclose, never revise | 0.00 [0.00-0.00] | 1.00 | 31 | 3.2 s |
| C - check, model revises with measured feedback | 0.39 [0.29-0.45] | 1.00 | 81 | 8.3 s |
| R - check, repair in code | 0.60 [0.58-0.65] | 1.00 | 31 | 3.7 s |

Held in every one of the three runs: C_check_only was exactly as correct as A and
fully honest, so **checking buys honesty and no correctness - all of C's
correctness comes from repair**; R was more correct than C; R used fewer model
calls; no condition turned a scenario A got right into a wrong one. R's range
(0.58-0.65) does not overlap C's (0.29-0.45), and it is less than half as wide:
code repair has no sampling in it, a model's revision does.

**The caveat, stated here rather than left for a reader to find.** Part of R's
advantage is that it holds the network: when it replans a walking leg it uses the
same planner the agent uses everywhere. The argument for R is not that it reasons
better - it is that the party which already has the ground truth should do the
repair, rather than describing the ground truth to a party that does not and
asking it to try again. R keeps the model's itinerary - which services, where to
change - and replaces only the parts the model cannot be trusted with.

A second caveat: each condition makes its own model call, so C_check_only and A do
not always receive the same route (they share one in 73% of scenarios across the three runs: 0.74, 0.71, 0.74).
The decomposition therefore holds across the scenario set rather than scenario by
scenario. A design in which every condition starts from one shared first route
would make it exact; given that C_check_only matched A's correctness in all three
runs, it would not change a conclusion, and it is left as future work.

**Code.** `routing.repair()` (`src/routing.py:1595`) and `routing.next_departure()`
(`src/routing.py:1555`); the tool plumbing `_repair()` (`src/agents/network.py:940`);
conditions `run_check_only` and `run_code_repair` (`src/conditions.py:528`, `:565`).

**Evaluation.** 8 unit tests in `src/test_routing.py`, four of them pinning what
repair must NOT do: fake a change, alter its input, move a traveller onto a bus that
could be going anywhere, or claim a limit is met that no leg can meet. 3 evaluation
cases (`repair: true`), including the console's own demonstration question, where
one violation is repaired and the other is correctly reported as unrepairable.
2 condition tests in `src/test_conditions.py`.

---

## 13. A claimed walking time is judged against its distance, at 2.5 m/s

**Decision.** A walking segment may not claim to take less time than its distance
allows at 2.5 m/s. The distance is the longer of the claimed and the measured one,
less the 60 m snapping tolerance the distance check already allows; the time is
`duration_s`, or arrive minus depart. Slower is never wrong - people stop, and walk
slowly. When `max_duration_s` is summed from segments, a walking leg counts for no
less than that floor, as `max_walking_distance_m` counts the measured distance
rather than an under-claim. Repair replans a leg claimed too fast, which gives it
the planning pace.

**Why.** Found in the 25 Sep console probe. Asked whether "a 10-minute walk from
South Bank to Roma Street" was right, the router put `duration_s: 600` on the
segment, and `validate_route` answered valid: it judged claimed distances and
never claimed times. The walk is 1,816 m. The final answers were right only
because the router did the arithmetic itself, and it came out differently on each
run (21, 21-22, 22-24 minutes) - which is the model judging, the thing row 1 argues
against.

**Why 2.5 m/s.** It is a ceiling on a *claim*, not a planning figure; every time
the agent reports still uses 1.4 m/s. 2.5 m/s is 9 km/h, faster than anyone keeps
up on foot, so a claim beyond it is impossible rather than brisk, and the check
never argues with a fast walker. The planner's own legs are 1.4 m/s and cannot
trip it. The cost of the margin is that a claim between the two - "15 minutes"
for this walk, 2.0 m/s - passes. That is the right side to err on for a check
whose failures are reported as impossible.

**Literature.** *Needed* for the two figures: a source for 1.4 m/s as preferred
adult walking speed (pedestrian planning guidance), and one for the upper end of
sustained walking speed that 2.5 m/s sits above. Neither has been read yet.

**Data.** The South Bank claim: 1,816 m measured, fastest possible (1,816 - 60) /
2.5 = 702 s against 600 claimed; 1,297 s, about 22 minutes, at the planning pace.

**This changes the experiment's verifier.** The three runs in rows 1 and 12 used
the verifier without it. Their scenarios are judged on distances, services and
constraints, and the final experiment batch is run after this change, so the
reported figures come from one verifier.

**Code.** `MAX_WALKING_SPEED_MPS` (`src/routing.py:63`), the check
`claimed_walk_times_possible` (`src/routing.py:1188`), `duration_s` read in
`read_segments`.

**Evaluation.** 7 unit tests in `src/test_routing.py` (from line 1167), including
that the planner's own legs pass, that clock times are judged like `duration_s`,
and that repair replans a leg claimed too fast. 1 evaluation case: the 25 Sep claim
itself, through the live tool.

---

## What this register says is missing

1. **Nine rows need literature.** Nothing can be cited that has not been read;
   the leads above are starting points, not references.
2. ~~Row 3's headline measurement is not reproducible.~~ Done 23 Sep 2026:
   `src/measure_surface_impact.py`. The re-derivation confirmed the direction of the
   claim and corrected its mechanism, which is the argument for writing these down.
3. ~~No malformed-input evaluation case~~ (row 7). Covered since 23 Sep by
   `src/conformance.py`, which generates hostile calls from each tool's own schema
   (63 at the last run) and checks the server survives them. It found one real
   problem - free text reaching the model unbounded at 23 times the cost of a
   normal call - now bounded.
4. **Tool selection is measured through our own router only** (row 11): 0.96
   over thirty unseen questions, by one model through the console's prompt. The
   week 12 claim needs the Orchestrator.
5. **Nine rows need literature; rows 12 and 13 are the newest.** Rows 1 and 12
   carry the thesis, so they come first.

Rows 2, 6, 7 and 9 are marked as needing no literature. That is a claim in its own
right and should survive a supervisor disagreeing with it; if a citation is
expected for engineering judgement, these are the four to argue about.

---

## Deviations from the proposal

Chengbo's guidance in week 9: the proposal is not binding, provided the agent, the
report and the main theme hold. Each deviation is listed with the row that gives
its reason. **Check this list against the proposal text before submission** - it
is compiled from the project's own history, not from a fresh reading of the
proposal.

| Change | Why | Row |
|---|---|---|
| `find_shortest_route` withdrawn, `compare_route_options` in its slot | Shortest-path is what every routing agent already offers; the trade-off is what this one can uniquely measure | 3 |
| Timetable widened to all of SEQ; walking graph kept metropolitan | A service's existence is checkable wherever the stop is; a 30 km walk is not a route | 6 |
| Route repair added (`repair=true`, condition R) | Extends the agent beyond validation; measured as more correct and cheaper than model repair | 12 |
| Two conditions added (C_check_only, R_code_repair) | C's result could not say whether checking or repairing earned it | 12 |
| "Self-review is worse than no check" not claimed | Did not survive repetition; the narrower claim does | 1 |
