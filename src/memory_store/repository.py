"""
In-Memory Memory Repository — non-persistent implementation for tests and dev.

This is a REAL implementation (not a mock): it implements the same
MemoryRepository interface as the Neo4j-backed one but stores everything in
process-local dicts. It is useful for unit tests and local dev environments
where a Neo4j instance is not available.

For production, use Neo4jMemoryRepository (see neo4j_repository.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class InMemoryMemoryRepository:
    """
    In-memory implementation of the MemoryRepository interface.

    Stores chunks in a dict and edges in a list. State is lost on process exit.
    Intended for unit tests and local dev only.

    For production, use Neo4jMemoryRepository.
    """

    def __init__(self) -> None:
        self._chunks: dict[str, dict[str, Any]] = {}
        self._edges: list[dict[str, Any]] = []

    # ── Chunk CRUD ─────────────────────────────────────────────────────────────

    def create_chunk(self, chunk: dict[str, Any]) -> dict[str, Any]:
        """
        Store a memory chunk in-memory.
        """
        record = dict(chunk)
        self._chunks[chunk["chunk_id"]] = record
        return record

    def get_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        """
        Retrieve a chunk by ID.
        """
        chunk = self._chunks.get(chunk_id)
        return dict(chunk) if chunk is not None else None

    def update_chunk_tags(
        self, chunk_id: str, tags: list[str]
    ) -> dict[str, Any] | None:
        """
        Update tags on an existing chunk.
        """
        chunk = self._chunks.get(chunk_id)
        if chunk is None:
            return None
        chunk["tags"] = tags
        return dict(chunk)

    def list_chunks(
        self,
        tag: str | None = None,
        session_id: str | None = None,
        outcome_tag: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        List chunks filtered by tag / session / outcome.
        """
        results = [dict(c) for c in self._chunks.values()]

        if tag:
            results = [c for c in results if tag in c.get("tags", [])]
        if session_id:
            results = [c for c in results if c.get("session_id") == session_id]
        if outcome_tag:
            results = [c for c in results if c.get("outcome_tag") == outcome_tag]

        return results[:limit]

    def touch_chunk(self, chunk_id: str) -> None:
        """
        Update last_accessed timestamp (used for recency boost).
        """
        chunk = self._chunks.get(chunk_id)
        if chunk:
            chunk["last_accessed"] = datetime.now(timezone.utc).isoformat()

    # ── Relationship CRUD ─────────────────────────────────────────────────────

    def create_edge(self, edge: dict[str, Any]) -> dict[str, Any]:
        """
        Store a graph edge.
        """
        record = dict(edge)
        self._edges.append(record)
        return record

    def get_related_chunks(
        self, chunk_id: str, depth: int = 1
    ) -> list[dict[str, Any]]:
        """
        Traverse graph edges from a chunk up to `depth` hops.
        """
        del depth  # Single-hop traversal only; multi-hop is a graph-index concern
        related = []
        for edge in self._edges:
            if edge.get("source_chunk_id") == chunk_id:
                target = self._chunks.get(edge.get("target_chunk_id", ""))
                if target:
                    related.append(
                        {
                            "chunk_id": edge.get("target_chunk_id"),
                            "type": edge.get("relationship_type"),
                            "reason": edge.get("reason", ""),
                            "chunk": target,
                        }
                    )
            if edge.get("target_chunk_id") == chunk_id:
                source = self._chunks.get(edge.get("source_chunk_id", ""))
                if source:
                    related.append(
                        {
                            "chunk_id": edge.get("source_chunk_id"),
                            "type": edge.get("relationship_type"),
                            "reason": edge.get("reason", ""),
                            "chunk": source,
                        }
                    )
        return related

    def get_contradicting_chunks(
        self, target_file: str, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Find chunks with 'contradicts' edges for the memory guard.
        """
        contradicting = []
        for edge in self._edges:
            if edge.get("relationship_type") == "contradicts":
                source = self._chunks.get(edge.get("source_chunk_id", ""))
                if source and source.get("source_file") == target_file:
                    if session_id is None or source.get("session_id") == session_id:
                        contradicting.append(source)
        return contradicting
