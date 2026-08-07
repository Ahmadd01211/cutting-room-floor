# CLAUDE.md — The Cutting Room Floor

## What this is

Hackathon submission for **Agentic Cinema** (Google Cloud × Devpost),
**ClickHouse track**. Deadline **Mon 7 Sept 2026, 2:00pm PT**.

Test-screening telemetry joined to the edit timeline via ClickHouse `ASOF JOIN`,
turned into timecoded editorial notes — then it measures whether its own notes
worked. **The persona is film editors and post supervisors.** Name them first in
the README, the video, and the Devpost fields.

## Hard rules — breaking any of these disqualifies the entry

1. **Only Google AI.** *"No other AI models, agent frameworks, or AI APIs are
   permitted, regardless of vendor — this includes but is not limited to AWS,
   Microsoft, OpenAI, and Anthropic AI tools."* Use `gemini-embedding-001`, not
   OpenAI embeddings. No Whisper. Audit the lockfile before submitting.
2. **Accepted Google packages, exhaustive:** `google-adk`, `google-genai`,
   `google-generativeai`, `google-cloud-aiplatform`. One must be actually called.
3. **ClickHouse at runtime via `mcp-clickhouse`.** Naming it in the README does
   not count. See "MCP gap" below — currently unmet.
4. **Newly created during the contest period** (opened 27 July 2026), your
   original work. Third-party libraries as dependencies are fine.
5. **Public repo, OSI licence detected in the GitHub About sidebar.** `LICENSE`
   is the canonical 11,358-byte Apache-2.0 text — do not edit it or detection
   breaks and the partly-automated Stage One screen can fail the entry.
6. **Video ≤ 3 min**, public, English, showing the software *working* — a demo,
   not a cinematic trailer, despite the theme.

**Judging:** four equal criteria; ties break on **Technological Implementation
first**, so depth of Google Cloud + ClickHouse usage is the effective primary.
The others: Design (complete product, not a PoC), Potential Impact (real problem,
real audience), Quality of Idea (creative, non-obvious).

## MCP gap — highest priority

`crf/db.py` talks to ClickHouse through `clickhouse-connect`. Fine for seeding;
**insufficient for the agent** under rule 3.

Fix: an `McpBackend` implementing the same `Backend` protocol, routing the
agent's reads through `mcp-clickhouse`'s `run_query` tool via ADK's `McpToolset`.
The tool is `run_query` — `run_select_query` belongs to the ClickHouse Cloud
remote MCP, a different server. Bulk insert stays on `clickhouse-connect`; make
that split explicit so it reads as architecture, not a half-migration.

**The model never authors SQL.** Agent tools load a named file from
`sql/queries/`, substitute typed params, and pass the statement to `run_query`.

## Agent architecture

An agent earns its place only if **replacing it with a Python function would
make the output worse**. Four pass that test:

| Agent | Why it cannot be code |
|---|---|
| **Investigator** | Chooses which thread to pull on a drop-off — cohort split, comments, scene context, other cut. Real tool-choice. |
| **Sentiment analyst** | Reads and clusters thousands of free-text comments into named themes. |
| **Recommender** | Synthesises telemetry + sentiment into a specific quantified edit. |
| **Grounding checker** | Adversarially rejects any note containing a number absent from its tool payloads. Hallucinated figures are this product's real failure mode. |

Everything else — ingest, validation, rollups, detection, export, reporting — is
deterministic code. Do **not** wrap those in agents: it makes them slower,
costlier and non-reproducible, and a data-quality step that answers differently
each run is worse than useless.

Graph (`from google.adk import Workflow`):

```
START → load_cut → detect_dropoffs → per segment {investigator → JoinNode
      → recommender → grounding_checker} → RequestInput (editor approves)
      → export (markers + EDL) → measure (if a later cut exists)
```

## Verified stack facts (Aug 2026)

Checked against installed packages, not recalled. Re-verify with `pip show`.

- **ADK 2.x deprecated `SequentialAgent`/`LoopAgent`/`ParallelAgent`** for the
  graph `Workflow`. Most tutorials and `adk-samples` still teach the old ones.
  Exports: `Workflow`, `Edge`, `FunctionNode`, `JoinNode`, `START`, `RetryConfig`.
  Branch with `(node, {"ROUTE": target})`; fan in with `JoinNode` (emits a dict
  keyed by node name).
  **Gotcha:** results are on `event.output` / `event.node_info`, *not*
  `event.content.parts[0].text`.
  Custom `_run_async_impl()` overrides no longer work — use agent callbacks.
  Broad `try/except` disables framework retry; let exceptions propagate.
- **MCP:** `McpToolset` (lowercase c); `MCPToolset` is a legacy alias. Params in
  `google.adk.tools.mcp_tool.mcp_session_manager`.
  🔴 `pip install "google-adk[mcp]"` — a bare `pip install mcp` pulls SDK 2.x
  and every ADK MCP import dies with `No module named 'mcp.shared.session'`.
- **HITL is native:** `from google.adk.events import RequestInput`. State
  survives process restarts — good demo beat.
- **Model:** `gemini-3.5-flash`, `thinking_level=low`. `temperature`/`top_p`/
  `top_k` are **deprecated** on Gemini 3.x. No 3.x `pro` is GA.
- **Deploy:** `adk deploy cloud_run --with_ui`, unauthenticated, plus
  `--min-instances=1` so a cold start doesn't stall a judge.

## Current state

Data layer complete, **39 tests passing**. Not built: the agents, the UI, the
real-film ingest, the marker export wiring.

```
sql/001_schema  002_rollups  003_vector_index(optional)  004_feedback
sql/queries/    shot_attention · dropoff_segments · cohort_divergence
                cut_comparison · note_outcome · comment_themes
crf/            timeline · screening · comments · embed · db · analysis · pipeline
```

Proven, not merely written:

- ASOF correct on every boundary; **needs ≥1 equality key** — carry `cut_id`
  through every CTE or you get `NOT_IMPLEMENTED: ASOF join ... needs at least
  one equi-join column`.
- Detector recovers the planted signal: 100% of weak scenes flagged, 0% of
  strong. That test is the credibility claim — keep it green.
- Closed loop runs: detect → note → trim → re-screen → measure, and reports
  honest variance (4 improved / 3 no effect / 1 regressed).
- Comments are generated *from* measured attention, so sentiment and telemetry
  agree by construction.

**Two bugs already fixed — do not reintroduce.** Scene→index came from a Python
`set`, and string hashing is randomised per process, so quality landed on
different scenes every run. And scene quality was drawn from the *screening* RNG,
so the same scene differed between cuts and contaminated every before/after
measurement. Quality is now keyed on `(film_id, scene_id, film_seed)`.

**Unverified — needs a real server:** `003_vector_index.sql` (chdb compiles the
index type out) and `docker compose up` (never executed).

**chdb runs one embedded server per process** — tests share a session-scoped
backend via `tests/conftest.py`. Don't construct a second `ChdbBackend`.

## Design principles

1. Determinism lives in SQL. The model narrates and chooses what to ask; it
   never computes a statistic, sets a threshold, or sees a raw row.
2. The deliverable is a file an editor imports, not a chat window.
3. Be honest about synthetic data. The generators ship, seeded and documented.
   Say so on camera.
4. ClickHouse must be load-bearing. If a feature would work identically on
   SQLite, it is not earning its place in a ClickHouse-track submission.
5. Never let the system grade its own homework in prose. Verdicts are computed
   in SQL, adjusted for film-wide drift.
6. No silent scope cuts — if you drop something, say so.

## Commands

```bash
pip install -e ".[test]" && pytest        # real ClickHouse via chdb, no server
CRF_BACKEND=chdb python -m scripts.demo --seed --report
docker compose up --build                 # full stack
```
