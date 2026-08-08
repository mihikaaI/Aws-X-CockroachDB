# AgentOps — Autonomous AI Infrastructure Engineer

Multi-agent system that watches a CockroachDB-backed app, diagnoses performance
incidents using vector-recalled memory + LLM reasoning, fixes them (schema
changes, then AWS scaling if needed), and explains what it did in plain English.

## Architecture

```
                    ┌─────────────────┐
   traffic spike →  │  MonitorAgent   │  polls hot query latency + EXPLAIN ANALYZE
                    └────────┬────────┘
                             │ latency > threshold
                             ▼
                    ┌─────────────────┐
                    │  MemoryAgent    │  embeds symptoms → vector search over
                    │  (CRDB VECTOR)  │  past incidents (CockroachDB C-SPANN index)
                    └────────┬────────┘
                             │ similar incidents
                             ▼
                    ┌─────────────────┐
                    │ DiagnosticAgent │  Claude (Bedrock or Anthropic API) reasons
                    │                 │  over metrics + plan + memory → root cause
                    └────────┬────────┘  + proposed CREATE INDEX
                             │
                             ▼
                    ┌─────────────────┐
                    │ ExecutionAgent  │  applies index → re-benchmarks →
                    │                 │  if CPU still high: scales ECS
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │ ReportingAgent  │  writes NL report, saves outcome
                    │                 │  back to vector memory for next time
                    └─────────────────┘

  Every step from every agent is written to agent_trace (explainability).
```

**Why CockroachDB, specifically:** the incident memory lives in the *same*
distributed SQL database as the operational data, using CockroachDB's native
`VECTOR` type + `CREATE VECTOR INDEX` (C-SPANN) — no separate vector DB, no
sync pipeline. That's a genuine, judge-visible use of a CockroachDB-specific
capability, not just "Postgres with pgvector bolted on."

**Why AWS:** ECS is the scaled resource (CloudWatch → `UpdateService`), and
the reasoning agent can run on Bedrock, which doubles as a second AWS
integration point beyond raw compute.

## Project layout

```
agentops/
├── db/schema.sql        # orders (no index, on purpose) + incidents (vector memory) + agent_trace
├── db/seed.py            # seeds demo data + one pre-loaded memory
├── tools/crdb_client.py  # CockroachDB access (timed queries, EXPLAIN ANALYZE, DDL)
├── tools/aws_client.py   # CloudWatch metrics + ECS scaling
├── tools/embeddings.py   # text -> 384-dim vector (sentence-transformers, offline fallback)
├── tools/llm.py          # Bedrock or Anthropic API, same interface either way
├── agents/               # one file per agent, all inherit agents/base.py for tracing
├── orchestrator.py       # wires the agents into the incident pipeline
├── load_generator.py     # simulates a traffic spike for a repeatable demo
└── demo_scenario.py      # single command: seed -> incident -> fix -> report
```

## Setup

1. **CockroachDB** — easiest path is a free CockroachDB Cloud serverless
   cluster (vector indexes are supported there). For local dev:
   `docker compose up -d`, then `cockroach sql --insecure -e "CREATE DATABASE agentops"`.
2. `cp .env.example .env` and fill in `DATABASE_URL` and your LLM backend
   (Bedrock is the default; flip to `LLM_BACKEND=anthropic` if you'd rather
   use an Anthropic API key directly — faster to set up under time pressure).
3. `pip install -r requirements.txt`
4. `python demo_scenario.py` — seeds ~200k unindexed orders, detects the
   slow query, diagnoses it, creates the index, re-benchmarks, and prints
   the report. That's your whole demo in one command.
5. Optional, for the "watch it happen live" version:
   `python load_generator.py --customer-id <id> &` then
   `python orchestrator.py --customer-id <id> --loop`.

ECS scaling and CloudWatch metrics are stubbed to no-op gracefully if
`ECS_CLUSTER_NAME` / `ECS_SERVICE_NAME` aren't set, so the DB-only demo
above works even before you've stood up a real ECS service.

## Roadmap to Aug 18

| Days | Focus |
|---|---|
| **Aug 8–9** | CockroachDB Cloud cluster + AWS account/ECS sample service. Run this scaffold end-to-end locally with `demo_scenario.py`. |
| **Aug 10** | Real EXPLAIN ANALYZE parsing edge cases (joins, multiple missing indexes); tighten the diagnostic prompt with a couple more few-shot examples. |
| **Aug 11** | Wire `tools/aws_client.py` to your real ECS cluster/service; confirm CloudWatch CPU pulls correctly; test `maybe_scale`. |
| **Aug 12** | Deploy a small sample app to ECS in front of the CockroachDB cluster (this is what "traffic increases" is monitoring) — a trivial FastAPI/Express app hitting the `orders` table is enough. |
| **Aug 13** | Guardrails: dry-run mode, rollback path if a fix doesn't help, max-one-fix-per-incident to avoid loops. |
| **Aug 14** | Polish `load_generator.py` timing so the live demo reliably reproduces the spike; add a second incident type (e.g., stale table stats) to show generality, not just one hardcoded scenario. |
| **Aug 15** | Optional: minimal live-status HTML page reading `agent_trace` in real time — strong demo value for "explainability" if you have time. |
| **Aug 16** | Full dry runs of the demo, 3–4 times, timing it. Fix whatever breaks. |
| **Aug 17** | Record a backup demo video (live demos fail; judges appreciate a fallback). Write the submission write-up: architecture, what's CockroachDB-specific, what's AWS-specific. |
| **Aug 18** | Buffer day + submission. |

## What to emphasize for judging

- **CockroachDB-specific**: vector memory colocated with operational data,
  `EXPLAIN ANALYZE`-driven diagnosis, distributed SQL resilience story if
  you get time to demo a node failure mid-incident.
- **AWS-specific**: Bedrock for reasoning, CloudWatch → ECS scaling loop.
- **Explainability**: the `agent_trace` table + the final NL report are your
  answer to "why did the agent do that" — surface both in the demo, not
  just the end result.
