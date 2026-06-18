"""
Graph Index — cross-chunk relationship traversal via the memory repository.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.memory_store import MemoryRepository


class GraphIndex:
    """
    Cross-chunk relationship traversal.

    Wraps MemoryRepository graph methods with:
    - get_related(chunk_id, depth): direct edges at given depth
    - get_chains(chunk_id, depth): multi-hop traversal chains
    """

    def __init__(
        self,
        repository: MemoryRepository | None = None,
    ) -> None:
        self._repo = repository

    def get_related(
        self,
        chunk_id: str,
        depth: int = 1,
    ) -> dict[str, Any]:
        """
        Get chunks related to this chunk_id up to `depth` hops.

        Returns a dict with:
          - chunk_id: the source chunk
          - relationships: list of {chunk_id, type, reason}
        """
        related = self._repo.get_related_chunks(chunk_id=chunk_id, depth=depth)
        return {
            "chunk_id": chunk_id,
            "relationships": [
                {
                    "chunk_id": r["chunk_id"],
                    "type": r["type"],
                    "reason": r["reason"],
                }
                for r in related
            ],
        }

    def get_chains(
        self,
        chunk_id: str,
        depth: int = 3,
    ) -> dict[str, Any]:
        """
        Get multi-hop traversal chains from this chunk_id.

        Returns a dict with:
          - chunk_id: the source chunk
          - depth: requested depth
          - chains: list of {path: [chunk_ids], relationship_types: [...], reason}
        """
        chains: list[dict[str, Any]] = []
        visited_paths: set[tuple[str, ...]] = set()

        def _traverse(current_id: str, remaining_depth: int, path: list[str]) -> None:
            if remaining_depth == 0:
                return
            related = self._repo.get_related_chunks(current_id, depth=1)
            for r in related:
                next_id = r["chunk_id"]
                if next_id in path:
                    continue
                new_path = path + [next_id]
                path_key = tuple(new_path)
                if path_key in visited_paths:
                    continue
                visited_paths.add(path_key)
                if len(new_path) > 1:
                    chains.append(
                        {
                            "path": new_path,
                            "relationship_types": [r["type"]],
                            "reason": r["reason"],
                        }
                    )
                if len(new_path) <= depth:
                    _traverse(next_id, remaining_depth - 1, new_path)

        _traverse(chunk_id, depth, [chunk_id])

        return {
            "chunk_id": chunk_id,
            "depth": depth,
            "chains": chains,
        }
