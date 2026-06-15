"""
[MOCK] Mock Graph Index — returns deterministic mock relationships.
Phase 3 is "done" when this mock fires AND the real Neo4j path is documented.
Real implementation → graph/index.py::GraphIndex
"""
from __future__ import annotations

from typing import Any


class MockGraphIndex:
    """
    [MOCK] Returns deterministic mock relationships for Phase 3 verification.
    Uses the same interface as GraphIndex.
    """

    def get_related(
        self,
        chunk_id: str,
        depth: int = 1,
    ) -> dict[str, Any]:
        """
        [MOCK] Return mock related chunks for any chunk_id.
        Real implementation → graph/index.py::GraphIndex.get_related()
        """
        return {
            "_mock": True,
            "chunk_id": chunk_id,
            "relationships": [
                {
                    "chunk_id": "mem_007",
                    "type": "same_tool_call",
                    "reason": (
                        "Both deal with token refresh in auth flow — "
                        "same OAuth endpoint"
                    ),
                },
                {
                    "chunk_id": "mem_012",
                    "type": "prerequisite",
                    "reason": (
                        "mem_012 sets up the auth token that mem_001 "
                        "tried to use and failed"
                    ),
                },
            ],
            "_implementation_note": (
                "Real: graph/index.py::GraphIndex.get_related() — "
                "queries Neo4j for edges from this chunk_id"
            ),
        }

    def get_chains(
        self,
        chunk_id: str,
        depth: int = 3,
    ) -> dict[str, Any]:
        """
        [MOCK] Return mock multi-hop chains for any chunk_id.
        Real implementation → graph/index.py::GraphIndex.get_chains()
        """
        return {
            "_mock": True,
            "chunk_id": chunk_id,
            "depth": depth,
            "chains": [
                {
                    "path": ["mem_001", "mem_007", "mem_015"],
                    "relationship_types": ["same_tool_call", "prerequisite"],
                    "reason": "Auth retry chain: failed → fixed → verified",
                },
                {
                    "path": ["mem_001", "mem_012"],
                    "relationship_types": ["prerequisite"],
                    "reason": "mem_012 sets up the token that mem_001 consumed",
                },
            ],
            "_implementation_note": (
                "Real: graph/index.py::GraphIndex.get_chains() — "
                "Neo4j variable-length path traversal"
            ),
        }
