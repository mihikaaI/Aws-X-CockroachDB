"""Long-term incident memory, backed by CockroachDB's native VECTOR column
and vector index (see db/schema.sql). This is what lets AgentOps say
'I've seen something like this before' instead of reasoning from scratch
every time.
"""
from agents.base import Agent
from tools import crdb_client
from tools.embeddings import embed


class MemoryAgent(Agent):
    name = "memory"

    def search_similar(self, symptom_text, k=3, incident_id=None):
        vec = embed(symptom_text)
        rows = crdb_client.run_query(
            """SELECT id, symptom_text, root_cause, resolution_sql,
                      latency_before_ms, latency_after_ms,
                      symptom_embedding <-> %s::VECTOR AS distance
               FROM incidents
               WHERE resolved = true
               ORDER BY symptom_embedding <-> %s::VECTOR
               LIMIT %s""",
            (str(vec), str(vec), k),
        )
        nearest = f", nearest distance {rows[0]['distance']:.3f}" if rows else ""
        self.log(
            incident_id,
            "recall",
            f"found {len(rows)} similar past incident(s) in memory{nearest}",
            data={"matches": len(rows), "nearest_distance": (rows[0]["distance"] if rows else None)},
        )
        return rows

    def store(
        self,
        symptom_text,
        root_cause,
        resolution_sql,
        latency_before_ms,
        latency_after_ms,
        resolved=False,
    ):
        vec = embed(symptom_text)
        rows = crdb_client.run_query(
            """INSERT INTO incidents
               (symptom_text, symptom_embedding, root_cause, resolution_sql,
                latency_before_ms, latency_after_ms, resolved)
               VALUES (%s, %s::VECTOR, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                symptom_text,
                str(vec),
                root_cause,
                resolution_sql,
                latency_before_ms,
                latency_after_ms,
                resolved,
            ),
        )
        return rows[0]["id"]

    def update_resolution(self, incident_id, root_cause, resolution_sql, latency_after_ms, resolved):
        crdb_client.run_query(
            """UPDATE incidents
               SET root_cause = %s, resolution_sql = %s,
                   latency_after_ms = %s, resolved = %s
               WHERE id = %s""",
            (root_cause, resolution_sql, latency_after_ms, resolved, incident_id),
            fetch=False,
        )
