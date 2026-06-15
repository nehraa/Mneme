"""
Tests for the graph index (Phase 3).

Verifies:
- GraphIndex.get_related() produces correct manifest structure
- GraphIndex.get_chains() produces correct manifest structure
- MockGraphIndex returns deterministic mock data
- GET /graph/related/{id} and GET /graph/chains/{id} endpoints
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.graph.index import GraphIndex
from src.graph.mock_graph import MockGraphIndex
from src.server import app


class TestMockGraphIndex:
    """Unit tests for MockGraphIndex."""

    def test_get_related_returns_required_keys(self):
        """get_related must return _mock, chunk_id, relationships."""
        mock = MockGraphIndex()
        result = mock.get_related(chunk_id="mem_001", depth=1)

        assert result["_mock"] is True
        assert result["chunk_id"] == "mem_001"
        assert "relationships" in result
        assert isinstance(result["relationships"], list)

    def test_get_related_relationships_have_required_fields(self):
        """Each relationship must have chunk_id, type, reason."""
        mock = MockGraphIndex()
        result = mock.get_related(chunk_id="mem_001", depth=1)

        for rel in result["relationships"]:
            assert "chunk_id" in rel
            assert "type" in rel
            assert "reason" in rel

    def test_get_related_implementation_note_present(self):
        """_implementation_note must document the real path."""
        mock = MockGraphIndex()
        result = mock.get_related(chunk_id="mem_001", depth=1)
        assert "_implementation_note" in result
        assert "graph/index.py::GraphIndex.get_related()" in result["_implementation_note"]

    def test_get_chains_returns_required_keys(self):
        """get_chains must return _mock, chunk_id, depth, chains."""
        mock = MockGraphIndex()
        result = mock.get_chains(chunk_id="mem_001", depth=3)

        assert result["_mock"] is True
        assert result["chunk_id"] == "mem_001"
        assert result["depth"] == 3
        assert "chains" in result
        assert isinstance(result["chains"], list)

    def test_get_chains_paths_are_lists_of_chunk_ids(self):
        """Each chain path must be a list of chunk_id strings."""
        mock = MockGraphIndex()
        result = mock.get_chains(chunk_id="mem_001", depth=3)

        for chain in result["chains"]:
            assert isinstance(chain["path"], list)
            for item in chain["path"]:
                assert isinstance(item, str)
            assert "relationship_types" in chain
            assert "reason" in chain

    def test_get_chains_implementation_note_present(self):
        """_implementation_note must document the real path."""
        mock = MockGraphIndex()
        result = mock.get_chains(chunk_id="mem_001", depth=3)
        assert "_implementation_note" in result
        assert "graph/index.py::GraphIndex.get_chains()" in result["_implementation_note"]


class TestGraphIndex:
    """Unit tests for GraphIndex with use_mock=True."""

    def test_get_related_with_mock_returns_mock_result(self):
        """use_mock=True must delegate to MockGraphIndex."""
        index = GraphIndex(use_mock=True)
        result = index.get_related(chunk_id="mem_001", depth=1)
        assert result["_mock"] is True

    def test_get_related_passes_chunk_id_to_mock(self):
        """chunk_id must be forwarded to the mock."""
        index = GraphIndex(use_mock=True)
        result = index.get_related(chunk_id="mem_042", depth=2)
        assert result["chunk_id"] == "mem_042"

    def test_get_chains_with_mock_returns_mock_result(self):
        """use_mock=True must delegate to MockGraphIndex for chains too."""
        index = GraphIndex(use_mock=True)
        result = index.get_chains(chunk_id="mem_001", depth=3)
        assert result["_mock"] is True


class TestGraphEndpoints:
    """Integration tests for graph API endpoints."""

    @pytest.fixture
    def client(self) -> TestClient:
        return TestClient(app)

    def test_related_returns_200(self, client: TestClient):
        """GET /graph/related/{id} must respond 200."""
        response = client.get("/graph/related/mem_001")
        assert response.status_code == 200

    def test_related_returns_mock_manifest(self, client: TestClient):
        """Response must be a valid mock manifest with all required fields."""
        response = client.get("/graph/related/mem_001")
        data = response.json()

        assert data["_mock"] is True
        assert "chunk_id" in data
        assert "relationships" in data
        assert isinstance(data["relationships"], list)

    def test_related_depth_param_passed(self, client: TestClient):
        """depth query param must be accepted without error."""
        response = client.get("/graph/related/mem_001?depth=3")
        assert response.status_code == 200

    def test_related_relationships_have_required_fields(self, client: TestClient):
        """Each relationship in the response must have chunk_id, type, reason."""
        response = client.get("/graph/related/mem_001")
        data = response.json()

        for rel in data["relationships"]:
            assert "chunk_id" in rel
            assert "type" in rel
            assert "reason" in rel

    def test_chains_returns_200(self, client: TestClient):
        """GET /graph/chains/{id} must respond 200."""
        response = client.get("/graph/chains/mem_001")
        assert response.status_code == 200

    def test_chains_returns_mock_manifest(self, client: TestClient):
        """Response must be a valid mock manifest with all required fields."""
        response = client.get("/graph/chains/mem_001")
        data = response.json()

        assert data["_mock"] is True
        assert "chunk_id" in data
        assert "depth" in data
        assert "chains" in data
        assert isinstance(data["chains"], list)

    def test_chains_depth_param_passed(self, client: TestClient):
        """depth query param must be accepted without error."""
        response = client.get("/graph/chains/mem_001?depth=4")
        assert response.status_code == 200

    def test_chains_paths_are_list_of_strings(self, client: TestClient):
        """Each chain path must be a list of chunk_id strings."""
        response = client.get("/graph/chains/mem_001")
        data = response.json()

        for chain in data["chains"]:
            assert isinstance(chain["path"], list)
            for item in chain["path"]:
                assert isinstance(item, str)
