"""One-command demo: seed (no index) -> detect -> diagnose -> fix -> report.

    python demo_scenario.py

This alone reproduces the workflow in the project brief without needing a
real ECS app or live traffic -- good for a reliable, repeatable judging demo.
Run load_generator.py + orchestrator.py --loop separately if you want the
'live dashboard watching traffic climb' version instead.
"""
from db.seed import seed_all
from orchestrator import run_once


def main():
    print("=== AgentOps Demo ===")
    print("1. Seeding database (orders table has NO index on customer_id/order_date)...")
    customer_id = seed_all()

    print(f"\n2. Running the incident pipeline for customer {customer_id}...\n")
    report = run_once(customer_id)

    if report is None:
        print(
            "Latency was already under threshold -- try raising SEED_NUM_ORDERS "
            "or lowering LATENCY_THRESHOLD_MS in .env."
        )
    else:
        print("=== Demo complete. Full trace is in the agent_trace table. ===")


if __name__ == "__main__":
    main()
