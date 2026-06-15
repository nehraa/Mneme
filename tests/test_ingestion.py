"""
Tests for the ingestion pipeline (Phase 2).

Verifies:
- IngestionPipeline.run() produces correct manifest structure
- MockIngestionPipeline returns deterministic mock data
- Real pipeline integration path (use_mock=False) is documented
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.ingestion.mock_ingestion import MockIngestionPipeline
from src.ingestion.pipeline import IngestionPipeline
from src.server import app


class TestMockIngestionPipeline:
    """Unit tests for MockIngestionPipeline."""

    def test_run_returns_manifest_with_required_keys(self):
        """Manifest must have all fields the spec defines."""
        mock = MockIngestionPipeline()
        result = mock.run(
            file_paths=["src/**/*.py"],
            session_id="test-session-001",
            project_root="/test/project",
        )

        assert "_mock" in result
        assert result["_mock"] is True
        assert "chunks_created" in result
        assert "edges_created" in result
        assert "session_id" in result
        assert "files_processed" in result
        assert "tag_tree_summary" in result
        assert "chunks" in result

    def test_run_session_id_passed_through(self):
        """session_id from request must appear verbatim in manifest."""
        mock = MockIngestionPipeline()
        result = mock.run(
            file_paths=["src/**/*.py"],
            session_id="sessions/2024-03-15-auth-flow.md",
            project_root="/test/project",
        )
        assert result["session_id"] == "sessions/2024-03-15-auth-flow.md"

    def test_run_chunks_are_list_of_dicts(self):
        """chunks field must be a list of chunk dicts with required keys."""
        mock = MockIngestionPipeline()
        result = mock.run(
            file_paths=["src/**/*.py"],
            session_id="test-session",
            project_root="/test/project",
        )

        assert isinstance(result["chunks"], list)
        for chunk in result["chunks"]:
            assert isinstance(chunk, dict)
            assert "id" in chunk
            assert "content" in chunk
            assert "tags" in chunk
            assert "linked_chunks" in chunk
            assert "page_order" in chunk

    def test_run_tag_tree_has_expected_outcome_tags(self):
        """tag_tree_summary must include the spec-defined outcome counts."""
        mock = MockIngestionPipeline()
        result = mock.run(
            file_paths=["src/**/*.py"],
            session_id="test-session",
            project_root="/test/project",
        )

        tag_tree = result["tag_tree_summary"]
        assert "outcome" in tag_tree
        outcome = tag_tree["outcome"]
        assert outcome["work_done"] == 8
        assert outcome["failed"] == 7
        assert outcome["successfully_called"] == 39
        assert outcome["no_tool_called"] == 11
        assert outcome["stopped"] == 1

    def test_run_implementation_note_present(self):
        """_implementation_note must document the real path."""
        mock = MockIngestionPipeline()
        result = mock.run(
            file_paths=["src/**/*.py"],
            session_id="test-session",
            project_root="/test/project",
        )
        assert "_implementation_note" in result
        assert "ingestion/pipeline.py::IngestionPipeline.run()" in result["_implementation_note"]


class TestIngestionPipeline:
    """Unit tests for IngestionPipeline with use_mock=True."""

    def test_run_with_mock_returns_mock_result(self):
        """use_mock=True must delegate to MockIngestionPipeline."""
        pipeline = IngestionPipeline(use_mock=True)
        result = pipeline.run(
            file_paths=["src/**/*.py"],
            session_id="mock-session-001",
            project_root="/test/project",
        )
        assert result["_mock"] is True

    def test_run_passes_session_id_to_mock(self):
        """session_id must be forwarded to the mock pipeline."""
        pipeline = IngestionPipeline(use_mock=True)
        result = pipeline.run(
            file_paths=["src/**/*.py"],
            session_id="my-auth-session.md",
            project_root="/test/project",
        )
        assert result["session_id"] == "my-auth-session.md"


class TestIngestEndpoint:
    """Integration tests for POST /ingest endpoint."""

    @pytest.fixture
    def client(self) -> TestClient:
        return TestClient(app)

    def test_ingest_returns_200(self, client: TestClient):
        """Endpoint must respond 200 for valid request."""
        response = client.post("/ingest", json={"file_paths": ["src/**/*.py"]})
        assert response.status_code == 200

    def test_ingest_returns_mock_manifest(self, client: TestClient):
        """Response must be a valid mock manifest with all required fields."""
        response = client.post("/ingest", json={"file_paths": ["src/**/*.py"]})
        data = response.json()

        assert data["_mock"] is True
        assert "chunks_created" in data
        assert "edges_created" in data
        assert "tag_tree_summary" in data
        assert "chunks" in data
        assert isinstance(data["chunks"], list)

    def test_ingest_returns_200_on_empty_file_paths(self, client: TestClient):
        """Empty file_paths list must not 4xx — pipeline handles gracefully."""
        response = client.post("/ingest", json={"file_paths": []})
        assert response.status_code == 200
        data = response.json()
        assert data["_mock"] is True

    def test_ingest_session_id_from_first_file_path(self, client: TestClient):
        """session_id is derived from the first file path."""
        response = client.post("/ingest", json={"file_paths": ["src/models.py"]})
        data = response.json()
        assert data["session_id"] == "src/models.py"

    def test_ingest_tag_tree_summary_shape(self, client: TestClient):
        """tag_tree_summary must have nested tag counts."""
        response = client.post("/ingest", json={"file_paths": ["src/**/*.py"]})
        data = response.json()

        tag_tree = data["tag_tree_summary"]
        assert isinstance(tag_tree, dict)
        for tag_category, values in tag_tree.items():
            assert isinstance(values, dict)
            for value, count in values.items():
                assert isinstance(count, int)