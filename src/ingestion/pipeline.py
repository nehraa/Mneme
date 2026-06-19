"""
Ingestion Pipeline — orchestrates file reading + LLM chunking + store writes.
"""
from __future__ import annotations

import glob
import structlog
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.memory_store import MemoryRepository

logger = structlog.get_logger(__name__)


def _cosine(v1: list[float], v2: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = sum(a * a for a in v1) ** 0.5
    norm2 = sum(b * b for b in v2) ** 0.5
    return dot / (norm1 * norm2 + 1e-10)


class IngestionPipeline:
    """
    Orchestrates the full ingestion flow:

    1. Read files from disk (glob patterns)
    2. Send each file's content to LLM for chunking
    3. Store resulting chunks in MemoryRepository
    4. Store cross-chunk relationships as graph edges
    5. Embed all chunks via Ollama (or Gemini) and link near-duplicates
       with DUPLICATE edges in Neo4j when cosine similarity >= threshold.
    """

    # Minimum cosine similarity to consider two chunks "the same content".
    # 0.95 = very high fidelity; 0.90 = more aggressive deduplication.
    CHUNK_LINK_THRESHOLD = 0.95

    def __init__(
        self,
        repository: MemoryRepository | None = None,
    ) -> None:
        self._repo = repository

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
        # Collect all files
        all_files: list[tuple[str, str]] = []  # (file_path, content)
        for pattern in file_paths:
            # Auto-expand directory paths to a recursive glob so users can
            # pass a folder directly and get every file at any depth.
            if Path(pattern).is_dir():
                pattern = str(Path(pattern) / "**" / "*")
            for path in glob.glob(pattern, recursive=True):
                p = Path(path)
                if p.is_file() and not p.name.startswith("."):
                    try:
                        content = p.read_text(encoding="utf-8", errors="replace")
                        all_files.append((str(p), content))
                    except Exception as exc:
                        logger.warning("file_read_failed", file=str(p), error=str(exc))

        total_chunks = 0
        total_edges = 0
        dup_edges = 0  # count of DUPLICATE edges created by embedding similarity
        all_chunk_ids: list[str] = []
        # Accumulate chunk_id → (content, file_path) for embedding + linking
        chunk_content_map: dict[str, tuple[str, str]] = {}

        if all_files:
            from src.ingestion.llm_client import MiniMaxClient

            client = MiniMaxClient()

        for file_path, content in all_files:
            if not content.strip():
                continue

            try:
                result = client.chunk_content(content, file_path=file_path)
            except Exception as e:
                # Log and continue — don't fail entire pipeline for one bad file
                logger.warning("llm_chunking_failed", file=file_path, error=str(e))
                continue

            # Store each chunk
            for chunk_data in result.chunks:
                chunk_id = chunk_data["chunk_id"]
                all_chunk_ids.append(chunk_id)
                chunk_content_map[chunk_id] = (chunk_data["content"], file_path)

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

        # ── Ollama embedding + near-duplicate chunk linking ─────────────────
        if chunk_content_map and self._repo is not None:
            from src.config import get_config

            cfg = get_config()
            embedding_provider = cfg.llm.embedding_provider

            if embedding_provider == "ollama":
                from src.retrieval.ollama_embeddings import OllamaEmbeddingClient

                embed_client: Any = OllamaEmbeddingClient()
            else:
                # Default to Gemini; gracefully skip if API key not set
                try:
                    from src.retrieval.gemini_embeddings import GeminiEmbeddingClient

                    embed_client = GeminiEmbeddingClient()
                except RuntimeError:
                    embed_client = None

            if embed_client is not None:
                # Embed all chunk contents
                chunk_ids = list(chunk_content_map.keys())
                texts = [chunk_content_map[cid][0] for cid in chunk_ids]
                try:
                    embeddings = embed_client.embed_batch(texts)
                except Exception as exc:
                    logger.warning("ollama_embedding_batch_failed", error=str(exc))
                    embeddings = []

                if embeddings:
                    # Pairwise cosine similarity — flag near-duplicates
                    dup_edges = 0
                    for i in range(len(embeddings)):
                        for j in range(i + 1, len(embeddings)):
                            sim = _cosine(embeddings[i], embeddings[j])
                            if sim >= self.CHUNK_LINK_THRESHOLD:
                                try:
                                    self._repo.create_edge(
                                        {
                                            "source_chunk_id": chunk_ids[i],
                                            "target_chunk_id": chunk_ids[j],
                                            "relationship_type": "duplicate",
                                            "reason": (
                                                f"Content similarity={sim:.4f} "
                                                f"(threshold={self.CHUNK_LINK_THRESHOLD})"
                                            ),
                                        }
                                    )
                                    dup_edges += 1
                                except Exception as exc:
                                    logger.warning(
                                        "duplicate_edge_create_failed",
                                        src=chunk_ids[i],
                                        tgt=chunk_ids[j],
                                        error=str(exc),
                                    )
                    total_edges += dup_edges
                    if dup_edges > 0:
                        logger.info(
                            "chunk_linking_complete",
                            total_chunks=len(embeddings),
                            duplicate_links=dup_edges,
                            threshold=self.CHUNK_LINK_THRESHOLD,
                        )

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
            "duplicate_links": dup_edges,
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
