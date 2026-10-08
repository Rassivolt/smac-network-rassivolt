# smac-network-routecheck - a self-verifying routing agent

The Network & Routing agent for the REIT7820 Smart Mobility Agent Collective: an
MCP server (Streamable HTTP, protocol 2025-11-25) that plans walking routes on
the real Brisbane footpath network and checks any route or claim against it -
distances, walking times, step-free and sealed-surface access, and whether a
Translink service actually runs at the time it is said to.

Five tools: `geocode_place`, `parse_route_constraints`, `plan_verified_route`,
`compare_route_options`, `validate_route`, plus `health`. Every number comes from
code over the data; a model only reads the request and writes the prose.

## Run it from a fresh clone

You need Docker Desktop and an Anthropic API key. Nothing else is installed on
your machine: every command below runs inside the containers.

1. **Keys.** Copy `.env.example` to `.env` at the repository root and fill it in.
   Without a working `ANTHROPIC_API_KEY` nothing starts; the evaluation also needs
   `WANDB_API_KEY`. Never commit `.env`.

2. **Data snapshot.** The road graph and timetable every evaluation figure was
   measured on live in `snapshot/`, not in git; `snapshot/MANIFEST.json` is, with
   every file's sha256. The agent loads the two binary files,
   `network_graph_walk-v4.bin` and `gtfs_timetable_seq-v1.bin` (115 MB together,
   about 200 MB of memory once loaded); copying those two is enough. They are
   derived from the pinned JSON (271 MB) by `python src/pin_snapshot.py --compact`,
   and `python src/pin_snapshot.py --verify` checks whatever is present against the
   manifest. *(Download location: to be added.)* With only the JSON the agent
   still runs, converting at startup with a peak near 2 GB; with neither it builds
   its own copies from OpenStreetMap and Translink, which is slow and will not
   match every evaluation figure exactly.

3. **Start.**

   ```bash
   docker compose up -d --build                           # agent on :8000, console on :8080
   docker compose exec network python preflight.py --fetch   # Python, keys and datasets
   ```

   Then open http://localhost:8080 (Windows: `start http://localhost:8080`, macOS:
   `open http://localhost:8080`). If `up` reports the agent failed to start,
   `docker compose logs network` says why - usually a missing or rejected key.

The agent's startup log names the snapshot it loaded and whether it matches the
manifest:

```
road_graph: pinned network_graph_walk-v4.bin sha256 513008dfe4d66e88
timetable: pinned gtfs_timetable_seq-v1.bin sha256 34e7ce864cfa8574
```

## Handshake test

The six conformance checks from the specification, run the way the Orchestrator
connects:

```bash
docker compose run --rm handshake
```

It ends with `HANDSHAKE PASSED` and writes `handshake_report.json` at the
repository root, which is committed as the proof. `examples.json` gives check 4
one real call per tool.

## Ask it anything

`prompt.txt` holds example questions, one per line. Ask one (in bash or Git Bash):

```bash
docker compose exec network python ask.py "Is it true the walk from UQ to the city is only 2 km?"
sed -n 2p prompt.txt | docker compose exec -T network python ask.py
```

or all of them in turn:

```bash
while IFS= read -r q; do docker compose exec -T network python ask.py "$q" < /dev/null; done < prompt.txt
```

A model sees only the tools' names and descriptions, chooses which to call, and
answers from what they return - how the Orchestrator will use the agent. In
Windows PowerShell, `<` does not work: pass the question as an argument instead.

## Checks

| Command | What it checks | Cost |
|---|---|---|
| `docker compose exec network python test_routing.py` | routing and verification, 104 unit tests (also `test_*.py`) | none |
| `docker compose exec network python eval/run_eval.py --tasks eval/tasks.network.json` | 59 evaluation cases, tools called over MCP, logged to W&B | a few cents |
| `docker compose exec network python conformance.py` | the handshake's checks 2-7, hostile input included | none |
| `docker compose exec network python audit_scenarios.py` | every scenario claim against the pinned data | none |
| `docker compose exec network python smoke_console.py --url http://console:8080` | a model choosing tools through the console | a few cents |
| `docker compose exec network python robustness.py --url http://console:8080 --repeats 3` | 30 unseen questions (`eval/unseen.network.json`) | under $1 |

Each catches something the others cannot. The evaluation calls tools directly;
the smoke check and the robustness set are the only ones that go through a model
choosing tools from their descriptions, which is the path the Orchestrator takes.

## The experiment

`src/conditions.py` runs five architectures over 31 hand-authored scenarios -
no check, self-review, check in code with model repair, check only, and repair
in code - and scores correctness and honesty:

```bash
docker compose exec network python conditions.py --all --perfect-extraction --producer model --json --wandb
```

Drop `--perfect-extraction` to have the model read each request (RQ1); set
`LLM_MODEL` to change the tier. Results and their W&B run ids are recorded in
`docs/final-experiment.md`; the reasons behind each design decision in
`docs/design-rationale.md`.

## Files

```
src/agents/network.py         the MCP server: tool schemas, descriptions, refusals
src/routing.py                planning, verification and repair, as pure functions
src/data.py                   geocoding, the road graph and the timetable index, cached
src/common.py, smac.py, llm.py    shared plumbing from the course kit
src/eval/tasks.network.json   evaluation cases
src/descriptions.md           v1 and v2 of every tool description (a submission)
src/conditions.py, producers.py   the experiment
src/pin_snapshot.py           pins and verifies the data snapshot
application/                  the console, which calls the agent as the Orchestrator will
smac-handshake-test/          the course's handshake test (v1.0.1)
logs/                         cost and tool-call logs, written by the containers
REIT7820_mcp_spec_v1.0.1.md   the contract the agent satisfies
```

## Model tier

The agent runs on `claude-haiku-4-5` unless `LLM_MODEL` is set in `.env`
(`claude-sonnet-5` or `claude-opus-5`; both run with extended thinking off).
`health` reports the model in use, and every Claude call is appended to
`logs/usage.network.jsonl` with its tokens, latency and cost.

## Reaching it from elsewhere

`docker compose --profile tunnel up -d` gives the agent a public HTTPS address
through Cloudflare, with no account. On a local network, other machines reach it
at `http://<this machine's IP>:8000/mcp`; on Windows, allow inbound TCP 8000 in
the firewall first.
