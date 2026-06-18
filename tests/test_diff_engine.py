"""
Tests for DiffEngine — Phase 5 (Memory Guard) similarity backend.

Verifies:
- Jaccard fallback path produces correct similarity scores
- Semantic path is selected when both qdrant_search and embedding_service
  are wired in (and fails gracefully when only one is provided)
- The semantic backend raises → Jaccard fallback fires without crashing
- Public ``check()`` API contract: result shape, threshold logic,
  warning format, and empty-input handling
"""
from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.guard import diff_engine as diff_engine_module
from src.guard.diff_engine import (
    FAILED_OUTCOME,
    SIMILARITY_THRESHOLD,
    DiffEngine,
)
from src.memory_store.repository import InMemoryMemoryRepository


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def repo_with_failed_chunk() -> InMemoryMemoryRepository:
    """
    Seed a repo with one failed chunk that has a 'contradicts' edge
    pointing at auth/token.py.
    """
    repo = InMemoryMemoryRepository()
    repo.create_chunk(
        {
            "chunk_id": "mem_failed_jwt",
            "content": "tried JWT in auth/token.py and it failed",
            "source_file": "auth/token.py",
            "outcome_tag": FAILED_OUTCOME,
            "tags": ["outcome=failed", "tool=auth"],
            "session_id": "sess_001",
            "linked_chunks": [],
        }
    )
    repo.create_chunk(
        {
            "chunk_id": "mem_target",
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
            "source_chunk_id": "mem_failed_jwt",
            "target_chunk_id": "mem_target",
            "relationship_type": "contradicts",
        }
    )
    return repo


# ── Jaccard fallback tests ───────────────────────────────────────────────────


class TestJaccardFallback:
    """DiffEngine uses Jaccard when no semantic backend is configured."""

    def test_jaccard_triggers_guard_when_words_overlap(self, repo_with_failed_chunk):
        engine = DiffEngine(repository=repo_with_failed_chunk)
        result = engine.check(
            proposed_change="I want to try JWT auth in auth/token.py again",
            target_file="auth/token.py",
            session_id="sess_001",
        )
        assert result["guard_triggered"] is True
        assert "mem_failed_jwt" in result["related_memories"]
        assert "auth/token.py" in result["warning"]

    def test_jaccard_does_not_trigger_on_unrelated_text(self, repo_with_failed_chunk):
        engine = DiffEngine(repository=repo_with_failed_chunk)
        result = engine.check(
            proposed_change="rewrite the entire UI in React with TypeScript",
            target_file="auth/token.py",
            session_id="sess_001",
        )
        assert result["guard_triggered"] is False
        assert result["related_memories"] == []
        assert result["warning"] is None


# ── Semantic backend selection tests ─────────────────────────────────────────


class TestSemanticBackendSelection:
    """Verify the engine picks the semantic path only when BOTH dependencies
    are wired in, and falls back to Jaccard otherwise."""

    def test_qdrant_only_no_embedding_falls_back_to_jaccard(
        self, repo_with_failed_chunk, caplog
    ):
        """Qdrant without embedding service → Jaccard fallback (with debug log)."""
        mock_qdrant = MagicMock(name="QdrantSearch")
        engine = DiffEngine(repository=repo_with_failed_chunk, qdrant_search=mock_qdrant)

        with caplog.at_level(logging.DEBUG, logger=diff_engine_module.__name__):
            result = engine.check(
                proposed_change="retry JWT in auth/token.py",
                target_file="auth/token.py",
                session_id="sess_001",
            )

        # Qdrant was NOT called — without an embedding service the
        # semantic path cannot run, so Jaccard is used.
        mock_qdrant.search.assert_not_called()
        mock_qdrant.upsert_chunk.assert_not_called()

        # The Jaccard path correctly fires the guard for overlapping words.
        assert result["guard_triggered"] is True
        assert "Semantic similarity backend unavailable" in caplog.text

    def test_embedding_only_no_qdrant_falls_back_to_jaccard(
        self, repo_with_failed_chunk, caplog
    ):
        """Embedding service without Qdrant → Jaccard fallback."""
        mock_embedding = MagicMock(name="EmbeddingFn")
        engine = DiffEngine(
            repository=repo_with_failed_chunk, embedding_service=mock_embedding
        )

        with caplog.at_level(logging.DEBUG, logger=diff_engine_module.__name__):
            result = engine.check(
                proposed_change="retry JWT in auth/token.py",
                target_file="auth/token.py",
                session_id="sess_001",
            )

        mock_embedding.assert_not_called()
        assert result["guard_triggered"] is True
        assert "Semantic similarity backend unavailable" in caplog.text

    def test_both_qdrant_and_embedding_uses_semantic_path(
        self, repo_with_failed_chunk, monkeypatch
    ):
        """When BOTH are wired, semantic path is taken and Jaccard is bypassed."""
        captured: dict[str, Any] = {}

        def fake_cosine(a, b, embedding_service, qdrant_search):
            captured["called"] = True
            captured["a"] = a
            captured["b"] = b
            captured["embedding_service"] = embedding_service
            captured["qdrant_search"] = qdrant_search
            return 0.9  # above threshold → triggers guard

        monkeypatch.setattr(
            diff_engine_module,
            "_cosine_similarity_over_embeddings",
            fake_cosine,
        )

        mock_qdrant = MagicMock(name="QdrantSearch")
        mock_embedding = MagicMock(name="EmbeddingFn")

        engine = DiffEngine(
            repository=repo_with_failed_chunk,
            qdrant_search=mock_qdrant,
            embedding_service=mock_embedding,
        )

        result = engine.check(
            proposed_change="retry JWT in auth/token.py",
            target_file="auth/token.py",
            session_id="sess_001",
        )

        # Semantic helper was invoked with both dependencies.
        assert captured["called"] is True
        assert captured["embedding_service"] is mock_embedding
        assert captured["qdrant_search"] is mock_qdrant
        assert captured["a"] == "retry JWT in auth/token.py"
        assert captured["b"] == "tried JWT in auth/token.py and it failed"

        # Score 0.9 clears the threshold → guard fires.
        assert result["guard_triggered"] is True
        assert result["related_memories"] == ["mem_failed_jwt"]

    def test_semantic_backend_failure_falls_back_to_jaccard(
        self, repo_with_failed_chunk, caplog, monkeypatch
    ):
        """If the semantic helper raises, the engine falls back to Jaccard."""

        def boom(*_args, **_kwargs):
            raise RuntimeError("simulated Qdrant outage")

        monkeypatch.setattr(
            diff_engine_module,
            "_cosine_similarity_over_embeddings",
            boom,
        )

        mock_qdrant = MagicMock(name="QdrantSearch")
        mock_embedding = MagicMock(name="EmbeddingFn")

        engine = DiffEngine(
            repository=repo_with_failed_chunk,
            qdrant_search=mock_qdrant,
            embedding_service=mock_embedding,
        )

        with caplog.at_level(logging.WARNING, logger=diff_engine_module.__name__):
            result = engine.check(
                proposed_change="retry JWT in auth/token.py",
                target_file="auth/token.py",
                session_id="sess_001",
            )

        # Jaccard fallback fires the guard (overlapping words).
        assert result["guard_triggered"] is True
        assert "Semantic similarity backend failed" in caplog.text
        assert "simulated Qdrant outage" in caplog.text

    def test_below_threshold_does_not_trigger(self, repo_with_failed_chunk, monkeypatch):
        """When semantic score is below SIMILARITY_THRESHOLD, guard stays quiet."""
        monkeypatch.setattr(
            diff_engine_module,
            "_cosine_similarity_over_embeddings",
            lambda *a, **kw: 0.05,  # well below 0.3
        )

        engine = DiffEngine(
            repository=repo_with_failed_chunk,
            qdrant_search=MagicMock(),
            embedding_service=MagicMock(),
        )
        result = engine.check(
            proposed_change="retry JWT in auth/token.py",
            target_file="auth/token.py",
            session_id="sess_001",
        )
        assert result["guard_triggered"] is False
        assert result["related_memories"] == []


# ── Public API contract tests ────────────────────────────────────────────────


class TestCheckAPIContract:
    """Verify the public ``check()`` contract matches existing callers."""

    def test_empty_proposed_change_returns_clean_manifest(
        self, repo_with_failed_chunk
    ):
        engine = DiffEngine(repository=repo_with_failed_chunk)
        result = engine.check(
            proposed_change="",
            target_file="auth/token.py",
            session_id="sess_001",
        )
        for key in (
            "guard_triggered",
            "warning",
            "related_memories",
            "override_allowed",
            "_implementation_note",
        ):
            assert key in result
        assert result["guard_triggered"] is False
        assert result["warning"] is None
        assert result["related_memories"] == []
        assert result["override_allowed"] is True

    def test_whitespace_only_proposed_change_treated_as_empty(
        self, repo_with_failed_chunk
    ):
        engine = DiffEngine(repository=repo_with_failed_chunk)
        result = engine.check(
            proposed_change="   \n\t  ",
            target_file="auth/token.py",
            session_id="sess_001",
        )
        assert result["guard_triggered"] is False

    def test_no_contradicting_chunks_returns_clean_manifest(self):
        repo = InMemoryMemoryRepository()
        engine = DiffEngine(repository=repo)
        result = engine.check(
            proposed_change="any change at all",
            target_file="missing.py",
            session_id=None,
        )
        assert result["guard_triggered"] is False
        assert result["warning"] is None
        assert result["related_memories"] == []

    def test_warning_message_format_is_stable(self, repo_with_failed_chunk):
        engine = DiffEngine(repository=repo_with_failed_chunk)
        result = engine.check(
            proposed_change="retry JWT in auth/token.py",
            target_file="auth/token.py",
            session_id="sess_001",
        )
        # The exact warning format must remain stable for downstream
        # consumers (mneme_inject hook, dashboard, tests).
        assert result["warning"] == (
            "You tried a similar change in auth/token.py and it failed "
            "(chunk mem_failed_jwt, outcome=failed). "
            "Are you sure you want to retry?"
        )

    def test_threshold_constant_is_exported(self):
        assert SIMILARITY_THRESHOLD == 0.3


# ── Jaccard helper unit tests ────────────────────────────────────────────────


class TestJaccardHelper:
    """Direct coverage of the Jaccard helper (the fallback path)."""

    def test_identical_strings_yield_one(self):
        text = "the quick brown fox"
        score = diff_engine_module._jaccard_similarity(text, text)
        assert score == pytest.approx(1.0)

    def test_disjoint_strings_yield_zero(self):
        score = diff_engine_module._jaccard_similarity("apple banana", "cat dog")
        assert score == 0.0

    def test_partial_overlap_yields_fraction(self):
        score = diff_engine_module._jaccard_similarity("apple banana cherry", "banana cherry date")
        # intersection = {banana, cherry} (2), union = {apple, banana, cherry, date} (4)
        assert score == pytest.approx(0.5)

    def test_empty_input_yields_zero(self):
        assert diff_engine_module._jaccard_similarity("", "anything") == 0.0
        assert diff_engine_module._jaccard_similarity("anything", "") == 0.0

    def test_case_insensitive(self):
        assert diff_engine_module._jaccard_similarity("JWT Auth", "jwt auth") == pytest.approx(1.0)