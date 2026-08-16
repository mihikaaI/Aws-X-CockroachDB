"""Combines the current incident's symptoms with retrieved memories and asks
Claude (via Bedrock or the Anthropic API -- see tools/llm.py) to diagnose a
root cause and propose a fix. The model is constrained to only ever propose
CREATE INDEX statements; execution_agent.py enforces that constraint again
independently before running anything.

The LLM is a synchronous dependency on the incident critical path, so it's
wrapped with retries in tools/llm.py and a memory-based fallback here: if the
model is unreachable, we degrade to the closest past incident from vector
memory rather than failing the whole pipeline.
"""
import json
import re

from agents.base import Agent
from tools.llm import LLMError, call_llm

SYSTEM_PROMPT = """You are AgentOps, an autonomous database reliability engineer.
You diagnose CockroachDB performance incidents from query latency, EXPLAIN
ANALYZE plans, and memories of similar past incidents. Respond with STRICT
JSON only -- no markdown fences, no commentary outside the JSON -- matching
this schema:
{
  "root_cause": "short string",
  "confidence": 0.0-1.0,
  "proposed_fix_sql": "a single fix statement (see allowed fixes), or null if none applies",
  "reasoning": "2-3 sentence explanation a human engineer could read"
}

Choose the fix that matches the incident:
- Missing index (a full table scan in the plan): propose ONE
  `CREATE INDEX ...` statement.
- Stale table statistics (no full scan, but the optimizer picked a poor plan
  from bad row-count estimates): propose ONE `ANALYZE <table>` statement to
  refresh statistics.

Propose exactly one statement, and ONLY from those two families. Never propose
DROP, ALTER TABLE, DELETE, UPDATE, or any other DML/DDL.
"""

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class DiagnosticAgent(Agent):
    name = "diagnostic"

    def diagnose(self, monitor_result, similar_incidents, incident_id=None):
        # Compact JSON in the prompt: no indentation whitespace, tight
        # separators. Same information, materially fewer input tokens.
        memory_json = json.dumps(similar_incidents, default=str, separators=(",", ":"))
        user_prompt = f"""Current incident:
- latency: {monitor_result['latency_ms']:.1f} ms
- full table scan detected: {monitor_result['full_scan']}
- EXPLAIN ANALYZE plan:
{monitor_result['plan_text']}

Similar past incidents from memory (may be empty):
{memory_json}

Diagnose the root cause and propose a fix."""

        try:
            raw = call_llm(SYSTEM_PROMPT, user_prompt)
            diagnosis = self._parse(raw)
        except LLMError as e:
            diagnosis = self._fallback_from_memory(similar_incidents, str(e))

        self.log(
            incident_id,
            "diagnosis",
            f"{diagnosis.get('root_cause')} (confidence={diagnosis.get('confidence')})",
            data={
                "root_cause": diagnosis.get("root_cause"),
                "confidence": diagnosis.get("confidence"),
                "proposed_fix_sql": diagnosis.get("proposed_fix_sql"),
                "source": diagnosis.get("source", "llm"),
            },
        )
        return diagnosis

    def diagnose_from_memory(self, top_incident, incident_id=None):
        """Short-circuit: a near-identical resolved incident is already in
        vector memory, so reuse its known fix and skip the LLM entirely. This
        is the 'it gets smarter and cheaper every time' beat -- the second
        occurrence of an incident costs zero model calls."""
        distance = top_incident.get("distance")
        diagnosis = {
            "root_cause": top_incident.get("root_cause") or "recalled from memory",
            "confidence": 0.95,
            "proposed_fix_sql": top_incident.get("resolution_sql"),
            "reasoning": (
                "Recalled a near-identical resolved incident from vector memory "
                "and reused its known fix without invoking the LLM."
            ),
            "source": "memory_shortcircuit",
            "llm_skipped": True,
        }
        self.log(
            incident_id,
            "diagnosis (recalled)",
            f"{diagnosis['root_cause']} — LLM skipped"
            + (f" (distance={distance:.3f})" if distance is not None else ""),
            data={
                "root_cause": diagnosis["root_cause"],
                "confidence": diagnosis["confidence"],
                "proposed_fix_sql": diagnosis["proposed_fix_sql"],
                "source": "memory_shortcircuit",
                "llm_skipped": True,
                "distance": distance,
            },
        )
        return diagnosis

    @staticmethod
    def _fallback_from_memory(similar_incidents, error):
        """LLM unreachable: degrade to the closest resolved past incident
        instead of taking the whole pipeline down. The vector recall already
        ran, so we reuse its top hit as a lower-confidence diagnosis."""
        if similar_incidents:
            best = similar_incidents[0]
            return {
                "root_cause": best.get("root_cause") or "recalled from memory",
                "confidence": 0.5,
                "proposed_fix_sql": best.get("resolution_sql"),
                "reasoning": (
                    "LLM unavailable; reused the most similar past incident from "
                    f"vector memory. (LLM error: {error})"
                ),
                "source": "memory_fallback",
            }
        return {
            "root_cause": "LLM unavailable and no similar incident in memory",
            "confidence": 0.0,
            "proposed_fix_sql": None,
            "reasoning": f"LLM error: {error}",
            "source": "unavailable",
        }

    @classmethod
    def _parse(cls, raw: str) -> dict:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.split("\n", 1)[-1] if cleaned.lower().startswith("json") else cleaned
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Last resort: pull the first {...} block out of surrounding prose.
            match = _JSON_OBJECT.search(cleaned)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            return {
                "root_cause": "unparseable LLM response",
                "confidence": 0.0,
                "proposed_fix_sql": None,
                "reasoning": raw[:500],
            }
