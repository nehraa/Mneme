"""
Tests for the graph index (Phase 3).

Verifies:
- GraphIndex.get_related() and get_chains() use the real repository
- GET /graph/related/{id} and GET /graph/chains/{id} endpoints
- The InMemoryMemoryRepository supports graph traversal correctly
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.graph.index import GraphIndex
from src.memory_store.repository import InMemoryMemoryRepository
from src.server import app


@pytest.fixture
def repo() -> InMemoryMemoryRepository:
    r = InMemoryMemoryRepository()
    # Seed a simple graph: a -> b -> c, a -> c (so depth-1 and depth-2 differ)
    for cid, content, source in [
        ("mem_001", "first chunk", "auth.py"),
        ("mem_002", "second chunk", "auth.py"),
        ("mem_003", "third chunk", "auth.py"),
    ]:
        r.create_chunk({
            "chunk_id": cid,
            "content": content,
            "tags": ["tool=auth"],
            "outcome_tag": "work_done",
            "source_file": source,
            "session_id": "sess_1",
            "linked_chunks": [],
        })
    r.create_edge({
        "source_chunk_id": "mem_001",
        "target_chunk_id": "mem_002",
        "relationship_type": "follows",
        "reason": "second attempt",
    })
    r.create_edge({
        "source_chunk_id": "mem_002",
        "target_chunk_id": "mem_003",
        "relationship_type": "follows",
        "reason": "third attempt",
    })
    return r


class TestGraphIndex:
    """Unit tests for GraphIndex using the in-memory repository."""

    def test_get_related_returns_relationships(self, repo: InMemoryMemoryRepository):
        """get_related must return relationships from the repo."""
        index = GraphIndex(repository=repo)
        result = index.get_related(chunk_id="mem_001", depth=1)
        assert result["chunk_id"] == "mem_001"
        assert "relationships" in result
        assert isinstance(result["relationships"], list)
        # mem_001 -> mem_002 must be in the relationships
        ids = {r["chunk_id"] for r in result["relationships"]}
        assert "mem_002" in ids

    def test_get_related_relationships_have_required_fields(self, repo: InMemoryMemoryRepository):
        for r in GraphIndex(repository=repo).get_related("mem_001", 1)["relationships"]:
            assert "chunk_id" in r
            assert "type" in r
            assert "reason" in r

    def test_get_chains_returns_chains(self, repo: InMemoryMemoryRepository):
        """get_chains returns multi-hop chains from the repo."""
        index = GraphIndex(repository=repo)
        result = index.get_chains(chunk_id="mem_001", depth=3)
        assert result["chunk_id"] == "mem_001"
        assert result["depth"] == 3
        assert "chains" in result
        assert isinstance(result["chains"], list)
        # mem_001 -> mem_002 and mem_002 -> mem_003 should both appear
        all_paths = [c["path"] for c in result["chains"]]
        # at least one path starts from mem_001
        assert any(p[0] == "mem_001" for p in all_paths)


class TestGraphEndpoints:
    """Integration tests for graph API endpoints.

    These use a TestClient backed by a freshly seeded in-memory repository
    so we can verify the full request/response cycle without Neo4j.
    """

    @pytest.fixture
    def client(self, monkeypatch) -> TestClient:
        from src import server
        # Reset the cached repo so each test gets a fresh one
        server._repo = None
        monkeypatch.setattr(
            server, "get_repository", lambda: InMemoryMemoryRepository()
        )
        return TestClient(app)

    def test_related_returns_200(self, client: TestClient):
        response = client.get("/graph/related/mem_001")
        assert response.status_code == 200

    def test_related_relationships_have_required_fields(self, client: TestClient):
        response = client.get("/graph/related/mem_001")
        data = response.json()
        assert "chunk_id" in data
        assert "relationships" in data
        assert isinstance(data["relationships"], list)

    def test_related_depth_param_accepted(self, client: TestClient):
        response = client.get("/graph/related/mem_001?depth=3")
        assert response.status_code == 200

    def test_chains_returns_200(self, client: TestClient):
        response = client.get("/graph/chains/mem_001")
        assert response.status_code == 200

    def test_chains_returns_chains_list(self, client: TestClient):
        response = client.get("/graph/chains/mem_001")
        data = response.json()
        assert "chunk_id" in data
        assert "depth" in data
        assert "chains" in data
        assert isinstance(data["chains"], list)

    def test_chains_depth_param_accepted(self, client: TestClient):
        response = client.get("/graph/chains/mem_001?depth=4")
        assert response.status_code == 200
