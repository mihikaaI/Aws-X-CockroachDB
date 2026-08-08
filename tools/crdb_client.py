"""Thin CockroachDB helper on top of psycopg2 (CRDB is Postgres wire-compatible).

All other modules talk to the database through this file only, so if you ever
swap psycopg2 for asyncpg / sqlalchemy later, this is the one place to change.
"""
import os
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv("DATABASE_URL")


@contextmanager
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


def run_query(sql, params=None, fetch=True):
    """Run a query/statement. Returns list[dict] rows if fetch=True, else None."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            if fetch:
                rows = cur.fetchall()
                conn.commit()
                return rows
            conn.commit()
            return None


def execute_ddl(sql):
    """Run a DDL statement (CREATE INDEX, etc). Autocommits, no params -- DDL
    statements can't be parameterized in CockroachDB, so callers must only
    pass pre-validated, trusted SQL strings here (see agents/execution_agent.py)."""
    with get_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)


def timed_query(sql, params=None):
    """Run a query, measuring wall-clock latency, and separately capture its
    EXPLAIN ANALYZE plan so the diagnostic agent can see *why* it's slow
    (e.g. a full scan) rather than just *that* it's slow."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            start = time.perf_counter()
            cur.execute(sql, params or ())
            rows = cur.fetchall()
            latency_ms = (time.perf_counter() - start) * 1000

            cur.execute("EXPLAIN ANALYZE " + sql, params or ())
            plan_rows = cur.fetchall()
            conn.commit()

    plan_text = "\n".join(str(r.get("info", r)) for r in plan_rows)
    return {
        "latency_ms": latency_ms,
        "row_count": len(rows),
        "plan_text": plan_text,
        "full_scan": "full scan" in plan_text.lower(),
    }
