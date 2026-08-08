"""The multi-agent loop: monitor -> memory -> diagnostic -> execution -> reporting.

Run directly:
    python orchestrator.py --customer-id <uuid>          # single check
    python orchestrator.py --customer-id <uuid> --loop    # continuous monitoring
"""
import argparse
import time
from datetime import date, timedelta

from agents.diagnostic_agent import DiagnosticAgent
from agents.execution_agent import ExecutionAgent
from agents.memory_agent import MemoryAgent
from agents.monitor_agent import MonitorAgent
from agents.reporting_agent import ReportingAgent

SINCE_DATE_DEFAULT = date.today() - timedelta(days=90)

monitor = MonitorAgent()
memory = MemoryAgent()
diagnostic = DiagnosticAgent()
execution = ExecutionAgent()
reporting = ReportingAgent()


def run_once(customer_id, since_date=SINCE_DATE_DEFAULT):
    baseline = monitor.check(customer_id, since_date)
    if not baseline["breached"]:
        print("No incident: latency within threshold.")
        return None

    symptom_text = f"Query latency {baseline['latency_ms']:.0f}ms, full_scan={baseline['full_scan']}"
    incident_id = memory.store(
        symptom_text,
        root_cause=None,
        resolution_sql=None,
        latency_before_ms=baseline["latency_ms"],
        latency_after_ms=None,
        resolved=False,
    )

    similar = memory.search_similar(symptom_text, incident_id=incident_id)
    diagnosis = diagnostic.diagnose(baseline, similar, incident_id=incident_id)

    fix_applied = execution.apply_fix(diagnosis.get("proposed_fix_sql"), incident_id=incident_id)
    after = execution.benchmark(customer_id, since_date, incident_id=incident_id)

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
