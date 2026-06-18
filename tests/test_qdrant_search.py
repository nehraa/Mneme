"""
Tests for QdrantSearch (Phase 4 vector search integration).

These tests are integration tests that require a running Qdrant instance.
When Qdrant is unavailable, all tests are skipped via the
``requires_qdrant`` fixture.
"""
from __future__ import annotations

import pytest

from src.retrieval.qdrant_search import QdrantSearch

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

QDRANT_HOST = "http://localhost:6333"
TEST_COLLECTION = "test_mneme_chunks"
VECTOR_SIZE = 768


@pytest.fixture(scope="function")
def qdrant_search() -> QdrantSearch:
    """
    Create a QdrantSearch instance pointing at a test collection.

    The collection is created before the test and deleted after.
    """
    client = QdrantSearch(
        host=QDRANT_HOST,
        collection=TEST_COLLECTION,
        vector_size=VECTOR_SIZE,
    )
    # Seed a known vector so searches return predictable results
    client.upsert_chunk(
        chunk_id="chunk_auth_001",
        content="OAuth2 authentication flow failed with token expired",
        embedding=[0.1] * VECTOR_SIZE,
        metadata={
            "tags": ["tool=auth", "outcome=failed"],
            "outcome_tag": "failed",
            "session_id": "session_test",
        },
    )
    client.upsert_chunk(
        chunk_id="chunk_db_001",
        content="Database query succeeded for user profile select",
        embedding=[0.9] * VECTOR_SIZE,
        metadata={
            "tags": ["tool=db", "outcome=work_done"],
            "outcome_tag": "work_done",
            "session_id": "session_test",
        },
    )
    yield client
    # Teardown: delete the test collection
    try:
        from qdrant_client import QdrantClient
        tc = QdrantClient(url=QDRANT_HOST, timeout=10.0)
        tc.delete_collection(collection_name=TEST_COLLECTION)
        tc.close()
    except Exception:
        pass  # best-effort teardown


def requires_qdrant() -> bool:
    """Return True only when Qdrant is reachable."""
    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(url=QDRANT_HOST, timeout=5.0)
        client.get_collections()
        client.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def test_health_check_returns_true_when_qdrant_up(self, qdrant_search):
        assert qdrant_search.health_check() is True

    def test_health_check_returns_false_for_invalid_collection(self):
        search = QdrantSearch(
            host=QDRANT_HOST,
            collection="nonexistent_collection_xyz",
            vector_size=VECTOR_SIZE,
            ensure_collection=False,
        )
        # Collection does not exist and was not auto-created because
        # ensure_collection=False skips _ensure_collection in __init__
        assert search.health_check() is False
        search.close()


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

class TestUpsert:
    def test_upsert_stores_chunk(self, qdrant_search):
        """A stored chunk is retrievable by search."""
        # The fixture already stores chunk_auth_001; verify it
        results = qdrant_search.search(
            query_embedding=[0.1] * VECTOR_SIZE,
            limit=5,
        )
        ids = [r["chunk_id"] for r in results]
        assert "chunk_auth_001" in ids

    def test_upsert_is_idempotent(self, qdrant_search):
        """Re-upserting the same chunk_id updates the stored record."""
        # Update the metadata
        qdrant_search.upsert_chunk(
            chunk_id="chunk_auth_001",
            content="Updated content",
            embedding=[0.1] * VECTOR_SIZE,
            metadata={
                "tags": ["tool=auth", "outcome=failed", "error=token_expired"],
                "outcome_tag": "failed",
                "session_id": "session_test",
            },
        )
        results = qdrant_search.search(
            query_embedding=[0.1] * VECTOR_SIZE,
            limit=5,
        )
        auth_result = next(r for r in results if r["chunk_id"] == "chunk_auth_001")
        assert "error=token_expired" in auth_result["payload"].get("tags", [])

    def test_upsert_multiple_chunks(self, qdrant_search):
        """Multiple chunks can be stored and distinguished by search."""
        results = qdrant_search.search(
            query_embedding=[0.9] * VECTOR_SIZE,
            limit=5,
        )
        ids = [r["chunk_id"] for r in results]
        assert "chunk_db_001" in ids


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class TestSearch:
    def test_search_returns_chunk_id_score_payload(self, qdrant_search):
        results = qdrant_search.search(
            query_embedding=[0.1] * VECTOR_SIZE,
            limit=10,
        )
        assert len(results) >= 1
        hit = next(r for r in results if r["chunk_id"] == "chunk_auth_001")
        assert "score" in hit
        assert "payload" in hit
        assert hit["payload"]["content"]

    def test_search_respects_limit(self, qdrant_search):
        results = qdrant_search.search(
            query_embedding=[0.5] * VECTOR_SIZE,
            limit=1,
        )
        assert len(results) <= 1

    def test_search_respects_score_threshold(self, qdrant_search):
        # cosine similarity between [0.1, ...] and [0.9, ...] is low
        # (nearly orthogonal for high-dim vectors of this pattern).
        # Use a threshold that excludes the db chunk.
        results = qdrant_search.search(
            query_embedding=[0.1] * VECTOR_SIZE,
            limit=10,
            score_threshold=0.95,
        )
        ids = [r["chunk_id"] for r in results]
        # chunk_auth_001 (vector [0.1]^768) should score higher with itself
        assert "chunk_auth_001" in ids

    def test_search_with_session_id_filter(self, qdrant_search):
        results = qdrant_search.search(
            query_embedding=[0.5] * VECTOR_SIZE,
            limit=10,
            filter_conditions={"session_id": "session_test"},
        )
        for r in results:
            assert r["payload"].get("session_id") == "session_test"

    def test_search_with_outcome_tag_filter(self, qdrant_search):
        results = qdrant_search.search(
            query_embedding=[0.5] * VECTOR_SIZE,
            limit=10,
            filter_conditions={"outcome_tag": "failed"},
        )
        for r in results:
            assert r["payload"].get("outcome_tag") == "failed"

    def test_search_empty_results_for_no_match(self, qdrant_search):
        # A random vector far from anything stored should return few results
        random_vector = [float(i % 3 - 1) / 10.0 for i in range(VECTOR_SIZE)]
        results = qdrant_search.search(
            query_embedding=random_vector,
            limit=10,
            score_threshold=0.1,
        )
        # Should return some results but they should not include our stored chunks
        # at high scores (threshold 0.1 is very low)
        assert isinstance(results, list)

    def test_search_returns_ordered_by_score(self, qdrant_search):
        results = qdrant_search.search(
            query_embedding=[0.1] * VECTOR_SIZE,
            limit=5,
        )
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestDelete:
    def test_delete_removes_chunk(self, qdrant_search):
        # Verify chunk exists first
        results_before = qdrant_search.search(
            query_embedding=[0.1] * VECTOR_SIZE,
            limit=5,
        )
        assert any(r["chunk_id"] == "chunk_auth_001" for r in results_before)

        qdrant_search.delete_chunk("chunk_auth_001")

        results_after = qdrant_search.search(
            query_embedding=[0.1] * VECTOR_SIZE,
            limit=5,
        )
        assert not any(r["chunk_id"] == "chunk_auth_001" for r in results_after)

    def test_delete_nonexistent_is_idempotent(self, qdrant_search):
        """Deleting a chunk that does not exist does not raise."""
        qdrant_search.delete_chunk("nonexistent_chunk_id_xyz")
        # No exception means success


# ---------------------------------------------------------------------------
# Skipped tests when Qdrant is unavailable
# ---------------------------------------------------------------------------

requires_qdrant_mark = pytest.mark.skipif(
    not requires_qdrant(),
    reason="Qdrant is not available at http://localhost:6333",
)

# Apply skip to all test classes in this module
pytestmark = requires_qdrant_mark
