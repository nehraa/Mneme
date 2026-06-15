"""
[MOCK] MiniMax LLM Client — for Phase 2 chunking.
Real implementation: sends requests to MiniMax-Text-01 via OpenAI-compatible /v1/chat/completions.
Real path: this file IS the real implementation — the [MOCK] label is on the call site in pipeline.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx


# ── Chunking prompt ─────────────────────────────────────────────────────────


CHUNKING_SYSTEM_PROMPT = """You are a code analysis and chunking assistant.
Your job is to analyze code and divide it into semantic chunks.

For each chunk, you must determine:
1. **chunk_id**: a short unique ID like "c001", "c002"
2. **content**: the actual code/text content of this chunk (keep it coherent — don't split mid-function)
3. **page_order**: position in the original file (0-indexed)
4. **tags**: flat tag list. MUST include exactly one "outcome=..." tag from this set:
   - outcome=work_done        — completed successfully, no issues
   - outcome=no_tool_called  — code path with no tool/function call
   - outcome=successfully_called — a function/tool call that succeeded
   - outcome=failed          — a function/tool call that failed or errored
   - outcome=stopped         — execution was stopped mid-flow
   Also add descriptive tags like: tool=<name>, error=<error_type>, file=<type>
5. **source_file**: the file path this chunk came from
6. **analysis_notes**: your reasoning for why you chunked here and what the relationships are

Then identify relationships BETWEEN chunks:
7. **cross_chunk_relationships**: list of {source_id, target_id, relationship_type, reason}
   relationship_type is one of: same_tool_call | prerequisite | follows | contradicts | same_concept

Return your response as a JSON object with this exact shape:
{
  "chunks": [
    {
      "chunk_id": "c001",
      "content": "...",
      "page_order": 0,
      "tags": ["outcome=successfully_called", "tool=auth"],
      "source_file": "src/auth.py",
      "analysis_notes": "..."
    }
  ],
  "cross_chunk_relationships": [
    {
      "source_chunk_id": "c001",
      "target_chunk_id": "c003",
      "relationship_type": "prerequisite",
      "reason": "c001 sets up the auth token that c003 uses"
    }
  ]
}

IMPORTANT: Return ONLY valid JSON. No markdown fences. No explanation outside the JSON."""


@dataclass
class ChunkingResult:
    """Structured result from the LLM chunking call."""

    chunks: list[dict[str, Any]]
    cross_chunk_relationships: list[dict[str, Any]]

    def to_manifest(self) -> dict[str, Any]:
        """Convert to the ingestion manifest format used by the store."""
        tag_tree: dict[str, dict[str, int]] = {}
        for chunk in self.chunks:
            for tag in chunk.get("tags", []):
                if "=" in tag:
                    key, val = tag.split("=", 1)
                    if key not in tag_tree:
                        tag_tree[key] = {}
                    tag_tree[key][val] = tag_tree[key].get(val, 0) + 1

        return {
            "chunks_created": len(self.chunks),
            "edges_created": len(self.cross_chunk_relationships),
            "tag_tree_summary": tag_tree,
            "chunks": [
                {
                    "id": c["chunk_id"],
                    "content": c["content"][:100] + "..." if len(c["content"]) > 100 else c["content"],
                    "tags": c["tags"],
                    "linked_chunks": [
                        r["target_chunk_id"]
                        for r in self.cross_chunk_relationships
                        if r["source_chunk_id"] == c["chunk_id"]
                    ],
                    "page_order": c["page_order"],
                }
                for c in self.chunks
            ],
        }


class MiniMaxClient:
    """
    [MOCK] MiniMax API client for LLM-assisted chunking.

    Uses MiniMax-Text-01 via OpenAI-compatible /v1/chat/completions endpoint.

    Usage:
        client = MiniMaxClient(api_key="sk-cp-...")
        result = client.chunk_content(code_text, file_path="src/auth.py")
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.minimax.io",
        model: str = "MiniMax-Text-01",
        timeout: float = 120.0,
    ) -> None:
        self._api_key = api_key or "sk-cp-KjobHFSNe1A5LaEtTY0qrBV5l85bitrDDWkjO4VEtsGd6h8uTnRmbcuEQflj1FXbUwFX2L9S1Qt5_M-dqpFnX7qMGg7GUtGTfYp5EJJ05MVyuLN7N5WWoyA"
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def chunk_content(
        self,
        content: str,
        file_path: str | None = None,
    ) -> ChunkingResult:
        """
        Send content to MiniMax-Text-01 for LLM-assisted chunking.

        Returns a ChunkingResult with chunks and cross-chunk relationships.

        [MOCK] This is the real implementation — the mock is in mock_ingestion.py
        """
        url = f"{self._base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "max_tokens": 4096,
            "messages": [
                {"role": "system", "content": CHUNKING_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Analyze and chunk this file{f': {file_path}' if file_path else ''}:\n\n```{content}```",
                },
            ],
        }

        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()

        data = response.json()
        text = data["choices"][0]["message"]["content"]

        # Parse JSON from the response
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code blocks
            import re
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if match:
                parsed = json.loads(match.group(1))
            else:
                # Last resort: try finding any {...} block
                match = re.search(r"(\{[\s\S]+\})", text)
                if match:
                    parsed = json.loads(match.group(1))
                else:
                    raise ValueError(f"Could not parse JSON from LLM response: {text[:200]}")

        return ChunkingResult(
            chunks=parsed.get("chunks", []),
            cross_chunk_relationships=parsed.get("cross_chunk_relationships", []),
        )
