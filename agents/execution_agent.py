"""Turns a diagnosis into action:
  1. Validates and applies the proposed CREATE INDEX (defense in depth --
     the LLM is already constrained by its system prompt, this is the
     independent server-side check).
  2. Re-runs the benchmark query to measure the improvement.
  3. If CPU is still high after the DB-level fix, scales the ECS service.
"""
import os
import re

from agents.base import Agent
from agents.monitor_agent import MonitorAgent
from tools import aws_client, crdb_client

SAFE_DDL = re.compile(r"^\s*CREATE\s+INDEX\b", re.IGNORECASE)

CPU_SCALE_THRESHOLD = float(os.getenv("CPU_SCALE_THRESHOLD", "75.0"))
ECS_CLUSTER = os.getenv("ECS_CLUSTER_NAME")
ECS_SERVICE = os.getenv("ECS_SERVICE_NAME")


class ExecutionAgent(Agent):
    name = "execution"

    def apply_fix(self, proposed_fix_sql, incident_id=None):
        if not proposed_fix_sql or not SAFE_DDL.match(proposed_fix_sql):
            self.log(
                incident_id,
                "fix rejected",
                f"not an allowed CREATE INDEX statement: {proposed_fix_sql!r}",
            )
            return False
        crdb_client.execute_ddl(proposed_fix_sql)
        self.log(incident_id, "fix applied", proposed_fix_sql)
        return True

    def benchmark(self, customer_id, since_date, incident_id=None):
        return MonitorAgent().check(customer_id, since_date, incident_id=incident_id)

    def maybe_scale(self, incident_id=None):
        if not ECS_CLUSTER or not ECS_SERVICE:
            self.log(
                incident_id,
                "scale skipped",
                "ECS_CLUSTER_NAME / ECS_SERVICE_NAME not configured",
            )
            return None

        cpu = aws_client.get_ecs_avg_cpu(ECS_CLUSTER, ECS_SERVICE)
        if cpu is None:
            self.log(incident_id, "scale check", "no CloudWatch datapoints yet")
            return None

        if cpu < CPU_SCALE_THRESHOLD:
            self.log(incident_id, "scale check", f"CPU {cpu:.1f}% under threshold, no action needed")
            return None

        current = aws_client.get_current_desired_count(ECS_CLUSTER, ECS_SERVICE) or 1
        new_count = current + 1
        aws_client.scale_ecs_service(ECS_CLUSTER, ECS_SERVICE, new_count)
        self.log(
            incident_id,
            "scaled ECS",
            f"CPU {cpu:.1f}% >= {CPU_SCALE_THRESHOLD:.0f}% threshold, desiredCount {current} -> {new_count}",
        )
        return new_count
