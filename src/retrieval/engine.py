"""
Retrieval Engine — tag-aware memory retrieval with priority scoring.

Real implementation: intent detection (keyword heuristics or BitNet) plus
vector-style ranking over repository chunks. Scoring combines tag match,
embedding (Jaccard) similarity, recency, and graph boost.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any

from src.models import OUTCOME_PRIORITY

if TYPE_CHECKING:
    from src.memory_store import MemoryRepository
    from src.retrieval.qdrant_search import QdrantSearch

from src.retrieval.intent_detector import IntentDetector

logger = logging.getLogger(__name__)


# Tag match score weights
TAG_MATCH_EXACT = 1.0
TAG_MATCH_PARTIAL = 0.5
RECENCY_BOOST_DAYS = 7
RECENCY_BOOST_AMOUNT = 0.1
EMBEDDING_WEIGHT = 0.3
GRAPH_BOOST_WEIGHT = 0.2
GRAPH_HOPS = 1
QDRANT_WEIGHT = 1.5  # cosine similarity dominates when present (June 19 2026)

# Category weights — outcome tags are most important (failure context),
# error tags are second (specific failure mode), tool tags are third (area).
CATEGORY_WEIGHTS = {
    "outcome": 1.5,
    "error": 1.2,
    "tool": 1.0,
}


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
    """

    def __init__(
        self,
        repository: MemoryRepository | None = None,
        intent_detector: IntentDetector | None = None,
        qdrant_search: QdrantSearch | None = None,
    ) -> None:
        """
        Initialize RetrievalEngine.

        Args:
            repository: MemoryRepository instance for chunk storage.
                        Required for retrieval to find candidates.
            intent_detector: IntentDetector instance. If None, a default
                             keyword-based detector is created.
            qdrant_search: QdrantSearch instance for real vector search.
                           When provided, the engine uses cosine similarity
                           over dense embeddings instead of Jaccard on tags.
        """
        self._repo = repository
        self._intent_detector = intent_detector
        self._qdrant_search = qdrant_search
        # Validate detector type if provided
        if intent_detector is not None and not isinstance(intent_detector, IntentDetector):
            raise TypeError(
                f"intent_detector must be an IntentDetector instance, "
                f"got {type(intent_detector).__name__}"
            )

    def retrieve(
        self,
        prompt_context: str,
        session_id: str | None = None,
        query_embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        """
        Retrieve relevant memory chunks for a given prompt.

        When ``query_embedding`` is provided and Qdrant is configured, performs
        a vector similarity search as the first-pass candidate retrieval,
        then applies the priority scoring formula to those candidates.
        Otherwise falls back to the tag-based candidate retrieval from the
        repository.

        Returns a dict with:
          - detected_tags: list of tags extracted from prompt
          - intent: intent label
          - injected_context: formatted string of chunk contents
          - chunks_used: list of chunk_ids returned
          - tag_matches: per-tag match quality (exact/partial/none)
          - priority_scores: per-chunk score breakdown
        """
        # Step 1: Intent detection via keyword heuristics (or BitNet if configured)
        intent_result = self._detect_intent(prompt_context)
        detected_tags = intent_result["detected_tags"]
        intent = intent_result["intent"]

        # Step 2: Retrieve candidate chunks
        candidates = self._get_candidates(
            query_embedding=query_embedding,
            session_id=session_id,
            limit=50,
        )

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

    def upsert_chunk_embedding(
        self,
        chunk_id: str,
        content: str,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> None:
        """
        Index a chunk's embedding in Qdrant for vector search.

        Idempotent: re-calling with the same chunk_id overwrites the stored vector.

        Best-effort: if Qdrant is unavailable, logs a warning and returns
        without crashing the calling code.
        """
        if self._qdrant_search is None:
            return
        try:
            self._qdrant_search.upsert_chunk(
                chunk_id=chunk_id,
                content=content,
                embedding=embedding,
                metadata=metadata,
            )
        except Exception as exc:
            logger.warning(
                "Failed to upsert chunk embedding to Qdrant (chunk_id=%s): %s",
                chunk_id, exc,
            )

    def search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        score_threshold: float = 0.0,
        filter_conditions: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Pure vector search via Qdrant.

        Returns Qdrant search results directly when Qdrant is configured.
        When Qdrant is unavailable, returns an empty list.
        """
        if self._qdrant_search is None:
            return []
        try:
            return self._qdrant_search.search(
                query_embedding=query_embedding,
                limit=limit,
                score_threshold=score_threshold,
                filter_conditions=filter_conditions,
            )
        except Exception as exc:
            logger.warning("Qdrant search failed: %s", exc)
            return []

    # ── Private helpers ────────────────────────────────────────────────────

    def _get_candidates(
        self,
        query_embedding: list[float] | None,
        session_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        Retrieve candidate chunks.

        When ``query_embedding`` is provided and Qdrant is configured, uses
        vector search to retrieve candidates. Otherwise falls back to the
        repository's tag-based listing.

        NEW (June 19 2026): When Qdrant is unavailable but query_embedding is
        provided AND chunks have in-memory embeddings, do brute-force cosine
        similarity search across all chunks. This makes Ollama embeddings
        useful without needing a Qdrant deployment.
        """
        if self._qdrant_search is not None and query_embedding is not None:
            filter_conditions: dict[str, Any] = {}
            if session_id:
                filter_conditions["session_id"] = session_id
            try:
                vector_results = self._qdrant_search.search(
                    query_embedding=query_embedding,
                    limit=limit,
                    filter_conditions=filter_conditions or None,
                )
                if vector_results:
                    chunk_ids = [r["chunk_id"] for r in vector_results]
                    # Fetch full chunk records from repository
                    candidates = []
                    for cid in chunk_ids:
                        chunk = self._repo.get_chunk(cid) if self._repo else None
                        if chunk:
                            # Merge Qdrant score into the chunk record
                            candidates.append({**chunk, "qdrant_score": next(
                                (r["score"] for r in vector_results if r["chunk_id"] == cid),
                                0.0,
                            )})
                    return candidates
            except Exception as exc:
                logger.warning(
                    "Qdrant candidate retrieval failed, falling back to repo: %s",
                    exc,
                )

        # No Qdrant — try in-memory cosine similarity over stored embeddings.
        # The dev_server loader preserves "embedding" in REPO, so we can use it.
        if query_embedding is not None and self._repo is not None:
            return self._cosine_search_in_memory(
                query_embedding=query_embedding,
                session_id=session_id,
                limit=limit,
            )

        # Fallback: repository-based candidate retrieval (tag/recency)
        if self._repo is None:
            return []
        return self._repo.list_chunks(session_id=session_id, limit=limit)

    def _cosine_search_in_memory(
        self,
        query_embedding: list[float],
        session_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Brute-force cosine similarity over in-memory chunk embeddings.

        Used when Qdrant isn't running but we still want semantic search.
        Vectorized with numpy for speed — 7000+ chunks in single-digit ms.
        """
        if not hasattr(self._repo, "_chunks"):
            return self._repo.list_chunks(session_id=session_id, limit=limit) if self._repo else []

        # Collect chunks + embeddings, filter by session_id + has embedding
        candidate_chunks: list[dict[str, Any]] = []
        candidate_vecs: list[list[float]] = []
        for chunk in self._repo._chunks.values():
            if session_id and chunk.get("session_id") != session_id:
                continue
            emb = chunk.get("embedding")
            if not emb or not isinstance(emb, list):
                continue
            candidate_chunks.append(chunk)
            candidate_vecs.append(emb)

        if not candidate_chunks:
            return []

        # Vectorized cosine similarity via numpy
        try:
            import numpy as np
            q = np.asarray(query_embedding, dtype=np.float32)
            mat = np.asarray(candidate_vecs, dtype=np.float32)
            q_norm = np.linalg.norm(q)
            mat_norms = np.linalg.norm(mat, axis=1)
            # Avoid divide-by-zero
            mask = (mat_norms > 0) & (q_norm > 0)
            sims = np.zeros(len(candidate_chunks), dtype=np.float32)
            sims[mask] = (mat[mask] @ q) / (mat_norms[mask] * q_norm)
        except ImportError:
            # numpy not available — pure python fallback (slow but works)
            q_norm = sum(x * x for x in query_embedding) ** 0.5
            sims = []
            for emb in candidate_vecs:
                c_norm = sum(a * a for a in emb) ** 0.5
                if q_norm == 0 or c_norm == 0:
                    sims.append(0.0)
                    continue
                dot = sum(a * b for a, b in zip(query_embedding, emb))
                sims.append(dot / (q_norm * c_norm))
            sims_arr = __import__("numpy").asarray(sims, dtype=__import__("numpy").float32) if False else sims
            # sort by score desc, take top
            scored = sorted(zip(sims, candidate_chunks), key=lambda x: -x[0])[:limit]
            return [{**chunk, "qdrant_score": float(sim)} for sim, chunk in scored]

        # Numpy path: get top-N indices
        n = min(limit, len(sims))
        # argpartition is O(n) vs full sort; faster for top-k
        top_idx = np.argpartition(-sims, n - 1)[:n]
        # Sort those by score desc
        top_idx = top_idx[np.argsort(-sims[top_idx])]

        return [
            {**candidate_chunks[i], "qdrant_score": float(sims[i])}
            for i in top_idx
        ]  

    def _detect_intent(self, prompt_context: str) -> dict[str, Any]:
        """
        Detect intent and tags from prompt.

        Uses the injected IntentDetector if available, otherwise creates
        a default keyword-based one.
        """
        if self._intent_detector is None:
            self._intent_detector = IntentDetector()
        return self._intent_detector.detect(prompt_context)

    def _score_chunks(
        self,
        chunks: list[dict[str, Any]],
        detected_tags: list[str],
    ) -> list[dict[str, Any]]:
        """Apply priority scoring formula to each chunk.

        Formula (June 19 2026 revision):
          score = (tag_match * outcome_weight)
                  + recency_boost
                  + (embedding_similarity * EMBEDDING_WEIGHT)
                  + (graph_boost * GRAPH_BOOST_WEIGHT)
                  + (qdrant_score * QDRANT_WEIGHT)   ← NEW: cosine wins when present

        QDRANT_WEIGHT (1.5) is dominant because cosine similarity over
        dense Ollama embeddings is the strongest semantic signal. Tag
        match is a tiebreaker.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=RECENCY_BOOST_DAYS)
        scored_chunks = []
        all_candidate_ids = {c["chunk_id"] for c in chunks if c.get("chunk_id")}

        for chunk in chunks:
            chunk_tags = chunk.get("tags", []) or []
            tag_match_score = self._compute_tag_match(chunk_tags, detected_tags)
            embedding_similarity = self._compute_embedding_similarity(
                chunk_tags, detected_tags
            )
            graph_boost = self._compute_graph_boost(chunk, all_candidate_ids)
            # Cosine similarity from in-memory vector search (or qdrant).
            # Already in [-1, 1]; shift to [0, 1] so a 0 doesn't tank the score.
            qdrant_score = chunk.get("qdrant_score", 0.0)
            qdrant_norm = (qdrant_score + 1.0) / 2.0  # [-1,1] → [0,1]

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
                except Exception as exc:
                    logger.debug(
                        "last_accessed_parse_failed (chunk_id=%s, value=%r): %s",
                        chunk.get("chunk_id"), last_accessed, exc,
                    )

            score = (
                (tag_match_score * outcome_weight)
                + recency_boost
                + (embedding_similarity * EMBEDDING_WEIGHT)
                + (graph_boost * GRAPH_BOOST_WEIGHT)
                + (qdrant_norm * QDRANT_WEIGHT)
            )
            scored_chunks.append({**chunk, "score": score})

        return scored_chunks

    def _compute_tag_match(
        self,
        chunk_tags: list[str],
        detected_tags: list[str],
    ) -> float:
        """
        Compute tag match score between chunk tags and detected tags.

        Three match types, each weighted by category importance:
          - Exact:      tag matches exactly (full credit, weighted by category)
          - Substring:  value is substring of chunk value (e.g. "auth" in "oauth")
          - Category:   same category prefix, different value (e.g. tool=auth vs tool=db)

        Category weights (CATEGORY_WEIGHTS):
          - outcome: 1.5x — failure context is most important
          - error:   1.2x — specific failure mode
          - tool:    1.0x — tool area

        Returns a score in [0.0, 1.0] (capped).
        """
        if not detected_tags:
            return 0.5  # Neutral when no tags detected

        total_score = 0.0
        total_weight = 0.0

        for detected in detected_tags:
            category, detected_value = self._parse_tag(detected)
            cat_weight = CATEGORY_WEIGHTS.get(category, 1.0)
            total_weight += cat_weight

            # 1. Exact match
            if detected in chunk_tags:
                total_score += TAG_MATCH_EXACT * cat_weight
                continue

            # 2. Substring match (e.g., "auth" in "oauth" or "authentication")
            if detected_value and any(
                detected_value in self._parse_tag(t)[1]
                for t in chunk_tags
                if "=" in t
            ):
                total_score += TAG_MATCH_PARTIAL * cat_weight
                continue

            # 3. Category match (same prefix, different value)
            if category and any(
                self._parse_tag(t)[0] == category for t in chunk_tags if "=" in t
            ):
                total_score += TAG_MATCH_PARTIAL * cat_weight
                continue

        # Normalize: divide by total possible weight.
        # No cap needed: each iteration adds at most cat_weight to total_score
        # (TAG_MATCH_EXACT * cat_weight), so total_score ≤ total_weight.
        if total_weight == 0:
            return 0.0
        return total_score / total_weight

    def _parse_tag(self, tag: str) -> tuple[str, str]:
        """
        Parse a tag into (category, value).

        Examples:
          "outcome=failed"        → ("outcome", "failed")
          "tool=auth"             → ("tool", "auth")
          "no_tool"               → ("no_tool", "")  # no separator → value empty
          ""                      → ("", "")          # empty input → both empty
        """
        if not tag:
            return "", ""
        if "=" in tag:
            category, value = tag.split("=", 1)
            return category, value
        return tag, ""

    def _compute_graph_boost(
        self,
        chunk: dict[str, Any],
        all_candidate_ids: set[str],
        graph_hops: int = GRAPH_HOPS,
    ) -> float:
        """
        Compute graph-based boost for a chunk based on its links to other candidates.

        Counts how many of the chunk's `linked_chunks` are also in the candidate
        set. The score is the fraction of `linked_chunks` that are reachable,
        capped at 1.0.

        Multi-hop note: this helper only has access to one chunk, so traversal
        beyond direct neighbors would require a graph index keyed by chunk_id.
        `graph_hops` is accepted for forward compatibility but the current
        implementation is exact for hop=1 and degrades to hop=1 otherwise.

        Args:
            chunk: Chunk dict with optional `linked_chunks` (list of chunk_ids).
            all_candidate_ids: Set of chunk_ids in the current candidate pool.
            graph_hops: Max number of hops to traverse (1 = direct neighbors only).
                Currently unused; see multi-hop note in the docstring.

        Returns:
            Boost in [0.0, 1.0]. 0.0 if the chunk has no links or none are
            reachable in the candidate set.
        """
        del graph_hops  # see multi-hop note; reserved for future graph index
        linked = chunk.get("linked_chunks", []) or []
        if not linked:
            return 0.0

        reachable = set(linked) & all_candidate_ids
        if not reachable:
            return 0.0

        # No cap needed: reachable is a subset of linked (set intersection),
        # so the ratio can never exceed 1.0.
        return len(reachable) / len(linked)

    def _compute_embedding_similarity(
        self,
        chunk_tags: list[str],
        detected_tags: list[str],
    ) -> float:
        """
        Compute semantic similarity between chunk tags and detected tags.

        Uses Jaccard similarity over tag sets:
            similarity = |chunk_tags ∩ detected_tags| / |chunk_tags ∪ detected_tags|

        This is a tag-set similarity (not a true semantic embedding) and
        serves as a stand-in until a real sentence-transformer embedding
        model is wired in. When that happens, replace this with cosine
        similarity over dense embeddings.

        Returns:
            - 1.0 if sets are identical
            - 0.0 if sets are disjoint
            - Fractional in between

        Returns 0.5 (neutral) if either set is empty.
        """
        if not chunk_tags or not detected_tags:
            return 0.5  # Neutral when one side is empty

        chunk_set = set(chunk_tags)
        detected_set = set(detected_tags)

        intersection = chunk_set & detected_set
        union = chunk_set | detected_set

        if not union:
            return 0.5

        return len(intersection) / len(union)

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
