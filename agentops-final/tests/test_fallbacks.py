"""Tests for the two 'best-effort, never crash' fallback paths: offline
embeddings (no sentence-transformers needed) and the ccloud CLI wrapper (no
ccloud binary needed). Both must degrade gracefully rather than raising.
"""
from tools import ccloud_client
from tools.embeddings import embed, EMBED_DIM


class TestOfflineEmbeddings:
    def test_embed_returns_correct_dimension(self):
        vec = embed("hot query performing a full table scan; missing secondary index")
        assert len(vec) == EMBED_DIM

    def test_embed_is_deterministic(self):
        text = "hot query slow despite no full table scan; likely stale table statistics"
        assert embed(text) == embed(text)

    def test_different_text_produces_different_vector(self):
        assert embed("incident class A") != embed("incident class B")


class TestCcloudFallback:
    def test_available_is_false_when_binary_missing(self, monkeypatch):
        monkeypatch.setattr(ccloud_client.shutil, "which", lambda name: None)
        assert ccloud_client.available() is False

    def test_cluster_info_returns_none_without_cluster_name(self, monkeypatch):
        monkeypatch.setattr(ccloud_client, "CCLOUD_CLUSTER_NAME", None)
        assert ccloud_client.cluster_info() is None

    def test_cluster_info_returns_none_when_ccloud_not_installed(self, monkeypatch):
        monkeypatch.setattr(ccloud_client.shutil, "which", lambda name: None)
        assert ccloud_client.cluster_info(cluster_name="whatever") is None

    def test_recent_audit_events_returns_none_when_ccloud_not_installed(self, monkeypatch):
        monkeypatch.setattr(ccloud_client.shutil, "which", lambda name: None)
        assert ccloud_client.recent_audit_events() is None
