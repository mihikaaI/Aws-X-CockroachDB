"""Seeds demo data: customers + a large orders table with NO helpful index,
plus one pre-loaded 'past incident' so vector memory has something to
recall from on the very first demo run (nice for showing off retrieval
even before AgentOps has resolved anything itself).
"""
import os
import random
import uuid
from datetime import date, timedelta

from tools import crdb_client

NUM_CUSTOMERS = 50
NUM_ORDERS = int(os.getenv("SEED_NUM_ORDERS", "200000"))


def apply_schema():
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path) as f:
        sql = f.read()
    with crdb_client.get_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)


def seed_customers():
    ids = [str(uuid.uuid4()) for _ in range(NUM_CUSTOMERS)]
    rows = [(cid, f"Customer {i}", random.choice(["APAC", "EU", "US"])) for i, cid in enumerate(ids)]
    crdb_client.run_batch(
        "INSERT INTO customers (id, name, region) VALUES %s ON CONFLICT DO NOTHING",
        rows,
    )
    return ids


def seed_orders(customer_ids, batch_size=5000):
    """Bulk-load orders with execute_values (batched multi-row INSERTs) instead
    of per-row executemany, which was one network round-trip per row -- minutes
    of wall time for 200k rows."""
    start_date = date.today() - timedelta(days=365)
    batch = []
    for _ in range(NUM_ORDERS):
        cid = random.choice(customer_ids)
        odate = start_date + timedelta(days=random.randint(0, 365))
        amount = round(random.uniform(5, 500), 2)
        batch.append((cid, odate, amount))
        if len(batch) >= batch_size:
            crdb_client.run_batch(
                "INSERT INTO orders (customer_id, order_date, amount) VALUES %s",
                batch,
            )
            batch = []
    if batch:
        crdb_client.run_batch(
            "INSERT INTO orders (customer_id, order_date, amount) VALUES %s",
            batch,
        )


def seed_memory():
    from agents.memory_agent import MemoryAgent

    MemoryAgent().store(
        symptom_text="Query latency 950ms, full_scan=True",
        root_cause="Missing composite index on orders(customer_id, order_date)",
        resolution_sql="CREATE INDEX idx_orders_customer_date ON orders (customer_id, order_date)",
        latency_before_ms=950,
        latency_after_ms=72,
        resolved=True,
    )


def seed_all():
    print("Applying schema...")
    apply_schema()
    print(f"Seeding {NUM_CUSTOMERS} customers...")
    customer_ids = seed_customers()
    print(f"Seeding {NUM_ORDERS} orders (no index on customer_id/order_date)...")
    seed_orders(customer_ids)
    print("Seeding one past incident into vector memory...")
    seed_memory()
    print("Seed complete.")
    return customer_ids[0]


if __name__ == "__main__":
    seed_all()
