"""Turns a diagnosis into action:
  1. Validates and applies the proposed fix -- CREATE INDEX or ANALYZE only
     (defense in depth: the LLM is already constrained by its system prompt,
     this is the independent server-side check), gated by confidence and
     DRY_RUN.
  2. Re-runs the benchmark query to measure the improvement.
  3. Rolls back an index fix that didn't help (ANALYZE has nothing to undo).
  4. If CPU is still high after the DB-level fix, scales the ECS service.
"""
import os
import re

from agents.base import Agent
from agents.monitor_agent import MonitorAgent
from tools import aws_client, crdb_client

# Only these two statement shapes may ever be executed as a "fix", independent
# of whatever the LLM's system prompt says -- this is the server-side guard.
SAFE_CREATE_INDEX = re.compile(r"^\s*CREATE\s+INDEX\b", re.IGNORECASE)
SAFE_ANALYZE = re.compile(r"^\s*ANALYZE\s+[A-Za-z_][A-Za-z0-9_.\"]*\s*;?\s*$", re.IGNORECASE)
_INDEX_NAME_RE = re.compile(
    r"CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE
)


def _normalize_create_index(sql: str) -> str:
    """Ensure a CREATE INDEX statement has both an explicit name and
    IF NOT EXISTS. CockroachDB's grammar requires a name whenever
    IF NOT EXISTS is present -- `CREATE INDEX IF NOT EXISTS ON t (...)` with
    no name is a syntax error -- but `CREATE INDEX ON t (...)` (unnamed) is
    valid SQL on its own, and the LLM sometimes proposes exactly that. If we
    naively insert IF NOT EXISTS without checking for a name first, we
    produce a statement CockroachDB will reject. This parses the table and
    columns out and rebuilds the statement with a generated name whenever one
    is missing.
    """
    m = re.match(
        r"(?i)^\s*CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*\s+)?"
        r"ON\s+(?P<table>[A-Za-z_][A-Za-z0-9_.\"]*)\s*\((?P<cols>[^)]+)\)",
        sql,
    )
    if not m:
        # Unexpected shape -- leave it alone rather than guess; it'll fail
        # loudly at execute_ddl() if it's genuinely malformed, which is safer
        # than silently mangling SQL we don't understand.
        return sql

    table = m.group("table")
    cols = m.group("cols")
    name = m.group("name")
    if name:
        name = name.strip()
    else:
        col_slug = re.sub(r"[^A-Za-z0-9_]+", "_", cols).strip("_").lower()
        name = f"idx_{table}_{col_slug}"[:63]  # CockroachDB identifier length limit

    return f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})"

CPU_SCALE_THRESHOLD = float(os.getenv("CPU_SCALE_THRESHOLD", "75.0"))
ECS_CLUSTER = os.getenv("ECS_CLUSTER_NAME")
ECS_SERVICE = os.getenv("ECS_SERVICE_NAME")

AUTO_APPLY_MIN_CONFIDENCE = float(os.getenv("AUTO_APPLY_MIN_CONFIDENCE", "0.6"))
DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


class ExecutionAgent(Agent):
    name = "execution"

    def apply_fix(self, proposed_fix_sql, incident_id=None, confidence=None):
        """Returns a status string: "applied", "dry_run", "held_for_approval",
        "rejected", or "no_fix" -- orchestrator.py checks for "applied"."""
        if not proposed_fix_sql:
            self.log(incident_id, "fix skipped", "no fix proposed")
            return "no_fix"

        if not (SAFE_CREATE_INDEX.match(proposed_fix_sql) or SAFE_ANALYZE.match(proposed_fix_sql)):
            self.log(
                incident_id,
                "fix rejected",
                f"not an allowed CREATE INDEX / ANALYZE statement: {proposed_fix_sql!r}",
            )
            return "rejected"

        if confidence is not None and confidence < AUTO_APPLY_MIN_CONFIDENCE:
            self.log(
                incident_id,
                "fix held for approval",
                f"confidence {confidence:.2f} < {AUTO_APPLY_MIN_CONFIDENCE:.2f} threshold: {proposed_fix_sql}",
            )
            return "held_for_approval"

        if DRY_RUN:
            self.log(incident_id, "fix proposed (dry run)", proposed_fix_sql)
            return "dry_run"

        # Normalize to IF NOT EXISTS regardless of what the LLM (or a recalled
        # memory) proposed: a recalled fix on a repeat incident re-applies the
        # *same* index name against a schema that already has it (the table
        # isn't reset between demo runs), which would otherwise crash with a
        # duplicate-index error on a real cluster right at the "it remembers"
        # moment.
        safe_sql = proposed_fix_sql
        if SAFE_CREATE_INDEX.match(safe_sql):
            safe_sql = _normalize_create_index(safe_sql)

        crdb_client.execute_ddl(safe_sql)
        self.log(incident_id, "fix applied", safe_sql)
        return "applied"

    def rollback_fix(self, proposed_fix_sql, incident_id=None):
        """Undo an applied fix that didn't improve latency enough. Only
        CREATE INDEX fixes can be rolled back, via a controlled DROP INDEX
        built from the *parsed* index name -- never from raw model text.
        ANALYZE is idempotent maintenance with nothing to undo, so this is a
        no-op (returns False) for that fix family, matching the orchestrator's
        "ANALYZE has nothing to undo" comment.
        """
        if not proposed_fix_sql:
            return False
        match = _INDEX_NAME_RE.search(proposed_fix_sql)
        if not match:
            self.log(
                incident_id,
                "rollback skipped",
                "fix wasn't a CREATE INDEX (e.g. ANALYZE) -- nothing to undo",
            )
            return False
        index_name = match.group(1)
        crdb_client.execute_ddl(f"DROP INDEX IF EXISTS {index_name}")
        self.log(
            incident_id,
            "fix rolled back",
            f"DROP INDEX {index_name} (didn't meet MIN_IMPROVEMENT_RATIO)",
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
