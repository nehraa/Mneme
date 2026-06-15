"""
[MOCK] Memory Repository — Phase 1 mock implementation.
Real implementation → memory_store/repository.py::Neo4jMemoryRepository
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class MockMemoryRepository:
    """
    [MOCK] In-memory mock for Phase 1 verification.
    Stores chunks in a dict. Real backend is Neo4j.

    Phase 1 is "done" when:
    - All CRUD endpoints return correct JSON schema
    - All mock records have _mock: True
    - Real Neo4j path is documented in each method docstring
    """

    def __init__(self) -> None:
        self._chunks: dict[str, dict[str, Any]] = {}
        self._edges: list[dict[str, Any]] = []

    # ── Chunk CRUD ─────────────────────────────────────────────────────────────

    def create_chunk(self, chunk: dict[str, Any]) -> dict[str, Any]:
        """
        [MOCK] Store a memory chunk in-memory.
        Real implementation → Neo4jMemoryRepository.create_chunk()
        """
        record = {**chunk, "_mock": True}
        self._chunks[chunk["chunk_id"]] = record
        return record

    def get_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        """
        [MOCK] Retrieve a chunk by ID.
        Real implementation → Neo4jMemoryRepository.get_chunk()
        """
        return self._chunks.get(chunk_id)

    def update_chunk_tags(
        self, chunk_id: str, tags: list[str]
    ) -> dict[str, Any] | None:
        """
        [MOCK] Update tags on an existing chunk.
        Real implementation → Neo4jMemoryRepository.update_chunk_tags()
        """
        chunk = self._chunks.get(chunk_id)
        if chunk is None:
            return None
        chunk["tags"] = tags
        return chunk

    def list_chunks(
        self,
        tag: str | None = None,
        session_id: str | None = None,
        outcome_tag: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        [MOCK] List chunks filtered by tag / session / outcome.
        Real implementation → Neo4jMemoryRepository.list_chunks()
        """
        results = list(self._chunks.values())

        if tag:
            results = [c for c in results if tag in c.get("tags", [])]
        if session_id:
            results = [c for c in results if c.get("session_id") == session_id]
        if outcome_tag:
            results = [c for c in results if c.get("outcome_tag") == outcome_tag]

        return results[:limit]

    def touch_chunk(self, chunk_id: str) -> None:
        """
        [MOCK] Update last_accessed timestamp (used for recency boost).
        Real implementation → Neo4jMemoryRepository.touch_chunk()
        """
        chunk = self._chunks.get(chunk_id)
        if chunk:
            chunk["last_accessed"] = datetime.now(timezone.utc).isoformat()

    # ── Relationship CRUD ─────────────────────────────────────────────────────

    def create_edge(self, edge: dict[str, Any]) -> dict[str, Any]:
        """
        [MOCK] Store a graph edge.
        Real implementation → Neo4jMemoryRepository.create_edge()
        """
        record = {**edge, "_mock": True}
        self._edges.append(record)
        return record

    def get_related_chunks(
        self, chunk_id: str, depth: int = 1
    ) -> list[dict[str, Any]]:
        """
        [MOCK] Traverse graph edges from a chunk.
        Real implementation → Neo4jMemoryRepository.get_related_chunks()
        """
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
        [MOCK] Find chunks with 'contradicts' edges for the memory guard.
        Real implementation → Neo4jMemoryRepository.get_contradicting_chunks()
        """
        contradicting = []
        for edge in self._edges:
            if edge.get("relationship_type") == "contradicts":
                source = self._chunks.get(edge.get("source_chunk_id", ""))
                if source and source.get("source_file") == target_file:
                    if session_id is None or source.get("session_id") == session_id:
                        contradicting.append(source)
        return contradicting


# ── Alias for the interface ───────────────────────────────────────────────────
MemoryRepository = MockMemoryRepository
