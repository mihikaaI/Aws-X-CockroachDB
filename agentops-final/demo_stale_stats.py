"""Second incident type: STALE TABLE STATISTICS (fix = ANALYZE), to show the
system generalizes past the single missing-index scenario.

    python demo_stale_stats.py --customer-id <uuid>

Where demo_scenario.py reproduces a *missing index* (full table scan ->
CREATE INDEX), this reproduces a query that is slow because the optimizer is
working from stale row-count statistics even though an index exists -- so the
right fix is `ANALYZE orders`, not another index.

Reproduction note: CockroachDB collects table statistics automatically, so to
make staleness deterministic we (best-effort) disable automatic collection,
create the index, then bulk-insert a large skew so the stored stats badly
understate reality. If your cluster doesn't permit the cluster setting (e.g.
some serverless tiers), the script still runs and shows the ANALYZE fix path;
it just can't *guarantee* the optimizer misestimates.
"""
import argparse
import random
import uuid
from datetime import date, timedelta

from db.seed import apply_schema, seed_customers
from orchestrator import run_once
from tools import crdb_client

SKEW_ROWS = 100_000


def _try(sql, why):
    try:
        crdb_client.execute_ddl(sql)
        return True
    except Exception as e:
        print(f"  (skipped: {why}: {e})")
        return False


def setup():
    print("Applying schema + seeding customers...")
    apply_schema()
    customer_ids = seed_customers()

    print("Ensuring an index exists (so this is NOT a missing-index incident)...")
    _try(
        "CREATE INDEX IF NOT EXISTS idx_orders_customer_date "
        "ON orders (customer_id, order_date)",
        "index creation",
    )

    print("Best-effort: disabling automatic statistics collection...")
    _try(
        "SET CLUSTER SETTING sql.stats.automatic_collection.enabled = false",
        "needs admin; auto-stats may mask staleness",
    )

    print(f"Bulk-inserting {SKEW_ROWS:,} skewed rows so stored stats go stale...")
    start_date = date.today() - timedelta(days=365)
    hot_customer = customer_ids[0]
    batch = []
    for _ in range(SKEW_ROWS):
        odate = start_date + timedelta(days=random.randint(0, 365))
        amount = round(random.uniform(5, 500), 2)
        # Concentrate on one customer so per-customer estimates are very wrong.
        batch.append((hot_customer, odate, amount))
        if len(batch) >= 5000:
            crdb_client.run_batch(
                "INSERT INTO orders (customer_id, order_date, amount) VALUES %s", batch
            )
            batch = []
    if batch:
        crdb_client.run_batch(
            "INSERT INTO orders (customer_id, order_date, amount) VALUES %s", batch
        )
    return hot_customer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--customer-id", help="reuse an existing customer id; otherwise one is seeded")
    args = parser.parse_args()

    print("=== AgentOps Demo — stale statistics (fix = ANALYZE) ===")
    customer_id = args.customer_id or str(setup())

    print(f"\nRunning the incident pipeline for customer {customer_id}...\n")
    report = run_once(customer_id)

    if report is None:
        print(
            "Latency stayed under threshold -- the optimizer may already have "
            "fresh stats. Lower LATENCY_THRESHOLD_MS, raise SKEW_ROWS, or ensure "
            "auto-stats is disabled, then re-run."
        )
    else:
        print("=== Demo complete. Full trace is in the agent_trace table. ===")


if __name__ == "__main__":
    main()
