
from tools import crdb_client

with crdb_client.get_conn() as conn:
    conn.autocommit = True
    with conn.cursor() as cur:
        print("=== indexes on orders BEFORE reset ===")
        try:
            cur.execute("SHOW INDEXES FROM orders;")
            for row in cur.fetchall():
                print(row)
        except Exception as e:
            print(f"(couldn't read indexes: {e})")

        print("\n=== row counts BEFORE reset ===")
        try:
            cur.execute("SELECT count(*) FROM orders;")
            print("orders:", cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM customers;")
            print("customers:", cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM incidents;")
            print("incidents:", cur.fetchone()[0])
        except Exception as e:
            print(f"(couldn't read counts: {e})")

        print("\n=== dropping demo tables ===")
        for t in ("orders", "customers", "incidents", "agent_trace"):
            cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE;")
            print(f"dropped {t}")

        print("\n=== confirming nothing demo-related is left ===")
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public';")
        remaining = [r[0] for r in cur.fetchall()]
        print("remaining tables:", remaining or "(none)")
