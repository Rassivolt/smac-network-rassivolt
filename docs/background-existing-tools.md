# Background: existing tools and services

Drafted for the **Background** criterion, which asks what already solves this problem
and what this project adds beyond it. The proposal's current Section 2 reviews the
research literature ([1]–[10]) but no production tool or service, which is the gap
this text fills.

Slots in as **Section 2.2**, after "2.1 Deterministic Routing and Geospatial Tool Use"
and before the self-correction review (renumber the existing 2.2 and 2.3 to 2.3 and
2.4). Every claim below was checked against the tool's own documentation or source
repository in September 2026; the verification notes at the end record what was
checked and how.

---

## 2.2 Existing Routing Tools and Geospatial Services

The research reviewed above is not the only prior art. Route planning is a mature
production capability, and any claim of novelty has to survive contact with the tools
practitioners actually use. Three classes are relevant, and they fail to close this
project's gap in three different ways.

**Production routing engines solve a different problem correctly.** Open-source
engines including OSRM, Valhalla and GraphHopper compute road and pedestrian routes at
scale, and OpenTripPlanner extends this to multimodal journeys over GTFS schedules
[11]. Commercial APIs such as Google Directions and Geoapify expose comparable
capability as a service [12]. Preference handling is not missing from these systems:
Geoapify accepts restrictions and desired conditions, and constraint families covering
maximum walking distance, maximum transfers and maximum waiting time at interchanges
are well established in the navigation literature [12], [13]. The decisive point is
*where* those constraints sit. In every one of these systems a constraint is an **input
to the planner**, and compliance follows by construction: the engine searches only the
space of routes that already satisfy what it was given. None of them accepts a route
produced elsewhere and reports whether it satisfies a constraint set. The question
"is this route valid?" is one these engines never have to answer, because within their
own boundary it cannot arise.

**Validation tooling exists, but validates a different object.** The transport
ecosystem does have mature validators. MobilityData's canonical `gtfs-validator`
checks a GTFS archive for file integrity, type parsing, value ranges and feed-level
problems such as expiry [14] — that is, it validates *the feed*, establishing that the
timetable is well formed, not that any particular journey over it is feasible. Map
matching sits closer still: Valhalla's Meili takes a noisy sequence of positions and
recovers the most likely path over the network using a hidden Markov model solved with
the Viterbi algorithm, returning the matched geometry snapped to road segments [15].
Meili is genuinely reasoning about whether a trace corresponds to the network, but its
output is a *correction* — a cleaned-up geometry — rather than a *verdict*. It does not
take a constraint set, it does not report which requirement failed and by how much, and
it has no notion of a traveller's stated limit to check against.

**MCP-exposed geospatial tools already exist, and none of them verifies.** Because the
present project is an MCP server, the closest prior art is the set of Model Context
Protocol servers already published for OpenStreetMap. These are not hypothetical: the
`osmmcp` server exposes twenty-five tools spanning geocoding, route fetching over OSRM,
turn-by-turn directions, nearby-place search, commute analysis, polyline encoding and
emissions enrichment [16], and further OSM MCP servers offer overlapping geocoding and
route-planning capability [17]. This matters in two directions. It confirms that a
plain "shortest route" or "geocode this place" MCP tool is already available to any
agent that wants one, so neither is a contribution. It also confirms the gap: across
that tool surface every capability generates, searches or transforms, and not one
accepts an existing route and judges it. Verification is absent from the MCP
geospatial ecosystem, not merely under-served in it.

Taken together, the position is narrower and more defensible than "nobody routes with
LLMs". Planners guarantee the routes they themselves produce; feed validators check the
data; map matching cleans a trace; and the MCP servers already deployed expose the
first of those three. What none of them provides is a verdict on a route that arrived
from somewhere else, measured against the constraints a traveller actually stated, with
the observed value and threshold attached.

That absence is tolerable while a trusted planner is the only thing producing routes,
and stops being tolerable once a language model sits in the loop. The correctness of a
routing engine is conditional on being asked the right question, and it is precisely
that condition an LLM breaks: MobilityBench reports that route-planning agents weaken
markedly once user preferences enter the request [2], and GeoAgentBench finds success
turning on whether an agent supplies correct tool parameters rather than merely
selecting the correct tool [3]. A constraint dropped or mistranslated before the
planner is called produces a route the engine considers entirely valid and the traveller
does not, and no component in the stack above is positioned to notice — the planner
because it was never told, the feed validator because the feed is fine, the map matcher
because the geometry is real. Verification has to be an independent step applied *after*
the route exists, which is the step this project builds and measures.

---

## New references

Continues the proposal's existing numbering, which ends at [10].

```
[11] OpenTripPlanner Project, "OpenTripPlanner: An open source multi-modal trip
     planner," 2026. [Online]. Available: https://github.com/opentripplanner/OpenTripPlanner

[12] Geoapify, "Routing API - routes and directions," 2026. [Online].
     Available: https://www.geoapify.com/routing-api/

[13] G. R. Jagadeesh and T. Srikanthan, "Methods and systems for determining routes in
     a navigation system," U.S. Patent 9 228 841, Jan. 5, 2016.

[14] MobilityData, "gtfs-validator: Canonical GTFS validator project for schedule
     (static) files," 2026. [Online]. Available: https://github.com/MobilityData/gtfs-validator

[15] Valhalla Project, "Meili: map matching," Valhalla Documentation, 2026. [Online].
     Available: https://valhalla.github.io/valhalla/meili/

[16] NERV Systems, "osmmcp: OpenStreetMap MCP server providing precision geospatial
     tools for LLMs via Model Context Protocol," 2026. [Online].
     Available: https://github.com/nervsystems/osmmcp

[17] J. Wiseman, "osm-mcp: Model Context Protocol server for OpenStreetMap data," 2026.
     [Online]. Available: https://github.com/wiseman/osm-mcp
```

---

## Suggested edit to the Knowledge Gap (Section 2.3, currently 2.3)

The existing gap paragraph argues only against the research literature. One sentence
inserted after "Classical routing engines, meanwhile, are dependable but blind to
intent [9], [10]." carries the production evidence into the same argument:

> The same blindness holds in deployment rather than only in principle: production
> engines and the OpenStreetMap MCP servers already published treat constraints as
> planner inputs and expose no capability that judges a route arriving from elsewhere,
> while the validators that do exist check the feed or clean a trace rather than
> evaluate a journey against what was asked for [11]–[17].

---

## What was verified, and how

Recorded so the claims above can be re-derived rather than taken on trust — the same
standard the project applies to its own evaluation data.

| Claim | How it was checked | Result |
|---|---|---|
| Meili performs map matching and returns snapped geometry, not a verdict | Valhalla's own Meili documentation | Confirmed: HMM with Viterbi decoding over candidate road segments; returns matched geometry |
| `gtfs-validator` validates the feed, not a journey | MobilityData repository and GTFS.org validation guide | Confirmed: file integrity, type parsing, ranges, feed-level rules including expiry |
| OpenTripPlanner plans trips and has no distinct itinerary-validation endpoint | OTP repository and API documentation | Confirmed as a planner; no separate validation endpoint found in the documentation surveyed |
| Existing systems treat constraints as planner inputs | Geoapify routing API documentation; navigation patent literature | Confirmed: restrictions and desired conditions are request parameters |
| OSM MCP servers with routing already exist | Search of published MCP servers | Confirmed: several, actively available |
| No MCP geospatial tool surveyed validates an existing route | Direct review of `osmmcp`'s twenty-five tools | Confirmed: all generate, search or transform; none judges an existing route or checks one against constraints |

**A caveat worth keeping in the report.** "No existing tool does this" cannot be proven
exhaustively, and the text above is deliberately written as a claim about the tools
surveyed rather than about every tool in existence. The strongest form of the argument
does not need the absolute claim: even if a route validator exists somewhere, this
project's contribution is the controlled comparison of three architectures over matched
scenarios, including the measurement of when verification *harms* a result that was
already correct — which no tool, validator or otherwise, reports.
