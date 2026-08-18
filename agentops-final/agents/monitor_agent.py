"""Watches a canonical 'hot query' representative of real app traffic and
flags an incident when its latency crosses a threshold. In a fuller build
you'd pull this from crdb_internal.node_statement_statistics / a Prometheus
scrape instead of a single fixed query -- this keeps the demo deterministic
and easy to reproduce on stage.
"""
import os

from agents.base import Agent
from tools import ccloud_client, crdb_client

HOT_QUERY = """
SELECT id, amount, status
FROM orders
WHERE customer_id = %s AND order_date > %s
ORDER BY order_date DESC
"""

LATENCY_THRESHOLD_MS = float(os.getenv("LATENCY_THRESHOLD_MS", "300"))


class MonitorAgent(Agent):
    name = "monitor"

    def check(self, customer_id, since_date, incident_id=None):
        result = crdb_client.timed_query(HOT_QUERY, (customer_id, since_date))
        result["breached"] = result["latency_ms"] > LATENCY_THRESHOLD_MS
        self.log(
            incident_id,
            "latency check",
            f"{result['latency_ms']:.1f} ms (threshold {LATENCY_THRESHOLD_MS:.0f} ms), "
            f"full_scan={result['full_scan']}",
        )
        return result

    def discover_hot_query(self, incident_id=None):
        """Autonomous signal: instead of only trusting the hardcoded HOT_QUERY
        above, ask CockroachDB's own per-statement telemetry which statement
        touching `orders` is actually the worst performer right now.

        Best-effort by design: `crdb_internal.node_statement_statistics`'s
        exact JSON shape can vary by CockroachDB version, and some serverless
        tiers restrict access to it. Any failure here is caught and logged as
        a fallback -- it never breaks the pipeline, which keeps working off
        the built-in HOT_QUERY either way.
        """
        try:
            rows = crdb_client.run_query(
                """
                SELECT metadata->>'query' AS query,
                       (statistics->'statistics'->>'cnt')::INT AS exec_count,
                       (statistics->'statistics'->'runLat'->>'mean')::FLOAT8 AS mean_latency_s
                FROM crdb_internal.node_statement_statistics
                WHERE metadata->>'query' ILIKE '%orders%'
                ORDER BY (statistics->'statistics'->'runLat'->>'mean')::FLOAT8 DESC NULLS LAST
                LIMIT 1
                """
            )
        except Exception as e:
            self.log(
                incident_id,
                "hot-query discovery",
                f"crdb_internal telemetry unavailable, using built-in query ({e})",
            )
            return None

        if not rows:
            self.log(
                incident_id,
                "hot-query discovery",
                "crdb_internal returned no matching statements, using built-in query",
            )
            return None

        top = rows[0]
        mean_ms = (top.get("mean_latency_s") or 0) * 1000
        self.log(
            incident_id,
            "hot-query discovery",
            f"crdb_internal.node_statement_statistics reports the worst statement "
            f"touching orders: {mean_ms:.1f} ms mean, {top.get('exec_count')} executions",
        )
        return top

    def check_cluster_capacity(self, incident_id=None):
        """ccloud CLI: pull cluster-level context (plan/state/node count) so
        the diagnosis can tell an app-level problem (missing index, stale
        stats -- both fixable with SQL) apart from a cluster-level one
        (undersized plan -- not fixable by a schema change). Best-effort: if
        `ccloud` isn't installed or configured, this is skipped silently.
        """
        info = ccloud_client.cluster_info()
        if info is None:
            return None
        regions = info.get("regions") or [{}]
        node_count = regions[0].get("node_count")
        self.log(
            incident_id,
            "cluster capacity (ccloud)",
            f"plan={info.get('plan')} state={info.get('state')} "
            f"nodes={node_count}",
        )
        return info
