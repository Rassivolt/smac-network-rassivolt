"""
The demo query catalogue.

Each query is a question a person might actually ask, paired with a scripted
plan of tool calls that answers it. The plan is a fallback: when a model is
configured the router chooses the tools itself, and the scripted plan is only
used when no API key is set, or when someone ticks "scripted" to guarantee a
demo runs the same way twice.

An argument written as "$0.location.lat" means "take it from step 0's result",
which is how a query gets geocoded before it is answered.

The catalogue includes a question the agent should refuse. Watching
an agent decline to answer is the most useful thing an observer can see: it is
the difference between a system that knows its coverage and one that invents.
"""

DOMAINS = [
    {
        "id": "network",
        "name": "Network & Routing",
        "blurb": "OpenStreetMap road and footpath network for inner-to-middle Brisbane. Plans walking routes, and checks any route against the network it claims to run on.",
    },
]

QUERIES = [
    {
        "id": "net-named-places",
        "domain": "network",
        "text": "What's the shortest route from UQ St Lucia to the Brisbane CBD?",
        "why": "Two place names, not coordinates - the agent geocodes both ends before it can measure anything, three tool calls in total.",
        "plan": [
            {"agent": "network", "tool": "geocode_place",
             "arguments": {"place": "University of Queensland, St Lucia"}},
            {"agent": "network", "tool": "geocode_place",
             "arguments": {"place": "Brisbane CBD"}},
            {"agent": "network", "tool": "plan_verified_route",
             "arguments": {"origin_lat": "$0.lat", "origin_lon": "$0.lon",
                           "dest_lat": "$1.lat", "dest_lon": "$1.lon"}},
        ],
    },
    {
        "id": "net-tradeoff",
        "domain": "network",
        "text": "I can't manage stairs and 7.25 km is my limit - Indooroopilly to Spring Hill. What are my options?",
        "why": "Neither requirement can be dropped and no route satisfies both. Watch the agent "
               "lay out the choice instead of reporting a violation: 7,103 m with four flights "
               "of stairs, three of them with a handrail, against 7,430 m step-free - 327 extra "
               "metres. The handrail counts come from OpenStreetMap tags nothing else here uses.",
        "plan": [
            {"agent": "network", "tool": "compare_route_options",
             "arguments": {"origin_lat": -27.4990, "origin_lon": 152.9730,
                           "dest_lat": -27.4610, "dest_lon": 153.0230,
                           "constraints": {"avoid_highway_types": ["steps"],
                                           "max_walking_distance_m": 7250}}},
        ],
    },
    {
        "id": "net-out-of-scope",
        "domain": "network",
        "text": "What's the shortest route from the Sydney Opera House to Bondi Beach?",
        "why": "Outside the bounded Brisbane road network this agent covers. A refusal that still cites its source is the correct answer here.",
        "plan": [
            {"agent": "network", "tool": "compare_route_options",
             "arguments": {"origin_lat": -33.8568, "origin_lon": 151.2153,
                           "dest_lat": -33.8908, "dest_lon": 151.2743}},
        ],
    },
    {
        "id": "net-verified-unsatisfiable",
        "domain": "network",
        "text": "Walk me from UQ St Lucia to the Brisbane CBD, but I can only manage 800 m on foot.",
        "why": "The shortest walk is 5.1 km, so the requirement cannot be met. Watch the agent return the route anyway with the violation attached - the observed 5,057 m against the stated 800 m - instead of an apology or a route quietly pretending to comply.",
        "plan": [
            {"agent": "network", "tool": "plan_verified_route",
             "arguments": {"origin_lat": -27.4975, "origin_lon": 153.0137,
                           "dest_lat": -27.4698, "dest_lon": 153.0251,
                           "constraints": {"max_walking_distance_m": 800}}},
        ],
    },
    {
        "id": "net-catch-a-false-route",
        "domain": "network",
        "text": "Someone sent me a route claiming it's a 300 m walk from UQ St Lucia to the Brisbane CBD. Is that true? I can only walk 800 m.",
        "why": "The claim is fluent, specific and impossible. The validator measures the leg against the real footpath network, finds no walk under 5,057 m exists between those points, and fails it on the measured distance rather than the claimed one. This is the whole thesis in one query.",
        "plan": [
            {"agent": "network", "tool": "validate_route",
             "arguments": {"segments": [{"mode": "walk",
                                         "from": {"lat": -27.4975, "lon": 153.0137},
                                         "to": {"lat": -27.4698, "lon": 153.0251},
                                         "distance_m": 300}],
                           "constraints": {"max_walking_distance_m": 800}}},
        ],
    },
]


def by_id(query_id: str) -> dict | None:
    return next((q for q in QUERIES if q["id"] == query_id), None)
