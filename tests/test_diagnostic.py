"""LLM response parsing and the memory-based fallback. No network / no DB."""
from agents.diagnostic_agent import DiagnosticAgent


def test_parse_strict_json():
    out = DiagnosticAgent._parse('{"root_cause":"x","confidence":0.9}')
    assert out["root_cause"] == "x"
    assert out["confidence"] == 0.9


def test_parse_strips_code_fence():
    out = DiagnosticAgent._parse('```json\n{"root_cause":"y"}\n```')
    assert out["root_cause"] == "y"


def test_parse_extracts_json_from_prose():
    raw = 'Sure! Here is the diagnosis: {"root_cause":"z","confidence":0.5} — hope it helps.'
    out = DiagnosticAgent._parse(raw)
    assert out["root_cause"] == "z"


def test_parse_unparseable_is_safe():
    out = DiagnosticAgent._parse("not json at all")
    assert out["confidence"] == 0.0
    assert out["proposed_fix_sql"] is None


def test_fallback_uses_top_memory():
    fb = DiagnosticAgent._fallback_from_memory(
        [{"root_cause": "missing idx", "resolution_sql": "CREATE INDEX ..."}], "timeout"
    )
    assert fb["source"] == "memory_fallback"
    assert fb["proposed_fix_sql"].startswith("CREATE INDEX")
    assert 0.0 < fb["confidence"] < 1.0


def test_fallback_without_memory_is_unavailable():
    fb = DiagnosticAgent._fallback_from_memory([], "boom")
    assert fb["source"] == "unavailable"
    assert fb["proposed_fix_sql"] is None
