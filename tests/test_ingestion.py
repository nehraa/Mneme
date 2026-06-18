"""
Tests for the ingestion pipeline (Phase 2).

Verifies:
- IngestionPipeline.run() produces correct manifest structure
- The /ingest endpoint integrates the real pipeline (no mocks)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.ingestion.pipeline import IngestionPipeline
from src.memory_store.repository import InMemoryMemoryRepository
from src.server import app


class _FakeLLM:
    """Stub LLM client that returns a fixed chunk list.

    The pipeline calls MiniMaxClient.chunk_content() which makes real API
    calls; this stub stands in for the network boundary so unit tests stay
    deterministic and offline.
    """

    def __init__(self, chunks: list[dict], relationships: list[dict] | None = None) -> None:
        self._chunks = chunks
        self._relationships = relationships or []

    def chunk_content(self, content: str, file_path: str = ""):
        return SimpleNamespace(
            chunks=self._chunks,
            cross_chunk_relationships=self._relationships,
        )


class TestIngestionPipeline:
    """Unit tests for IngestionPipeline using the real in-memory repository
    and a stubbed LLM client (network boundary)."""

    def test_run_returns_manifest_with_required_keys(self, tmp_path):
        """Manifest must have all fields the spec defines."""
        sample = tmp_path / "src" / "auth.py"
        sample.parent.mkdir(parents=True)
        sample.write_text("def login(): return 'ok'")

        repo = InMemoryMemoryRepository()
        pipeline = IngestionPipeline(repository=repo)

        from src.ingestion import llm_client as llm_mod
        original = llm_mod.MiniMaxClient
        llm_mod.MiniMaxClient = lambda: _FakeLLM([
            {
                "chunk_id": "c1",
                "content": "login function returns 'ok'",
                "tags": ["tool=auth", "outcome=work_done"],
                "page_order": 0,
                "source_file": str(sample),
            }
        ])
        try:
            result = pipeline.run(
                file_paths=[str(sample)],
                session_id="test-session-001",
                project_root=str(tmp_path),
            )
        finally:
            llm_mod.MiniMaxClient = original

        assert "chunks_created" in result
        assert "edges_created" in result
        assert "session_id" in result
        assert result["session_id"] == "test-session-001"
        assert "files_processed" in result
        assert "tag_tree_summary" in result
        assert "chunks" in result

    def test_run_session_id_passed_through(self, tmp_path):
        """session_id must appear verbatim in the result."""
        sample = tmp_path / "x.py"
        sample.write_text("x = 1")

        repo = InMemoryMemoryRepository()
        pipeline = IngestionPipeline(repository=repo)

        from src.ingestion import llm_client as llm_mod
        original = llm_mod.MiniMaxClient
        llm_mod.MiniMaxClient = lambda: _FakeLLM([])
        try:
            result = pipeline.run(
                file_paths=[str(sample)],
                session_id="sessions/2024-03-15-auth-flow.md",
                project_root=str(tmp_path),
            )
        finally:
            llm_mod.MiniMaxClient = original

        assert result["session_id"] == "sessions/2024-03-15-auth-flow.md"

    def test_run_chunks_are_list_of_dicts(self, tmp_path):
        """chunks field must be a list of chunk dicts with required keys."""
        sample = tmp_path / "x.py"
        sample.write_text("x = 1")

        repo = InMemoryMemoryRepository()
        pipeline = IngestionPipeline(repository=repo)

        from src.ingestion import llm_client as llm_mod
        original = llm_mod.MiniMaxClient
        llm_mod.MiniMaxClient = lambda: _FakeLLM([
            {
                "chunk_id": "c1",
                "content": "test",
                "tags": ["tool=auth"],
                "page_order": 0,
                "source_file": str(sample),
            }
        ])
        try:
            result = pipeline.run(
                file_paths=[str(sample)],
                session_id="test-session",
                project_root=str(tmp_path),
            )
        finally:
            llm_mod.MiniMaxClient = original

        assert isinstance(result["chunks"], list)
        for chunk in result["chunks"]:
            assert isinstance(chunk, dict)
            assert "id" in chunk
            assert "tags" in chunk


class TestIngestEndpoint:
    """Integration tests for POST /ingest endpoint.

    These tests use the FastAPI test client against a real in-memory
    repository (overridden in the app state) so we don't need Neo4j.
    """

    @pytest.fixture
    def client(self, monkeypatch) -> TestClient:
        from src.memory_store import InMemoryMemoryRepository
        from src import server
        # Replace get_repository with one that returns the in-memory repo
        monkeypatch.setattr(
            server, "get_repository", lambda: InMemoryMemoryRepository()
        )
        return TestClient(app)

    def test_ingest_returns_200_for_empty_glob(self, client: TestClient):
        """Empty file_paths list must not 4xx — pipeline handles gracefully."""
        response = client.post("/ingest", json={"file_paths": []})
        assert response.status_code == 200
