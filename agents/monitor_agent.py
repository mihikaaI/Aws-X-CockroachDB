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
