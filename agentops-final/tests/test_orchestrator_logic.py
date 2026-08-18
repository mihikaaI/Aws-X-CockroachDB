import orchestrator
from agents.diagnostic_agent import DiagnosticAgent


class TestSymptomSignature:
    def test_full_scan_maps_to_missing_index_signature(self):
        sig = orchestrator.symptom_signature({"full_scan": True})
        assert "missing" in sig.lower() or "index" in sig.lower()

    def test_no_full_scan_maps_to_stale_stats_signature(self):
        sig = orchestrator.symptom_signature({"full_scan": False})
        assert "stale" in sig.lower() or "statistic" in sig.lower()

    def test_same_class_always_produces_identical_text(self):
        """This is what makes vector memory cluster reliably: same incident
        class -> same embedded text -> same (or near-identical) vector."""
        a = orchestrator.symptom_signature({"full_scan": True})
        b = orchestrator.symptom_signature({"full_scan": True})
        assert a == b


class TestShortCircuit:
    def test_no_top_incident_never_short_circuits(self):
        assert orchestrator.should_short_circuit(None) is False

    def test_close_match_with_resolution_short_circuits(self):
        top = {"distance": 0.01, "resolution_sql": "CREATE INDEX idx_x ON orders (customer_id)"}
        assert orchestrator.should_short_circuit(top, threshold=0.05) is True

    def test_far_match_does_not_short_circuit(self):
        top = {"distance": 0.9, "resolution_sql": "CREATE INDEX idx_x ON orders (customer_id)"}
        assert orchestrator.should_short_circuit(top, threshold=0.05) is False

    def test_missing_distance_field_does_not_crash_or_short_circuit(self):
        """Regression test for the bug where memory_agent.search_similar
        never selected `distance` at all, so this always silently returned
        False -- the self-improving short-circuit could never fire."""
        top = {"resolution_sql": "CREATE INDEX idx_x ON orders (customer_id)"}
        assert orchestrator.should_short_circuit(top) is False

    def test_close_match_without_a_resolution_does_not_short_circuit(self):
        # e.g. an incident that was recalled but never actually got fixed.
        top = {"distance": 0.0, "resolution_sql": None}
        assert orchestrator.should_short_circuit(top) is False


class TestDiagnosisParsing:
    def test_parses_clean_json(self):
        raw = '{"root_cause": "x", "confidence": 0.8, "proposed_fix_sql": "ANALYZE orders", "reasoning": "y"}'
        parsed = DiagnosticAgent._parse(raw)
        assert parsed["root_cause"] == "x"
        assert parsed["confidence"] == 0.8

    def test_strips_markdown_fences(self):
        raw = '```json\n{"root_cause": "x", "confidence": 0.5, "proposed_fix_sql": null, "reasoning": "y"}\n```'
        parsed = DiagnosticAgent._parse(raw)
        assert parsed["root_cause"] == "x"
        assert parsed["proposed_fix_sql"] is None

    def test_unparseable_response_falls_back_gracefully(self):
        raw = "the model rambled instead of returning JSON"
        parsed = DiagnosticAgent._parse(raw)
        assert parsed["confidence"] == 0.0
        assert parsed["proposed_fix_sql"] is None
        assert "unparseable" in parsed["root_cause"].lower()
