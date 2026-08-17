"""Thin CockroachDB helper on top of psycopg2 (CRDB is Postgres wire-compatible).

All other modules talk to the database through this file only, so if you ever
swap psycopg2 for asyncpg / sqlalchemy later, this is the one place to change.

Connections come from a process-wide ``ThreadedConnectionPool`` rather than a
fresh ``connect()`` per call. That matters a lot here: ``load_generator.py``
runs 20 threads hammering the hot query in a tight loop, and every agent step
writes a trace row. Without pooling each of those was a full TCP+TLS+auth
handshake, which is a self-inflicted connection storm against the cluster
(and hits connection limits fast on CockroachDB Cloud serverless).
"""
import os
import time
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

from dotenv import load_dotenv

# Force load and override any existing environment cached state
load_dotenv(override=True)

DATABASE_URL = os.getenv("DATABASE_URL")

# Pool sizing: enough headroom for load_generator's workers + the agent
# pipeline running concurrently, but bounded so we never overwhelm the cluster.
POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
POOL_MAX = int(os.getenv("DB_POOL_MAX", "24"))

_pool = None
_pool_lock = threading.Lock()

# Statements that mutate state and therefore need a commit even when they also
# return rows (e.g. INSERT ... RETURNING). Everything else is treated as a read
# and rolled back, so we don't pay a write-commit round-trip on every SELECT.
_WRITE_PREFIXES = ("insert", "update", "delete", "upsert")


def _get_pool():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    
    _pool = psycopg2.pool.ThreadedConnectionPool(
        POOL_MIN, POOL_MAX, dsn=db_url
    )
    return _pool

def _is_write(sql: str) -> bool:
    lowered = sql.lstrip().lower()
    if lowered.startswith(_WRITE_PREFIXES):
        return True
    # WITH ... INSERT/UPDATE, or a trailing RETURNING on a read-shaped CTE.
    return "returning" in lowered


@contextmanager
def get_conn():
    """Borrow a connection from the pool and hand it back in a clean state.

    Callers should not commit/rollback themselves for the common paths; the
    higher-level helpers below do that. This context manager guarantees the
    connection is reset (autocommit off, no open transaction) before it goes
    back to the pool so the next borrower starts fresh.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        try:
            if conn.autocommit:
                conn.autocommit = False
            else:
                # End any transaction we may have left open.
                conn.rollback()
        except Exception:
            pass
        pool.putconn(conn)


def run_query(sql, params=None, fetch=True, commit=None):
    """Run a query/statement. Returns list[dict] rows if fetch=True, else None.

    ``commit`` defaults to auto-detecting writes from the statement, so plain
    SELECTs are rolled back (cheap, releases the txn) instead of committed.
    """
    should_commit = commit if commit is not None else (not fetch or _is_write(sql))
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall() if fetch else None
            if should_commit:
                conn.commit()
            else:
                conn.rollback()
            return rows


def run_batch(sql, rows, page_size=1000):
    """Bulk insert/update via psycopg2.extras.execute_values.

    ``sql`` must contain a single ``VALUES %s`` placeholder, e.g.
    ``INSERT INTO t (a, b) VALUES %s``. This replaces per-row ``executemany``
    (one network round-trip per row) with batched multi-row statements, which
    is 10-50x faster for seeding.
    """
    if not rows:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, rows, page_size=page_size)
        conn.commit()


def execute_ddl(sql):
    """Run a DDL statement (CREATE INDEX, etc). Autocommits, no params -- DDL
    statements can't be parameterized in CockroachDB, so callers must only
    pass pre-validated, trusted SQL strings here (see agents/execution_agent.py)."""
    with get_conn() as conn:
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
        finally:
            conn.autocommit = False


def timed_query(sql, params=None):
    """Run a query, measuring wall-clock latency, and separately capture its
    EXPLAIN ANALYZE plan so the diagnostic agent can see *why* it's slow
    (e.g. a full scan) rather than just *that* it's slow.

    The real query is what we time (that's the honest user-facing latency);
    EXPLAIN ANALYZE is run once more only to harvest the plan text.
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            start = time.perf_counter()
            cur.execute(sql, params or ())
            rows = cur.fetchall()
            latency_ms = (time.perf_counter() - start) * 1000

            cur.execute("EXPLAIN ANALYZE " + sql, params or ())
            plan_rows = cur.fetchall()
            conn.rollback()

    plan_text = "\n".join(str(r.get("info", r)) for r in plan_rows)
    return {
        "latency_ms": latency_ms,
        "row_count": len(rows),
        "plan_text": plan_text,
        "full_scan": "full scan" in plan_text.lower(),
    }
