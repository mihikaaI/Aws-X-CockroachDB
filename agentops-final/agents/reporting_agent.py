"""Closes the loop: writes the plain-language incident report (the
'Explainability' pillar) and persists the resolved outcome back into
long-term memory so future incidents can recall it.
"""
from agents.base import Agent
from agents.memory_agent import MemoryAgent
from tools import ccloud_client


class ReportingAgent(Agent):
    name = "reporting"

    def generate(self, incident_id, before, after, diagnosis, fix_applied):
        improvement_pct = 0.0
        if before["latency_ms"] > 0:
            improvement_pct = (before["latency_ms"] - after["latency_ms"]) / before["latency_ms"] * 100

        report = (
            f"Latency reduced from {before['latency_ms']:.0f} ms to {after['latency_ms']:.0f} ms "
            f"({improvement_pct:.1f}% improvement) after diagnosing: {diagnosis.get('root_cause', 'unknown cause')}.\n"
            f"Fix applied: {diagnosis.get('proposed_fix_sql') if fix_applied else 'none -- see reasoning below'}\n"
            f"Reasoning: {diagnosis.get('reasoning', '')}"
        )

        # ccloud CLI, second use: cross-reference the Cloud-side audit trail so
        # a human reviewing this incident sees "what the agent did" alongside
        # "what happened on the account" in the same report. Best-effort --
        # silently omitted if ccloud isn't installed/configured.
        audit_events = ccloud_client.recent_audit_events(limit=5)
        if audit_events:
            report += f"\nCockroachDB Cloud audit trail (last {len(audit_events)} events): " + "; ".join(
                f"{e.get('action_name', e.get('action', '?'))} by {e.get('user', '?')}" for e in audit_events
            )

        self.log(incident_id, "report", report.replace("\n", " | "))

        MemoryAgent().update_resolution(
            incident_id,
            root_cause=diagnosis.get("root_cause"),
            resolution_sql=diagnosis.get("proposed_fix_sql") if fix_applied else None,
            latency_after_ms=after["latency_ms"],
            resolved=fix_applied,
        )
        return report
