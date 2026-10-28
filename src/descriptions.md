# Tool descriptions - v1 and v2

**The specification requires two versions of every domain tool description:** `v1`,
the initial draft, and `v2`, the refined version informed by testing. Only v2 is
served live. This file is submitted with the final report.

The Orchestrator never reads this agent's code. Choosing whether to call it, it sees
**only the tool name, the description, and the parameter descriptions**. That text is
the whole interface, and it is graded. A useful description covers four things: what
it does concretely, where its answers are valid, how fresh its data is, and what it
does *not* do - the most-skipped and most valuable, because it stops the Orchestrator
calling this agent for questions it would answer badly.

A revision is only worth recording with the evidence that prompted it. "Sounded
better" is not evidence.

## How to read this file

| Section | What it is |
|---|---|
| **v1 - initial draft**, under each tool | What the tool was first written with, before testing |
| Interim revision 1, under each tool | Early September, against behaviour observed running the agent |
| Interim revision 2, after the tools | 18 September, after the Week 7 briefing; one table across all tools |
| **v2 - served live**, at the end | The descriptions the agent serves now, copied verbatim from its `tools/list` on 7 Oct 2026, and every change since 18 September with its evidence |

The submission pair for each tool is its **v1** and its **v2 - served live**. The interim
revisions are kept because each row is the evidence for a change, and the evidence is
what the before/after analysis in the report reads.

Five tools are served: `geocode_place`, `parse_route_constraints`,
`plan_verified_route`, `validate_route` and `compare_route_options`. Tool 1,
`find_shortest_route`, was withdrawn in week 7 and its slot given to
`compare_route_options`; its drafts stay because the reasoning that retired it is part
of what this file is for. `geocode_place` came with the course kit and was revised
here, so its v1 is the kit's text.

---

## Tool 1 - `find_shortest_route` (withdrawn, week 7)

**Withdrawn after the Week 7 sprint review.** The review's finding was that "nearest +
compatible" is the floor for a charging agent and that the same convergence had happened
in every domain; for a routing agent the floor is *shortest*. This tool was one of five
slots spent on the answer every routing agent in the cohort already gives - and
`plan_verified_route` gives it too, with a verdict attached, when no constraints are
stated. The slot now holds `compare_route_options` (Tool 5). Two further facts settled
it: nothing in the evaluation set needed this tool that `plan_verified_route` could not
serve, and the claim in its own code comment - that it was the experiment's control
condition - had quietly stopped being true, because condition A calls `routing.plan_route`
directly and never went through MCP at all.

### v1 - initial draft
> Compute the shortest path between two points on the local road and footpath network.
>
> Answers "what is the shortest way from A to B" for trips entirely inside this
> agent's bounded coverage area (inner-to-middle Brisbane). The distance and the
> ordered sequence of road segments are computed deterministically from OpenStreetMap
> way data with Dijkstra's algorithm over a cached graph.
>
> Covers trunk, primary, secondary and tertiary roads, residential streets and
> footways/paths inside lat -27.60 to -27.35, lon 152.90 to 153.15. Does NOT use
> public transport, does NOT know about traffic or road closures, and does NOT check
> any rider preference such as a walking limit.

### Interim revision 1 - refined
> Compute the shortest walking path between two points, with no constraint checking.
>
> Returns the minimum-distance route across the local road and footpath network, as
> an ordered list of coordinates plus the total distance. The distance and the path
> are computed with Dijkstra's algorithm over a cached OpenStreetMap graph; nothing
> is asked of a model.
>
> Use this only when the request is plain "shortest way from A to B" with no stated
> requirement. If the traveller asked for anything — a walking limit, no steps, an
> arrival time, particular modes — call plan_verified_route instead, which honours
> those and checks the result against them. This tool will happily return a 3 km walk
> to someone who said they can manage 800 m.
>
> Covers the full walkable OpenStreetMap network — roads of every class, footways,
> paths, steps, shared cycleways, laneways and service lanes — inside lat -27.60 to
> -27.35, lon 152.90 to 153.15, not all of South East Queensland. Ways closed to
> walkers (private access, foot=no) are excluded, and vehicle one-way rules are ignored
> because they do not bind a pedestrian. Both points must be inside that box or the
> request is declined. Built from an OpenStreetMap snapshot refreshed at most every 30
> days, so very recent construction may be missing. Does NOT use public transport, does
> NOT know traffic or closures, and does NOT check any preference or constraint
> whatsoever.

### What changed and why

| # | Change | Evidence that prompted it |
|---|---|---|
| 1 | First line now says "walking path… with no constraint checking" | Once `plan_verified_route` existed, two tools on this agent both answered "route from A to B". The distinguishing fact is that this one does not verify, so it belongs in the line a router actually reads |
| 2 | Added a routing rule naming the sibling tool and when to prefer it | Direct mitigation for row 1: the cheapest way to stop a mis-selection is to name the alternative rather than hope the router infers it |
| 3 | Added the concrete failure — "will happily return a 3 km walk to someone who said 800 m" | Observed in running: given `max_walking_distance_m: 800` this tool returns the 5,057 m route and reports nothing wrong, because checking is not its job. The abstract "does not check constraints" did not convey that the output is actively misleading in that case |
| 4 | Stated snapshot freshness ("at most every 30 days") | The graph is cached for 30 days (`data.road_graph`), so answers can lag recent construction. Freshness is one of the four graded elements and v1 omitted it |
| 5 | Added "turn restrictions" to the gap list | Confirmed while implementing `_way_direction`: OSM turn-restriction relations are not fetched by this agent's Overpass query, so they are genuinely unmodelled |
| 6 | Coverage restated as the full walkable network, with pedestrian one-way semantics | Found while building the edge-case scenarios: the graph omitted steps, service lanes, laneways, unclassified roads and shared cycleways — about 34,000 ways, 27% of those in the area — and imposed vehicle one-ways on pedestrians. Five of eight ground-truth distances were overstated beyond the snap tolerance, by up to 261 m. The interim-1 text had faithfully described a network that was wrong |
| 7 | Removed "turn restrictions" from the gap list | Reversal of row 5. Turn restrictions bind vehicles, not pedestrians; listing them implied a limitation a walking router does not have |

### Parameter descriptions

| Parameter | v1 | Interim 1 |
|---|---|---|
| `origin_lat` | WGS84 latitude of the trip origin, in South East Queensland. | WGS84 latitude of the trip origin. |
| `origin_lon` | WGS84 longitude of the trip origin, in South East Queensland. | WGS84 longitude of the trip origin. |
| `dest_lat` | WGS84 latitude of the destination. | WGS84 latitude of the trip destination. |
| `dest_lon` | WGS84 longitude of the destination. | WGS84 longitude of the trip destination. |

> "in South East Queensland" was **removed** from the parameter text because it was
> wrong: coverage is the smaller `NETWORK_BBOX`, not SEQ, and repeating a looser bound
> on every parameter contradicted the tool description above it.

---

## Tool 2 - `parse_route_constraints`

### v1 - initial draft
> Turn a free-text travel requirement into a structured constraint set.
>
> Extracts walking limits, transfer limits, permitted modes and time windows from a
> requirement written in ordinary language, and returns them in the shape the other
> tools on this agent accept.
>
> Does not geocode and does not plan a route.

### Interim revision 1 - refined
> Turn a free-text travel requirement into a machine-checkable constraint set.
>
> Reads a sentence a person would actually say and returns the numeric thresholds
> inside it — walking limit in metres, transfer limit, permitted modes, way types to
> avoid, arrival and departure windows — in the exact shape validate_route and
> plan_verified_route accept. Use it when you have a requirement in prose and need it
> as parameters.
>
> Only states what the text states. A vague preference with no number in it ("I don't
> like long walks") yields no constraint rather than a guessed threshold, because a
> guessed 800 m would then be verified against and reported as satisfied, which would
> be a fabricated finding. An empty constraint set is a normal, correct result. The
> reply also reports how many unrecognised items were discarded.
>
> Does NOT geocode, does NOT plan or evaluate any route, and does NOT read the
> traveller's history or infer anything about them. Constraint types outside the fixed
> list above cannot be represented and are dropped, not approximated — fares, comfort,
> crowding and step-free boarding are not modelled anywhere in this agent. Needs a
> Claude API key; extraction is the one step here that is not arithmetic.

### What changed and why

| # | Change | Evidence that prompted it |
|---|---|---|
| 1 | Spelled out that an empty constraint set is a correct result | A router reading v1 has no way to know whether `{}` means failure. It is the right answer for "get me to the CBD", and a retry against another agent would be wasted |
| 2 | Added why a vague preference yields nothing, **with its downstream consequence** | The failure is invisible from outside: a guessed threshold does not stay a guess, it gets verified against and comes back reported as satisfied, manufacturing a finding. Stating the consequence is what makes the limit legible |
| 3 | Named the API-key dependency | Observed in running: with no key (or a rejected one) this tool declines while every other tool on the agent still answers. A router that knows this can route around one dead capability instead of assuming the agent is down |
| 4 | Listed what cannot be represented (fares, comfort, crowding, step-free boarding) | These are plausible things to ask a "constraints" tool for, and exactly the requests that belong to other domains. Naming them is what stops the call arriving |
| 5 | Added "does NOT read the traveller's history or infer anything about them" | The spec forbids personal data; saying so in the interface makes the boundary visible to a router that might otherwise try to pass a profile |

### Parameter descriptions

| Parameter | v1 | Interim 1 |
|---|---|---|
| `requirement` | The traveller's requirement in free text. | The traveller's requirement in their own words, e.g. 'get me there by 9am, I can't do stairs and I'd rather not walk more than 800 metres'. |

> The worked example was added because "free text" did not signal that a *whole
> sentence* is wanted — a router given that phrasing tends to pass a keyword.

---

## Tool 3 - `plan_verified_route`

### v1 - initial draft
> Plan a route between two points that satisfies a set of constraints.
>
> Computes a route and checks it against the constraints, re-planning if a check
> fails, and returns the route together with a verification verdict.
>
> Covers inner-to-middle Brisbane. Does not guarantee optimality and does not use
> real-time delays.

### Interim revision 1 - refined
> Plan a walking route that satisfies stated constraints, and report whether it does.
>
> Computes a route, then checks the result against the network and against the
> constraints in ordinary code — segment continuity, whether every leg exists on the
> mapped network, walking distance, way types, timing — and revises and retries when a
> check fails and a different plan could fix it. The verdict comes back with the
> route: per-constraint pass or fail, the observed value, the threshold it was
> measured against, and how many planning attempts it took.
>
> A failed verdict is still returned, with the route and the violations, rather than
> replaced by an apology. Some requirements cannot be met — the shortest walk between
> two points may simply exceed a stated walking limit — and reporting the shortfall
> with its numbers is more useful than reporting nothing.
>
> Covers inner-to-middle Brisbane (lat -27.60 to -27.35, lon 152.90 to 153.15) from an
> OpenStreetMap snapshot at most 30 days old. Both points must be inside it. Plans
> WALKING ROUTES ONLY — it does not plan bus, train or ferry legs and cannot satisfy a
> request that requires them, so route a multimodal journey to a public-transport
> planner instead. It does NOT guarantee an optimal route, does NOT use real-time
> delays, traffic or closures, and does NOT price a journey. To check a route that
> already exists rather than build one, use validate_route.

### What changed and why

| # | Change | Evidence that prompted it |
|---|---|---|
| 1 | "WALKING ROUTES ONLY" in capitals, with the instruction to route multimodal elsewhere | The largest mis-selection risk on this agent. v1's "plan a route… covers inner-to-middle Brisbane" reads as a general journey planner, and the Collective contains a Public Transport agent whose job that is. Confirmed in running: a request with `allowed_modes: ["bus","train"]` can only be declined |
| 2 | Stated that a failed verdict still returns the route and the violations | Observed with an 800 m limit on a 5.1 km walk: the response is `valid: false` **plus** the route and the measured 5,057 m. A router expecting either success or an error would otherwise discard a useful answer |
| 3 | Listed what the verdict contains — observed value, threshold, attempt count | v1 promised "a verification verdict" without saying it is per-constraint and evidenced. That is the differentiator against every other routing agent in the domain, and it was invisible |
| 4 | Added the cross-reference to `validate_route` for routes that already exist | The two are easy to confuse: one builds and checks, the other only checks. Naming the split in both descriptions is cheaper than letting a router guess |
| 5 | Added snapshot freshness and "does NOT price a journey" | Freshness was missing (graded), and fares are a live capability in another domain — a "verified route" tool is a plausible place for a router to look for a total cost |

### Parameter descriptions

| Parameter | v1 | Interim 1 |
|---|---|---|
| `origin_lat` / `origin_lon` | WGS84 coordinates of the origin. | WGS84 latitude/longitude of the trip origin. |
| `dest_lat` / `dest_lon` | WGS84 coordinates of the destination. | WGS84 latitude/longitude of the trip destination. |
| `constraints` | The constraint set to satisfy. | Constraint set as returned by parse_route_constraints, e.g. `{"max_walking_distance_m": 800, "avoid_highway_types": ["steps"]}`. Pass `{}` when the traveller stated no requirements. |
| `departure_time` | When the traveller departs. | When the traveller leaves, ISO 8601 with offset, e.g. '2026-08-26T08:30:00+10:00'. Needed for any arrival or departure constraint to be checkable. |

> Both revisions add a **worked value**. `constraints` in particular is a nested
> object, and a JSON schema type of `object` tells a router nothing about the keys —
> the example is the only place the shape is actually communicated. `departure_time`
> gained the reason it matters: without it, a time constraint silently cannot be
> evaluated, and the verifier reports it as unsatisfied rather than assumed.

---

## Tool 4 - `validate_route`

### v1 - initial draft
> Check whether a route is valid against the network and a set of constraints.
>
> Returns a per-constraint verdict with the observed value and the threshold, for a
> route produced by any planner.
>
> Does not generate an alternative route and does not rank routes.

### Interim revision 1 - refined
> Check whether a route someone else produced is valid, and say why with evidence.
>
> Takes a route from any planner — another agent, a model, a journey planning API —
> and returns a per-constraint verdict: what was checked, whether it passed, the
> observed value, the threshold, and the evidence behind the call. Checks that
> segments join up, that walking legs exist on the real OpenStreetMap network rather
> than crossing a river that has no bridge, that times run forwards and connections
> can actually be made, and that stated limits on walking distance, transfers, modes
> and arrival times hold.
>
> Every verdict is computed arithmetically against network and timetable data. No
> model is asked whether the route looks right, which is the point: a confident
> description of an impossible route is exactly the failure this catches. Reports a
> constraint satisfaction score alongside the pass/fail so a partial failure is
> visible as a partial failure.
>
> Walking legs are verified against the mapped network for inner-to-middle Brisbane
> (lat -27.60 to -27.35, lon 152.90 to 153.15), from a snapshot at most 30 days old;
> legs outside that box are reported as unverifiable rather than passed. Bus, train
> and ferry legs are checked for continuity, mode and timing but their track geometry
> is NOT verified — this agent does not model the rail or busway network itself. Does
> NOT generate an alternative route, does NOT rank or score routes against each other,
> does NOT judge comfort, crowding, safety or cost, and never edits the route it was
> given.

### What changed and why

| # | Change | Evidence that prompted it |
|---|---|---|
| 1 | Opening line names the reuse case: "a route **someone else** produced" | This is the tool other agents in the Collective are meant to call, and v1 read like a private helper. If the Orchestrator does not understand that it accepts a competitor's output, the capability is invisible at the Showcase |
| 2 | Named a concrete catch — "crossing a river that has no bridge" | An abstract "checks the route is valid" does not tell a router which class of error is caught. Drawn from probing real Brisbane River crossings during development |
| 3 | Added "No model is asked whether the route looks right" | The differentiator against a self-reviewing agent, and the project's actual claim. It is the one sentence separating this from a peer agent that asks an LLM to sanity-check a route |
| 4 | Split verification honesty by mode — walking legs verified, transit geometry not | Implemented and confirmed in running: `network_topology_verified` is false when walk legs fall outside the box, and transit track geometry is never checked. v1 implied a uniform verification the agent does not perform |
| 5 | Extended the "does not" list to comfort, crowding, safety and cost | A tool called `validate_route` invites every kind of "is this route OK?" question, and only some are answerable here |
| 6 | Added "never edits the route it was given" | Makes it safe to call speculatively — a router weighing whether to check another agent's answer needs to know this is read-only |

### Parameter descriptions

| Parameter | v1 | Interim 1 |
|---|---|---|
| `segments` | The route to check. | The route to check, as an ordered list of segments. Each segment needs `{"from": {"lat","lon"}, "to": {"lat","lon"}, "mode"}` where mode is walk, bus, train, ferry or tram. Optional per segment: distance_m, duration_s, highway_type, route_id, and depart/arrive as ISO 8601 timestamps — timing checks need those. |
| `constraints` | The constraints to check against. | What the traveller asked for, as returned by parse_route_constraints, e.g. `{"max_walking_distance_m": 800, "max_transfers": 1}`. Pass `{}` to check structural validity alone. |
| `expected_origin` | Where the route should start. | Where the route was supposed to start, `{"lat": .., "lon": ..}`. Supply it to check the route actually goes where it was asked to. |
| `expected_destination` | Where the route should end. | Where the route was supposed to end, `{"lat": .., "lon": ..}`. |

> `segments` carries the heaviest revision on this agent, deliberately: it is a list
> of nested objects, the JSON schema renders it as `array`, and a caller that guesses
> the shape gets a refusal. Naming which fields are optional **and what is lost
> without them** ("timing checks need those") turns a silently-skipped check into an
> informed choice by the caller.

---

## Tool 5 - `compare_route_options`

Replaces Tool 1. Where the withdrawn tool answered "how far is it", this one answers
the question a verdict cannot: *"I can't manage that - what does the way round cost
me?"*

### v1 - initial draft
> Compare walking route options for a traveller with a mobility need. Plans the
> shortest route, the step-free route and the sealed-surface route, and reports the
> distance of each so the traveller can choose.

### Interim revision 1 - refined (no longer served; see v2 at the end)
> Compare the walking routes available to someone with a mobility need, and price each.
>
> Answers the question a verdict cannot: "I can't manage stairs - what does the way
> round cost me?" Plans the shortest walk, the step-free walk, the walk on sealed
> ground only, and the walk avoiding both, then measures each against the traveller's
> stated requirements and reports what each option gives up: extra metres over the
> shortest, how many flights of steps and how many of those have a handrail, how many
> metres of unsealed ground, and how many crossings have a lowered kerb.
>
> Use this when a requirement might not be satisfiable, or when the traveller has a
> mobility need at all - a wheelchair, a pram, a mobility scooter, a bad knee, luggage.
> Where no option satisfies everything, that is stated plainly and the options are
> still returned with their costs, so the traveller chooses instead of being told no.
> Where the shortest walk already satisfies everything, the options collapse into one
> and it says so.
>
> Nothing here is weighted or scored. Options are ordered by whether they satisfy every
> stated requirement, then by how many they breach, then by distance - a preference
> ranking invented by this agent would be exactly the unjustified design choice it
> exists to argue against. Every number is computed from the network; no model is asked
> for an opinion.
>
> Covers the Brisbane metropolitan area (lat -27.75 to -27.15, lon 152.70 to 153.35 -
> Ipswich, Redcliffe, Logan and the Redlands included, the Gold Coast and the Sunshine
> Coast not), from an OpenStreetMap snapshot at most 30 days old. WALKING ONLY: it does
> not compare bus, train or ferry options. Surface and handrail figures are only as
> complete as OpenStreetMap - about three quarters of this network states a surface and
> about half its steps state a handrail - and each option reports its own coverage, so
> an option with no unsealed ground found is not the same claim as an option proven
> sealed. To plan and verify a single route, use `plan_verified_route`; to audit a
> route that already exists, use `validate_route`.

### What changed and why

| # | Change | Why |
|---|--------|-----|
| 1 | Opening line says "and price each" rather than "compare options" | The draft read as a convenience - three routes instead of one. The capability is the *cost* attached to each option, which is what makes the reply a decision rather than a list. A selector comparing first lines has to see that in the first line |
| 2 | Names who this is for: a wheelchair, a pram, a mobility scooter, a bad knee, luggage | The draft said "a mobility need", which a router may not match to "I'm pushing a pram". The list is deliberately wider than disability: the same constraint serves a traveller with a suitcase, and a description that only says "accessibility" gets called for less than it can serve |
| 3 | States what happens when nothing satisfies everything | The draft implied every option is offered on equal terms. The case this tool was built for is the one where none of them works, and a router needs to know it will still get an answer - measured, with the shortfall named - rather than a refusal |
| 4 | States that identical options collapse into one | Observed on the first run: between Kangaroo Point and the CBD the shortest walk is already step-free and sealed, so all four variants are one route. Returning it four times would look like deliberation and tell the traveller nothing, and a router that expected four options would have to work out that they were the same |
| 5 | Added the ordering rule, and that nothing is weighted | A tool that returns ranked options invites the question "ranked how?". The honest answer is the only defensible one here: satisfies-everything first, then fewest breaches, then distance. Any weighting between "327 extra metres" and "four flights of stairs" would be this agent inventing a preference on the traveller's behalf, which is the design failure the whole project argues against |
| 6 | Added the tagging-completeness caveat per option | The figures are only as good as OpenStreetMap: 72.8% of edges state a surface, about half of steps state a handrail. "No unsealed ground found" and "proven sealed" are different claims and the response reports which one it is making |
| 7 | Added cross-references to the other two route tools | Three tools on this agent answer to "route": plan one, audit one, compare several. The same internal ambiguity that interim revision 1 fixed for the others applies here, and naming the sibling is cheaper than hoping a router infers the split |

### Parameter descriptions

| Parameter | Interim 1 |
|---|---|
| `origin_lat` / `origin_lon` | WGS84 latitude / longitude of the trip origin. |
| `dest_lat` / `dest_lon` | WGS84 latitude / longitude of the trip destination. |
| `constraints` | What the traveller needs, e.g. `{"avoid_highway_types": ["steps"], "max_walking_distance_m": 2000}`. Every option returned is judged against these, so state them even when you expect them to be impossible — that is the case this tool is for. Pass `{}` to see the alternatives with no verdict attached. |
| `requirement` | The traveller's requirement in their own words, e.g. "I use a wheelchair and can manage about 2 km". Pass this INSTEAD of constraints to do the whole job in one call. |
| `departure_time` | When the traveller leaves, ISO 8601 with offset. Only needed for timing constraints. |

---

## Cross-cutting observations

Three things emerged from writing these that belong in the report rather than in a
table.

**1. The largest revisions were about tools on the *same* agent, not rival agents.**
The cohort framing is that description quality wins selection against a peer's agent.
In practice the sharpest ambiguity was internal: `find_shortest_route`,
`plan_verified_route` and `validate_route` all answer to "route from A to B", and the
fix in every case was to name the sibling and the condition for preferring it.

**2. A limit is only legible when its consequence is stated.**
"Does not check constraints" and "will happily return a 3 km walk to someone who said
they can manage 800 m" are the same fact. Only the second tells a router what it is
risking. The same rewrite applies to the guessed-threshold note on
`parse_route_constraints`.

**3. Some interim-1 rows still await external evidence.**
The rows above cite behaviour observed running the agent locally — real evidence, but
not the strongest kind. Whether `find_shortest_route` is still mis-selected for
constrained requests, and whether `validate_route` gets called on another agent's
output unprompted, can only be settled with Orchestrator data. Those are the rows to
re-examine after the Week 9 handshake test, and the natural input for the next revision.

---

## Interim revision 2 - widened coverage, sharper opening lines (18 September)

Prompted by the Week 7 briefing, which described how the Showcase call actually works:
the Orchestrator sees every tool from every server at once, picks by reading the
descriptions, and makes **one call per agent with no retry**. Two consequences that interim
revision 1 had not been written against.

**First, coverage is not a neutral disclosure.** A request landing outside the study
area gets an honest structured refusal - correct behaviour, and specified behaviour -
but it demonstrates nothing, and there is no second call. The study area was widened
from inner Brisbane to the metropolitan area (1,428,640 nodes against 573,140, 6.2x
the area for 2.5x the network, about a minute to build and under four seconds to load),
so every description that quoted the old box had to move with it.

**Second, the first line is the whole pitch.** A selector reading forty tools sees the
opening sentence of each. Two of ours opened with the words every competing tool opens
with.

| # | Tool | Change | Why |
|---|------|--------|-----|
| 1 | `find_shortest_route` | Coverage restated as the Brisbane metropolitan area, with the suburbs it reaches named (Ipswich, Redcliffe, Logan, the Redlands) and the ones it excludes named too (Gold Coast, Sunshine Coast, Toowoomba) | A selector reasons in place names, not in decimal degrees. "lat -27.75 to -27.15" does not answer "can it walk me across Ipswich?", and the excluded names are what stop the call arriving at all. The opening line stays blunt about doing no checking - that is what steers a constrained request to the sibling tool |
| 2 | `plan_verified_route` | Opening line changed from "Plan a walking route that satisfies stated constraints, and report whether it does" to "Plan a walking route, then prove in code whether it meets the stated limits" | "Plan a walking route" is the opening every route planner in the cohort has, including the Public Transport agent's. The differentiator - that the verdict is computed rather than asserted - was in the third sentence, where a selector comparing first lines never reached it |
| 3 | `validate_route` | Opening line now names whose routes it audits: "another agent, model or journey planner" | Interim 1 said "a route someone else produced", which reads as a human's route. This is the one tool in this agent meant to be called on **another agent's output**, and the Orchestrator can only see that if the sentence says so |
| 4 | `plan_verified_route`, `validate_route` | Both now take an optional `requirement` in plain language, and say in the reply which source the constraints came from | The chain `parse_route_constraints` → route tool cannot complete when the Orchestrator makes **one call per agent with no retry**: it would call one of the two and get half an answer. A tool that can read the sentence itself needs no second call. Structured constraints still win when both are supplied, and extraction failing now degrades to an unconstrained route that is labelled as one, rather than to a refusal |
| 5 | `validate_route` | Segments and endpoints are read in whatever shape they arrive - `origin`/`from`, `[lat, lon]` or `{"lat", "lon"}`, distances quoted as text - and constraint keys accept unambiguous synonyms | Same one-call logic, applied to the tool most likely to be handed another agent's output. Its call shape is the most complex on this agent, and a route rejected over a key spelling spends the call for nothing. Values stay strict: a coordinate that is not a coordinate is still refused, because guessing one would put a made-up number into a verdict. The alias that mattered most was `max_walking_distance` without the unit suffix, which used to be dropped silently - and a dropped constraint comes back reported as satisfied |
| 6 | all four | Accessibility made visible: the two route tools now say they plan for and audit step-free and sealed-surface journeys and report surface coverage, `parse_route_constraints` names surfaces among what it extracts, and `find_shortest_route` sends such requests away and admits it would hand a gravel track to a wheelchair user | Found by auditing the agent against the Week 7 standard rather than by testing it: **not one of the four descriptions mentioned surface, wheelchair, kerb, accessibility or pram.** The constraint was implemented, the graph carried the tags, three scenarios and two evaluation cases covered it - and the one artefact that decides whether the capability is ever reached said nothing about it. A capability the Orchestrator cannot see is one it cannot choose, which makes this the cheapest correction in this table and among the most consequential |
| 7 | the tool budget | `find_shortest_route` withdrawn; its slot now holds `compare_route_options` (Tool 5 above) | The Week 7 review's central finding was convergence: six of seven charging proposals defined "suitable" as nearest + compatible, and every domain had its own version of it. For routing that version is "shortest", and this agent was spending a fifth of its tool budget on it while `plan_verified_route` already returned the same route with a verdict. The slot now answers something no competing tool does. Recorded here rather than in a commit message alone because it is a description-level decision: the tool was not removed for being wrong, but for being indistinguishable |
| 8 | `validate_route` | Says that walking legs and transit legs are covered differently: walking across metropolitan Brisbane only, transit across the whole of South East Queensland | The timetable index had been cut to the walking box for no better reason than that one constant existed. They answer different questions - a walking route to the Gold Coast is not a route anyone takes, but whether the 8:05 from Nerang runs is a question the feed settles wherever the stop is, and the Week 7 lecture used a Gold Coast to Brisbane trip as its own Showcase example. Widening the index took the stop count from 9,712 to 13,119. The description has to state the split, because a router that believes this agent covers only Brisbane will never send it the itinerary it can now check |
| 9 | all three | The coverage box is now asserted by a test (`test_every_tool_description_states_the_current_coverage_box`) | The box is quoted in prose in three descriptions. Widening it meant editing all three by hand, which is exactly the kind of edit that gets missed - and a description advertising a box the agent no longer has is worse than a vague one, because the refusals it triggers look like bugs |

### Still open

Interim 1's unsettled rows remained unsettled at this point, and this revision added one. Whether the widened box is
actually exercised - whether the Orchestrator's prompts land in Ipswich or Logan at all,
rather than all within two kilometres of the CBD - cannot be known from here. If they
never leave the inner city, the widening bought insurance rather than coverage, and the
honest thing is to report it that way.


---

## v2 - served live (7 October 2026)

Copied verbatim from the running agent's `tools/list` (branch
`smac-network-rassivolt-v2`, after the two fixes in rows 7 and 8). This is what the Orchestrator reads. To
compare it with the docstrings, start the stack and list the tools; any difference
means this file is stale.

### `geocode_place`

**v1** (the course kit's text, before any revision here):

> Convert a South East Queensland place name or address into WGS84 coordinates.
>
> Use this before any tool that needs a lat/lon when you were given a place name
> instead of numbers. Do not guess coordinates - a recalled coordinate has no
> source and cannot be cited. Returns the matched address and the OpenStreetMap
> feature type, so you can judge how precise the match is.
>
> Covers South East Queensland; a place resolving outside SEQ is reported as out
> of scope. Returns the single best match and does not disambiguate between
> places sharing a name.

v1 parameter: `place` - Place name, landmark, suburb or address to locate, e.g.
'Toowong Village Brisbane'.

**v2** (served live):

> Convert a South East Queensland place name or address into WGS84 coordinates.
>
> Use this before any tool that needs a lat/lon when you were given a place name
> instead of numbers. Do not guess coordinates - a recalled coordinate has no
> source and cannot be cited. Returns the matched address and the OpenStreetMap
> feature type, so you can judge how precise the match is.
>
> Covers South East Queensland; a place resolving outside SEQ is reported as out
> of scope. Where a name exists in several states, the South East Queensland
> one is preferred. Local shorthand is looked up as the place it means - "the
> CBD" as Brisbane City, "UQ" as the University of Queensland campus - and the
> response says so in interpreted_as. When no place of that name exists in
> South East Queensland, the closest-named place inside the Brisbane walking
> area is returned with a match_note saying so - check matched_name before
> relying on it. Returns one match; it does not list alternatives.

| Parameter | Description served |
|---|---|
| `place` | Place name, landmark, suburb or address to locate, in English as it appears on a map, e.g. 'Toowong Village Brisbane'. Translate a name given in another language first. |

### `parse_route_constraints`

**v2** (served live):

> Turn a free-text travel requirement into a machine-checkable constraint set.
>
> Reads a sentence a person would actually say and returns the numeric
> thresholds inside it - walking limit in metres, transfer limit, permitted
> modes, way types and ground surfaces to avoid, arrival and departure
> windows - in the exact
> shape validate_route and plan_verified_route accept. Use it when you need
> the constraint set itself - to show a traveller what was understood, or to
> reuse it across several calls. Both route tools can read a requirement
> themselves via their own `requirement` parameter, so a route does not need
> this call first.
>
> Only states what the text states. A vague preference with no number in it
> ("I don't like long walks") yields no constraint rather than a guessed
> threshold, because a guessed 800 m would then be verified against and
> reported as satisfied, which would be a fabricated finding. An empty
> constraint set is a normal, correct result. The reply also reports how many
> unrecognised items were discarded.
>
> Does NOT geocode, does NOT plan or evaluate any route, and does NOT read
> the traveller's history or infer anything about them. Constraint types
> outside the fixed list above cannot be represented and are dropped, not
> approximated - fares, comfort and crowding are not modelled anywhere in
> this agent. Step-free access ON FOOT is modelled and checkable; step-free
> BOARDING of a bus or train is not, because the Translink feed this agent
> reads carries no accessibility fields for stops or vehicles. Needs a Claude API key; extraction is the
> one step here that is not arithmetic.

| Parameter | Description served |
|---|---|
| `requirement` | The traveller's requirement in their own words, e.g. 'get me there by 9am, I can't do stairs and I'd rather not walk more than 800 metres'. |

### `plan_verified_route`

**v2** (served live):

> Plan a walking route, then prove in code whether it meets the stated limits.
>
> Computes a route, then checks the result against the network and against
> the constraints in ordinary code - segment continuity, whether every leg
> exists on the mapped network, walking distance, way types, timing - and
> revises and retries when a check fails and a different plan could fix it.
> The verdict comes back with the route: per-constraint pass or fail, the
> observed value, the threshold it was measured against, and how many
> planning attempts it took.
>
> Plans for accessibility, not just for distance. It can exclude steps and
> unsealed surfaces - gravel, dirt, ground, grass - which is what a
> wheelchair, a pram or a mobility scooter actually needs, and it re-checks
> the route it produced against those exclusions. Where no step-free or
> sealed route exists between two points, it says so and names the
> constraint that made it impossible, rather than handing over a route the
> traveller cannot use. The verdict also states how much of the route's
> surface OpenStreetMap records - about three quarters of this network - so
> a clean result is never mistaken for a guarantee.
>
> One call is enough: pass `requirement` with the traveller's own sentence
> and this reads the limits out of it itself, or pass `constraints` if you
> already hold them as structured values. The reply says which it used.
>
> A failed verdict is still returned, with the route and the violations,
> rather than replaced by an apology. Some requirements cannot be met - the
> shortest walk between two points may simply exceed a stated walking limit -
> and reporting the shortfall with its numbers is more useful than reporting
> nothing.
>
> Covers the Brisbane metropolitan area (lat -27.75 to -27.15, lon 152.70
> to 153.35 - Ipswich, Redcliffe, Logan and the Redlands included, the Gold
> Coast and the Sunshine Coast not) from an OpenStreetMap snapshot at most 30
> days old. Both points must be inside it. Plans WALKING ROUTES ONLY - it does not plan bus, train
> or ferry legs and cannot satisfy a request that requires them, so route a
> multimodal journey to a public-transport planner instead. It does NOT
> guarantee an optimal route, does NOT use real-time delays, traffic or
> closures, and does NOT price a journey. To check whether a route or a
> claim someone else made is true - "it's a 300 m walk", "the 740 leaves at
> 8:12" - use validate_route: it judges the claim itself, where planning here
> would leave you to compare the two.

| Parameter | Description served |
|---|---|
| `origin` | Where the trip starts, as the traveller said it - a place name or address in English as it appears on a map ('UQ St Lucia', 'Toowong Village'; translate a name given in another language), or coordinates as '-27.4975,153.0137'. Use this INSTEAD of origin_lat/origin_lon to do the whole job in one call; there is no need to call geocode_place first. Ignored when origin_lat and origin_lon are supplied. |
| `destination` | Where the trip ends, in the same forms as origin, and likewise in English as on a map. Use this INSTEAD of dest_lat/dest_lon. |
| `origin_lat` | WGS84 latitude of the trip origin. Optional if origin is given. |
| `origin_lon` | WGS84 longitude of the trip origin. Optional if origin is given. |
| `dest_lat` | WGS84 latitude of the destination. Optional if destination is given. |
| `dest_lon` | WGS84 longitude of the destination. Optional if destination is given. |
| `constraints` | Constraint set as returned by parse_route_constraints, e.g. {"max_walking_distance_m": 800, "avoid_highway_types": ["steps"], "avoid_surfaces": ["gravel", "dirt", "ground"]}. For a wheelchair, a pram or a mobility scooter use BOTH avoid_highway_types ["steps"] and avoid_surfaces. Pass {} when the traveller stated no requirements. |
| `departure_time` | When the traveller leaves, ISO 8601 with offset, e.g. '2026-08-26T08:30:00+10:00'. Needed for any arrival or departure constraint to be checkable. |
| `requirement` | The traveller's requirement in their own words, e.g. 'get me there by 9am, I can't do stairs'. Pass this INSTEAD of constraints to do the whole job in one call - this tool reads it itself, so there is no need to call parse_route_constraints first. Ignored when constraints are supplied. |

### `validate_route`

**v2** (served live):

> Check whether a claim about a journey is true - and repair the route if it is not.
>
> Use this, not a planner, whenever someone ELSE has said something about a
> route: "it's a 300 m walk from UQ to the CBD", "the 740 leaves Nerang at
> 8:12", "you can do it without stairs", or an itinerary from an app or
> another agent. Planning the real route and comparing it with the claim
> yourself puts the judgement back in a model; this returns a verdict on the
> claim itself, measured. Write the claim as segments - place names are fine
> for the ends - with the claimed figure on the segment. It checks claimed
> distances against the network, claimed walking times against the fastest
> anyone walks (2.5 m/s over the measured distance - "a 10-minute walk" goes
> on the segment as duration_s: 600), claimed services against the timetable,
> and stated limits.
>
> Takes a route from any planner - another agent, a model, a journey planning
> API - and returns a per-constraint verdict: what was checked, whether it
> passed, the observed value, the threshold, and the evidence behind the
> call. Checks that segments join up, that walking legs exist on the real
> OpenStreetMap network rather than crossing a river that has no bridge, that
> times run forwards and connections can actually be made, and that stated
> limits on walking distance, transfers, modes and arrival times hold. For an
> accessibility requirement it checks the ground itself: that no leg uses
> steps or an unsealed surface the traveller excluded, and it reports what
> share of the route states a surface at all, because OpenStreetMap records
> one for about three quarters of this network and silence is not evidence.
>
> Segments are read the way they arrive: "origin"/"from", [lat, lon] pairs
> or {"lat", "lon"} objects, distances quoted as numbers or as text. Pass
> `requirement` with the traveller's sentence to check the route against a
> plain-language requirement in the same call.
>
> Every verdict is computed arithmetically against network and timetable
> data. No model is asked whether the route looks right, which is the point:
> a confident description of an impossible route is exactly the failure this
> catches. Reports a constraint satisfaction score alongside the pass/fail so
> a partial failure is visible as a partial failure.
>
> The two halves of a journey are covered differently, on purpose. Walking
> legs are verified against the mapped network for the Brisbane metropolitan
> area only (lat -27.75 to -27.15, lon 152.70 to 153.35 - Ipswich, Redcliffe,
> Logan and the Redlands), from a snapshot at most 30 days old; a walking leg
> outside that box is reported as unverifiable rather than passed. Bus, train
> and ferry legs are checked across the WHOLE of South East Queensland, from
> the Gold Coast to the Sunshine Coast, because whether a service runs is a
> question the timetable can settle wherever it is - so an itinerary that
> starts at Nerang or Robina gets its transit legs verified even though no
> walking route there could be. Those legs are checked against the Translink
> timetable: that the leg boards at a real stop, and that a service actually
> departs it at the claimed time on that date, on the claimed route if one is
> given. A leg riding a service that does not run is rejected. Their track
> GEOMETRY is still not verified - this agent does not model the rail or
> busway alignment itself, only whether the service exists.
>
> With repair=true, a failing route also comes back repaired where a small
> change can fix it: a walking leg whose distance is impossible, or which
> uses steps or a surface the traveller excluded, is replanned between its
> own two ends; a bus or train that does not run at the claimed time is moved
> to the next departure of the SAME route from the same stop; a route that
> starts or ends away from the requested point gets the walk to it. Every
> change is listed with the reason for it, and the repaired route is verified
> again by exactly these checks. What needs a different itinerary - fewer
> transfers, another mode, a walking total no leg can shorten - is reported
> as unrepairable with the reason, not attempted.
>
> Does NOT plan a journey from scratch (use plan_verified_route), does NOT
> rank routes against each other (use compare_route_options), does NOT judge
> comfort, crowding, safety or cost, and never alters the route it was given:
> a repair is a separate, separately verified copy.

| Parameter | Description served |
|---|---|
| `segments` | The route or claim to check, as an ordered list of segments. Each segment needs "from", "to" and "mode" (walk, bus, train, ferry or tram). The ends may be place names in English as on a map ('UQ St Lucia', 'Roma Street station') as well as {"lat", "lon"} objects, so a claim can be checked in one call. Put the claimed figure on the segment: distance_m for 'a 300 m walk', duration_s for 'a 10-minute walk', route_id with depart for 'the 740 at 8:12'. Optional: highway_type, arrive. e.g. [{"mode": "walk", "from": "UQ St Lucia", "to": "Brisbane CBD", "distance_m": 300}] |
| `constraints` | What the traveller asked for, as returned by parse_route_constraints, e.g. {"max_walking_distance_m": 800, "max_transfers": 1}. Pass {} to check structural validity alone. |
| `expected_origin` | Where the route was supposed to start, {"lat": .., "lon": ..}. Supply it to check the route actually goes where it was asked to. |
| `expected_destination` | Where the route was supposed to end, {"lat": .., "lon": ..}. |
| `requirement` | What the traveller asked for, in their own words. Pass this INSTEAD of constraints to check a route against a plain-language requirement in one call - this tool reads it itself. Ignored when constraints are supplied. |
| `repair` | Set true to get, when the route fails, the smallest change that would make it pass: impossible or excluded walking legs replanned, a service that does not run moved to one that does, a gap to the requested start or end walked. The repaired route is checked again by the same rules and only reported as repaired if it passes. The verdict on the original route is unchanged. |

### `compare_route_options`

**v2** (served live):

> Compare the walking routes available to someone with a mobility need, and price each.
>
> Answers the question a verdict cannot: "I can't manage stairs - what does
> the way round cost me?" Plans the shortest walk, the step-free walk, the
> walk on sealed ground only, and the walk avoiding both, then measures each
> against the traveller's stated requirements and reports what each option
> gives up: extra metres over the shortest, how many flights of steps and how
> many of those have a handrail, how many metres of unsealed ground, and how
> many crossings have a lowered kerb.
>
> Use this when a requirement might not be satisfiable, or when the traveller
> has a mobility need at all - a wheelchair, a pram, a mobility scooter, a bad
> knee, luggage. Where no option satisfies everything, that is stated plainly
> and the options are still returned with their costs, so the traveller
> chooses instead of being told no. Where the shortest walk already satisfies
> everything, the options collapse into one and it says so.
>
> Nothing here is weighted or scored. Options are ordered by whether they
> satisfy every stated requirement, then by how many they breach, then by
> distance - a preference ranking invented by this agent would be exactly the
> unjustified design choice it exists to argue against. Every number is
> computed from the network; no model is asked for an opinion.
>
> Covers the Brisbane metropolitan area (lat -27.75 to -27.15, lon 152.70 to
> 153.35 - Ipswich, Redcliffe, Logan and the Redlands included, the Gold Coast
> and the Sunshine Coast not), from an OpenStreetMap snapshot at most 30 days
> old. WALKING ONLY: it does not compare bus, train or ferry options. Surface
> and handrail figures are only as complete as OpenStreetMap - about three
> quarters of this network states a surface and about half its steps state a
> handrail - and each option reports its own coverage, so an option with no
> unsealed ground found is not the same claim as an option proven sealed. To
> plan and verify a single route, use plan_verified_route; to audit a route
> that already exists, use validate_route.

| Parameter | Description served |
|---|---|
| `origin` | Where the trip starts, as the traveller said it - a place name or address in English as it appears on a map ('UQ St Lucia', 'Toowong Village'; translate a name given in another language), or coordinates as '-27.4975,153.0137'. Use this INSTEAD of origin_lat/origin_lon to do the whole job in one call; there is no need to call geocode_place first. Ignored when origin_lat and origin_lon are supplied. |
| `destination` | Where the trip ends, in the same forms as origin, and likewise in English as on a map. Use this INSTEAD of dest_lat/dest_lon. |
| `origin_lat` | WGS84 latitude of the trip origin. Optional if origin is given. |
| `origin_lon` | WGS84 longitude of the trip origin. Optional if origin is given. |
| `dest_lat` | WGS84 latitude of the destination. Optional if destination is given. |
| `dest_lon` | WGS84 longitude of the destination. Optional if destination is given. |
| `constraints` | What the traveller needs, e.g. {"avoid_highway_types": ["steps"], "max_walking_distance_m": 2000}. Every option returned is judged against these, so state them even when you expect them to be impossible - that is the case this tool is for. Pass {} to see the alternatives with no verdict attached. |
| `requirement` | The traveller's requirement in their own words, e.g. 'I use a wheelchair and can manage about 2 km'. Pass this INSTEAD of constraints to do the whole job in one call. |
| `departure_time` | When the traveller leaves, ISO 8601 with offset, e.g. '2026-09-18T08:30:00+10:00'. Only needed for timing constraints. |

### Changes since 18 September, and the evidence for each

| # | Date | Tool | Change | Evidence that prompted it |
|---|---|---|---|---|
| 1 | 23 Sep | `plan_verified_route`, `compare_route_options` | New `origin` and `destination` parameters that take a place name, an address or coordinates as text; the lat/lon parameters became optional | The Orchestrator makes one call per agent with no retry (Week 7 briefing). With coordinates only, a question naming places needs `geocode_place` first - a second call that never comes. One parameter now does the whole job |
| 2 | 24 Sep | `validate_route` | Opening line "... - and repair it"; a `repair` parameter; the "does NOT" list now names the sibling tool for each thing it does not do | Repair in code was added on Chengbo's week 9 advice and measured more correct than model repair at under half the model calls (register row 12). A capability the router cannot see is one it cannot choose |
| 3 | 25 Sep | `geocode_place` | States that the South East Queensland match is preferred and that local shorthand ("the CBD", "UQ") is looked up as the place it means, reported in `interpreted_as` | The 25 Sep console smoke check: "Brisbane CBD" resolved to an office in West End, and a walk to the city came back 4,452 m instead of about 5.1 km; "UQ St Lucia" resolved to the ferry terminal |
| 4 | 25 Sep | `validate_route`, `plan_verified_route` | `validate_route` now opens claim-first ("Check whether a claim about a journey is true"), with three example claims, and `segments` takes place names and says to put the claimed figure on the segment. `plan_verified_route` now sends claims to `validate_route` | In the 25 Sep probes, asked "it's a 300 m walk - is that true?" and "a 10-minute walk - is that right?", the router planned the real route and compared the numbers itself. The judgement moved back into a model, which is the failure this project argues against |
| 5 | 3 Oct | `geocode_place`, every `origin` / `destination` / `segments` parameter | Names must be given "in English as it appears on a map", translated first; when nothing of that name exists in South East Queensland, the closest-named place in the Brisbane walking area is returned with a `match_note` | Ten unseen prompts: a Chinese question's city-centre phrase was passed verbatim, then translated two ways on two runs, and the walk came back 1,200 m and 4,410 m. "Southbank" resolved to Melbourne, "Central station" to K'gari, "Powerhouse" to a Gold Coast clinic |
| 6 | 3 Oct | `validate_route` | `segments` says to put `duration_s` on the segment for "a 10-minute walk" | The walking-time check (register row 13): `validate_route` had called "a 10-minute walk" over 1,816 m valid, because it judged claimed distances and never claimed times |
| 7 | 7 Oct | `validate_route` | Removed "it does NOT check a claimed walking TIME against walking pace"; it now says claimed walking times are checked against the fastest anyone walks, 2.5 m/s over the measured distance, with "a 10-minute walk" as `duration_s: 600` | Found writing this file: row 4 had added the limit honestly, row 6 added the check and changed the parameter, and the sentence was left behind. The description contradicted its own parameter and understated what the tool does |
| 8 | 7 Oct | `plan_verified_route` (`constraints`), and the requirement extraction behind every `requirement` parameter | A wheelchair, a pram or pushchair, or a mobility scooter now means BOTH no steps and sealed ground; a traveller on foot gets exactly what they state | Observed on 7 Oct through `ask.py`: "Get me to Suncorp Stadium from Central station, I've got a pram" was read as sealed ground only, the 1,975 m route with eight flights of steps passed, and the router said the stairs could not be avoided - with a 2,156 m step-free route available. The guidance named surfaces for a pram and never steps. After the fix, nine phrasings three times each extracted as intended (27 of 27, including the Chinese phrasing), and the same question answered with the step-free route in three runs of three. The first wording of the fix over-reached - "I can't manage stairs" gained sealed surfaces and "a bad knee" gained both - so the rule now applies only to a named wheeled device |
| 9 | 28 Oct | `validate_route` (`segments`) | Says to give `depart` and `arrive` as ISO 8601 with offset, and that a time with no date is read on the date the route states, or today in Brisbane, reported in `times_read_as` | Found on 21 Oct: the example "the 740 at 8:12" invited a bare time, the router sent `"depart": "03:00"` for a 412 that does not run, and the verifier skipped the leg while its evidence said every leg had been checked - the verdict was valid. Fixed in the verifier as well (register row 5): an unreadable time is now reported as not checked, and a bare time is dated rather than dropped |

### Evidence since 18 September, beyond the rows above

The Week 9 rows that interim revision 1 deferred now have evidence of a kind: thirty
unseen questions through the console's router, which chose among four agents' tools
from their descriptions alone, three runs each (`src/robustness.py`): 86 of 90 runs
passed, and all four failures were the router calling no tool at all - never a network
tool chosen wrongly or returning a wrong fact. That is selection by one router model through one prompt, not the Orchestrator,
and the pram case in row 8 shows a pass criterion can be too lenient, so it is evidence
about the descriptions rather than a verdict on them.
