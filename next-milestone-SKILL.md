---
name: next-milestone
description: Build the next milestone of The Cutting Room Floor — the agent layer, the MCP backend, and the export wiring. Invoke with /next-milestone.
disable-model-invocation: true
---

Read `CLAUDE.md` first. It carries competition rules that disqualify the entry if
broken, plus verified 2026 stack facts that contradict most tutorials and a lot
of model priors. Trust it over your training data, and trust the installed
package over both — run `pip show google-adk` if anything disagrees.

The data layer is complete: 39 tests passing, closed loop working end to end
(detect → note → trim → re-screen → measure). Your job is the agent layer.
Work in this order and do not skip step 0.

## Step 0 — verify what has never been run

Two claims in this repo were written but never executed. Confirm or refute each
and report plainly:

1. `docker compose up --build`. Never run. Confirm ClickHouse comes up, the seed
   loads, and both reports print. Fix what breaks.
2. `sql/003_vector_index.sql` — a `vector_similarity` HNSW index authored
   against chdb, which compiles that index type out. Apply it to the Docker
   ClickHouse. If the syntax is wrong for this server version, fix it. If the
   index type is genuinely unavailable, say so and we cut the feature rather
   than ship a README claim we cannot demo.
3. `pytest` — all 39 must pass before you change anything, so any later failure
   is yours and not pre-existing.

## Step 1 — close the MCP gap (highest priority)

The ClickHouse track requires the project to *actively use ClickHouse at runtime
via `mcp-clickhouse`*. Right now `crf/db.py` uses `clickhouse-connect` directly.
That is fine for seeding; it does not satisfy the rule for the agent.

Add an `McpBackend` implementing the existing `Backend` protocol, routing the
agent's reads through `mcp-clickhouse`'s `run_query` tool via `McpToolset`. Note
it is `run_query` — `run_select_query` belongs to the ClickHouse Cloud remote
MCP, a different server.

Keep the other backends. Bulk insert stays on `clickhouse-connect` because
inserting 300k rows through MCP would be absurd; the agent's *reads* go through
MCP. Make that split explicit in code and README so it reads as a deliberate
decision rather than a half-migration.

**The model must never author SQL.** Agent tools load a named file from
`sql/queries/`, substitute typed parameters, and hand the statement to
`run_query`. If you find yourself letting Gemini write a query string, stop —
that trades away the project's main technical claim.

## Step 2 — build the four agents

`CLAUDE.md` has the architecture and the test that governs it: an agent earns its
place only if replacing it with a Python function would make the output worse.
Four pass — investigator, sentiment analyst, recommender, grounding checker.
Everything else stays deterministic code. Do not add a fifth for symmetry.

Build the graph with `from google.adk import Workflow`. Do not use
`SequentialAgent`/`LoopAgent`/`ParallelAgent` — deprecated in ADK 2.x, and using
them signals a stale tutorial to anyone who knows the framework.

Specifics that will cost you an hour each if missed:

- Node results arrive on `event.output` and `event.node_info`, **not**
  `event.content.parts[0].text`.
- Put `RetryConfig` on MCP-backed nodes — a flaky session should retry, not kill
  the run.
- `RequestInput` state persists across process restarts. Demo it.
- `gemini-3.5-flash`, `thinking_level=low`, and do not pass `temperature`,
  `top_p` or `top_k`.

The **grounding checker** is the one to get right. Give it the note plus the raw
tool payloads and have it reject any figure not present in them. Write the test
first: hand it a note with a fabricated percentage and assert it fails. A note
that invents a number is a bug, not a style issue.

The **investigator** is where agency actually lives. Give it the analysis
functions as tools and let it choose which to call — cohort divergence, comment
themes, scene context, comparison against another cut. It cannot change what
counts as a drop-off; it decides what is worth looking at.

## Step 3 — wire the export

`crf/timeline.py` has `write_markers_csv` and `write_edl` scaffolded but not
connected to approved notes. Connect them. The output file is the product — the
pitch is "the editor imports this into the bay", so it has to actually open
somewhere. Verify EDL round-trips through `parse_edl` and add a test.

## Constraints

- Only Google AI, anywhere, including dev tooling. Check the lockfile.
- `pip install "google-adk[mcp]"` — never a bare `pip install mcp`.
- Do not edit `LICENSE`; GitHub's automated detection must keep matching it.
- chdb allows one embedded server per process — use the shared fixture in
  `tests/conftest.py`, do not construct a second `ChdbBackend`.
- Commit as you go, real messages, history inside the contest window.
- If something in `CLAUDE.md` blocks you, say so and stop. Several of those are
  competition rules, not preferences.

## Done means

- `docker compose up --build` works from clean, and you have stated plainly
  whether the vector index applied.
- `pytest` green, including new tests for the MCP backend, the grounding
  checker rejecting a fabricated number, and the EDL round-trip.
- An end-to-end run produces approved notes and writes a marker file, with the
  agent's reads demonstrably going through `mcp-clickhouse`.
- README status table reflects reality.

Report at the end: what works, what you changed, what you could not verify, and
what you think is weakest about the submission. Be blunt about the last one — I
would rather hear it from you than from a judge.
