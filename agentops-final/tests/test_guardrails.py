"""Guardrail tests: DDL statement safety, confidence gating, DRY_RUN,
rollback index-name parsing. No DB or network required -- crdb_client.execute_ddl
is mocked throughout.
"""
from unittest import mock

import pytest

from agents import execution_agent


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Every test gets a clean, explicit view of the guardrail thresholds
    rather than whatever happened to be in the environment."""
    monkeypatch.setattr(execution_agent, "AUTO_APPLY_MIN_CONFIDENCE", 0.6)
    monkeypatch.setattr(execution_agent, "DRY_RUN", False)


class TestSafeDDLPatterns:
    def test_create_index_matches(self):
        assert execution_agent.SAFE_CREATE_INDEX.match(
            "CREATE INDEX idx_orders_customer_date ON orders (customer_id, order_date)"
        )

    def test_create_index_if_not_exists_matches(self):
        assert execution_agent.SAFE_CREATE_INDEX.match(
            "CREATE INDEX IF NOT EXISTS idx_x ON orders (customer_id)"
        )

    def test_analyze_matches(self):
        assert execution_agent.SAFE_ANALYZE.match("ANALYZE orders")
        assert execution_agent.SAFE_ANALYZE.match("ANALYZE orders;")

    def test_drop_table_never_matches_either_pattern(self):
        assert not execution_agent.SAFE_CREATE_INDEX.match("DROP TABLE orders")
        assert not execution_agent.SAFE_ANALYZE.match("DROP TABLE orders")

    def test_delete_never_matches(self):
        stmt = "DELETE FROM orders WHERE 1=1"
        assert not execution_agent.SAFE_CREATE_INDEX.match(stmt)
        assert not execution_agent.SAFE_ANALYZE.match(stmt)

    def test_analyze_with_trailing_garbage_rejected(self):
        # Guards against prompt-injection-style payloads riding along on a
        # superficially-valid ANALYZE statement.
        assert not execution_agent.SAFE_ANALYZE.match("ANALYZE orders; DROP TABLE orders;")


class TestApplyFix:
    def test_rejects_unsafe_statement(self):
        agent = execution_agent.ExecutionAgent()
        with mock.patch.object(execution_agent, "crdb_client") as mock_db, \
             mock.patch.object(execution_agent.Agent, "log"):
            status = agent.apply_fix("DROP TABLE orders", incident_id=None, confidence=0.99)
        assert status == "rejected"
        mock_db.execute_ddl.assert_not_called()

    def test_no_fix_proposed(self):
        agent = execution_agent.ExecutionAgent()
        with mock.patch.object(execution_agent.Agent, "log"):
            status = agent.apply_fix(None, incident_id=None, confidence=0.9)
        assert status == "no_fix"

    def test_low_confidence_is_held_not_applied(self, monkeypatch):
        monkeypatch.setattr(execution_agent, "AUTO_APPLY_MIN_CONFIDENCE", 0.6)
        agent = execution_agent.ExecutionAgent()
        with mock.patch.object(execution_agent, "crdb_client") as mock_db, \
             mock.patch.object(execution_agent.Agent, "log"):
            status = agent.apply_fix("ANALYZE orders", incident_id=None, confidence=0.3)
        assert status == "held_for_approval"
        mock_db.execute_ddl.assert_not_called()

    def test_dry_run_never_executes_ddl(self, monkeypatch):
        monkeypatch.setattr(execution_agent, "DRY_RUN", True)
        agent = execution_agent.ExecutionAgent()
        with mock.patch.object(execution_agent, "crdb_client") as mock_db, \
             mock.patch.object(execution_agent.Agent, "log"):
            status = agent.apply_fix("ANALYZE orders", incident_id=None, confidence=0.99)
        assert status == "dry_run"
        mock_db.execute_ddl.assert_not_called()

    def test_applied_fix_normalizes_create_index_to_if_not_exists(self):
        """A recalled fix replays the same index name against a schema that
        already has it (table isn't reset between demo runs) -- this must
        not crash with a duplicate-index error."""
        agent = execution_agent.ExecutionAgent()
        with mock.patch.object(execution_agent, "crdb_client") as mock_db, \
             mock.patch.object(execution_agent.Agent, "log"):
            status = agent.apply_fix(
                "CREATE INDEX idx_x ON orders (customer_id)", incident_id=None, confidence=0.9
            )
        assert status == "applied"
        executed_sql = mock_db.execute_ddl.call_args.args[0]
        assert "IF NOT EXISTS" in executed_sql

    def test_analyze_fix_applies_unmodified(self):
        agent = execution_agent.ExecutionAgent()
        with mock.patch.object(execution_agent, "crdb_client") as mock_db, \
             mock.patch.object(execution_agent.Agent, "log"):
            status = agent.apply_fix("ANALYZE orders", incident_id=None, confidence=0.9)
        assert status == "applied"
        mock_db.execute_ddl.assert_called_once_with("ANALYZE orders")


class TestRollbackFix:
    def test_rolls_back_create_index_by_parsed_name(self):
        agent = execution_agent.ExecutionAgent()
        with mock.patch.object(execution_agent, "crdb_client") as mock_db, \
             mock.patch.object(execution_agent.Agent, "log"):
            rolled_back = agent.rollback_fix(
                "CREATE INDEX idx_orders_customer_date ON orders (customer_id, order_date)"
            )
        assert rolled_back is True
        mock_db.execute_ddl.assert_called_once_with("DROP INDEX IF EXISTS idx_orders_customer_date")

    def test_analyze_has_nothing_to_roll_back(self):
        agent = execution_agent.ExecutionAgent()
        with mock.patch.object(execution_agent, "crdb_client") as mock_db, \
             mock.patch.object(execution_agent.Agent, "log"):
            rolled_back = agent.rollback_fix("ANALYZE orders")
        assert rolled_back is False
        mock_db.execute_ddl.assert_not_called()

    def test_rollback_never_executes_raw_model_text(self):
        """Even a malicious/malformed fix string can only ever produce a
        DROP INDEX built from the *parsed* identifier, never the original text."""
        agent = execution_agent.ExecutionAgent()
        with mock.patch.object(execution_agent, "crdb_client") as mock_db, \
             mock.patch.object(execution_agent.Agent, "log"):
            agent.rollback_fix(
                "CREATE INDEX legit_name ON orders (customer_id); DROP TABLE customers;"
            )
        executed_sql = mock_db.execute_ddl.call_args.args[0]
        assert executed_sql == "DROP INDEX IF EXISTS legit_name"
