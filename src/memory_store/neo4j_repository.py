"""
Neo4j-backed Memory Repository — real implementation.

Schema:
  Nodes  : (c:Chunk {chunk_id, session_id, project_root, content, page_order,
                      outcome_tag, source_file, created_at, last_accessed, tags})
  Edges  : (a)-[:SAME_TOOL_CALL|PREREQUISITE|FOLLOWS|CONTRADICTS|SAME_CONCEPT]->(b)
            with property: reason :: string

All queries use parameterized Cypher — no string interpolation.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from neo4j import GraphDatabase
from neo4j.exceptions import DriverError, ServiceUnavailable

# Edge relationship type constants (Cypher relationship type names)
_REL_TYPE_SAME_TOOL_CALL = "SAME_TOOL_CALL"
_REL_TYPE_PREREQUISITE = "PREREQUISITE"
_REL_TYPE_FOLLOWS = "FOLLOWS"
_REL_TYPE_CONTRADICTS = "CONTRADICTS"
_REL_TYPE_SAME_CONCEPT = "SAME_CONCEPT"

# Map relationship_type enum value to Cypher relationship type name
_REL_TYPE_MAP: dict[str, str] = {
    "same_tool_call": _REL_TYPE_SAME_TOOL_CALL,
    "prerequisite": _REL_TYPE_PREREQUISITE,
    "follows": _REL_TYPE_FOLLOWS,
    "contradicts": _REL_TYPE_CONTRADICTS,
    "same_concept": _REL_TYPE_SAME_CONCEPT,
}

_SCHEMA_SETUP_CYPHER = """
CREATE CONSTRAINT chunk_id IF NOT EXISTS
FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE
"""


def _serialize_tag_tree(tag_tree: dict | str | None) -> str:
    """Serialize tag_tree to a JSON string for Neo4j storage."""
    if tag_tree is None:
        return "{}"
    if isinstance(tag_tree, str):
        return tag_tree
    return json.dumps(tag_tree)


def _deserialize_tag_tree(value: str | dict | None) -> dict[str, Any]:
    """Deserialize tag_tree from Neo4j back to a dict."""
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, dict):
        return value
    return {}


def _chunk_dict(record: Any) -> dict[str, Any]:
    """Convert a Neo4j record row (a Node) into a flat dict matching the API shape."""
    node = record["c"]
    return {
        "chunk_id": node["chunk_id"],
        "session_id": node.get("session_id"),
        "project_root": node.get("project_root", ""),
        "content": node.get("content", ""),
        "page_order": node.get("page_order", 0),
        "tags": node.get("tags", []),
        "tag_tree": _deserialize_tag_tree(node.get("tag_tree")),
        "linked_chunks": node.get("linked_chunks", []),
        "outcome_tag": node.get("outcome_tag", "work_done"),
        "source_file": node.get("source_file"),
        "created_at": node.get("created_at"),
        "last_accessed": node.get("last_accessed"),
    }


class Neo4jMemoryRepository:
    """
    Neo4j-backed memory repository.

    Provides the same interface as InMemoryMemoryRepository so existing code
    (server.py, graph/index.py, guard/diff_engine.py, etc.) works unchanged.

    Parameters
    ----------
    uri : str
        Bolt URI, e.g. "bolt://localhost:7687"
    user : str
        Neo4j username, e.g. "neo4j"
    password : str
        Neo4j password
    """

    def __init__(self, uri: str, user: str, password: str) -> None:
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._setup_schema()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _setup_schema(self) -> None:
        """Create constraints idempotently on startup."""
        with self._driver.session() as session:
            try:
                session.run(_SCHEMA_SETUP_CYPHER)
            except Exception as exc:
                # Constraint may already exist — that's fine
                pass

    def _run(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        """Execute a query in a session and return consumed records.

        The result is fully consumed inside the session block so that the caller
        receives plain dicts rather than a live Result object (which becomes
        invalid when its session closes).

        Raises
        ------
        RuntimeError
            If Neo4j is unreachable (wraps ServiceUnavailable).
        """
        try:
            with self._driver.session() as session:
                result = session.run(cypher, **params)
                return [dict(record) for record in result]
        except (ServiceUnavailable, DriverError) as exc:
            raise RuntimeError(
                f"Neo4j connection failed. Is Neo4j running at bolt://localhost:7687? "
                f"Original error: {exc}"
            ) from exc

    # ── Chunk CRUD ─────────────────────────────────────────────────────────────

    def create_chunk(self, chunk: dict[str, Any]) -> dict[str, Any]:
        """
        Create a new Chunk node in Neo4j.

        Parameters
        ----------
        chunk : dict
            Must contain: chunk_id, session_id, project_root, content, page_order,
            outcome_tag, source_file, created_at, tags, tag_tree, linked_chunks,
            last_accessed.

        Returns
        -------
        dict
            The created chunk record (same shape as input dict).
        """
        cypher = """
        MERGE (c:Chunk {chunk_id: $chunk_id})
        SET c.session_id    = $session_id,
            c.project_root  = $project_root,
            c.content       = $content,
            c.page_order    = $page_order,
            c.outcome_tag   = $outcome_tag,
            c.source_file   = $source_file,
            c.created_at    = $created_at,
            c.last_accessed = $last_accessed,
            c.tags          = $tags,
            c.tag_tree      = $tag_tree,
            c.linked_chunks = $linked_chunks
        RETURN c
        """
        records = self._run(
            cypher,
            chunk_id=chunk["chunk_id"],
            session_id=chunk.get("session_id", ""),
            project_root=chunk.get("project_root", ""),
            content=chunk.get("content", ""),
            page_order=chunk.get("page_order", 0),
            outcome_tag=chunk.get("outcome_tag", "work_done"),
            source_file=chunk.get("source_file"),
            created_at=chunk.get("created_at"),
            last_accessed=chunk.get("last_accessed"),
            tags=chunk.get("tags", []),
            tag_tree=_serialize_tag_tree(chunk.get("tag_tree")),
            linked_chunks=chunk.get("linked_chunks", []),
        )
        if not records:
            raise RuntimeError(f"Failed to create chunk {chunk['chunk_id']}")
        return _chunk_dict(records[0])

    def get_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        """
        Retrieve a single Chunk node by chunk_id.

        Parameters
        ----------
        chunk_id : str

        Returns
        -------
        dict | None
            The chunk dict, or None if not found.
        """
        cypher = "MATCH (c:Chunk {chunk_id: $chunk_id}) RETURN c"
        records = self._run(cypher, chunk_id=chunk_id)
        if not records:
            return None
        return _chunk_dict(records[0])

    def update_chunk_tags(self, chunk_id: str, tags: list[str]) -> dict[str, Any] | None:
        """
        Update the tags array on an existing Chunk.

        Parameters
        ----------
        chunk_id : str
        tags : list[str]

        Returns
        -------
        dict | None
            Updated chunk dict, or None if chunk not found.
        """
        cypher = """
        MATCH (c:Chunk {chunk_id: $chunk_id})
        SET c.tags = $tags
        RETURN c
        """
        records = self._run(cypher, chunk_id=chunk_id, tags=tags)
        if not records:
            return None
        return _chunk_dict(records[0])

    def list_chunks(
        self,
        tag: str | None = None,
        session_id: str | None = None,
        outcome_tag: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        List Chunk nodes with optional filters.

        Parameters
        ----------
        tag : str | None
            If provided, only chunks whose tags array contains this exact string.
        session_id : str | None
            If provided, only chunks with this session_id.
        outcome_tag : str | None
            If provided, only chunks with this outcome_tag.
        limit : int
            Maximum number of results (default 50).

        Returns
        -------
        list[dict]
        """
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": limit}

        if tag is not None:
            conditions.append("'$tag' IN c.tags")
            params["tag"] = tag
        if session_id is not None:
            conditions.append("c.session_id = $session_id")
            params["session_id"] = session_id
        if outcome_tag is not None:
            conditions.append("c.outcome_tag = $outcome_tag")
            params["outcome_tag"] = outcome_tag

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        cypher = f"""
        MATCH (c:Chunk)
        {where_clause}
        RETURN c
        ORDER BY c.created_at DESC
        LIMIT $limit
        """
        result = self._run(cypher, **params)
        return [_chunk_dict(record) for record in result]

    def touch_chunk(self, chunk_id: str) -> None:
        """
        Update last_accessed to now (UTC) for recency boosting.

        Parameters
        ----------
        chunk_id : str
        """
        cypher = """
        MATCH (c:Chunk {chunk_id: $chunk_id})
        SET c.last_accessed = $now
        """
        now = datetime.now(timezone.utc).isoformat()
        self._run(cypher, chunk_id=chunk_id, now=now)

    # ── Relationship CRUD ───────────────────────────────────────────────────────

    def create_edge(self, edge: dict[str, Any]) -> dict[str, Any]:
        """
        Create a directed edge between two Chunk nodes.

        Parameters
        ----------
        edge : dict
            Must contain: source_chunk_id, target_chunk_id, relationship_type,
            reason.

        Returns
        -------
        dict
            Edge record with source_chunk_id, target_chunk_id, relationship_type,
            reason (matching the API response shape).
        """
        rel_type_str = edge.get("relationship_type", "")
        rel_type_cypher = _REL_TYPE_MAP.get(rel_type_str, rel_type_str.upper())

        cypher = f"""
        MATCH (src:Chunk {{chunk_id: $source_chunk_id}})
        MATCH (tgt:Chunk {{chunk_id: $target_chunk_id}})
        OPTIONAL MATCH (src)-[r:{rel_type_cypher}]->(tgt)
        DELETE r
        CREATE (src)-[new_r:{rel_type_cypher} {{reason: $reason}}]->(tgt)
        RETURN src.chunk_id AS source_chunk_id,
               tgt.chunk_id AS target_chunk_id,
               $relationship_type AS relationship_type,
               $reason AS reason
        """
        records = self._run(
            cypher,
            source_chunk_id=edge["source_chunk_id"],
            target_chunk_id=edge["target_chunk_id"],
            relationship_type=rel_type_str,
            reason=edge.get("reason", ""),
        )
        if not records:
            raise RuntimeError(
                f"Failed to create edge {edge['source_chunk_id']} -> "
                f"{edge['target_chunk_id']} (relationship={rel_type_str})"
            )
        return dict(records[0])

    def get_related_chunks(
        self, chunk_id: str, depth: int = 1
    ) -> list[dict[str, Any]]:
        """
        Traverse graph edges from a given chunk.

        Parameters
        ----------
        chunk_id : str
        depth : int
            Maximum hop depth (default 1 = direct neighbours only).

        Returns
        -------
        list[dict]
            Each dict has: chunk_id, type (relationship type), reason, chunk
            (the related chunk dict).
        """
        cypher = f"""
        MATCH path = (start:Chunk {{chunk_id: $chunk_id}})
                    -[r:SAME_TOOL_CALL|PREREQUISITE|FOLLOWS|CONTRADICTS|SAME_CONCEPT*1..{depth}]->
                    (end:Chunk)
        UNWIND relationships(path) AS rel
        WITH start, end, rel
        RETURN end.chunk_id AS chunk_id,
               type(rel)    AS type,
               rel.reason   AS reason,
               end          AS chunk_node
        ORDER BY end.created_at DESC
        """
        result = self._run(cypher, chunk_id=chunk_id)
        related = []
        seen: set[str] = set()
        for record in result:
            cid = record["chunk_id"]
            if cid in seen:
                continue
            seen.add(cid)
            node = record["chunk_node"]
            raw_tag_tree = node.get("tag_tree", "{}")
            if isinstance(raw_tag_tree, str):
                related_tag_tree: dict[str, Any] = _deserialize_tag_tree(raw_tag_tree)
            related.append(
                {
                    "chunk_id": cid,
                    "type": record["type"].lower(),
                    "reason": record["reason"] or "",
                    "chunk": {
                        "chunk_id": node["chunk_id"],
                        "session_id": node.get("session_id"),
                        "project_root": node.get("project_root", ""),
                        "content": node.get("content", ""),
                        "page_order": node.get("page_order", 0),
                        "tags": node.get("tags", []),
                        "tag_tree": related_tag_tree,
                        "linked_chunks": node.get("linked_chunks", []),
                        "outcome_tag": node.get("outcome_tag", "work_done"),
                        "source_file": node.get("source_file"),
                        "created_at": node.get("created_at"),
                        "last_accessed": node.get("last_accessed"),
                    },
                }
            )
        return related

    def get_contradicting_chunks(
        self, target_file: str, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Find chunks linked by CONTRADICTS edges whose source_file == target_file.

        Used by the Memory Guard to warn before retrying a failed approach.

        Parameters
        ----------
        target_file : str
            The source_file path to match against.
        session_id : str | None
            Optional session filter.

        Returns
        -------
        list[dict]
            List of source chunk dicts.
        """
        conditions = ["c.source_file = $target_file"]
        params: dict[str, Any] = {"target_file": target_file}

        if session_id is not None:
            conditions.append("c.session_id = $session_id")
            params["session_id"] = session_id

        where_clause = "WHERE " + " AND ".join(conditions)

        cypher = f"""
        MATCH (c:Chunk)-[r:CONTRADICTS]->(:Chunk)
        {where_clause}
        RETURN DISTINCT c
        ORDER BY c.created_at DESC
        """
        result = self._run(cypher, **params)
        return [_chunk_dict(record) for record in result]

    # ── Lifecycle ───────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the driver connection (graceful shutdown)."""
        self._driver.close()
