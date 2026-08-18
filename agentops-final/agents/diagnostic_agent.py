"""Combines the current incident's symptoms with retrieved memories and asks
Claude (via Bedrock or the Anthropic API -- see tools/llm.py) to diagnose a
root cause and propose a fix. The model is constrained to only ever propose
one of two fix families -- CREATE INDEX or ANALYZE -- matching the two
incident classes AgentOps handles; execution_agent.py enforces that
constraint again independently before running anything.
"""
import json

from agents.base import Agent
from tools.llm import call_llm

SYSTEM_PROMPT = """You are AgentOps, an autonomous database reliability engineer.
You diagnose CockroachDB performance incidents from query latency, EXPLAIN
ANALYZE plans, and memories of similar past incidents. Respond with STRICT
JSON only -- no markdown fences, no commentary outside the JSON -- matching
this schema:
{
  "root_cause": "short string",
  "confidence": 0.0-1.0,
  "proposed_fix_sql": "a single CREATE INDEX statement, a single ANALYZE <table> statement, or null if no fix applies",
  "reasoning": "2-3 sentence explanation a human engineer could read"
}
Choose CREATE INDEX when the EXPLAIN ANALYZE plan shows a full table scan
(missing secondary index). Choose ANALYZE <table> when the query is slow but
the plan shows no full scan (stale optimizer statistics). Only ever propose
one of those two statement types. Never propose DROP, ALTER TABLE, DELETE, or
any other DML/DDL.
"""


class DiagnosticAgent(Agent):
    name = "diagnostic"

    def diagnose(self, monitor_result, similar_incidents, incident_id=None):
        user_prompt = f"""Current incident:
- latency: {monitor_result['latency_ms']:.1f} ms
- full table scan detected: {monitor_result['full_scan']}
- EXPLAIN ANALYZE plan:
{monitor_result['plan_text']}

Similar past incidents from memory (may be empty):
{json.dumps(similar_incidents, default=str, indent=2)}

Diagnose the root cause and propose a fix."""

        raw = call_llm(SYSTEM_PROMPT, user_prompt)
        diagnosis = self._parse(raw)

        self.log(
            incident_id,
            "diagnosis",
            f"{diagnosis.get('root_cause')} (confidence={diagnosis.get('confidence')})",
        )
        return diagnosis

    def diagnose_from_memory(self, top_incident, incident_id=None):
        """Self-improving short-circuit: a near-identical incident was already
        resolved before (see orchestrator.should_short_circuit), so reuse its
        root cause and fix instead of calling the LLM at all. This is what
        makes the *second* occurrence of a known incident cost zero model
        calls -- logged here as 'diagnosis (recalled) — LLM skipped' in the
        detail, so it's visible in both the console and the live dashboard.
        """
        diagnosis = {
            "root_cause": top_incident.get("root_cause") or "matched a previously resolved incident",
            "confidence": 1.0,
            "proposed_fix_sql": top_incident.get("resolution_sql"),
            "reasoning": (
                f"Vector memory matched a previously resolved incident "
                f"(id={top_incident.get('id')}); reusing its known fix instead "
                f"of calling the LLM."
            ),
        }
        self.log(
            incident_id,
            "diagnosis",
            f"(recalled) — LLM skipped | {diagnosis['root_cause']} "
            f"| reused fix: {diagnosis['proposed_fix_sql']}",
        )
        return diagnosis

    @staticmethod
    def _parse(raw: str) -> dict:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.split("\n", 1)[-1] if cleaned.lower().startswith("json") else cleaned
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {
                "root_cause": "unparseable LLM response",
                "confidence": 0.0,
                "proposed_fix_sql": None,
                "reasoning": raw[:500],
            }
