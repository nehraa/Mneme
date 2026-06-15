"""
[MOCK] Ingestion Pipeline — orchestrates file reading + LLM chunking + store writes.
Real implementation: this IS the real pipeline. Mock is in mock_ingestion.py.
"""
from __future__ import annotations

import glob
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.memory_store import MemoryRepository


class IngestionPipeline:
    """
    Orchestrates the full ingestion flow:

    1. Read files from disk (glob patterns)
    2. Send each file's content to LLM for chunking
    3. Store resulting chunks in MemoryRepository
    4. Store cross-chunk relationships as graph edges

    [MOCK] Currently uses MiniMaxClient (real API). Pass use_mock=True to use
    the mock pipeline instead.
    """

    def __init__(
        self,
        repository: MemoryRepository | None = None,
        use_mock: bool = False,
    ) -> None:
        self._repo = repository
        self._use_mock = use_mock

    def run(
        self,
        file_paths: list[str],
        session_id: str,
        project_root: str,
    ) -> dict[str, Any]:
        """
        Run the full ingestion pipeline on a list of file paths / glob patterns.

        Returns an ingestion manifest dict.
        """
        from src.ingestion.mock_ingestion import MockIngestionPipeline

        if self._use_mock:
            return MockIngestionPipeline().run(file_paths, session_id, project_root)

        # Collect all files
        all_files: list[tuple[str, str]] = []  # (file_path, content)
        for pattern in file_paths:
            for path in glob.glob(pattern, recursive=True):
                p = Path(path)
                if p.is_file() and not p.name.startswith("."):
                    try:
                        content = p.read_text(encoding="utf-8", errors="replace")
                        all_files.append((str(p), content))
                    except Exception:
                        pass

        total_chunks = 0
        total_edges = 0
        all_chunk_ids: list[str] = []

        from src.ingestion.llm_client import MiniMaxClient

        client = MiniMaxClient()

        for file_path, content in all_files:
            if not content.strip():
                continue

            try:
                result = client.chunk_content(content, file_path=file_path)
            except Exception as e:
                # Log and continue — don't fail entire pipeline for one bad file
                import structlog

                logger = structlog.get_logger()
                logger.warning("llm_chunking_failed", file=file_path, error=str(e))
                continue

            # Store each chunk
            for chunk_data in result.chunks:
                chunk_id = chunk_data["chunk_id"]
                all_chunk_ids.append(chunk_id)

                self._repo.create_chunk(
                    {
                        "chunk_id": chunk_id,
                        "session_id": session_id,
                        "project_root": project_root,
                        "content": chunk_data["content"],
                        "page_order": chunk_data.get("page_order", 0),
                        "tags": chunk_data.get("tags", []),
                        "source_file": chunk_data.get("source_file", file_path),
                        "outcome_tag": self._extract_outcome_tag(
                            chunk_data.get("tags", [])
                        ),
                        "linked_chunks": [
                            r["target_chunk_id"]
                            for r in result.cross_chunk_relationships
                            if r["source_chunk_id"] == chunk_id
                        ],
                    }
                )
                total_chunks += 1

            # Store graph edges
            for edge_data in result.cross_chunk_relationships:
                self._repo.create_edge(
                    {
                        "source_chunk_id": edge_data["source_chunk_id"],
                        "target_chunk_id": edge_data["target_chunk_id"],
                        "relationship_type": edge_data["relationship_type"],
                        "reason": edge_data.get("reason", ""),
                    }
                )
                total_edges += 1

        # Build tag tree summary
        from collections import Counter

        tag_counter: Counter[str] = Counter()
        for chunk_id in all_chunk_ids:
            chunk = self._repo.get_chunk(chunk_id)
            if chunk:
                for tag in chunk.get("tags", []):
                    tag_counter[tag] += 1

        tag_tree: dict[str, dict[str, int]] = {}
        for tag, count in tag_counter.items():
            if "=" in tag:
                key, val = tag.split("=", 1)
                if key not in tag_tree:
                    tag_tree[key] = {}
                tag_tree[key][val] = count

        return {
            "chunks_created": total_chunks,
            "edges_created": total_edges,
            "session_id": session_id,
            "files_processed": len(all_files),
            "tag_tree_summary": tag_tree,
            "chunks": [
                {
                    "id": cid,
                    "tags": self._repo.get_chunk(cid).get("tags", [])
                    if self._repo.get_chunk(cid)
                    else [],
                }
                for cid in all_chunk_ids
            ],
        }

    @staticmethod
    def _extract_outcome_tag(tags: list[str]) -> str:
        for tag in tags:
            if tag.startswith("outcome="):
                return tag.split("=", 1)[1]
        return "work_done"
