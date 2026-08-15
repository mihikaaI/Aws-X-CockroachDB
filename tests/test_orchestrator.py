"""Self-improving short-circuit decision + canonical symptom signature.
Pure functions -- no DB, no LLM."""
from orchestrator import should_short_circuit, symptom_signature


def test_symptom_signature_is_latency_independent():
    a = symptom_signature({"full_scan": True, "latency_ms": 934})
    b = symptom_signature({"full_scan": True, "latency_ms": 1200})
    assert a == b  # same class -> same string -> same embedding -> recall works


def test_symptom_signature_distinguishes_full_scan():
    assert symptom_signature({"full_scan": True}) != symptom_signature({"full_scan": False})


def test_short_circuit_on_near_identical_with_fix():
    top = {"distance": 0.0, "resolution_sql": "CREATE INDEX ..."}
    assert should_short_circuit(top, threshold=0.05) is True


def test_no_short_circuit_when_too_far():
    top = {"distance": 0.9, "resolution_sql": "CREATE INDEX ..."}
    assert should_short_circuit(top, threshold=0.05) is False


def test_no_short_circuit_without_fix():
    top = {"distance": 0.0, "resolution_sql": None}
    assert should_short_circuit(top, threshold=0.05) is False


def test_no_short_circuit_on_empty():
    assert should_short_circuit(None) is False
    assert should_short_circuit({"resolution_sql": "x"}) is False  # no distance
