"""Watches a canonical 'hot query' representative of real app traffic and
flags an incident when its latency crosses a threshold. In a fuller build
you'd pull this from crdb_internal.node_statement_statistics / a Prometheus
scrape instead of a single fixed query -- this keeps the demo deterministic
and easy to reproduce on stage.
"""
import os

from agents.base import Agent
from tools import crdb_client

HOT_QUERY = """
SELECT id, amount, status
FROM orders
WHERE customer_id = %s AND order_date > %s
ORDER BY order_date DESC
"""

LATENCY_THRESHOLD_MS = float(os.getenv("LATENCY_THRESHOLD_MS", "300"))
# Table whose statements we scan in crdb_internal to auto-find the hot query.
HOT_QUERY_TABLE = os.getenv("HOT_QUERY_TABLE", "orders")


class MonitorAgent(Agent):
    name = "monitor"

    def discover_hot_query(self, table=HOT_QUERY_TABLE, incident_id=None):
        """Read CockroachDB's own per-statement telemetry to identify the
        worst-performing query touching `table`, instead of assuming the
        hardcoded HOT_QUERY. This is a genuinely autonomous, CockroachDB-native
        signal (crdb_internal.node_statement_statistics). Best-effort: returns
        None and falls back to the built-in hot query if stats aren't available
        yet (fresh cluster) or the view isn't reachable."""
        try:
            rows = crdb_client.run_query(
                """SELECT key, count, service_lat_avg
                   FROM crdb_internal.node_statement_statistics
                   WHERE key ILIKE %s
                   ORDER BY service_lat_avg DESC
                   LIMIT 1""",
                (f"%{table}%",),
            )
        except Exception as e:  # view unavailable / permissions / serverless quirks
            self.log(incident_id, "hot-query discovery skipped",
                     f"crdb_internal unavailable, using built-in hot query: {e}")
            return None

        if not rows:
            self.log(incident_id, "hot-query discovery",
                     "no statement statistics yet; using the built-in hot query")
            return None

        top = rows[0]
        avg_ms = float(top.get("service_lat_avg") or 0) * 1000.0
        self.log(
            incident_id,
            "hot-query discovery",
            f"worst statement by avg latency: {top['key']} "
            f"({avg_ms:.1f} ms avg over {top['count']} execs)",
            data={"fingerprint": top["key"], "avg_ms": round(avg_ms, 1), "count": top["count"]},
        )
        return {"fingerprint": top["key"], "avg_ms": avg_ms, "count": top["count"]}

    def check(self, customer_id, since_date, incident_id=None):
        result = crdb_client.timed_query(HOT_QUERY, (customer_id, since_date))
        result["breached"] = result["latency_ms"] > LATENCY_THRESHOLD_MS
        self.log(
            incident_id,
            "latency check",
            f"{result['latency_ms']:.1f} ms (threshold {LATENCY_THRESHOLD_MS:.0f} ms), "
            f"full_scan={result['full_scan']}",
            data={
                "latency_ms": round(result["latency_ms"], 1),
                "threshold_ms": LATENCY_THRESHOLD_MS,
                "full_scan": result["full_scan"],
                "breached": result["breached"],
            },
        )
        return result
