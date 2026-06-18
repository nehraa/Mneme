"""
Tests for the guard module (Phase 5 — Memory Guard).

Verifies:
- DiffEngine queries the real repository and filters by outcome_tag
- POST /guard endpoint returns 200 with the correct manifest shape
- Empty repository, no contradicting chunks, and non-failed chunks are
  all handled correctly
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.guard.diff_engine import DiffEngine
from src.memory_store.repository import InMemoryMemoryRepository
from src.server import app


# ── Test fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    """FastAPI test client for the /guard endpoint."""
    return TestClient(app)


def _seed_repo_with_failed_contradiction(repo: InMemoryMemoryRepository) -> str:
    """
    Seed an InMemoryMemoryRepository with a single failed chunk that has a
    'contradicts' edge pointing at the given target file.

    Returns the source chunk_id so tests can assert against it.
    """
    chunk_id = "mem_001"
    repo.create_chunk(
        {
            "chunk_id": chunk_id,
            "content": "tried JWT in auth/token.py and it failed",
            "source_file": "auth/token.py",
            "outcome_tag": "failed",
            "tags": ["outcome=failed", "tool=auth"],
            "session_id": "sess_001",
            "linked_chunks": [],
        }
    )
    # The edge needs a target — create a stub target so the edge is valid
    # even though the guard only consults the source side of the edge.
    repo.create_chunk(
        {
            "chunk_id": "mem_002",
            "content": "target chunk for the contradicts edge",
            "source_file": "auth/token.py",
            "outcome_tag": "work_done",
            "tags": [],
            "session_id": "sess_001",
            "linked_chunks": [],
        }
    )
    repo.create_edge(
        {
            "source_chunk_id": chunk_id,
            "target_chunk_id": "mem_002",
            "relationship_type": "contradicts",
        }
    )
    return chunk_id


# ── DiffEngine tests ──────────────────────────────────────────────────────────


class TestDiffEngine:
    """Unit tests for DiffEngine against a real in-memory repository."""

    def test_check_real_path_queries_repository(self):
        """DiffEngine must call repository.get_contradicting_chunks and
        surface the resulting chunks via the guard manifest."""
        repo = InMemoryMemoryRepository()
        _seed_repo_with_failed_contradiction(repo)

        engine = DiffEngine(repository=repo)
        # The proposed_change shares many words with the failed chunk,
        # so the Jaccard score should clear SIMILARITY_THRESHOLD (0.3).
        result = engine.check(
            proposed_change="I want to try JWT auth in auth/token.py again",
            target_file="auth/token.py",
            session_id="sess_001",
        )

        # The result must reflect a real lookup.
        assert result["guard_triggered"] is True
        assert "mem_001" in result["related_memories"]
        assert isinstance(result["warning"], str)
        assert result["override_allowed"] is True

    def test_check_real_path_filters_out_non_failed_chunks(self):
        """Real path must only consider chunks with outcome_tag == 'failed'."""
        repo = InMemoryMemoryRepository()
        # Seed a successful contradiction — this should NOT trigger the guard.
        repo.create_chunk(
            {
                "chunk_id": "mem_010",
                "content": "tried JWT in auth/token.py and it worked",
                "source_file": "auth/token.py",
                "outcome_tag": "work_done",
                "tags": [],
                "session_id": "sess_001",
                "linked_chunks": [],
            }
        )
        repo.create_chunk(
            {
                "chunk_id": "mem_011",
                "content": "stub target",
                "source_file": "auth/token.py",
                "outcome_tag": "work_done",
                "tags": [],
                "session_id": "sess_001",
                "linked_chunks": [],
            }
        )
        repo.create_edge(
            {
                "source_chunk_id": "mem_010",
                "target_chunk_id": "mem_011",
                "relationship_type": "contradicts",
            }
        )

        engine = DiffEngine(repository=repo)
        result = engine.check(
            proposed_change="retry JWT auth in auth/token.py",
            target_file="auth/token.py",
            session_id="sess_001",
        )

        # outcome_tag is 'work_done', not 'failed' — guard must NOT fire.
        assert result["guard_triggered"] is False
        assert result["related_memories"] == []

    def test_check_real_path_empty_proposed_change_does_not_trigger(self):
        """An empty proposed_change must not trigger the guard."""
        repo = InMemoryMemoryRepository()
        _seed_repo_with_failed_contradiction(repo)

        engine = DiffEngine(repository=repo)
        result = engine.check(
            proposed_change="",
            target_file="auth/token.py",
            session_id="sess_001",
        )
        assert result["guard_triggered"] is False
        assert result["warning"] is None

    def test_check_real_path_no_contradicting_chunks(self):
        """Empty repository → guard not triggered, warning is None."""
        repo = InMemoryMemoryRepository()
        engine = DiffEngine(repository=repo)
        result = engine.check(
            proposed_change="retry JWT auth",
            target_file="auth/token.py",
            session_id="sess_001",
        )
        assert result["guard_triggered"] is False
        assert result["related_memories"] == []
        assert result["warning"] is None


# ── /guard endpoint integration tests ────────────────────────────────────────


class TestGuardEndpoint:
    """Integration tests for POST /guard endpoint."""

    def test_guard_returns_200(self, client: TestClient):
        """Endpoint must respond 200 for a valid request."""
        response = client.post(
            "/guard",
            json={
                "proposed_change": "retry JWT auth",
                "target_file": "auth/token.py",
                "session_id": "sess_001",
            },
        )
        assert response.status_code == 200

    def test_guard_response_shape(self, client: TestClient):
        """Response must include the full manifest shape."""
        response = client.post(
            "/guard",
            json={
                "proposed_change": "retry JWT auth",
                "target_file": "auth/token.py",
                "session_id": None,
            },
        )
        data = response.json()
        for key in (
            "guard_triggered",
            "warning",
            "related_memories",
            "override_allowed",
            "_implementation_note",
        ):
            assert key in data, f"missing key: {key}"
        assert data["override_allowed"] is True

    def test_guard_accepts_session_id(self, client: TestClient):
        response = client.post(
            "/guard",
            json={
                "proposed_change": "retry JWT auth",
                "target_file": "auth/token.py",
                "session_id": "sessions/2024-03-10",
            },
        )
        assert response.status_code == 200
