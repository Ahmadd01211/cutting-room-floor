# Kickoff prompt for Claude Code

Save `CLAUDE.md` at the repo root first — Claude Code loads it automatically, so
the hackathon constraints and verified stack facts stay in context for every
session. Then paste everything below the line into Claude Code, running from
`cutting-room-floor/`.

---

Read `CLAUDE.md` before doing anything. It contains hard competition rules that
disqualify the entry if broken, plus verified 2026 stack facts that contradict
most tutorials and a lot of model priors. Trust it over your training data, and
trust the installed package over both — run `pip show google-adk` if anything
disagrees.

The data layer is complete and tested (27 passing). Your job this session is the
agent layer. Work in this order and do not skip step 0.

## Step 0 — verify three things before writing any code

Two claims in this repo were never executed, and one bug pattern recurs. Confirm
or refute each, and report what you find:

1. `docker compose up --build` has never been run. Run it. Confirm ClickHouse
   comes up, the seed loads, and the report prints. Fix what breaks.
2. `sql/003_vector_index.sql` declares a `vector_similarity` HNSW index that has
   never been applied to a real server — it was authored against `chdb`, which
   compiles that index type out. Apply it to the Docker ClickHouse. If the
   syntax is wrong for this server version, fix it. If the index type is
   genuinely unavailable, tell me and we will cut the feature rather than ship a
   README claim we cannot demo.
3. Run `pytest`. All 27 must pass before you change anything, so we know any
   later failure is yours and not pre-existing.

## Step 1 — close the MCP compliance gap

This is the highest-priority item in the repo. The ClickHouse track requires the
project to *"actively use ClickHouse at runtime via the official ClickHouse MCP
server (`mcp-clickhouse`)"*. Right now `crf/db.py` talks to ClickHouse directly
via `clickhouse-connect`. That is fine for seeding and loading; it is not
sufficient for the agent.

Add a third backend — `McpBackend` — implementing the same `Backend` protocol,
routing queries through `mcp-clickhouse`'s `run_query` tool via ADK's
`McpToolset`. Note the tool is `run_query`, not `run_select_query` (that name
belongs to the ClickHouse Cloud remote MCP, a different server).

Keep the existing backends. Seeding stays on `clickhouse-connect` because bulk
insert through MCP would be absurd; the *agent's reads* go through MCP. Make
that split explicit in the code and in the README so a judge can see it is a
deliberate architecture decision rather than a half-migration.

Critical constraint: the model must never author SQL. The agent's tools load a
named file from `sql/queries/`, substitute typed parameters, and pass the
resulting statement to `run_query`. The SQL stays version-controlled and
reviewable. If you find yourself letting Gemini write a query string, stop —
that trades away the project's main technical claim.

## Step 2 — build the agent as an ADK `Workflow` graph

Use `from google.adk import Workflow`. Do not use `SequentialAgent`,
`LoopAgent` or `ParallelAgent` — they are deprecated in ADK 2.x, and using them
signals a stale tutorial to anyone who knows the framework. The graph is also
literally what the brief asks for: a "deterministic, multi-step agent".

Target shape:

```
START
  → load_cut            FunctionNode  — verify telemetry exists, pull cut_summary
  → detect_dropoffs     FunctionNode  — analysis.dropoff_segments (pure SQL)
  → per segment, fanned out:
        cohort_divergence   FunctionNode
        scene_context       FunctionNode
        comparable_scenes   FunctionNode  — vector search, other films only
      → JoinNode
      → write_note        LlmAgent      — Gemini narrates; no numbers invented
  → review              RequestInput  — editor approves/rejects each note
  → export              FunctionNode  — marker CSV + EDL via crf/timeline.py
```

Notes on the graph:
- Workflow node results arrive on `event.output` and `event.node_info`, **not**
  `event.content.parts[0].text`. This will cost you an hour if you forget.
- Put `RetryConfig` on the MCP-backed nodes; a flaky MCP session should retry,
  not kill the run.
- `RequestInput` state persists across process restarts. Demo that — it is a
  strong beat and it is native, not something you have to build.
- Model: `gemini-3.5-flash`, `thinking_level=low`. Do not pass `temperature`,
  `top_p` or `top_k` — deprecated on Gemini 3.x.

The `write_note` step is the only place a language model appears. Constrain it
with an output schema and instruct it that every number in its note must come
from the tool payload it was given. A note that invents a percentage is a bug,
not a stylistic issue — add a test that catches it.

## Step 3 — make the export real

`crf/timeline.py` has `write_markers_csv` and `write_edl` scaffolded but not
wired to the agent's approved notes. Connect them. The output file is the
product — the whole pitch is "the editor imports this into the bay", so it needs
to actually open somewhere. Verify the EDL parses back through `parse_edl`
round-trip, and add a test for it.

## Constraints while you work

- Only Google AI. No OpenAI, Anthropic, or non-Google models anywhere,
  including in dev tooling you might reach for. Check the lockfile.
- `pip install "google-adk[mcp]"` — never a bare `pip install mcp`, which pulls
  MCP SDK 2.x and breaks every ADK MCP import.
- Do not edit `LICENSE`. It is the canonical Apache-2.0 text and GitHub's
  automated licence detection must keep matching it.
- Commit as you go with real messages. History must sit inside the contest
  window (opened 27 July 2026) and show genuine incremental work.
- If a design in `CLAUDE.md` blocks you, say so and stop rather than working
  around it. Several of those constraints are competition rules.

## Definition of done for this session

- `docker compose up --build` works from clean, and you have said plainly
  whether the vector index applied or not.
- `pytest` passes, including new tests for the MCP backend, the note-grounding
  check, and the EDL round-trip.
- An end-to-end run against the Docker ClickHouse produces approved notes and
  writes a marker file, with the agent's reads demonstrably going through
  `mcp-clickhouse`.
- README updated: the status table reflects reality, and the MCP architecture
  split is explained.

Report at the end: what works, what you changed, what you could not verify, and
anything you think is a weak point in the submission. Be blunt about the last
one — I would rather hear it now than from a judge.
