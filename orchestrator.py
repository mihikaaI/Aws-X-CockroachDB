"""The multi-agent loop: monitor -> memory -> diagnostic -> execution -> reporting.

Run directly:
    python orchestrator.py --customer-id <uuid>          # single check
    python orchestrator.py --customer-id <uuid> --loop    # continuous monitoring

Guardrails (all env-tunable): confidence-gated auto-apply, DRY_RUN propose-only,
one fix per incident, and automatic rollback if the applied index doesn't help.
"""
import argparse
import os
import time
from datetime import date, timedelta

from agents.diagnostic_agent import DiagnosticAgent
from agents.execution_agent import ExecutionAgent
from agents.memory_agent import MemoryAgent
from agents.monitor_agent import MonitorAgent
from agents.reporting_agent import ReportingAgent

SINCE_DATE_DEFAULT = date.today() - timedelta(days=90)

# Distance below which a recalled incident counts as "the same incident again"
# and we skip the LLM. Kept tight so only near-identical symptoms short-circuit.
MEMORY_MATCH_MAX_DISTANCE = float(os.getenv("MEMORY_MATCH_MAX_DISTANCE", "0.05"))
# Minimum fractional latency improvement to keep a fix; below this we roll back.
MIN_IMPROVEMENT_RATIO = float(os.getenv("MIN_IMPROVEMENT_RATIO", "0.10"))

monitor = MonitorAgent()
memory = MemoryAgent()
diagnostic = DiagnosticAgent()
execution = ExecutionAgent()
reporting = ReportingAgent()


def symptom_signature(monitor_result):
    """Canonical, latency-independent symptom string.

    Embedding this instead of the raw 'latency 934ms' text means the *same
    class* of incident maps to the same vector every time, so vector memory
    actually clusters (and, offline, matches exactly) -- which is what makes
    the short-circuit reliable."""
    if monitor_result.get("full_scan"):
        return "hot query performing a full table scan; missing secondary index"
    return "hot query with elevated latency; no full table scan detected"


def should_short_circuit(top_incident, threshold=MEMORY_MATCH_MAX_DISTANCE):
    """True when the closest recalled incident is near-identical AND carries a
    known fix -- meaning we can resolve it from memory without the LLM."""
    if not top_incident:
        return False
    distance = top_incident.get("distance")
    if distance is None:
        return False
    return float(distance) <= threshold and bool(top_incident.get("resolution_sql"))


def run_once(customer_id, since_date=SINCE_DATE_DEFAULT):
    baseline = monitor.check(customer_id, since_date)
    if not baseline["breached"]:
        print("No incident: latency within threshold.")
        return None

    symptom_text = symptom_signature(baseline)
    incident_id = memory.store(
        symptom_text,
        root_cause=None,
        resolution_sql=None,
        latency_before_ms=baseline["latency_ms"],
        latency_after_ms=None,
        resolved=False,
    )

    similar = memory.search_similar(symptom_text, incident_id=incident_id)
    top = similar[0] if similar else None

    # Self-improving path: identical resolved incident in memory -> skip the LLM.
    if should_short_circuit(top):
        diagnosis = diagnostic.diagnose_from_memory(top, incident_id=incident_id)
    else:
        diagnosis = diagnostic.diagnose(baseline, similar, incident_id=incident_id)

    # One fix per incident, confidence-gated, dry-run aware.
    status = execution.apply_fix(
        diagnosis.get("proposed_fix_sql"),
        incident_id=incident_id,
        confidence=diagnosis.get("confidence"),
    )
    fix_applied = status == "applied"

    after = execution.benchmark(customer_id, since_date, incident_id=incident_id)

    # Rollback guardrail: if the applied index didn't actually help, drop it.
    if fix_applied:
        before_ms = baseline["latency_ms"]
        after_ms = after["latency_ms"]
        improved = before_ms > 0 and after_ms <= before_ms * (1 - MIN_IMPROVEMENT_RATIO)
        if not improved:
            execution.rollback_fix(diagnosis.get("proposed_fix_sql"), incident_id=incident_id)
            fix_applied = False

    execution.maybe_scale(incident_id=incident_id)

    report = reporting.generate(incident_id, baseline, after, diagnosis, fix_applied)
    print("\n" + report + "\n")
    return report


def run_loop(customer_id, interval_s=15):
    while True:
        run_once(customer_id)
        time.sleep(interval_s)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--customer-id", required=True)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=15)
    args = parser.parse_args()

    if args.loop:
        run_loop(args.customer_id, args.interval)
    else:
        run_once(args.customer_id)
