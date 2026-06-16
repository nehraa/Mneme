"""
Retrieval Engine — tag-aware memory retrieval with priority scoring.
Real implementation: Ollama (intent) + Qdrant (vector search) + Gemini (tag-sort).
Mock is in mock_retrieval.py.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any

from src.models import OUTCOME_PRIORITY

if TYPE_CHECKING:
    from src.memory_store import MemoryRepository


# Tag match score weights
TAG_MATCH_EXACT = 1.0
TAG_MATCH_PARTIAL = 0.5
RECENCY_BOOST_DAYS = 7
RECENCY_BOOST_AMOUNT = 0.1


class RetrievalEngine:
    """
    Tag-aware retrieval with priority scoring.

    Scoring formula:
        score = (tag_match_score × OUTCOME_PRIORITY[outcome_tag]) + recency_boost

    Where:
      - tag_match_score: 1.0 (exact), 0.5 (partial/same-category), 0.0 (no match)
      - OUTCOME_PRIORITY: failed=1.0, stopped=0.8, work_done=0.6,
        successfully_called=0.4, no_tool_called=0.2
      - recency_boost: +0.1 if last_accessed within last 7 days

    [MOCK] Currently uses MockRetrievalEngine. Pass use_mock=False to use
    the real Ollama + Qdrant + Gemini implementation.
    """

    def __init__(
        self,
        repository: MemoryRepository | None = None,
        use_mock: bool = False,
    ) -> None:
        self._repo = repository
        self._use_mock = use_mock

    def retrieve(
        self,
        prompt_context: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Retrieve relevant memory chunks for a given prompt.

        Returns a dict with:
          - detected_tags: list of tags extracted from prompt
          - intent: intent label
          - injected_context: formatted string of chunk contents
          - chunks_used: list of chunk_ids returned
          - tag_matches: per-tag match quality (exact/partial/none)
          - priority_scores: per-chunk score breakdown
        """
        if self._use_mock:
            from src.retrieval.mock_retrieval import MockRetrievalEngine

            return MockRetrievalEngine().retrieve(
                prompt_context=prompt_context,
                session_id=session_id,
            )

        # Step 1: Intent detection via Ollama 100-200M
        intent_result = self._detect_intent(prompt_context)
        detected_tags = intent_result["detected_tags"]
        intent = intent_result["intent"]

        # Step 2: Retrieve candidate chunks from MemoryRepository
        candidates = self._repo.list_chunks(session_id=session_id, limit=50)

        # Step 3: Score each chunk using priority formula
        scored = self._score_chunks(candidates, detected_tags)

        # Step 4: Sort by score descending and take top chunks
        top_chunks = sorted(scored, key=lambda x: x["score"], reverse=True)[:5]

        # Step 5: Build injected context string
        injected_context = self._build_injected_context(top_chunks)

        # Step 6: Build tag matches map
        tag_matches = self._build_tag_matches(top_chunks, detected_tags)

        return {
            "detected_tags": detected_tags,
            "intent": intent,
            "injected_context": injected_context,
            "chunks_used": [c["chunk_id"] for c in top_chunks],
            "tag_matches": tag_matches,
            "priority_scores": {c["chunk_id"]: round(c["score"], 4) for c in top_chunks},
        }

    def _detect_intent(self, prompt_context: str) -> dict[str, Any]:
        """
        Detect intent and tags from prompt using Ollama 100-200M local model.

        Real implementation:
            import ollama
            response = ollama.chat(
                model="llama3.2:1b",  # or smaller 100-200M model
                messages=[{"role": "user", "content": prompt_context}],
            )
            return json.loads(response["message"]["content"])
        """
        from src.retrieval.intent_detector import IntentDetector

        detector = IntentDetector()
        return detector.detect(prompt_context)

    def _score_chunks(
        self,
        chunks: list[dict[str, Any]],
        detected_tags: list[str],
    ) -> list[dict[str, Any]]:
        """Apply priority scoring formula to each chunk."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=RECENCY_BOOST_DAYS)
        scored_chunks = []

        for chunk in chunks:
            tag_match_score = self._compute_tag_match(chunk.get("tags", []), detected_tags)
            outcome_tag = chunk.get("outcome_tag", "work_done")
            outcome_weight = OUTCOME_PRIORITY.get(outcome_tag, 0.2)

            # Recency boost
            recency_boost = 0.0
            last_accessed = chunk.get("last_accessed")
            if last_accessed:
                try:
                    accessed_at = datetime.fromisoformat(last_accessed)
                    if accessed_at >= cutoff:
                        recency_boost = RECENCY_BOOST_AMOUNT
                except Exception:
                    pass

            score = (tag_match_score * outcome_weight) + recency_boost
            scored_chunks.append({**chunk, "score": score})

        return scored_chunks

    def _compute_tag_match(
        self,
        chunk_tags: list[str],
        detected_tags: list[str],
    ) -> float:
        """
        Compute tag match score between chunk tags and detected tags.

        Returns:
          - 1.0: exact match on same tag
          - 0.5: same category, different value (e.g. tool=auth vs tool=db)
          - 0.0: no match
        """
        if not detected_tags:
            return 0.5  # Neutral when no tags detected

        score = 0.0
        for detected in detected_tags:
            if detected in chunk_tags:
                score += TAG_MATCH_EXACT
            else:
                # Check for same-category partial match
                if "=" in detected:
                    category = detected.split("=", 1)[0]
                    for chunk_tag in chunk_tags:
                        if "=" in chunk_tag:
                            chunk_category = chunk_tag.split("=", 1)[0]
                            if chunk_category == category:
                                score += TAG_MATCH_PARTIAL
                                break

        return min(score, 1.0)  # Cap at 1.0

    def _build_injected_context(
        self,
        top_chunks: list[dict[str, Any]],
    ) -> str:
        """Format top-scored chunks into injected context string."""
        if not top_chunks:
            return ""

        lines = ["Relevant memory from last session:"]
        for chunk in top_chunks:
            chunk_id = chunk.get("chunk_id", "?")
            content = chunk.get("content", "")
            outcome = chunk.get("outcome_tag", "work_done")
            # Truncate long content
            display_content = content[:120] + "..." if len(content) > 120 else content
            lines.append(f"[{chunk_id}] {display_content} [{outcome}]")

        return "\n".join(lines)

    def _build_tag_matches(
        self,
        top_chunks: list[dict[str, Any]],
        detected_tags: list[str],
    ) -> dict[str, str]:
        """Build per-tag match quality map."""
        tag_matches: dict[str, str] = {}
        for detected in detected_tags:
            found_exact = any(detected in c.get("tags", []) for c in top_chunks)
            if found_exact:
                tag_matches[detected] = "exact"
            else:
                # Check partial
                if "=" in detected:
                    category = detected.split("=", 1)[0]
                    found_partial = any(
                        category in t for c in top_chunks for t in c.get("tags", [])
                    )
                    tag_matches[detected] = "partial" if found_partial else "none"
                else:
                    tag_matches[detected] = "none"
        return tag_matches
