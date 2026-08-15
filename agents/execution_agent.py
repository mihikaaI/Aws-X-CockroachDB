"""Turns a diagnosis into action, with guardrails:
  1. Validates and applies the proposed CREATE INDEX (defense in depth --
     the LLM is already constrained by its system prompt, this is the
     independent server-side check).
  2. Supports dry-run (propose only) and confidence-gated human approval.
  3. Re-runs the benchmark query to measure the improvement.
  4. Rolls the index back if it didn't actually help.
  5. If CPU is still high after the DB-level fix, scales the ECS service.
"""
import os
import re

from agents.base import Agent
from agents.monitor_agent import MonitorAgent
from tools import aws_client, crdb_client

SAFE_DDL = re.compile(r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\b", re.IGNORECASE)

# Pull the index name + target table out of a validated CREATE INDEX so we can
# build a *controlled* DROP INDEX for rollback (never from raw LLM text).
_INDEX_TARGET = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>[A-Za-z_][\w]*)\s+ON\s+(?P<table>[A-Za-z_][\w.]*)",
    re.IGNORECASE,
)

# Guardrails (all env-tunable, all no-AWS).
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
AUTO_APPLY_MIN_CONFIDENCE = float(os.getenv("AUTO_APPLY_MIN_CONFIDENCE", "0.6"))

CPU_SCALE_THRESHOLD = float(os.getenv("CPU_SCALE_THRESHOLD", "75.0"))
ECS_CLUSTER = os.getenv("ECS_CLUSTER_NAME")
ECS_SERVICE = os.getenv("ECS_SERVICE_NAME")


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


def _index_target(sql):
    """Return (index_name, table) from a CREATE INDEX statement, or None."""
    if not sql:
        return None
    m = _INDEX_TARGET.match(sql.strip())
    if not m:
        return None
    return m.group("name"), m.group("table")


class ExecutionAgent(Agent):
    name = "execution"

    def apply_fix(self, proposed_fix_sql, incident_id=None, confidence=None, dry_run=None):
        """Apply the proposed index, subject to guardrails. Returns a status
        string: 'applied' | 'dry_run' | 'held' | 'rejected'."""
        if not _is_safe_create_index(proposed_fix_sql):
            self.log(
                incident_id,
                "fix rejected",
                f"not an allowed single CREATE INDEX statement: {proposed_fix_sql!r}",
                data={"proposed_fix_sql": proposed_fix_sql, "status": "rejected"},
            )
            return "rejected"

        # Confidence gate: below the bar, propose but don't auto-apply.
        if confidence is not None and confidence < AUTO_APPLY_MIN_CONFIDENCE:
            self.log(
                incident_id,
                "fix held for approval",
                f"confidence {confidence:.2f} < {AUTO_APPLY_MIN_CONFIDENCE:.2f}; "
                f"proposing without applying: {proposed_fix_sql}",
                data={"proposed_fix_sql": proposed_fix_sql, "status": "held", "confidence": confidence},
            )
            return "held"

        dry = DRY_RUN if dry_run is None else dry_run
        if dry:
            self.log(
                incident_id,
                "fix dry-run",
                f"DRY_RUN set; would apply: {proposed_fix_sql}",
                data={"proposed_fix_sql": proposed_fix_sql, "status": "dry_run"},
            )
            return "dry_run"

        crdb_client.execute_ddl(proposed_fix_sql)
        self.log(
            incident_id,
            "fix applied",
            proposed_fix_sql,
            data={"proposed_fix_sql": proposed_fix_sql, "status": "applied"},
        )
        return "applied"

    def rollback_fix(self, proposed_fix_sql, incident_id=None):
        """Drop the index we created (controlled DROP built from the parsed
        name/table, never from raw model text) when the fix didn't help."""
        target = _index_target(proposed_fix_sql)
        if not target:
            self.log(incident_id, "rollback skipped", f"could not parse index from {proposed_fix_sql!r}")
            return False
        name, table = target
        drop_sql = f"DROP INDEX IF EXISTS {table}@{name}"
        crdb_client.execute_ddl(drop_sql)
        self.log(
            incident_id,
            "fix rolled back",
            f"{drop_sql} (fix did not improve latency)",
            data={"dropped_index": name, "table": table, "status": "rolled_back"},
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
