"""
[MOCK] Mock Ingestion Pipeline — returns deterministic mock data.
Phase 2 is "done" when this mock fires AND the real MiniMax path is documented.
Real implementation → ingestion/pipeline.py::IngestionPipeline.run()
"""
from __future__ import annotations

import glob
from typing import Any


class MockIngestionPipeline:
    """
    [MOCK] Returns a deterministic mock ingestion manifest.
    Used for Phase 2 verification without calling the real LLM.
    """

    def run(
        self,
        file_paths: list[str],
        session_id: str,
        project_root: str,
    ) -> dict[str, Any]:
        """
        [MOCK] Return a deterministic mock manifest.
        Real implementation → ingestion/pipeline.py::IngestionPipeline.run()
        """
        # Count actual files that would be matched
        file_count = 0
        for pattern in file_paths:
            file_count += len(glob.glob(pattern, recursive=True))

        return {
            "_mock": True,
            "chunks_created": 47,
            "edges_created": 12,
            "session_id": session_id,
            "files_processed": file_count,
            "tag_tree_summary": {
                "tool=auth": {
                    "failed": 5,
                    "successfully_called": 31,
                    "no_tool_called": 11,
                },
                "tool=db": {
                    "failed": 2,
                    "successfully_called": 8,
                    "stopped": 1,
                },
                "outcome": {
                    "work_done": 8,
                    "failed": 7,
                    "successfully_called": 39,
                    "no_tool_called": 11,
                    "stopped": 1,
                },
            },
            "chunks": [
                {
                    "id": "c001",
                    "content": "[MOCK] Auth flow failed at token_refresh — error: token_expired",
                    "tags": ["tool=auth", "outcome=failed", "error=token_expired"],
                    "linked_chunks": ["c003", "c007"],
                    "page_order": 3,
                },
                {
                    "id": "c002",
                    "content": "[MOCK] DB connection pool initialized successfully",
                    "tags": ["tool=db", "outcome=successfully_called"],
                    "linked_chunks": [],
                    "page_order": 0,
                },
            ],
            "_implementation_note": (
                "Real: ingestion/pipeline.py::IngestionPipeline.run() — "
                "calls MiniMax-Text-01 via ingestion/llm_client.py::MiniMaxClient.chunk_content()"
            ),
        }
