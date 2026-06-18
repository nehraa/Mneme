"""
Qdrant vector search integration.

Provides real semantic search via dense embeddings stored in Qdrant,
replacing the Jaccard-on-tags placeholder in RetrievalEngine.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
)

logger = logging.getLogger(__name__)

# Cosine similarity score threshold below which results are discarded.
DEFAULT_SCORE_THRESHOLD = 0.0


def _chunk_id_to_uuid(chunk_id: str) -> uuid.UUID:
    """
    Convert a string chunk_id to a deterministic UUID.

    Qdrant accepts UUIDs or unsigned integers as point IDs, not arbitrary
    strings. This function hashes the chunk_id into a v5 UUID to keep
    the mapping deterministic (same chunk_id → same UUID every time).
    """
    return uuid.uuid5(uuid.NAMESPACE_DNS, chunk_id)


class QdrantSearch:
    """
    Real vector search against a Qdrant collection.

    Uses cosine distance as the similarity metric, suitable for
    normalized embedding models (e.g., Gemini, MiniLM).

    The collection is created automatically on first connection if it
    does not already exist.
    """

    def __init__(
        self,
        host: str,
        collection: str,
        vector_size: int,
        timeout: float = 30.0,
        ensure_collection: bool = True,
    ) -> None:
        """
        Connect to Qdrant and ensure the collection exists.

        Args:
            host: Qdrant HTTP endpoint (e.g. "http://localhost:6333").
            collection: Name of the Qdrant collection to use.
            vector_size: Embedding dimension (must match the embedding model).
            timeout: Request timeout in seconds.
            ensure_collection: If True (default), create the collection on first
                               connection. If False, skip collection creation —
                               useful for health-check-only instances.
        """
        self._host = host
        self._collection = collection
        self._vector_size = vector_size
        self._client = QdrantClient(
            url=host,
            timeout=timeout,
            check_compatibility=False,  # allow minor version mismatch with server
        )
        if ensure_collection:
            self._ensure_collection()

    # ── Public API ───────────────────────────────────────────────────────────

    def upsert_chunk(
        self,
        chunk_id: str,
        content: str,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> None:
        """
        Index (or re-index) a chunk with its embedding.

        Idempotent: calling this with the same ``chunk_id`` overwrites
        the previously stored point.

        Args:
            chunk_id: Unique identifier for the chunk.
            content: Text content of the chunk.
            embedding: Dense vector of shape (vector_size,).
            metadata: Arbitrary payload to store alongside the vector.
        """
        point = PointStruct(
            id=_chunk_id_to_uuid(chunk_id),
            vector=embedding,
            payload={
                "chunk_id": chunk_id,  # original string ID stored in payload
                "content": content,
                **metadata,
            },
        )
        self._client.upsert(
            collection_name=self._collection,
            points=[point],
        )

    def search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        score_threshold: float = DEFAULT_SCORE_THRESHOLD,
        filter_conditions: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Vector similarity search.

        Args:
            query_embedding: Query vector of shape (vector_size,).
            limit: Maximum number of results to return.
            score_threshold: Minimum cosine similarity score (default 0.0).
            filter_conditions: Optional Qdrant filter dict. Supported keys:
                - ``session_id``: str — exact match on session_id field
                - ``tags``: list[str] — must contain all listed tags
                - ``outcome_tag``: str — exact match on outcome_tag field
                - ``min_score``: float — minimum similarity score

        Returns:
            List of dicts, each with ``chunk_id``, ``score``, and ``payload``.
            Ordered by descending score.
        """
        qdrant_filter = self._build_filter(filter_conditions)

        response = self._client.query_points(
            collection_name=self._collection,
            query=query_embedding,
            limit=limit,
            score_threshold=score_threshold if score_threshold > 0 else None,
            query_filter=qdrant_filter,
            with_payload=True,
        )

        return [
            {
                # chunk_id is stored in the payload since the Qdrant point ID
                # is a deterministic UUID derived from the chunk_id string
                "chunk_id": hit.payload.get("chunk_id", str(hit.id)),
                "score": hit.score,
                "payload": hit.payload,
            }
            for hit in response.points
        ]

    def delete_chunk(self, chunk_id: str) -> None:
        """
        Remove a chunk from the Qdrant index.

        Args:
            chunk_id: ID of the chunk to delete. No-op if the chunk
                      does not exist.
        """
        self._client.delete(
            collection_name=self._collection,
            points_selector=[_chunk_id_to_uuid(chunk_id)],
        )

    def health_check(self) -> bool:
        """
        Verify Qdrant is reachable and the collection exists.

        Returns:
            True if healthy, False otherwise.
        """
        try:
            return self._client.collection_exists(collection_name=self._collection)
        except Exception as exc:
            logger.warning("Qdrant health check failed: %s", exc)
            return False

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    # ── Internals ────────────────────────────────────────────────────────────

    def _ensure_collection(self) -> None:
        """Create the collection if it does not already exist."""
        try:
            collections = self._client.get_collections().collections
            collection_names = [c.name for c in collections]
        except Exception as exc:
            logger.warning("Failed to list Qdrant collections: %s", exc)
            collection_names = []

        if self._collection in collection_names:
            return

        try:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config={
                    "": {  # default vector config
                        "size": self._vector_size,
                        "distance": Distance.COSINE,
                    },
                },
            )
            logger.info("Created Qdrant collection: %s", self._collection)
        except Exception as exc:
            # Another process may have created it concurrently — log and continue.
            logger.warning(
                "Failed to create Qdrant collection %s (may already exist): %s",
                self._collection, exc,
            )

    def _build_filter(
        self, conditions: dict[str, Any] | None
    ) -> Filter | None:
        """
        Convert a dict of filter conditions into a Qdrant Filter object.

        Returns None when no conditions are provided.
        """
        if not conditions:
            return None

        must_clauses: list[FieldCondition] = []

        session_id = conditions.get("session_id")
        if session_id is not None:
            must_clauses.append(
                FieldCondition(
                    key="session_id",
                    match=MatchValue(value=session_id),
                )
            )

        outcome_tag = conditions.get("outcome_tag")
        if outcome_tag is not None:
            must_clauses.append(
                FieldCondition(
                    key="outcome_tag",
                    match=MatchValue(value=outcome_tag),
                )
            )

        # Tags filter: all listed tags must be present in the payload's tags array.
        tags = conditions.get("tags")
        if tags:
            for tag in tags:
                must_clauses.append(
                    FieldCondition(
                        key="tags",
                        match=MatchValue(value=tag),
                    )
                )

        if not must_clauses:
            return None

        return Filter(must=must_clauses)
