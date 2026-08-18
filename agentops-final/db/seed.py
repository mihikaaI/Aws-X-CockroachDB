"""Seeds demo data: customers + a large orders table with NO helpful index,
plus one pre-loaded 'past incident' so vector memory has something to
recall from on the very first demo run (nice for showing off retrieval
even before AgentOps has resolved anything itself).
"""
import os
import random
import uuid
from datetime import date, timedelta

from psycopg2.extras import execute_values

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
    with crdb_client.get_conn() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO customers (id, name, region) VALUES %s ON CONFLICT DO NOTHING",
                rows,
            )
        conn.commit()
    return ids


def seed_orders(customer_ids, batch_size=5000):
    """Uses execute_values instead of executemany: executemany() silently
    sends one round-trip per row under the hood (a well-known psycopg2
    gotcha), which made seeding 2M rows take well over an hour. execute_values
    builds real multi-row INSERT statements, cutting network round-trips by
    ~batch_size and typically finishing in well under a minute even at 2M rows.
    """
    start_date = date.today() - timedelta(days=365)
    with crdb_client.get_conn() as conn:
        with conn.cursor() as cur:
            batch = []
            inserted = 0
            for _ in range(NUM_ORDERS):
                cid = random.choice(customer_ids)
                odate = start_date + timedelta(days=random.randint(0, 365))
                amount = round(random.uniform(5, 500), 2)
                batch.append((cid, odate, amount))
                if len(batch) >= batch_size:
                    execute_values(
                        cur,
                        "INSERT INTO orders (customer_id, order_date, amount) VALUES %s",
                        batch,
                        page_size=len(batch),
                    )
                    conn.commit()
                    inserted += len(batch)
                    print(f"  ...{inserted}/{NUM_ORDERS} orders inserted", flush=True)
                    batch = []
            if batch:
                execute_values(
                    cur,
                    "INSERT INTO orders (customer_id, order_date, amount) VALUES %s",
                    batch,
                    page_size=len(batch),
                )
                conn.commit()
                inserted += len(batch)
            print(f"  ...{inserted}/{NUM_ORDERS} orders inserted (done)", flush=True)


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
