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

SAFE_DDL = re.compile(r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\b", re.IGNORECASE)


def _is_safe_create_index(sql):
    """Allow exactly one CREATE INDEX statement and nothing else.

    The system prompt already constrains the model to CREATE INDEX, but that's
    not a security boundary -- a poisoned memory row or an off-prompt model
    could return `CREATE INDEX ...; DROP TABLE orders;`. Because execute_ddl
    runs under autocommit (which executes multiple `;`-separated statements),
    we must reject anything with a second statement here, not just check that
    the string *starts* with CREATE INDEX."""
    if not sql:
        return False
    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:  # a second statement is hiding after the first
        return False
    return bool(SAFE_DDL.match(stripped))

CPU_SCALE_THRESHOLD = float(os.getenv("CPU_SCALE_THRESHOLD", "75.0"))
ECS_CLUSTER = os.getenv("ECS_CLUSTER_NAME")
ECS_SERVICE = os.getenv("ECS_SERVICE_NAME")


class ExecutionAgent(Agent):
    name = "execution"

    def apply_fix(self, proposed_fix_sql, incident_id=None):
        if not _is_safe_create_index(proposed_fix_sql):
            self.log(
                incident_id,
                "fix rejected",
                f"not an allowed single CREATE INDEX statement: {proposed_fix_sql!r}",
                data={"proposed_fix_sql": proposed_fix_sql, "applied": False},
            )
            return False
        crdb_client.execute_ddl(proposed_fix_sql)
        self.log(
            incident_id,
            "fix applied",
            proposed_fix_sql,
            data={"proposed_fix_sql": proposed_fix_sql, "applied": True},
        )
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
