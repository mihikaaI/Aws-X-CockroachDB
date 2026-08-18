"""Second half of the self-improving-memory demo. Shows that if the exact
same incident recurs -- say the index fix gets rolled back, or the same
class of problem shows up in a different environment -- AgentOps recognizes
it instantly from vector memory and re-applies the known fix WITHOUT calling
the LLM again.

Run this *after* demo_scenario.py has already resolved one missing-index
incident. It does NOT reseed any data (so your table doesn't keep growing --
just re-running demo_scenario.py itself would do that, and wouldn't produce
a second incident anyway now that the real fix is already live). It just:

  1. Drops the composite index that demo_scenario.py applied, simulating the
     fix being lost.
  2. Re-runs the pipeline for the same customer.

Expect the agent_trace / dashboard to show "diagnosis (recalled) — LLM
skipped" this time, and the SQL applied should be near-instant since there's
no LLM call in the critical path.

Usage:
    python demo_recall.py <customer_id>

(use the customer_id printed by your last demo_scenario.py run)
"""
import sys

from tools import crdb_client
from orchestrator import run_once

INDEX_NAME = "idx_orders_customer_id_order_date"


def main():
    if len(sys.argv) < 2:
        print("Usage: python demo_recall.py <customer_id>")
        print("(use the customer_id printed by your last demo_scenario.py run)")
        sys.exit(1)
    customer_id = sys.argv[1]

    print(f"Dropping {INDEX_NAME} to simulate the fix recurring...")
    with crdb_client.get_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"DROP INDEX IF EXISTS {INDEX_NAME};")
    print("Index dropped.\n")

    print(f"Running the incident pipeline again for customer {customer_id}...\n")
    report = run_once(customer_id)
    if report is None:
        print("No incident detected this time -- the query may be fast for a different reason.")
    else:
        print("\n=== Recall demo complete. Check the trace above for 'diagnosis (recalled) — LLM skipped'. ===")


if __name__ == "__main__":
    main()
