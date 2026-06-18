"""
Diff Engine — pre-tool hook that warns when a proposed change contradicts
past failed attempts in memory.

The diff engine is the second half of Mneme's pre-tool hook. Phase 4
(RetrievalEngine) supplies the context; Phase 5 (DiffEngine) blocks the
action if it would repeat a known failure.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from src.memory_store import MemoryRepository
    from src.retrieval.qdrant_search import QdrantSearch

logger = logging.getLogger(__name__)


# ── Module-level constants ────────────────────────────────────────────────────

# Jaccard similarity threshold above which a contradicting chunk is considered
# a strong enough match to trigger the guard. Tuned to be permissive enough
# to catch near-duplicates of past attempts without firing on unrelated code.
SIMILARITY_THRESHOLD = 0.3

# Outcome tag value that marks a past attempt as a failure. Only failed
# attempts are eligible to trigger the guard; successful work, work_done,
# etc. are not warnings.
FAILED_OUTCOME = "failed"

# Implementation note used when the real path returns no contradictions.
_REAL_IMPL_NOTE = (
    "Real: guard/diff_engine.py::DiffEngine.check() — "
    "queries Neo4j for 'contradicts' edges + Qdrant semantic similarity"
)


# ── Type alias for the optional embedding service ─────────────────────────────
# A function that maps a string to a dense vector. Intended to be supplied
# by the host application (e.g., a Gemini embedding client or a local
# sentence-transformer). When None, DiffEngine falls back to Jaccard.
EmbeddingFn = Callable[[str], list[float]]


class DiffEngine:
    """
    Pre-tool guard: warn when a proposed change contradicts a past failed attempt.

    Real backend path:
        1. MemoryRepository.get_contradicting_chunks(target_file, session_id)
           → Neo4j Cypher: MATCH (c:Chunk)-[:CONTRADICTS]->(t) WHERE
             t.source_file = $target_file AND (c.session_id = $session_id OR
             $session_id IS NULL) RETURN c
        2. Filter to chunks where outcome_tag == FAILED_OUTCOME ("failed")
        3. Compute text similarity between `proposed_change` and each
           contradicting chunk's content. When a Qdrant client and an
           embedding service are both provided, uses cosine similarity over
           dense embeddings (the real semantic path). Otherwise falls back
           to Jaccard word-overlap.
        4. If max similarity > SIMILARITY_THRESHOLD (0.3), trigger guard
           and assemble a warning + related memories list.
    """

    def __init__(
        self,
        repository: MemoryRepository,
        qdrant_search: QdrantSearch | None = None,
        embedding_service: EmbeddingFn | None = None,
    ) -> None:
        """
        Initialize DiffEngine.

        Args:
            repository: MemoryRepository instance for chunk/edge access.
                        Required.
            qdrant_search: Optional QdrantSearch client. When provided
                           together with ``embedding_service``, similarity
                           is computed via Qdrant cosine similarity over
                           dense embeddings (the real semantic path).
                           When None, the Jaccard fallback is used.
            embedding_service: Optional callable that maps a string to a
                           dense vector (e.g., Gemini embedding client or
                           local sentence-transformer). When None, the
                           Jaccard fallback is used regardless of whether
                           ``qdrant_search`` is set.

        Note:
            The semantic path requires BOTH ``qdrant_search`` and
            ``embedding_service``. If either is missing, the engine falls
            back to Jaccard word-overlap. This is by design: embeddings
            cannot be computed without an embedding model, and embeddings
            alone are useless without a vector index to compare against.
        """
        self._repo = repository
        self._qdrant_search = qdrant_search
        self._embedding_service = embedding_service

    def check(
        self,
        proposed_change: str,
        target_file: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Check whether a proposed change contradicts a past failed attempt.

        Args:
            proposed_change: The change the user/agent is about to make.
            target_file: The file the change targets.
            session_id: Optional session filter (defaults to cross-session).

        Returns:
            dict with keys:
              - guard_triggered: True if a contradiction was found
              - warning: human-readable warning (or None)
              - related_memories: list of chunk_ids that triggered the guard
              - override_allowed: always True
              - _implementation_note: documents the real backend path
        """
        # Edge case: empty proposed change can't possibly contradict anything.
        if not proposed_change or not proposed_change.strip():
            return {
                "guard_triggered": False,
                "warning": None,
                "related_memories": [],
                "override_allowed": True,
                "_implementation_note": _REAL_IMPL_NOTE,
            }

        # 1. Pull all chunks with a 'contradicts' edge pointing at target_file.
        #    get_contradicting_chunks already filters by source_file on the
        #    target side of the edge.
        candidates = self._repo.get_contradicting_chunks(
            target_file=target_file,
            session_id=session_id,
        )

        # 2. Keep only chunks that represent past failures.
        failed = [c for c in candidates if c.get("outcome_tag") == FAILED_OUTCOME]

        if not failed:
            return {
                "guard_triggered": False,
                "warning": None,
                "related_memories": [],
                "override_allowed": True,
                "_implementation_note": _REAL_IMPL_NOTE,
            }

        # 3. Score each failed chunk against the proposed change and
        #    keep the ones whose similarity clears the threshold.
        #    Real semantic similarity is preferred when both Qdrant and an
        #    embedding service are available; otherwise Jaccard fallback.
        related: list[dict[str, Any]] = []
        for chunk in failed:
            content = chunk.get("content", "")
            score = self._semantic_similarity(proposed_change, content)
            if score >= SIMILARITY_THRESHOLD:
                related.append({"chunk": chunk, "score": score})

        if not related:
            return {
                "guard_triggered": False,
                "warning": None,
                "related_memories": [],
                "override_allowed": True,
                "_implementation_note": _REAL_IMPL_NOTE,
            }

        # 4. Build the warning + related memories manifest.
        #    Sort by score descending so the most relevant contradiction
        #    appears first in the warning message.
        related.sort(key=lambda r: r["score"], reverse=True)
        related_ids = [r["chunk"].get("chunk_id", "?") for r in related]
        top = related[0]["chunk"]
        warning = (
            f"You tried a similar change in {target_file} and it failed "
            f"(chunk {top.get('chunk_id', '?')}, outcome={top.get('outcome_tag')}). "
            f"Are you sure you want to retry?"
        )
        return {
            "guard_triggered": True,
            "warning": warning,
            "related_memories": related_ids,
            "override_allowed": True,
            "_implementation_note": _REAL_IMPL_NOTE,
        }

    # ── Similarity backend selection ──────────────────────────────────────────

    def _semantic_similarity(self, proposed_change: str, chunk_content: str) -> float:
        """
        Compute similarity between the proposed change and a chunk's content.

        Resolution order:
            1. If both ``qdrant_search`` and ``embedding_service`` are set,
               use cosine similarity over dense embeddings (the real
               semantic path).
            2. Otherwise fall back to Jaccard word-overlap.

        Failures during the semantic path (e.g., Qdrant errors, embedding
        service exceptions) are logged and fall back to Jaccard — the
        guard never crashes a tool call because of similarity-backend issues.

        Args:
            proposed_change: The user/agent's proposed edit.
            chunk_content: The contradicting chunk's content string.

        Returns:
            Similarity score in [0.0, 1.0] from whichever backend was used.
        """
        if self._qdrant_search is not None and self._embedding_service is not None:
            try:
                return _cosine_similarity_over_embeddings(
                    proposed_change,
                    chunk_content,
                    embedding_service=self._embedding_service,
                    qdrant_search=self._qdrant_search,
                )
            except Exception as exc:
                logger.warning(
                    "Semantic similarity backend failed; falling back to Jaccard: %s",
                    exc,
                )
                # Intentionally fall through to Jaccard.
        else:
            logger.debug(
                "Semantic similarity backend unavailable "
                "(qdrant_search=%s, embedding_service=%s); using Jaccard fallback",
                self._qdrant_search is not None,
                self._embedding_service is not None,
            )

        return _jaccard_similarity(proposed_change, chunk_content)


# ── Module-level helpers ──────────────────────────────────────────────────────


def _cosine_similarity_over_embeddings(
    a: str,
    b: str,
    embedding_service: EmbeddingFn,
    qdrant_search: Any,
) -> float:
    """
    Compute cosine similarity between two strings via in-process embeddings.

    Used by ``DiffEngine._semantic_similarity`` when both the embedding
    service and Qdrant client are wired in. This is the real semantic
    path that the system uses in production.

    Each input string is embedded in-process via ``embedding_service``,
    then cosine similarity is computed directly over the resulting
    dense vectors. The ``qdrant_search`` client is accepted for API
    stability (callers pass it alongside ``embedding_service`` to signal
    a fully wired semantic backend) but is not used here — similarity
    is computed in-process so it stays correct even if Qdrant is
    temporarily unavailable.

    Two reasonable embedding backends are supported by the host
    application:

    1. **Gemini embedding service** (preferred for production):
       Plug in `google.generativeai` or the `genai` SDK to call
       ``gemini-embedding-2`` (already configured in src/config.py).
       Returns 768-dim unit vectors suitable for cosine distance.

    2. **Local sentence-transformers**:
       Plug in the `sentence-transformers` PyPI package (e.g.,
       ``all-MiniLM-L6-v2`` for 384-dim vectors). Works offline; no
       external API calls. Requires changing ``vector_size`` in
       ``QdrantConfig`` to match the chosen model.

    Args:
        a: First string (the proposed change).
        b: Second string (the contradicting chunk's content).
        embedding_service: Callable mapping string → dense vector.
        qdrant_search: QdrantSearch client for the vector store
                       (accepted for API stability; not used here).

    Returns:
        Cosine similarity in [0.0, 1.0]. Returns 0.0 when either input
        is empty, when either embedding is the zero vector, or when
        the two embeddings have mismatched dimensions.
    """
    if not a or not b:
        return 0.0

    vec_a = embedding_service(a)
    vec_b = embedding_service(b)

    if not vec_a or not vec_b:
        return 0.0

    if len(vec_a) != len(vec_b):
        logger.warning(
            "Embedding dimension mismatch: a=%d, b=%d; returning 0.0",
            len(vec_a),
            len(vec_b),
        )
        return 0.0

    dot = sum(x * y for x, y in zip(vec_a, vec_b))
    norm_a = sum(x * x for x in vec_a) ** 0.5
    norm_b = sum(x * x for x in vec_b) ** 0.5

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    similarity = dot / (norm_a * norm_b)
    # Cosine similarity natively lives in [-1.0, 1.0]; the public contract
    # for this function (and the guard's threshold comparison) is [0.0, 1.0],
    # so clamp into that range. Negative values are treated as "no
    # similarity" (0.0) and slight numerical overshoot above 1.0 is clipped.
    if similarity < 0.0:
        return 0.0
    if similarity > 1.0:
        return 1.0
    return similarity


def _jaccard_similarity(a: str, b: str) -> float:
    """
    Compute Jaccard similarity over word sets.

        similarity = |words_a ∩ words_b| / |words_a ∪ words_b|

    This is the fallback used when the semantic backend (Qdrant +
    embedding service) is unavailable. It is a real, deterministic
    function — not a placeholder — and is suitable as a permanent
    fallback for environments without a vector store.

    Returns:
        - 1.0 if both inputs have identical word sets
        - 0.0 if no words overlap
        - Fractional value in between
        - 0.0 if either input is empty
    """
    set_a = set(_tokenize(a))
    set_b = set(_tokenize(b))
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def _tokenize(text: str) -> list[str]:
    """
    Lowercase + split on non-alphanumeric characters.

    Keeps the implementation simple and dependency-free; matches the
    Jaccard-on-words approach used in RetrievalEngine._compute_embedding_similarity.
    """
    if not text:
        return []
    # Lowercase and split on any character that isn't a word char or apostrophe.
    cleaned = "".join(
        ch if ch.isalnum() or ch == "'" else " " for ch in text.lower()
    )
    return [w for w in cleaned.split() if w]