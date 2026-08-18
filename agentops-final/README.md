# AgentOps — Autonomous AI Infrastructure Engineer

Multi-agent system that watches a CockroachDB-backed app, diagnoses performance
incidents using vector-recalled memory + LLM reasoning, fixes them (schema
changes, then AWS scaling if needed), and explains what it did in plain English.

## Architecture

```
                    ┌─────────────────┐
   traffic spike →  │  MonitorAgent   │  polls hot query latency + EXPLAIN ANALYZE
                    │                 │  (+ discovers the hot query itself via
                    │                 │  crdb_internal.node_statement_statistics)
                    └────────┬────────┘
                             │ latency > threshold
                             ▼
                    ┌─────────────────┐
                    │  MemoryAgent    │  embeds a canonical symptom signature →
                    │  (CRDB VECTOR)  │  vector search over past incidents
                    └────────┬────────┘  (CockroachDB C-SPANN index)
                             │ similar incidents (+ distance)
                             ▼
                 near-identical match? ──yes──► reuse known fix, SKIP THE LLM
                             │ no                    (self-improving: incident
                             ▼                         #2 costs zero model calls)
                    ┌─────────────────┐
                    │ DiagnosticAgent │  Claude (Bedrock or Anthropic API) reasons
                    │                 │  over metrics + plan + memory → root cause
                    └────────┬────────┘  + one fix: CREATE INDEX or ANALYZE
                             │
                             ▼
                    ┌─────────────────┐
                    │ ExecutionAgent  │  guardrails: confidence-gated auto-apply,
                    │                 │  DRY_RUN, one fix/incident → applies fix
                    │                 │  → re-benchmarks → rolls back index fixes
                    │                 │  that didn't help → if CPU still high:
                    │                 │  scales ECS
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │ ReportingAgent  │  writes NL report, saves outcome
                    │                 │  back to vector memory for next time
                    └─────────────────┘

  Every step from every agent is written to agent_trace (explainability),
  streamed live by dashboard.py.
```

**Why CockroachDB, specifically:** the incident memory lives in the *same*
distributed SQL database as the operational data, using CockroachDB's native
`VECTOR` type + `CREATE VECTOR INDEX` (C-SPANN) — no separate vector DB, no
sync pipeline. That's a genuine, judge-visible use of a CockroachDB-specific
capability, not just "Postgres with pgvector bolted on." The monitor also
reads CockroachDB's own telemetry (`crdb_internal`) to find the hot query
autonomously, and the memory short-circuit means recall isn't just flavor —
it measurably skips the LLM on repeat incidents.

**Why AWS:** ECS is the scaled resource (CloudWatch → `UpdateService`). The
reasoning agent can also run on Bedrock (`LLM_BACKEND=bedrock`), which
doubles as a second AWS integration point beyond raw compute -- the default
backend is Gemini for zero-setup local dev, so set `LLM_BACKEND=bedrock` and
`BEDROCK_MODEL_ID` in `.env` if you want Bedrock counted as part of your AWS
integration for judging.

## Project layout

```
agentops/
├── db/schema.sql          # orders (no index, on purpose) + incidents (vector memory) + agent_trace
├── db/seed.py              # seeds demo data + one pre-loaded memory (bulk INSERT via execute_values)
├── tools/crdb_client.py    # pooled CockroachDB access (timed queries, EXPLAIN ANALYZE, DDL)
├── tools/aws_client.py     # CloudWatch metrics + ECS scaling
├── tools/embeddings.py     # text -> 384-dim vector (sentence-transformers, offline fallback)
├── tools/llm.py            # Bedrock or Anthropic API, same interface either way, retry/timeout
├── agents/                 # one file per agent, all inherit agents/base.py for tracing
│   ├── monitor_agent.py    #   latency check + crdb_internal hot-query discovery
│   ├── memory_agent.py     #   vector store/recall, returns match distance
│   ├── diagnostic_agent.py #   LLM diagnosis (CREATE INDEX or ANALYZE) + memory fallback
│   ├── execution_agent.py  #   guardrails: confidence gate, dry-run, apply, rollback, ECS scale
│   └── reporting_agent.py  #   NL report + writes resolution back to memory
├── orchestrator.py         # wires the agents into the incident pipeline, short-circuit + guardrails
├── load_generator.py       # simulates a traffic spike for a repeatable demo
├── demo_scenario.py        # single command: seed -> missing-index incident -> fix -> report
├── demo_stale_stats.py     # single command: seed -> stale-statistics incident -> ANALYZE -> report
├── dashboard.py            # live agent_trace viewer with optimistic rendering (stdlib, port 8888)
├── tests/                  # pytest suite over pure logic, no DB/network required
└── .github/workflows/ci.yml
```

## Setup

1. **CockroachDB** — easiest path is a free CockroachDB Cloud serverless
   cluster (vector indexes are supported there). For local dev:
   `docker compose up -d`, then `cockroach sql --insecure -e "CREATE DATABASE agentops"`.
2. `cp .env.example .env` and fill in `DATABASE_URL` and your LLM backend
   (`LLM_BACKEND=gemini` is the zero-setup default for local dev; set
   `LLM_BACKEND=anthropic` + `ANTHROPIC_API_KEY`, or `LLM_BACKEND=bedrock` +
   `BEDROCK_MODEL_ID`, if you want that backend counted for judging).
   `.env.example` documents every guardrail threshold (`DRY_RUN`,
   `AUTO_APPLY_MIN_CONFIDENCE`, `MIN_IMPROVEMENT_RATIO`,
   `MEMORY_MATCH_MAX_DISTANCE`, connection pool sizing, etc). Optionally set
   `CCLOUD_CLUSTER_NAME` if you have the `ccloud` CLI installed and logged in
   (`ccloud auth login`) -- this is used as a second CockroachDB tool
   (`tools/ccloud_client.py`) for cluster-capacity context and the audit
   trail; the pipeline runs fine without it.
3. `pip install -r requirements.txt`
4. `python demo_scenario.py` — seeds ~200k unindexed orders, detects the
   slow query, diagnoses it, creates the index, re-benchmarks, and prints
   the report. That's your whole demo in one command. Run it twice to see
   the self-improving short-circuit: the second run recalls the resolved
   incident from memory and skips the LLM entirely.
5. `python demo_stale_stats.py` — the second incident type (fix = `ANALYZE`),
   to show the pipeline generalizes past one hardcoded scenario.
6. `python dashboard.py` (optional, run alongside either demo) — live view of
   `agent_trace` at `http://localhost:8888`.
7. Optional, for the "watch it happen live" version:
   `python load_generator.py --customer-id <id> &` then
   `python orchestrator.py --customer-id <id> --loop`.

ECS scaling and CloudWatch metrics are stubbed to no-op gracefully if
`ECS_CLUSTER_NAME` / `ECS_SERVICE_NAME` aren't set, so the DB-only demo
above works even before you've stood up a real ECS service.

## Deploying for the hackathon submission

The Devpost submission needs a public "functional demo app" URL, and the
challenge requires the app to be deployed on AWS (not just calling AWS APIs
from your laptop). See **[`deploy/DEPLOY.md`](deploy/DEPLOY.md)** for a full
EC2 + systemd runbook, plus `deploy/agentops-dashboard.service` and
`deploy/agentops-orchestrator.service` unit files so the demo survives SSH
disconnects during judging.

## Two incident types (not one hardcoded scenario)

AgentOps diagnoses and fixes two distinct classes of incident, picking the fix
family from the evidence rather than assuming one:

| Incident | Signal | Fix family |
|---|---|---|
| Missing index | full table scan in `EXPLAIN ANALYZE` | `CREATE INDEX` |
| Stale statistics | slow query with **no** full scan (bad optimizer estimates) | `ANALYZE <table>` |

- `python demo_scenario.py` — the missing-index scenario.
- `python demo_stale_stats.py` — the stale-statistics scenario (fix = `ANALYZE`).

The execution guard independently allows *only* these two single-statement fix
families and rejects everything else; index fixes are rolled back if they don't
help, while `ANALYZE` is idempotent maintenance with nothing to undo.

## Autonomous hot-query discovery

Instead of only trusting the built-in hot query, the monitor reads
CockroachDB's own per-statement telemetry
(`crdb_internal.node_statement_statistics`) to identify the worst-performing
statement touching the target table, and logs it into the trace. Best-effort:
if stats aren't available yet it falls back to the built-in query.

## Guardrails & self-improving memory

- **Self-improving:** when an incident's symptoms match a *resolved* past
  incident in vector memory (distance ≤ `MEMORY_MATCH_MAX_DISTANCE`), AgentOps
  reuses that known fix and **skips the LLM entirely** — the second occurrence
  of an incident costs zero model calls. Symptoms are embedded as a canonical,
  latency-independent signature so the same class of incident reliably clusters.
- **Confidence-gated auto-apply:** fixes below `AUTO_APPLY_MIN_CONFIDENCE` are
  proposed and held for a human instead of applied automatically.
- **Dry-run:** set `DRY_RUN=1` to propose fixes without executing any DDL.
- **Automatic rollback:** if an applied index doesn't improve latency by at
  least `MIN_IMPROVEMENT_RATIO`, AgentOps drops it (a controlled `DROP INDEX`
  built from the parsed index name — never from raw model text).
- **One fix per incident** — no fix/re-fix loops.

All guardrail thresholds live in `.env` (see `.env.example`).

## Live dashboard

`python dashboard.py` serves an explainability dashboard on
`http://localhost:8888` that tails `agent_trace` and renders each agent step as
it lands — optimistically drawing the expected pipeline steps as ghost cards,
then reconciling them to confirmed cards as the DB rows arrive. Stdlib only.

## Tests

`pip install -r requirements-dev.txt && pytest tests/ -v` runs the unit
suite (DDL safety patterns, confidence/DRY_RUN guardrails, rollback-by-parsed-name
safety, short-circuit distance logic, diagnosis JSON parsing, offline
embedding fallback, ccloud CLI fallback) -- 33 tests, no DB or network
required. CI runs it on every push/PR (`.github/workflows/ci.yml`).

There's also `smoke_test.py` at the repo root: an offline end-to-end run of
the *actual* orchestrator/agents code (DB, LLM, and AWS calls mocked) that
exercises all three incidents in one pass -- fresh missing-index incident,
repeat incident (verifies the LLM is actually skipped), and a stale-stats
incident. Run with `python smoke_test.py`.

## Fixed during testing (transparency note)

Before this pass, `smoke_test.py` and `pytest` didn't exist and several
interface mismatches between `orchestrator.py` and the `agents/` modules had
never been exercised end-to-end. All of the following are now fixed and
covered by the test suite above:

- `MonitorAgent.discover_hot_query`, `DiagnosticAgent.diagnose_from_memory`,
  and `ExecutionAgent.rollback_fix` didn't exist; `ExecutionAgent.apply_fix`
  didn't accept a `confidence` argument or return the status string the
  orchestrator expected. The pipeline would have raised `AttributeError` /
  `TypeError` on the first detected incident.
- `MemoryAgent.search_similar` never selected a `distance` column, so
  `orchestrator.should_short_circuit` always saw `None` and the
  self-improving short-circuit could never actually trigger.
- A recalled fix replaying the same `CREATE INDEX` on a second run would hit
  a duplicate-index error on a real cluster (the schema isn't reset between
  runs); fixes are now normalized to `IF NOT EXISTS`.
- The diagnostic system prompt only ever allowed `CREATE INDEX`, silently
  breaking the `ANALYZE` fix path that `demo_stale_stats.py` depends on.
- `tools/llm.py` imported `google-genai` unconditionally at module load, so
  the app would fail to import on any backend if that package wasn't
  installed. Imports are now lazy per backend, and the Bedrock/Anthropic
  branches are real implementations, not stubs.

## Roadmap to Aug 18

| Days | Focus | Status |
|---|---|---|
| **Aug 8–9** | CockroachDB Cloud cluster + AWS account/ECS sample service. Run this scaffold end-to-end locally with `demo_scenario.py`. | |
| **Aug 10** | Real EXPLAIN ANALYZE parsing edge cases (joins, multiple missing indexes); tighten the diagnostic prompt with a couple more few-shot examples. | |
| **Aug 11** | Wire `tools/aws_client.py` to your real ECS cluster/service; confirm CloudWatch CPU pulls correctly; test `maybe_scale`. | ⬜ next up |
| **Aug 12** | Deploy a small sample app to ECS in front of the CockroachDB cluster (this is what "traffic increases" is monitoring) — a trivial FastAPI/Express app hitting the `orders` table is enough. | ⬜ next up |
| **Aug 13** | Guardrails: dry-run mode, rollback path if a fix doesn't help, max-one-fix-per-incident to avoid loops. | ✅ done — confidence gate, `DRY_RUN`, index rollback, one-fix/incident |
| **Aug 14** | Polish `load_generator.py` timing; add a second incident type (e.g., stale table stats) to show generality, not just one hardcoded scenario. | ✅ done — `demo_stale_stats.py` + `ANALYZE` fix family; connection pool fixes the load-generator connection storm |
| **Aug 15** | Optional: minimal live-status HTML page reading `agent_trace` in real time — strong demo value for "explainability" if you have time. | ✅ done — `dashboard.py`, optimistic rendering |
| **Aug 16** | Full dry runs of the demo, 3–4 times, timing it. Fix whatever breaks. | ⬜ do this once AWS is wired |
| **Aug 17** | Record a backup demo video (live demos fail; judges appreciate a fallback). Write the submission write-up: architecture, what's CockroachDB-specific, what's AWS-specific. | |
| **Aug 18** | Buffer day + submission. | |

Also done, beyond the original roadmap: pooled DB connections (no more
connection-storm risk from `load_generator.py`), bulk seeding via
`execute_values`, LLM timeout/retry with a memory-based fallback, a hardened
single-statement DDL guard, `crdb_internal`-driven hot-query discovery, the
self-improving memory short-circuit, `.env.example`, and a 48-test CI suite
(`.github/workflows/ci.yml`).

**What's left before the AWS side:** wiring `tools/aws_client.py` to a real
ECS cluster/service and standing up the sample app CloudWatch is watching —
everything else in this list is DB/LLM-only and already demo-ready.

## USP — what makes this different

**An autonomous database reliability engineer whose long-term memory lives
inside the same distributed SQL database it's protecting — so every incident
it resolves makes the next one faster, cheaper, and auditable, with no
separate vector store and no sync pipeline.**

- **Self-improving, not just self-healing.** The resolution gets written back
  into vector memory, so the *second* occurrence of a known incident is
  recalled and fixed **without an LLM call**. Run `demo_scenario.py` twice —
  the second run's trace shows `diagnosis (recalled) — LLM skipped`. That's a
  measurable, on-stage "it gets smarter" moment most agent demos can't show.
- **Memory colocated with operational data.** Incident memory is a native
  `VECTOR` column + C-SPANN index in the *same* cluster as `orders` — no
  Pinecone, no ETL, no consistency drift between the data and the memory of
  the data.
- **Explainability as a first-class artifact.** Every autonomous action —
  including ones it *declined* to take (held for approval, rolled back) — is
  in `agent_trace`, replayable via `dashboard.py` and summarized in the NL
  report. That's the answer to "I can't let an AI run DDL I can't audit."
- **Generalizes past one hardcoded scenario.** Two independently-diagnosed
  incident classes (missing index, stale statistics), each with its own fix
  family and guard.

## What to emphasize for judging

- **CockroachDB-specific**: vector memory colocated with operational data,
  `EXPLAIN ANALYZE`-driven diagnosis, `crdb_internal`-driven hot-query
  discovery, `ccloud` CLI for cluster-capacity context and Cloud audit trail
  cross-referencing (`tools/ccloud_client.py`) -- two CockroachDB tools used,
  not one -- distributed SQL resilience story if you get time to demo a node
  failure mid-incident.
- **AWS-specific**: Bedrock for reasoning, CloudWatch → ECS scaling loop.
- **Explainability**: the `agent_trace` table + live dashboard + the final NL
  report are your answer to "why did the agent do that" — surface all three
  in the demo, not just the end result.
- **Self-improving memory**: run the same demo twice and point at the
  `LLM skipped` trace line — this is the single strongest differentiator
  versus observe-only APM tools and bolt-on vector databases.
