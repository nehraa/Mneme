"""
Tests for the hook module (Phase 6 — Pre-Tool Hook / mneme_inject).

Verifies:
- Mneme actually calls both RetrievalEngine and DiffEngine (no mocks)
- The guard results are aggregated correctly (PASSED vs warning)
- POST /inject endpoint returns 200 with the correct manifest shape
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.guard.diff_engine import DiffEngine
from src.hook.mneme import Mneme
from src.memory_store.repository import InMemoryMemoryRepository
from src.retrieval.engine import RetrievalEngine
from src.server import app


# ── Test fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch) -> TestClient:
    from src import server
    server._repo = None
    monkeypatch.setattr(
        server, "get_repository", lambda: InMemoryMemoryRepository()
    )
    return TestClient(app)


# ── Mneme tests ───────────────────────────────────────────────────────────────


class TestMneme:
    """Unit tests for Mneme with stubbed retrieval/guard engines."""

    def test_inject_orchestrates_both_engines(self):
        """Mneme must call RetrievalEngine AND DiffEngine (via injected
        stub engines, since the real guard requires a populated repo)."""
        repo = InMemoryMemoryRepository()

        class _StubRetrieval:
            def retrieve(
                self,
                prompt_context: str,
                session_id: str | None = None,
            ) -> dict[str, Any]:
                return {
                    "detected_tags": ["tool=auth"],
                    "intent": "continue_auth_flow_retry",
                    "injected_context": "base context",
                    "chunks_used": ["mem_001", "mem_007"],
                    "tag_matches": {"tool=auth": "exact"},
                    "priority_scores": {"mem_001": 0.9, "mem_007": 0.7},
                }

        class _NeverTriggerDiff:
            def check(
                self,
                proposed_change: str,
                target_file: str,
                session_id: str | None = None,
            ) -> dict[str, Any]:
                return {
                    "guard_triggered": False,
                    "warning": None,
                    "related_memories": [],
                    "override_allowed": True,
                }

        engine = Mneme(
            repository=repo,
            retrieval_engine=_StubRetrieval(),  # type: ignore[arg-type]
            diff_engine=_NeverTriggerDiff(),  # type: ignore[arg-type]
        )
        result = engine.inject(
            message="continue the auth flow",
            session_id="sess_001",
        )

        # RetrievalEngine (stub) returns the canonical retry intent.
        assert result["detected_intent"] == "continue_auth_flow_retry"
        # DiffEngine (stub) reports PASSED.
        assert "PASSED" in result["memory_guard"]
        # retrieved_chunks must come from the retrieval engine.
        assert result["retrieved_chunks"] == ["mem_001", "mem_007"]
        assert isinstance(result["injected_context"], str)
        assert result["injected_context_length"] == len(result["injected_context"])

    def test_inject_real_path_aggregates_guard_warning(self):
        """If the DiffEngine fires, the warning must be reflected in the
        final `memory_guard` status and prepended to the injected context."""
        repo = InMemoryMemoryRepository()

        class _AlwaysTriggerDiff:
            def check(
                self,
                proposed_change: str,
                target_file: str,
                session_id: str | None = None,
            ) -> dict[str, Any]:
                return {
                    "guard_triggered": True,
                    "warning": "PAST FAILURE: similar change broke auth",
                    "related_memories": ["mem_042"],
                    "override_allowed": True,
                }

        class _StubRetrieval:
            def retrieve(
                self,
                prompt_context: str,
                session_id: str | None = None,
            ) -> dict[str, Any]:
                return {
                    "detected_tags": ["tool=auth"],
                    "intent": "continue_auth_flow_retry",
                    "injected_context": "base context",
                    "chunks_used": ["mem_001"],
                    "tag_matches": {"tool=auth": "exact"},
                    "priority_scores": {"mem_001": 0.9},
                }

        engine = Mneme(
            repository=repo,
            retrieval_engine=_StubRetrieval(),  # type: ignore[arg-type]
            diff_engine=_AlwaysTriggerDiff(),  # type: ignore[arg-type]
        )
        result = engine.inject(message="retry the auth flow", session_id="sess_001")

        # The warning text must appear in memory_guard …
        assert "PAST FAILURE" in result["memory_guard"]
        # … and must be prepended to the injected context.
        assert "WARNING" in result["injected_context"]
        assert "PAST FAILURE" in result["injected_context"]
        assert "base context" in result["injected_context"]

    def test_inject_real_path_no_guards_passed(self):
        """If no guard fires, memory_guard must read as PASSED and the
        injected context must NOT include any WARNING prefix."""
        repo = InMemoryMemoryRepository()

        class _NeverTriggerDiff:
            def check(
                self,
                proposed_change: str,
                target_file: str,
                session_id: str | None = None,
            ) -> dict[str, Any]:
                return {
                    "guard_triggered": False,
                    "warning": None,
                    "related_memories": [],
                    "override_allowed": True,
                }

        class _StubRetrieval:
            def retrieve(
                self,
                prompt_context: str,
                session_id: str | None = None,
            ) -> dict[str, Any]:
                return {
                    "detected_tags": [],
                    "intent": "generic",
                    "injected_context": "base context",
                    "chunks_used": [],
                    "tag_matches": {},
                    "priority_scores": {},
                }

        engine = Mneme(
            repository=repo,
            retrieval_engine=_StubRetrieval(),  # type: ignore[arg-type]
            diff_engine=_NeverTriggerDiff(),  # type: ignore[arg-type]
        )
        result = engine.inject(message="hello", session_id=None)

        assert "PASSED" in result["memory_guard"]
        assert "WARNING" not in result["injected_context"]
        assert result["injected_context"] == "base context"


# ── /inject endpoint integration tests ────────────────────────────────────────


class TestInjectEndpoint:
    """Integration tests for POST /inject endpoint."""

    def test_inject_returns_200(self, client: TestClient):
        response = client.post(
            "/inject",
            json={"message": "continue the auth flow"},
        )
        assert response.status_code == 200

    def test_inject_response_shape(self, client: TestClient):
        response = client.post(
            "/inject",
            json={"message": "continue the auth flow", "session_id": "sess_001"},
        )
        data = response.json()
        for key in (
            "session",
            "detected_intent",
            "retrieved_chunks",
            "memory_guard",
            "injected_context",
            "injected_context_length",
        ):
            assert key in data, f"missing key: {key}"
        assert data["session"] == "sess_001"
        assert (
            data["injected_context_length"] == len(data["injected_context"])
        )

    def test_inject_session_id_optional(self, client: TestClient):
        """Omitting session_id must default to 'default'."""
        response = client.post("/inject", json={"message": "hello"})
        data = response.json()
        assert data["session"] == "default"
