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
| 11 | Tool descriptions name the accessibility capability | needed | ✅ | ✅ | ⚠️ indirect |

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

B is the load-bearing observation. The model did not fail to notice the problem -
it located the right quantity and fabricated a value that cleared the threshold.
That is a different failure from inattention, and it is the one an intrinsic review
cannot fix.

**Code.** `routing.validate()` (`src/routing.py:970`); condition C's replan loop in
`src/conditions.py`.

**Evaluation.** Scenario class `preference-unsatisfiable` (6 scenarios);
evaluation class `edge-and-failure` (17 of 46 cases).

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

**Evaluation.** ⚠️ Indirect. No case tests whether a selecting model *picks* this
agent given a description. **Action: with the API key now available, run a
tool-selection evaluation - this is also the direct evidence for the week 12
claim that an orchestrator has a reason to choose this agent.**

---

## What this register says is missing

1. **Seven rows need literature.** Nothing can be cited that has not been read;
   the leads above are starting points, not references.
2. ~~Row 3's headline measurement is not reproducible.~~ Done 23 Sep 2026:
   `src/measure_surface_impact.py`. The re-derivation confirmed the direction of the
   claim and corrected its mechanism, which is the argument for writing these down.
3. **No malformed-input evaluation case** (row 7), a week 9 handshake requirement.
4. **No tool-selection evaluation** (row 11), which is the week 12 claim itself.

Rows 2, 6, 7 and 9 are marked as needing no literature. That is a claim in its own
right and should survive a supervisor disagreeing with it; if a citation is
expected for engineering judgement, these are the four to argue about.
