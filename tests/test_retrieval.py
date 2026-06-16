"""
Tests for the retrieval engine (Phase 4).

Verifies:
- RetrievalEngine.retrieve() produces correct manifest structure
- MockRetrievalEngine returns deterministic mock data
- IntentDetector detects intent and tags from prompts
- POST /retrieve endpoint returns correct structure
- Failed chunks rank higher than successful chunks (priority scoring)
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.retrieval.engine import RetrievalEngine
from src.retrieval.intent_detector import IntentDetector
from src.retrieval.mock_retrieval import MockRetrievalEngine
from src.server import app


class TestIntentDetector:
    """Unit tests for IntentDetector."""

    def test_detect_returns_required_keys(self):
        """detect() must return _mock, intent, detected_tags."""
        detector = IntentDetector()
        result = detector.detect("continue the auth flow from last time")

        assert "_mock" in result
        assert result["_mock"] is True
        assert "intent" in result
        assert "detected_tags" in result
        assert isinstance(result["detected_tags"], list)

    def test_detect_auth_keyword_tags_as_auth(self):
        """Prompt with 'auth' keyword must include tool=auth tag."""
        detector = IntentDetector()
        result = detector.detect("continue the auth flow")
        assert "tool=auth" in result["detected_tags"]

    def test_detect_failed_keyword_tags_as_failed(self):
        """Prompt with 'failed' keyword must include outcome=failed tag."""
        detector = IntentDetector()
        result = detector.detect("the auth flow failed with token expired")
        assert "outcome=failed" in result["detected_tags"]

    def test_detect_retry_keyword_sets_retry_intent(self):
        """Prompt with 'retry' keyword must set intent to retry_previous_attempt."""
        detector = IntentDetector()
        result = detector.detect("retry the auth flow")
        assert result["intent"] == "retry_previous_attempt"

    def test_detect_continue_keyword_sets_continue_intent(self):
        """Prompt with 'continue' keyword must set intent to continue_previous_work."""
        detector = IntentDetector()
        result = detector.detect("continue the auth flow")
        assert result["intent"] == "continue_previous_work"

    def test_detect_implementation_note_present(self):
        """_implementation_note must document the real Ollama path."""
        detector = IntentDetector()
        result = detector.detect("continue the auth flow")
        assert "_implementation_note" in result
        assert "IntentDetector" in result["_implementation_note"]


class TestMockRetrievalEngine:
    """Unit tests for MockRetrievalEngine."""

    def test_retrieve_returns_required_keys(self):
        """retrieve() must return all fields the spec defines."""
        mock = MockRetrievalEngine()
        result = mock.retrieve(
            prompt_context="continue the auth flow",
            session_id="test-session",
        )

        assert result["_mock"] is True
        assert "detected_tags" in result
        assert "intent" in result
        assert "injected_context" in result
        assert "chunks_used" in result
        assert "tag_matches" in result
        assert "priority_scores" in result

    def test_retrieve_injected_context_is_string(self):
        """injected_context must be a non-empty string."""
        mock = MockRetrievalEngine()
        result = mock.retrieve(prompt_context="continue", session_id=None)
        assert isinstance(result["injected_context"], str)
        assert len(result["injected_context"]) > 0

    def test_retrieve_priority_scores_have_failed_chunk_first(self):
        """priority_scores must have mem_001 (failed) ranked above mem_007."""
        mock = MockRetrievalEngine()
        result = mock.retrieve(prompt_context="continue", session_id=None)
        scores = result["priority_scores"]
        assert scores["mem_001"] > scores["mem_007"]

    def test_retrieve_tag_matches_has_exact_for_failed(self):
        """tag_matches must have outcome=failed as exact match."""
        mock = MockRetrievalEngine()
        result = mock.retrieve(prompt_context="continue", session_id=None)
        assert result["tag_matches"]["outcome=failed"] == "exact"


class TestRetrievalEngine:
    """Unit tests for RetrievalEngine with use_mock=True."""

    def test_retrieve_with_mock_returns_mock_result(self):
        """use_mock=True must delegate to MockRetrievalEngine."""
        engine = RetrievalEngine(use_mock=True)
        result = engine.retrieve(prompt_context="continue", session_id=None)
        assert result["_mock"] is True

    def test_retrieve_intent_detected(self):
        """retrieve() must return a detected intent from the mock."""
        engine = RetrievalEngine(use_mock=True)
        result = engine.retrieve(prompt_context="retry the auth flow", session_id=None)
        assert "intent" in result
        # Mock returns its fixed intent regardless of prompt
        assert result["intent"] == "continue_auth_flow_retry"


class TestRetrieveEndpoint:
    """Integration tests for POST /retrieve endpoint."""

    @pytest.fixture
    def client(self) -> TestClient:
        return TestClient(app)

    def test_retrieve_returns_200(self, client: TestClient):
        """Endpoint must respond 200 for valid request."""
        response = client.post(
            "/retrieve",
            json={"prompt_context": "continue the auth flow", "session_id": None},
        )
        assert response.status_code == 200

    def test_retrieve_returns_mock_manifest(self, client: TestClient):
        """Response must be a valid mock manifest with all required fields."""
        response = client.post(
            "/retrieve",
            json={"prompt_context": "continue the auth flow", "session_id": None},
        )
        data = response.json()

        assert data["_mock"] is True
        assert "detected_tags" in data
        assert "intent" in data
        assert "injected_context" in data
        assert "chunks_used" in data
        assert "tag_matches" in data
        assert "priority_scores" in data

    def test_retrieve_session_id_passed(self, client: TestClient):
        """session_id must be accepted without error."""
        response = client.post(
            "/retrieve",
            json={
                "prompt_context": "continue the auth flow",
                "session_id": "sessions/test.md",
            },
        )
        assert response.status_code == 200

    def test_retrieve_injected_context_is_string(self, client: TestClient):
        """injected_context must be a non-empty string."""
        response = client.post(
            "/retrieve",
            json={"prompt_context": "continue the auth flow", "session_id": None},
        )
        data = response.json()
        assert isinstance(data["injected_context"], str)
        assert len(data["injected_context"]) > 0

    def test_retrieve_priority_scores_are_floats(self, client: TestClient):
        """priority_scores values must be floats."""
        response = client.post(
            "/retrieve",
            json={"prompt_context": "continue the auth flow", "session_id": None},
        )
        data = response.json()
        for score in data["priority_scores"].values():
            assert isinstance(score, float)

    def test_retrieve_tag_matches_has_outcome_field(self, client: TestClient):
        """tag_matches must have an outcome key."""
        response = client.post(
            "/retrieve",
            json={"prompt_context": "continue the auth flow", "session_id": None},
        )
        data = response.json()
        assert "outcome" in str(data["tag_matches"])
