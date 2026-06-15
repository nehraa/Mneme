"""
Graph Index — cross-chunk relationship traversal via Neo4j.
Real implementation: uses Neo4j driver for graph traversal queries.
Mock is in mock_graph.py.
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

    [MOCK] Currently uses MockGraphIndex. Pass use_mock=False to use
    the real Neo4j-backed implementation.
    """

    def __init__(
        self,
        repository: MemoryRepository | None = None,
        use_mock: bool = False,
    ) -> None:
        self._repo = repository
        self._use_mock = use_mock

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
        if self._use_mock:
            from src.graph.mock_graph import MockGraphIndex

            return MockGraphIndex().get_related(chunk_id=chunk_id, depth=depth)

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
        if self._use_mock:
            from src.graph.mock_graph import MockGraphIndex

            return MockGraphIndex().get_chains(chunk_id=chunk_id, depth=depth)

        # Real Neo4j implementation:
        # Uses Cypher variable-length path pattern:
        # MATCH (start {chunk_id: $chunk_id})-[*1..$depth]-(related)
        # RETURN relationships between each hop
        #
        # from neo4j import GraphDatabase
        # with GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)) as driver:
        #     with driver.session() as session:
        #         result = session.run("""
        #             MATCH path = (c {chunk_id: $chunk_id})-[*1..$depth]-(related)
        #             WHERE c.chunk_id = $chunk_id
        #             UNWIND relationships(path) AS rel
        #             RETURN DISTINCT related.chunk_id AS chunk_id,
        #                    type(rel) AS rel_type,
        #                    rel.reason AS reason
        #         """, chunk_id=chunk_id, depth=depth)
        #         ...
        #
        # For now, fall back to iterative depth traversal via repo
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
