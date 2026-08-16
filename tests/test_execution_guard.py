"""Guardrail logic: the CREATE INDEX allow-list and rollback target parsing.
No database needed -- these are pure functions."""
import pytest

from agents.execution_agent import (
    _fix_kind,
    _index_target,
    _is_safe_create_index,
    _is_safe_fix,
)


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("CREATE INDEX i ON orders (customer_id, order_date)", True),
        ("  create   index i ON orders (a)", True),
        ("CREATE UNIQUE INDEX i ON t (a)", True),
        ("CREATE INDEX i ON orders(a);", True),  # trailing ; is fine
        ("CREATE INDEX i ON orders(a) /* note */", True),
        # Injection: a second statement hides behind the first.
        ("CREATE INDEX i ON orders(a); DROP TABLE orders;", False),
        ("DROP TABLE orders", False),
        ("ALTER TABLE orders ADD COLUMN x INT", False),
        ("", False),
        (None, False),
    ],
)
def test_is_safe_create_index(sql, expected):
    assert _is_safe_create_index(sql) is expected


@pytest.mark.parametrize(
    "sql, kind",
    [
        ("CREATE INDEX i ON orders (a)", "index"),
        ("CREATE UNIQUE INDEX i ON t (a)", "index"),
        ("ANALYZE orders", "analyze"),
        ("  analyze   public.orders  ", "analyze"),
        ("ANALYZE orders;", "analyze"),
        ("CREATE STATISTICS s FROM orders", "stats"),
        # Not allowed:
        ("ANALYZE orders; DROP TABLE orders", None),  # injection
        ("VACUUM orders", None),
        ("DROP TABLE orders", None),
        ("ALTER TABLE orders ADD COLUMN x INT", None),
        ("", None),
        (None, None),
    ],
)
def test_fix_kind(sql, kind):
    assert _fix_kind(sql) == kind
    assert _is_safe_fix(sql) is (kind is not None)


def test_index_target_parses_name_and_table():
    assert _index_target("CREATE INDEX idx_o ON orders (customer_id, order_date)") == (
        "idx_o",
        "orders",
    )


def test_index_target_handles_if_not_exists_and_unique():
    assert _index_target("CREATE UNIQUE INDEX IF NOT EXISTS u_idx ON public.t (a)") == (
        "u_idx",
        "public.t",
    )


def test_index_target_none_on_garbage():
    assert _index_target("DROP TABLE orders") is None
    assert _index_target(None) is None


def test_index_target_none_for_analyze():
    # ANALYZE is not reversible -- rollback must find no index to drop.
    assert _index_target("ANALYZE orders") is None
