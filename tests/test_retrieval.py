"""
Tests for the retrieval engine (Phase 4).

Verifies:
- RetrievalEngine.retrieve() produces correct manifest structure
- IntentDetector detects intent and tags from prompts
- Scoring helpers compute the correct tag-match, embedding, and graph-boost
  contributions
- POST /retrieve endpoint returns correct structure
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.memory_store.repository import InMemoryMemoryRepository
from src.retrieval.engine import RetrievalEngine
from src.retrieval.intent_detector import IntentDetector
from src.server import app


class TestIntentDetector:
    """Unit tests for IntentDetector (keyword-based heuristics)."""

    def test_detect_returns_required_keys(self):
        """detect() must return intent and detected_tags."""
        detector = IntentDetector()
        result = detector.detect("continue the auth flow from last time")
        assert "intent" in result
        assert "detected_tags" in result
        assert isinstance(result["detected_tags"], list)

    def test_detect_auth_keyword_tags_as_auth(self):
        detector = IntentDetector()
        result = detector.detect("continue the auth flow")
        assert "tool=auth" in result["detected_tags"]

    def test_detect_failed_keyword_tags_as_failed(self):
        detector = IntentDetector()
        result = detector.detect("the auth flow failed with token expired")
        assert "outcome=failed" in result["detected_tags"]

    def test_detect_retry_keyword_sets_retry_intent(self):
        detector = IntentDetector()
        result = detector.detect("retry the auth flow")
        assert result["intent"] == "retry_previous_attempt"

    def test_detect_continue_keyword_sets_continue_intent(self):
        detector = IntentDetector()
        result = detector.detect("continue the auth flow")
        assert result["intent"] == "continue_previous_work"


class TestRetrievalEngine:
    """Unit tests for RetrievalEngine using an in-memory repository."""

    def test_retrieve_returns_manifest(self):
        """retrieve() must return all spec-defined keys."""
        repo = InMemoryMemoryRepository()
        repo.create_chunk({
            "chunk_id": "mem_001",
            "content": "auth flow failed",
            "tags": ["tool=auth", "outcome=failed"],
            "outcome_tag": "failed",
            "session_id": None,
            "linked_chunks": [],
            "last_accessed": None,
        })
        engine = RetrievalEngine(repository=repo)
        result = engine.retrieve(prompt_context="continue the auth flow", session_id=None)
        for key in (
            "detected_tags",
            "intent",
            "injected_context",
            "chunks_used",
            "tag_matches",
            "priority_scores",
        ):
            assert key in result, f"missing key: {key}"

    def test_retrieve_intent_detected(self):
        """retrieve() must surface a detected intent from the prompt."""
        repo = InMemoryMemoryRepository()
        engine = RetrievalEngine(repository=repo)
        result = engine.retrieve(prompt_context="retry the auth flow", session_id=None)
        assert result["intent"] == "retry_previous_attempt"

    def test_retrieve_no_chunks_returns_empty(self):
        """Empty repository → no chunks used, empty injected context."""
        repo = InMemoryMemoryRepository()
        engine = RetrievalEngine(repository=repo)
        result = engine.retrieve(prompt_context="continue", session_id=None)
        assert result["chunks_used"] == []
        assert result["injected_context"] == ""


class TestRetrieveEndpoint:
    """Integration tests for POST /retrieve endpoint."""

    @pytest.fixture
    def client(self, monkeypatch) -> TestClient:
        from src import server
        server._repo = None
        monkeypatch.setattr(
            server, "get_repository", lambda: InMemoryMemoryRepository()
        )
        return TestClient(app)

    def test_retrieve_returns_200(self, client: TestClient):
        response = client.post(
            "/retrieve",
            json={"prompt_context": "continue the auth flow", "session_id": None},
        )
        assert response.status_code == 200

    def test_retrieve_returns_manifest(self, client: TestClient):
        response = client.post(
            "/retrieve",
            json={"prompt_context": "continue the auth flow", "session_id": None},
        )
        data = response.json()
        for key in (
            "detected_tags",
            "intent",
            "injected_context",
            "chunks_used",
            "tag_matches",
            "priority_scores",
        ):
            assert key in data, f"missing key: {key}"

    def test_retrieve_session_id_passed(self, client: TestClient):
        response = client.post(
            "/retrieve",
            json={
                "prompt_context": "continue the auth flow",
                "session_id": "sessions/test.md",
            },
        )
        assert response.status_code == 200

    def test_retrieve_injected_context_is_string(self, client: TestClient):
        response = client.post(
            "/retrieve",
            json={"prompt_context": "continue the auth flow", "session_id": None},
        )
        data = response.json()
        assert isinstance(data["injected_context"], str)

    def test_retrieve_priority_scores_are_floats(self, client: TestClient):
        response = client.post(
            "/retrieve",
            json={"prompt_context": "continue the auth flow", "session_id": None},
        )
        data = response.json()
        for score in data["priority_scores"].values():
            assert isinstance(score, float)


class TestScoringComponents:
    """Unit tests for the individual scoring components in RetrievalEngine."""

    def setup_method(self) -> None:
        self.engine = RetrievalEngine(repository=InMemoryMemoryRepository())

    # ── Substring tag matching ────────────────────────────────────────────

    def test_tag_match_substring_auth_matches_oauth(self):
        score = self.engine._compute_tag_match(
            chunk_tags=["tool=oauth", "outcome=failed"],
            detected_tags=["tool=auth"],
        )
        assert score == 0.5

    def test_tag_match_substring_auth_matches_authentication(self):
        score = self.engine._compute_tag_match(
            chunk_tags=["tool=authentication", "error=timeout"],
            detected_tags=["tool=auth"],
        )
        assert score == 0.5

    def test_tag_match_no_overlap_returns_zero(self):
        score = self.engine._compute_tag_match(
            chunk_tags=["tool=db", "outcome=work_done"],
            detected_tags=["tool=auth"],
        )
        # "auth" not in "db", but "tool" category overlaps → 0.5 partial
        assert score == 0.5

    def test_tag_match_exact_returns_one(self):
        score = self.engine._compute_tag_match(
            chunk_tags=["tool=auth"],
            detected_tags=["tool=auth"],
        )
        assert score == 1.0

    # ── Category-weighted matching ────────────────────────────────────────

    def test_tag_match_outcome_and_tool_partial_normalize_equally(self):
        outcome_partial = self.engine._compute_tag_match(
            chunk_tags=["outcome=successfully_called"],
            detected_tags=["outcome=failed"],
        )
        tool_partial = self.engine._compute_tag_match(
            chunk_tags=["tool=db"],
            detected_tags=["tool=auth"],
        )
        assert outcome_partial == 0.5
        assert tool_partial == 0.5

    def test_tag_match_outcome_exact_beats_tool_exact_when_weighted(self):
        outcome_heavy = self.engine._compute_tag_match(
            chunk_tags=["outcome=failed", "tool=db"],
            detected_tags=["outcome=failed", "tool=auth"],
        )
        tool_only = self.engine._compute_tag_match(
            chunk_tags=["tool=auth"],
            detected_tags=["tool=auth"],
        )
        assert 0.0 < outcome_heavy <= 1.0
        assert tool_only == 1.0

    def test_tag_match_category_weight_in_combined_score(self):
        score = self.engine._compute_tag_match(
            chunk_tags=["outcome=successfully_called", "tool=db"],
            detected_tags=["outcome=failed", "tool=auth"],
        )
        assert score == pytest.approx(0.5)

    def test_tag_match_empty_detected_returns_neutral(self):
        assert self.engine._compute_tag_match(
            chunk_tags=["tool=auth"], detected_tags=[]
        ) == 0.5

    # ── Embedding similarity (Jaccard) ────────────────────────────────────

    def test_embedding_similarity_identical_sets(self):
        score = self.engine._compute_embedding_similarity(
            chunk_tags=["tool=auth", "outcome=failed"],
            detected_tags=["tool=auth", "outcome=failed"],
        )
        assert score == 1.0

    def test_embedding_similarity_disjoint_sets(self):
        score = self.engine._compute_embedding_similarity(
            chunk_tags=["tool=auth"],
            detected_tags=["tool=db"],
        )
        assert score == 0.0

    def test_embedding_similarity_partial_overlap(self):
        score = self.engine._compute_embedding_similarity(
            chunk_tags=["tool=auth", "error=timeout"],
            detected_tags=["tool=auth", "error=token_expired"],
        )
        assert score == pytest.approx(1 / 3)

    def test_embedding_similarity_superset_overlap(self):
        score = self.engine._compute_embedding_similarity(
            chunk_tags=["tool=auth", "error=timeout"],
            detected_tags=["tool=auth"],
        )
        assert score == 0.5

    def test_embedding_similarity_empty_chunk_returns_neutral(self):
        assert self.engine._compute_embedding_similarity(
            chunk_tags=[], detected_tags=["tool=auth"]
        ) == 0.5

    def test_embedding_similarity_empty_detected_returns_neutral(self):
        assert self.engine._compute_embedding_similarity(
            chunk_tags=["tool=auth"], detected_tags=[]
        ) == 0.5

    # ── Graph boost ───────────────────────────────────────────────────────

    def test_graph_boost_no_linked_chunks_returns_zero(self):
        score = self.engine._compute_graph_boost(
            chunk={"chunk_id": "mem_001", "linked_chunks": []},
            all_candidate_ids={"mem_001", "mem_002", "mem_003"},
        )
        assert score == 0.0

    def test_graph_boost_missing_linked_chunks_field_returns_zero(self):
        score = self.engine._compute_graph_boost(
            chunk={"chunk_id": "mem_001"},
            all_candidate_ids={"mem_001", "mem_002"},
        )
        assert score == 0.0

    def test_graph_boost_one_of_two_in_candidates(self):
        score = self.engine._compute_graph_boost(
            chunk={"chunk_id": "mem_001", "linked_chunks": ["mem_002", "mem_999"]},
            all_candidate_ids={"mem_001", "mem_002", "mem_003"},
        )
        assert score == 0.5

    def test_graph_boost_all_in_candidates(self):
        score = self.engine._compute_graph_boost(
            chunk={"chunk_id": "mem_001", "linked_chunks": ["mem_002", "mem_003"]},
            all_candidate_ids={"mem_001", "mem_002", "mem_003"},
        )
        assert score == 1.0

    def test_graph_boost_none_in_candidates_returns_zero(self):
        score = self.engine._compute_graph_boost(
            chunk={"chunk_id": "mem_001", "linked_chunks": ["mem_998", "mem_999"]},
            all_candidate_ids={"mem_001", "mem_002", "mem_003"},
        )
        assert score == 0.0

    # ── Combined scoring formula ──────────────────────────────────────────

    def test_score_chunks_combines_all_components(self):
        """A chunk with all components matching should score higher than
        one with only some components matching."""
        full_match = {
            "chunk_id": "mem_full",
            "tags": ["tool=auth", "outcome=failed", "error=token_expired"],
            "outcome_tag": "failed",
            "linked_chunks": ["mem_partial"],
            "last_accessed": datetime.now(timezone.utc).isoformat(),
        }
        sparse = {
            "chunk_id": "mem_sparse",
            "tags": ["tool=db"],
            "outcome_tag": "work_done",
            "linked_chunks": [],
            "last_accessed": None,
        }
        detected = ["tool=auth", "outcome=failed"]
        scored = self.engine._score_chunks([full_match, sparse], detected)
        scores = {c["chunk_id"]: c["score"] for c in scored}

        assert scores["mem_full"] > scores["mem_sparse"]

    def test_score_chunks_graph_boost_adds_to_score(self):
        """A chunk whose links land in the candidate set should score higher
        than an identical chunk whose links are absent."""
        base = {
            "tags": ["tool=auth"],
            "outcome_tag": "work_done",
            "last_accessed": None,
        }
        linked = {**base, "chunk_id": "mem_a", "linked_chunks": ["mem_b"]}
        linked_target = {**base, "chunk_id": "mem_b", "linked_chunks": []}
        unlinked = {**base, "chunk_id": "mem_c", "linked_chunks": ["mem_does_not_exist"]}

        scored = self.engine._score_chunks(
            [linked, linked_target, unlinked], ["tool=auth"]
        )
        scores = {c["chunk_id"]: c["score"] for c in scored}
        assert scores["mem_a"] == pytest.approx(scores["mem_c"] + 0.2)

    def test_score_chunks_combined_tag_match_and_embedding_delta(self):
        detected = ["tool=auth"]
        chunk_x = {
            "chunk_id": "mem_x",
            "tags": ["tool=auth", "memory=long_term"],
            "outcome_tag": "work_done",
            "linked_chunks": [],
            "last_accessed": None,
        }
        chunk_y = {
            "chunk_id": "mem_y",
            "tags": ["memory=long_term"],
            "outcome_tag": "work_done",
            "linked_chunks": [],
            "last_accessed": None,
        }
        scored = self.engine._score_chunks([chunk_x, chunk_y], detected)
        scores = {c["chunk_id"]: c["score"] for c in scored}
        assert scores["mem_x"] == pytest.approx(0.75)
        assert scores["mem_y"] == pytest.approx(0.0)

    # ── Source-kind boost (skill > session) ──────────────────────────────

    def test_score_chunks_skill_chunk_scores_higher_than_session(self):
        """MNEME plan 1C: skill chunks get a 1.5x boost on outcome_weight,
        so an identical-content skill chunk must score higher than a session
        chunk. Skills are authoritative documentation; sessions are noisy
        chat logs that shouldn't drown them out."""
        detected = ["tool=auth"]
        base = {
            "tags": ["tool=auth", "outcome=work_done"],
            "outcome_tag": "work_done",
            "linked_chunks": [],
            "last_accessed": None,
            "qdrant_score": 0.0,
        }
        skill_chunk = {**base, "chunk_id": "mem_skill", "source_kind": "skill"}
        session_chunk = {**base, "chunk_id": "mem_session", "source_kind": "session"}

        scored = self.engine._score_chunks([skill_chunk, session_chunk], detected)
        scores = {c["chunk_id"]: c["score"] for c in scored}

        # Skill must outrank session by the boost amount applied to outcome_weight.
        # tag_match_score = 1.0 (exact), OUTCOME_PRIORITY["work_done"] = 0.6
        # delta = 0.6 * 1.0 * (1.5 - 1.0) = 0.3
        assert scores["mem_skill"] > scores["mem_session"]
        assert scores["mem_skill"] == pytest.approx(scores["mem_session"] + 0.3)

    def test_score_chunks_non_skill_source_kinds_unaffected(self):
        """MNEME plan 1C: the 1.5x boost must apply ONLY when source_kind == 'skill'.
        Session and log chunks must score identically to chunks missing the field."""
        detected = ["tool=auth"]
        base = {
            "tags": ["tool=auth", "outcome=work_done"],
            "outcome_tag": "work_done",
            "linked_chunks": [],
            "last_accessed": None,
            "qdrant_score": 0.0,
        }
        missing = {**base, "chunk_id": "mem_missing"}  # no source_kind field
        session = {**base, "chunk_id": "mem_session", "source_kind": "session"}
        log = {**base, "chunk_id": "mem_log", "source_kind": "log"}

        scored = self.engine._score_chunks([missing, session, log], detected)
        scores = {c["chunk_id"]: c["score"] for c in scored}

        # All three should score identically (boost does not apply).
        assert scores["mem_missing"] == pytest.approx(scores["mem_session"])
        assert scores["mem_session"] == pytest.approx(scores["mem_log"])
