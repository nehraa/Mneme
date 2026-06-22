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


class TestMiniMaxJSONParser:
    """Tests for `MiniMaxClient._parse_json_response` — the parser that recovers
    JSON from LLM responses that may include trailing prose, code fences, or
    truncated output.

    Phase 1B of the MNEME retrieval quality fix plan: add strategies for
    largest balanced block, first balanced block, and truncated-JSON repair.
    """

    PARSER = staticmethod(__import__(
        "src.ingestion.llm_client", fromlist=["MiniMaxClient"]
    ).MiniMaxClient._parse_json_response)

    # ── Strategy 1: existing strategies still work ──────────────────────────

    def test_direct_clean_json_parses(self):
        """Plain JSON should still parse on the first attempt (strategy 1)."""
        text = '{"chunks": [{"chunk_id": "c1", "content": "hi"}], "cross_chunk_relationships": []}'
        result = self.PARSER(text)
        assert result["chunks"][0]["chunk_id"] == "c1"

    def test_markdown_fenced_json_parses(self):
        """JSON inside ```json ... ``` should parse (strategy 2 — fences)."""
        text = '```json\n{"chunks": [], "cross_chunk_relationships": []}\n```'
        result = self.PARSER(text)
        assert result["chunks"] == []

    # ── Strategy A: largest balanced {...} block ────────────────────────────

    def test_trailing_prose_after_json(self):
        """Trailing prose after a complete JSON object should be ignored."""
        text = '{"chunks": [{"chunk_id": "c1"}], "cross_chunk_relationships": []}\n\nHope this helps!'
        result = self.PARSER(text)
        assert result["chunks"][0]["chunk_id"] == "c1"

    def test_leading_prose_before_json(self):
        """Prose before a JSON object should be ignored."""
        text = (
            "Sure, here is the JSON you requested:\n"
            "```json\n"
            '{"chunks": [{"chunk_id": "c9"}], "cross_chunk_relationships": []}\n'
            "```\n"
            "Let me know if you need anything else!"
        )
        result = self.PARSER(text)
        assert result["chunks"][0]["chunk_id"] == "c9"

    def test_largest_balanced_block_wins(self):
        """When two balanced objects exist, the larger one is preferred
        (most likely the full payload, not a small embedded example)."""
        small = '{"example": "tiny"}'
        big = (
            '{"chunks": [{"chunk_id": "c1", "content": "real chunk", '
            '"page_order": 0, "tags": [], "source_file": "x.py", '
            '"analysis_notes": "n"}], "cross_chunk_relationships": []}'
        )
        text = f"preamble {small} more text {big}"
        result = self.PARSER(text)
        assert "chunks" in result
        assert result["chunks"][0]["chunk_id"] == "c1"

    def test_nested_object_is_balanced(self):
        """A block with nested objects should be recognized as one balanced span."""
        text = (
            "Here you go:\n"
            '{"chunks": [{"chunk_id": "c1", "nested": {"a": 1}}], '
            '"cross_chunk_relationships": []}\n'
            "Done!"
        )
        result = self.PARSER(text)
        assert result["chunks"][0]["nested"]["a"] == 1

    # ── Strategy B: first balanced block ────────────────────────────────────

    def test_first_balanced_block_with_garbage_after(self):
        """If the first balanced block is itself parseable, use it (ignore later)."""
        text = (
            '{"chunks": [{"chunk_id": "c1"}], "cross_chunk_relationships": []}'
            "\n\nThen I wrote this explanation: "
            "{not actually json, broken"
        )
        result = self.PARSER(text)
        assert result["chunks"][0]["chunk_id"] == "c1"

    # ── Strategy C: repair truncated JSON ───────────────────────────────────

    def test_truncated_json_repaired(self):
        """Truncated JSON (open string, no closing braces) should be repaired."""
        text = '{"chunks": [{"content": "hello world", "chunk_id": "c1"'
        result = self.PARSER(text)
        assert result["chunks"][0]["chunk_id"] == "c1"
        assert result["chunks"][0]["content"] == "hello world"

    def test_truncated_json_repaired_with_open_string(self):
        """Open string inside an array should be closed before the array/object."""
        text = '{"chunks": [{"content": "unterminated string'
        result = self.PARSER(text)
        # Should at minimum yield a dict with `chunks` key
        assert "chunks" in result
        assert isinstance(result["chunks"], list)

    # ── Multiple JSON objects, only one complete ────────────────────────────

    def test_multiple_objects_prefers_complete(self):
        """Among mixed complete and incomplete objects, the complete one wins."""
        text = (
            "{ broken: [unterminated\n"
            '{"chunks": [{"chunk_id": "real"}], "cross_chunk_relationships": []}'
            "\nmore junk {also broken"
        )
        result = self.PARSER(text)
        assert result["chunks"][0]["chunk_id"] == "real"

    # ── Realistic failure-mode samples (5) ──────────────────────────────────

    def test_realistic_minimax_failure_1(self):
        """MiniMax sometimes wraps with ```json and adds a signature line."""
        text = (
            "```json\n"
            "{\n"
            '  "chunks": [\n'
            '    {"chunk_id": "c001", "content": "auth check", '
            '"tags": ["tool=auth", "outcome=work_done"], '
            '"page_order": 0, "source_file": "src/auth.py", '
            '"analysis_notes": "entry point"},\n'
            '    {"chunk_id": "c002", "content": "login flow"}\n'
            "  ],\n"
            '  "cross_chunk_relationships": []\n'
            "}\n"
            "```\n"
            "\n"
            "Let me know if you need any adjustments!"
        )
        result = self.PARSER(text)
        assert len(result["chunks"]) == 2
        assert result["chunks"][1]["chunk_id"] == "c002"

    def test_realistic_minimax_failure_2(self):
        """MiniMax wraps in prose then JSON, with no fences."""
        text = (
            "I'll analyze this code for you.\n\n"
            "Here is the chunking result:\n\n"
            '{"chunks": [{"chunk_id": "c1", "content": "main", '
            '"tags": ["outcome=work_done"], "page_order": 0, '
            '"source_file": "main.py", "analysis_notes": ""}], '
            '"cross_chunk_relationships": []}\n\n'
            "This should help you understand the code structure."
        )
        result = self.PARSER(text)
        assert result["chunks"][0]["chunk_id"] == "c1"

    def test_realistic_minimax_failure_3(self):
        """MiniMax truncates mid-output on long content."""
        text = (
            '{"chunks": ['
            '{"chunk_id": "c1", "content": "first chunk content", '
            '"tags": ["outcome=work_done"], "page_order": 0, '
            '"source_file": "big.py", "analysis_notes": "ok"},'
            '{"chunk_id": "c2", "content": "second chunk content"'
        )
        result = self.PARSER(text)
        assert "chunks" in result
        assert result["chunks"][0]["chunk_id"] == "c1"
        # The repaired parse may include c2 with empty content; ensure it's at least a dict
        assert isinstance(result["chunks"][1], dict)
        assert result["chunks"][1]["chunk_id"] == "c2"

    def test_realistic_minimax_failure_4(self):
        """MiniMax adds a conversational prefix without code fences."""
        text = (
            "Certainly! Here is my analysis:\n\n"
            '{"chunks": [{"chunk_id": "c1", "content": "x", '
            '"tags": [], "page_order": 0, "source_file": "f.py", '
            '"analysis_notes": ""}], "cross_chunk_relationships": []}'
        )
        result = self.PARSER(text)
        assert result["chunks"][0]["chunk_id"] == "c1"

    def test_realistic_minimax_failure_5(self):
        """MiniMax double-fences or has extra closing prose."""
        text = (
            "Output:\n"
            "```json\n"
            '{"chunks": [{"chunk_id": "c1", "content": "code"}], '
            '"cross_chunk_relationships": []}\n'
            "```\n"
            "\n"
            "Note: I excluded empty functions as instructed.\n"
            "```\n"
            "Hope this works for you!"
        )
        result = self.PARSER(text)
        assert result["chunks"][0]["chunk_id"] == "c1"

    # ── Negative case: truly garbage input raises ───────────────────────────

    def test_garbage_raises_value_error(self):
        """Input with no extractable JSON must raise ValueError."""
        with pytest.raises(ValueError):
            self.PARSER("I cannot help with that. Have a nice day.")
