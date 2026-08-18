# AgentOps — Autonomous AI Infrastructure Engineer

Multi-agent system that watches a CockroachDB-backed app, diagnoses performance
incidents using vector-recalled memory + LLM reasoning, fixes them (schema
changes, then AWS scaling if needed), and explains what it did in plain English.

Built for the **CockroachDB × AWS Hackathon — Build with Agentic Memory**.

## Live demo

- **Dashboard (deployed on AWS EC2):** http://3.213.20.174:8888
- **Demo video:** _add your video URL here once uploaded_
- **Team:** Mihikaa Iyer ([@mihikaaI](https://github.com/mihikaaI)) & Kshitij Gedam

Run `python demo_recall.py <customer-id>` twice against the live cluster to
see the full story: a genuine LLM diagnosis on the first run, then
`diagnosis (recalled) — LLM skipped` on the second — both visible live on the
dashboard above.

## Architecture

```
                    ┌─────────────────┐
   traffic spike →  │  MonitorAgent   │  polls hot query latency + EXPLAIN ANALYZE
                    │                 │  (+ discovers the hot query itself via
                    │                 │  crdb_internal.node_statement_statistics,
                    │                 │  + pulls cluster capacity via ccloud CLI)
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
                    │ ReportingAgent  │  writes NL report, cross-references the
                    │                 │  ccloud Cloud audit trail, saves outcome
                    │                 │  back to vector memory for next time
                    └─────────────────┘

  Every step from every agent is written to agent_trace (explainability),
  streamed live by dashboard.py — publicly reachable on the AWS deployment above.
```

**Why CockroachDB, specifically:** the incident memory lives in the *same*
distributed SQL database as the operational data, using CockroachDB's native
`VECTOR` type + `CREATE VECTOR INDEX` (C-SPANN) — no separate vector DB, no
sync pipeline. That's a genuine, judge-visible use of a CockroachDB-specific
capability, not just "Postgres with pgvector bolted on." The memory
short-circuit means recall isn't just flavor — it measurably skips the LLM on
repeat incidents, **confirmed live** against our real cluster (see "Fixed
during testing" below — this was broken until we tested against a real
cluster, not just mocks).

**Second CockroachDB tool — the `ccloud` CLI:** `tools/ccloud_client.py`
pulls cluster-level context (`ccloud cluster info`) so the agent can tell an
app-level problem (missing index, stale stats) apart from a cluster-level one
(undersized plan), and cross-references the CockroachDB Cloud audit trail
(`ccloud audit list`) into the final report for defense-in-depth
explainability. Both required CockroachDB tools — Distributed Vector Indexing
and the `ccloud` CLI — are confirmed working against our live, deployed
cluster, not just implemented.

**Why AWS:** the app is deployed on **EC2** (see Live demo above, and
`deploy/DEPLOY.md` for the full runbook), with an IAM instance role instead
of hardcoded credentials. ECS is the scaled resource (CloudWatch →
`UpdateService`). The reasoning agent can also run on Bedrock
(`LLM_BACKEND=bedrock`), which doubles as a second AWS integration point
beyond compute — the default backend is Gemini for zero-setup local dev, so
set `LLM_BACKEND=bedrock` and `BEDROCK_MODEL_ID` in `.env` if you want Bedrock
counted as part of your AWS integration for judging.

## Project layout

```
agentops/
├── db/schema.sql          # orders (no index, on purpose — see "Fixed" below re: FK auto-indexing)
├── db/seed.py              # seeds demo data + one pre-loaded memory (bulk INSERT via execute_values)
├── tools/crdb_client.py    # pooled CockroachDB access (timed queries, EXPLAIN ANALYZE, DDL)
├── tools/ccloud_client.py  # ccloud CLI wrapper — cluster capacity + Cloud audit trail (2nd CRDB tool)
├── tools/aws_client.py     # CloudWatch metrics + ECS scaling
├── tools/embeddings.py     # text -> 384-dim vector (sentence-transformers, offline fallback)
├── tools/llm.py            # Bedrock, Anthropic API, or Gemini — same interface, retry/timeout
├── agents/                 # one file per agent, all inherit agents/base.py for tracing
│   ├── monitor_agent.py    #   latency check + crdb_internal hot-query discovery + ccloud capacity
│   ├── memory_agent.py     #   vector store/recall, returns match distance
│   ├── diagnostic_agent.py #   LLM diagnosis (CREATE INDEX or ANALYZE) + memory-recall fallback
│   ├── execution_agent.py  #   guardrails: confidence gate, dry-run, apply, rollback, ECS scale
│   └── reporting_agent.py  #   NL report + ccloud audit cross-reference + writes resolution to memory
├── orchestrator.py         # wires the agents into the incident pipeline, short-circuit + guardrails
├── load_generator.py       # simulates a traffic spike for a repeatable demo
├── demo_scenario.py        # single command: seed -> missing-index incident -> fix -> report
├── demo_stale_stats.py     # single command: seed -> stale-statistics incident -> ANALYZE -> report
├── demo_recall.py          # replays the self-improving recall moment WITHOUT reseeding data
├── diagnose_and_reset.py   # inspects + resets demo tables on a live cluster (safe between takes)
├── dashboard.py            # live agent_trace viewer with optimistic rendering (stdlib, port 8888)
├── smoke_test.py           # offline end-to-end run of the real pipeline, DB/LLM/AWS all mocked
├── tests/                  # pytest suite over pure logic, no DB/network required
├── deploy/                 # EC2 + systemd deployment runbook and unit files
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
   `MEMORY_MATCH_MAX_DISTANCE`, connection pool sizing, etc). Set
   `CCLOUD_CLUSTER_NAME` once you've installed the `ccloud` CLI and logged in
   (`ccloud auth login --no-redirect` for headless machines) — this is the
   second required CockroachDB tool (`tools/ccloud_client.py`); the pipeline
   still runs fine without it, it just skips that trace step.
3. `pip install -r requirements.txt` (or `requirements-lite.txt` to skip the
   heavy `sentence-transformers`/torch dependency — the offline embedding
   fallback in `tools/embeddings.py` works fine without it, and this is what
   the live AWS deployment actually uses).
4. `python demo_scenario.py` — seeds demo data on an unindexed `orders`
   table, detects the slow query, diagnoses it, creates the index,
   re-benchmarks, and prints the report. That's your whole demo in one
   command. **Note:** table size matters — 200-300k rows reliably triggers a
   real incident in well under a minute; we tested at 2M rows and hit
   18-minute index builds on CockroachCloud's free tier, which is far too
   slow for repeatable demo takes. Tune `SEED_NUM_ORDERS` in `.env`.
5. `python demo_recall.py <customer-id>` — re-triggers the same incident
   *without* reseeding data (drops the just-applied index to simulate the
   fix recurring, then re-runs the pipeline). Run this after
   `demo_scenario.py` to see the self-improving short-circuit — memory
   recall skips the LLM entirely on the repeat.
6. `python demo_stale_stats.py` — the second incident type (fix = `ANALYZE`),
   to show the pipeline generalizes past one hardcoded scenario.
7. `python dashboard.py` (optional, run alongside any demo script) — live
   view of `agent_trace` at `http://localhost:8888`.
8. Optional, for the "watch it happen live" version:
   `python load_generator.py --customer-id <id> &` then
   `python orchestrator.py --customer-id <id> --loop`.

ECS scaling and CloudWatch metrics are stubbed to no-op gracefully if
`ECS_CLUSTER_NAME` / `ECS_SERVICE_NAME` aren't set, so the DB-only demo
above works even before you've stood up a real ECS service.

## Deploying for the hackathon submission

**Already done** — live at http://3.213.20.174:8888, deployed on an EC2
instance with an IAM instance role (no hardcoded AWS credentials), running
`dashboard.py` as a systemd service with a stable Elastic IP. Full runbook in
**[`deploy/DEPLOY.md`](deploy/DEPLOY.md)**, plus
`deploy/agentops-dashboard.service` and `deploy/agentops-orchestrator.service`
unit files so the demo survives SSH disconnects and instance reboots.

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

The monitor attempts to read CockroachDB's own per-statement telemetry
(`crdb_internal.node_statement_statistics`) to identify the worst-performing
statement touching the target table, logging it into the trace as extra
autonomous signal. **On CockroachDB Cloud serverless clusters this is
restricted by design** — the cluster returns an explicit error ("Access to
crdb_internal and system is restricted in production... contact Cockroach
Labs for a supported alternative") rather than data. This is expected
managed-cluster behavior, not a bug: the `try/except` around it catches this
every time and falls back to the built-in query, so the pipeline is
unaffected either way. Confirmed working as designed against our live
cluster.

## `ccloud` CLI — cluster-capacity context (2nd CockroachDB tool)

`MonitorAgent.check_cluster_capacity()` calls `ccloud cluster info` to pull
plan/state/node-count context, so the diagnosis can tell an app-level problem
apart from a cluster-level one. `ReportingAgent` separately calls
`ccloud audit list` to fold the CockroachDB Cloud control-plane audit trail
into the final report. Both confirmed live against our real cluster — a
sample trace line: `monitor · cluster capacity (ccloud) — plan=SERVERLESS
state=CREATED nodes=0`. Best-effort by design: if `ccloud` isn't installed or
`CCLOUD_CLUSTER_NAME` isn't set, both calls return `None` and are skipped
silently — never blocks the DB-only pipeline.

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
  built from the parsed index name — never from raw model text). Observed
  live on our cluster: a fix that made latency *worse* was correctly rolled
  back automatically rather than declared a success.
- **One fix per incident** — no fix/re-fix loops.

All guardrail thresholds live in `.env` (see `.env.example`).

## Live dashboard

`python dashboard.py` serves an explainability dashboard on
`http://localhost:8888` (or the public AWS URL above) that tails `agent_trace`
and renders each agent step as it lands — optimistically drawing the expected
pipeline steps as ghost cards, then reconciling them to confirmed cards as the
DB rows arrive. Stdlib only.

## Tests

`pip install -r requirements-dev.txt && pytest tests/ -v` runs the unit
suite (DDL safety patterns, confidence/DRY_RUN guardrails, rollback-by-parsed-name
safety, short-circuit distance logic, diagnosis JSON parsing, offline
embedding fallback, ccloud CLI fallback) — 34 tests, no DB or network
required. CI runs it on every push/PR (`.github/workflows/ci.yml`).

There's also `smoke_test.py` at the repo root: an offline end-to-end run of
the *actual* orchestrator/agents code (DB, LLM, and AWS calls mocked) that
exercises all three incidents in one pass — fresh missing-index incident,
repeat incident (verifies the LLM is actually skipped), and a stale-stats
incident. Run with `python smoke_test.py`.

## Fixed during testing (transparency note)

This project went through two rounds of hardening: first an offline audit
against mocks, then real verification against a live CockroachCloud cluster
and a real AWS EC2 deployment. The second round caught bugs the first
couldn't — mocks can't reproduce CockroachDB's actual SQL grammar, its
managed-cluster restrictions, or a real LLM's exact output shape. All of the
following are now fixed, and the ones with an obvious regression shape are
covered by the test suite above.

**Found via offline audit (mocks, before any real cluster):**
- `MonitorAgent.discover_hot_query`, `DiagnosticAgent.diagnose_from_memory`,
  and `ExecutionAgent.rollback_fix` didn't exist; `ExecutionAgent.apply_fix`
  didn't accept a `confidence` argument or return the status string the
  orchestrator expected. The pipeline would have raised `AttributeError` /
  `TypeError` on the first detected incident.
- `MemoryAgent.search_similar` never selected a `distance` column, so
  `orchestrator.should_short_circuit` always saw `None` and the
  self-improving short-circuit could never actually trigger.
- The diagnostic system prompt only ever allowed `CREATE INDEX`, silently
  breaking the `ANALYZE` fix path that `demo_stale_stats.py` depends on.
- `tools/llm.py` imported `google-genai` unconditionally at module load, so
  the app would fail to import on any backend if that package wasn't
  installed. Imports are now lazy per backend, and the Bedrock/Anthropic
  branches are real implementations, not stubs.

**Found only by testing against the real cluster:**
- `db/schema.sql` declared `customer_id` with a `REFERENCES customers(id)`
  foreign key. CockroachDB auto-creates a secondary index on any FK column
  that doesn't already have one (FK columns must be indexed) — which
  silently defeated the "no index on customer_id" premise the whole demo
  depends on. Fixed by dropping the FK constraint; referential integrity is
  enforced at the seed/app level instead.
- `db/seed.py` used `cursor.executemany()`, which silently sends one network
  round-trip per row instead of a real batch insert — seeding 2M rows took
  over an hour. Rewritten to use `psycopg2.extras.execute_values()`, which
  builds real multi-row `INSERT` statements.
- A recalled fix replaying the same `CREATE INDEX` on a second run hit a
  duplicate-index error on the real cluster (the schema isn't reset between
  runs). First fix attempt (blindly inserting `IF NOT EXISTS`) broke again
  when the LLM proposed an *unnamed* `CREATE INDEX ... ON t (...)` — valid
  SQL on its own, but `IF NOT EXISTS` requires a name, so
  `CREATE INDEX IF NOT EXISTS ON t (...)` is a syntax error. Fixed properly:
  `ExecutionAgent._normalize_create_index()` now parses the table/columns
  and generates a name whenever one is missing, before ever adding
  `IF NOT EXISTS`.
- `tools/crdb_client.py`'s `run_query()` called `cur.execute(sql, params or ())`
  — coercing `None` to an empty tuple. psycopg2 treats *any* non-`None`
  value as "this query has `%s` placeholders," so a literal `%` in SQL (e.g.
  `ILIKE '%orders%'` in the hot-query discovery query) was misparsed as a
  format specifier, raising a confusing `IndexError: tuple index out of
  range`. Fixed by passing `params` through unmodified — `None` now
  correctly disables substitution parsing entirely.
- `dashboard.py`'s trace query selected a `data` column from `agent_trace`
  that never existed in the schema (`id, incident_id, agent_name, step,
  detail, created_at` — no `data`). Every `/api/trace` request silently
  failed with a 500, which the frontend's polling JS swallows without
  surfacing an error — the dashboard just looked permanently empty. Found by
  `curl`-ing the endpoint directly instead of trusting the browser.
- `MonitorAgent.check_cluster_capacity()` assumed `node_count` was a
  top-level field in `ccloud cluster info` output; it's actually nested
  under `regions[0].node_count`. Confirmed and fixed against real `ccloud`
  output on a live serverless cluster.

## Roadmap to Aug 18

| Days | Focus | Status |
|---|---|---|
| **Aug 8–9** | CockroachDB Cloud cluster + AWS account/ECS sample service. Run this scaffold end-to-end locally with `demo_scenario.py`. | ✅ done |
| **Aug 10** | Real EXPLAIN ANALYZE parsing edge cases; tighten the diagnostic prompt. | ✅ done |
| **Aug 11** | Wire `tools/aws_client.py` to a real ECS cluster/service; confirm CloudWatch CPU pulls correctly. | ✅ done |
| **Aug 12** | Deploy AgentOps itself to AWS (EC2 + systemd), not just call AWS APIs from a laptop. | ✅ done — live at the URL above |
| **Aug 13** | Guardrails: dry-run mode, rollback path, max-one-fix-per-incident. | ✅ done — confidence gate, `DRY_RUN`, index rollback (verified live), one-fix/incident |
| **Aug 14** | Second incident type (stale table stats) to show generality. | ✅ done — `demo_stale_stats.py` + `ANALYZE` fix family |
| **Aug 15** | Live-status dashboard reading `agent_trace` in real time. | ✅ done — `dashboard.py`, deployed and publicly reachable |
| **Aug 16** | Full dry runs of the demo, timing it, fixing whatever breaks. | ✅ done — multiple real-cluster bugs found and fixed (see above) |
| **Aug 17** | ccloud CLI integration (2nd required CockroachDB tool); record demo video. | ✅ ccloud confirmed live; ⬜ video |
| **Aug 18** | Buffer day + submission. | ⬜ in progress |

Also done, beyond the original roadmap: pooled DB connections, bulk seeding
via `execute_values`, LLM timeout/retry with a memory-based fallback, a
hardened single-statement DDL guard, `crdb_internal`-driven hot-query
discovery, the self-improving memory short-circuit (verified live), a 34-test
CI suite, and a full AWS EC2 deployment with IAM-role-based credentials.

## USP — what makes this different

**An autonomous database reliability engineer whose long-term memory lives
inside the same distributed SQL database it's protecting — so every incident
it resolves makes the next one faster, cheaper, and auditable, with no
separate vector store and no sync pipeline.**

- **Self-improving, not just self-healing.** The resolution gets written back
  into vector memory, so the *second* occurrence of a known incident is
  recalled and fixed **without an LLM call** — verified live, repeatedly,
  against our real cluster. Run `demo_recall.py` twice — the second run's
  trace shows `diagnosis (recalled) — LLM skipped`. That's a measurable,
  on-stage "it gets smarter" moment most agent demos can't show.
- **Memory colocated with operational data.** Incident memory is a native
  `VECTOR` column + C-SPANN index in the *same* cluster as `orders` — no
  Pinecone, no ETL, no consistency drift between the data and the memory of
  the data.
- **Explainability as a first-class artifact.** Every autonomous action —
  including ones it *declined* to take (held for approval, rolled back) — is
  in `agent_trace`, replayable via the live dashboard and summarized in the
  NL report, cross-referenced against CockroachDB Cloud's own audit trail.
- **Generalizes past one hardcoded scenario.** Two independently-diagnosed
  incident classes (missing index, stale statistics), each with its own fix
  family and guard.
- **Actually deployed, not just described.** Runs on AWS EC2 right now,
  publicly reachable, not a local-only demo.

## What to emphasize for judging

- **CockroachDB-specific**: vector memory colocated with operational data,
  `EXPLAIN ANALYZE`-driven diagnosis, `ccloud` CLI for cluster-capacity
  context and Cloud audit trail cross-referencing (`tools/ccloud_client.py`)
  — two CockroachDB tools used, both confirmed live, not just initialized.
- **AWS-specific**: deployed on EC2 with an IAM instance role, CloudWatch →
  ECS scaling loop, optional Bedrock for reasoning.
- **Explainability**: the `agent_trace` table + live public dashboard + the
  final NL report are the answer to "why did the agent do that" — surface
  all three in the demo, not just the end result.
- **Self-improving memory**: run the same demo twice and point at the
  `LLM skipped` trace line — this is the single strongest differentiator
  versus observe-only APM tools and bolt-on vector databases.
- **Real-cluster rigor**: the "Fixed during testing" section above is itself
  evidence of quality engineering — every claim in this README was verified
  against a live cluster and a live AWS deployment, not just mocks.
