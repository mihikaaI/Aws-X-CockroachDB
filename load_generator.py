"""Simulates a traffic spike against the demo 'orders' hot query, so the
missing-index incident can be reproduced on demand for a live demo instead
of waiting for real users to hit it.

Run alongside orchestrator.py --loop to show the full story:
    python load_generator.py --customer-id <uuid> &
    python orchestrator.py --customer-id <uuid> --loop
"""
import argparse
import threading
import time
from datetime import date, timedelta

from agents.monitor_agent import HOT_QUERY
from tools import crdb_client


def hammer(customer_id, since_date, duration_s, workers):
    stop_at = time.time() + duration_s

    def worker():
        while time.time() < stop_at:
            try:
                crdb_client.run_query(HOT_QUERY, (customer_id, since_date))
            except Exception as e:
                print(f"query error: {e}")

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--customer-id", required=True)
    parser.add_argument("--duration", type=int, default=30, help="seconds")
    parser.add_argument("--workers", type=int, default=20, help="concurrent query loops")
    args = parser.parse_args()

    since = date.today() - timedelta(days=90)
    print(f"Generating load: {args.workers} concurrent workers for {args.duration}s...")
    hammer(args.customer_id, since, args.duration, args.workers)
    print("Load generation done.")
