"""Write-vs-read detection: reads get rolled back, writes get committed.
Pure function, no database needed."""
import pytest

from tools.crdb_client import _is_write


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT 1", False),
        ("select * from orders", False),
        ("  SELECT * FROM t", False),
        ("WITH x AS (SELECT 1) SELECT * FROM x", False),
        ("INSERT INTO t VALUES (1)", True),
        ("update t set a=1", True),
        ("DELETE FROM t", True),
        ("UPSERT INTO t VALUES (1)", True),
        ("INSERT INTO t (a) VALUES (1) RETURNING id", True),
    ],
)
def test_is_write(sql, expected):
    assert _is_write(sql) is expected
