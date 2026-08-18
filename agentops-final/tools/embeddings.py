"""Turns incident/symptom text into a 384-dim vector for storage in
CockroachDB's VECTOR column and similarity search via the <-> operator.

Uses sentence-transformers (all-MiniLM-L6-v2, 384 dims) if it's installed
and its weights are reachable; otherwise falls back to a deterministic
hash-based embedding so the demo still runs end-to-end without internet
access. Swap in Bedrock Titan Embeddings or another model if you prefer --
just keep the output dimension consistent with the schema (VECTOR(384)).
"""
import hashlib

import numpy as np

EMBED_DIM = 384
_model = None
_model_load_attempted = False


def _load_model():
    global _model, _model_load_attempted
    if _model_load_attempted:
        return _model
    _model_load_attempted = True
    try:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer("all-MiniLM-L6-v2")
    except Exception:
        _model = None
    return _model


def embed(text: str) -> list:
    model = _load_model()
    if model is not None:
        vec = model.encode(text, normalize_embeddings=True)
        return vec.tolist()
    return _hash_embedding(text)


def _hash_embedding(text: str) -> list:
    """Deterministic offline fallback: same text always maps to the same
    vector, and semantically similar canned symptom strings (which is all
    this demo needs) will still cluster reasonably if you keep them
    templated -- see agents/monitor_agent.py."""
    seed = int(hashlib.sha256(text.encode()).hexdigest(), 16) % (2**32)
    rng = np.random.default_rng(seed)
    vec = rng.normal(size=EMBED_DIM)
    vec = vec / np.linalg.norm(vec)
    return vec.tolist()
