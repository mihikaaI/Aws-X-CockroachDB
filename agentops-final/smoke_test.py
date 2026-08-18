"""Offline smoke test: exercises the real orchestrator/agents code with the
DB, LLM, and AWS calls mocked out, so it can run with no live credentials at
all. Not a replacement for testing against a real cluster -- but it catches
interface mismatches (wrong method names, wrong argument counts, wrong return
types) that would otherwise only surface on stage. Every method call below
goes through the *actual* agents/orchestrator code, not a re-implementation.
"""
import json
import os
import sys
import uuid
from unittest import mock

os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:26257/agentops?sslmode=disable")
os.environ.setdefault("LLM_BACKEND", "gemini")
os.environ.setdefault("GEMINI_API_KEY", "fake")

sys.path.insert(0, os.path.dirname(__file__))

# ---- Fake "database" state -------------------------------------------------
INCIDENTS = {}   # id -> row dict, simulates the `incidents` table
TRACE_ROWS = []  # every agent_trace insert, so we can print the pipeline


def fake_run_query(sql, params=None, fetch=True, commit=None):
    s = sql.strip().lower()

    if s.startswith("insert into agent_trace"):
        incident_id, agent_name, step, detail = params
        TRACE_ROWS.append(
            {"incident_id": incident_id, "agent_name": agent_name, "step": step, "detail": detail}
        )
        return None

    if s.startswith("insert into incidents"):
        (symptom_text, embedding, root_cause, resolution_sql,
         latency_before_ms, latency_after_ms, resolved) = params
        new_id = str(uuid.uuid4())
        INCIDENTS[new_id] = {
            "id": new_id, "symptom_text": symptom_text, "root_cause": root_cause,
            "resolution_sql": resolution_sql, "latency_before_ms": latency_before_ms,
            "latency_after_ms": latency_after_ms, "resolved": resolved,
        }
        return [{"id": new_id}]

    if "select" in s and "from incidents" in s and "resolved = true" in s:
        symptom_text_for_query = _last_embedded_symptom[0]
        matches = [
            dict(row, distance=(0.0 if row["symptom_text"] == symptom_text_for_query else 0.9))
            for row in INCIDENTS.values() if row["resolved"]
        ]
        matches.sort(key=lambda r: r["distance"])
        return matches[: params[-1]] if matches else []

    if s.startswith("update incidents"):
        root_cause, resolution_sql, latency_after_ms, resolved, incident_id = params
        INCIDENTS[incident_id].update(
            root_cause=root_cause, resolution_sql=resolution_sql,
            latency_after_ms=latency_after_ms, resolved=resolved,
        )
        return None

    if "crdb_internal.node_statement_statistics" in s:
        # Simulate this telemetry table simply not being available -- exactly
        # the fallback path real serverless clusters may hit.
        raise RuntimeError("relation \"crdb_internal.node_statement_statistics\" does not exist (simulated)")

    raise AssertionError(f"fake_run_query got an unexpected statement: {sql[:80]!r}")


_last_embedded_symptom = [None]


def fake_embed(text):
    _last_embedded_symptom[0] = text
    return [0.0] * 384  # dimension only matters for real CRDB; mock doesn't care


LLM_CALLS = {"count": 0}


def fake_call_llm(system_prompt, user_prompt):
    LLM_CALLS["count"] += 1
    is_full_scan = "full table scan detected: True" in user_prompt
    fix = "CREATE INDEX idx_orders_customer_date ON orders (customer_id, order_date)" if is_full_scan else "ANALYZE orders"
    return json.dumps({
        "root_cause": "missing secondary index" if is_full_scan else "stale table statistics",
        "confidence": 0.92,
        "proposed_fix_sql": fix,
        "reasoning": "Simulated diagnosis for the offline smoke test.",
    })


def make_monitor_result(latency_ms, full_scan):
    return {"latency_ms": latency_ms, "row_count": 3, "plan_text": "fake plan",
            "full_scan": full_scan, "breached": latency_ms > 300}


def make_timed_query_side_effect(before_ms, after_ms, full_scan):
    """Simulates latency actually dropping once a fix is applied (execute_ddl
    called), so the improvement-ratio guardrail sees a real improvement
    instead of a flat mock value -- otherwise the rollback path always fires,
    which is correct guardrail behavior but not what this test is checking.
    """
    state = {"fix_applied": False}

    def _side_effect(sql, params=None):
        latency = after_ms if state["fix_applied"] else before_ms
        return make_monitor_result(latency, full_scan)

    return _side_effect, state


def run():
    with mock.patch("tools.crdb_client.run_query", side_effect=fake_run_query), \
         mock.patch("tools.crdb_client.execute_ddl") as mock_ddl, \
         mock.patch("tools.crdb_client.timed_query") as mock_timed, \
         mock.patch("tools.embeddings.embed", side_effect=fake_embed), \
         mock.patch("tools.llm.call_llm", side_effect=fake_call_llm):

        import orchestrator  # imported inside the patch context, after mocks are live

        customer_id = str(uuid.uuid4())

        print("=" * 70)
        print("RUN 1: fresh missing-index incident (expect: LLM called, fix applied)")
        print("=" * 70)
        timed_effect, timed_state = make_timed_query_side_effect(950.0, 60.0, full_scan=True)
        mock_timed.side_effect = timed_effect

        def ddl_side_effect(sql):
            if sql.strip().upper().startswith("CREATE INDEX"):
                timed_state["fix_applied"] = True
        mock_ddl.side_effect = ddl_side_effect

        report1 = orchestrator.run_once(customer_id, since_date=__import__("datetime").date.today())
        assert report1 is not None, "run_once returned None -- incident wasn't detected"
        assert LLM_CALLS["count"] == 1, f"expected 1 LLM call, got {LLM_CALLS['count']}"
        assert mock_ddl.called, "execute_ddl was never called -- fix wasn't applied"
        applied_sql = mock_ddl.call_args_list[0].args[0]
        assert applied_sql.strip().upper().startswith("CREATE INDEX"), applied_sql
        resolved_ids = [i for i, r in INCIDENTS.items() if r["resolved"]]
        assert resolved_ids, "incident was never marked resolved -- rollback guardrail fired unexpectedly"
        print("\n[PASS] Run 1: incident detected, LLM diagnosed it, CREATE INDEX applied and kept (real improvement).\n")

        print("=" * 70)
        print("RUN 2: identical symptom signature again (expect: short-circuit, LLM SKIPPED)")
        print("=" * 70)
        timed_effect2, timed_state2 = make_timed_query_side_effect(910.0, 55.0, full_scan=True)
        mock_timed.side_effect = timed_effect2
        mock_ddl.side_effect = ddl_side_effect
        report2 = orchestrator.run_once(customer_id, since_date=__import__("datetime").date.today())
        assert report2 is not None
        assert LLM_CALLS["count"] == 1, (
            f"expected LLM call count to STAY at 1 (short-circuit should skip it), "
            f"got {LLM_CALLS['count']} -- self-improving memory did not trigger"
        )
        recalled_steps = [r for r in TRACE_ROWS if r["step"] == "diagnosis" and "recalled" in (r["detail"] or "")]
        assert recalled_steps, "no 'diagnosis (recalled)' trace row found -- short-circuit path didn't log correctly"
        print("\n[PASS] Run 2: memory recall matched the resolved incident, LLM was skipped entirely.\n")

        print("=" * 70)
        print("RUN 3: stale-statistics incident, different customer (expect: ANALYZE fix family)")
        print("=" * 70)
        customer_id_2 = str(uuid.uuid4())
        timed_effect3, timed_state3 = make_timed_query_side_effect(450.0, 200.0, full_scan=False)
        mock_timed.side_effect = timed_effect3

        def ddl_side_effect3(sql):
            if sql.strip().upper().startswith("ANALYZE"):
                timed_state3["fix_applied"] = True
        mock_ddl.side_effect = ddl_side_effect3

        report3 = orchestrator.run_once(customer_id_2, since_date=__import__("datetime").date.today())
        assert report3 is not None
        analyze_calls = [c for c in mock_ddl.call_args_list if c.args[0].strip().upper().startswith("ANALYZE")]
        assert analyze_calls, "ANALYZE fix was never applied for the stale-stats incident class"
        print("\n[PASS] Run 3: stale-statistics incident correctly diagnosed and fixed with ANALYZE.\n")

        print("=" * 70)
        print(f"ALL SMOKE TESTS PASSED. {len(TRACE_ROWS)} trace rows written across 3 incidents.")
        print("=" * 70)


if __name__ == "__main__":
    run()
