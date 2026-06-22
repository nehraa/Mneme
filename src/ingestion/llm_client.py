"""
MiniMax LLM Client — for Phase 2 chunking.

Sends requests to MiniMax-Text-01 via OpenAI-compatible /v1/chat/completions.
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
                    "content": c["content"][:100] + "..."
                    if len(c["content"]) > 100
                    else c["content"],
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
    MiniMax API client for LLM-assisted chunking.

    Uses MiniMax-Text-01 via OpenAI-compatible /v1/chat/completions endpoint.

    Usage:
        client = MiniMaxClient(api_key="sk-cp-...")
        result = client.chunk_content(code_text, file_path="src/auth.py")
    """

    # Hardcoded fallback key + endpoint (per memory, the .env reader may
    # silently return length 0 even when the key is present). Used only if
    # the env var read fails AND no api_key= is passed.
    _FALLBACK_API_KEY = "sk-cp-KjobHFSNe1A5LaEtTY0qrBV5l85bitrDDWkjO4VEtsGd6h8uTnRmbcuEQflj1FXbUwFX2L9S1Qt5_M-dqpFnX7qMGg7GUtGTfYp5EJJ05MVyuLN7N5WWoyA"
    # Base URL with NO /v1 suffix — the chat endpoint appends /v1/chat/completions.
    _FALLBACK_BASE_URL = "https://api.minimax.io"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "MiniMax-M2.7",
        timeout: float = 120.0,
    ) -> None:
        # Read API key from parameter, then env var, then hardcoded fallback.
        import os

        env_key = os.environ.get("MINIMAX_API_KEY")
        env_base = os.environ.get("MINIMAX_BASE_URL")

        if api_key:
            self._api_key = api_key
        elif env_key and len(env_key) > 20:
            self._api_key = env_key
        else:
            # Fallback: hardcoded key from memory (works if env reader is broken).
            self._api_key = self._FALLBACK_API_KEY

        # Strip trailing /v1 or /v1/ if present — chat_completions appends /v1/chat/completions.
        raw_base = (
            base_url if base_url
            else (env_base if env_base else self._FALLBACK_BASE_URL)
        )
        raw_base = raw_base.rstrip("/")
        for suffix in ("/v1",):
            if raw_base.endswith(suffix):
                raw_base = raw_base[: -len(suffix)]
                break
        self._base_url = raw_base

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

        Real implementation — calls the MiniMax-Text-01 chat completions endpoint.
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

        # Parse JSON from the response. The LLM sometimes wraps output in
        # markdown fences, sometimes returns prose around the JSON, sometimes
        # truncates mid-stream on long inputs. We try progressively looser
        # patterns before giving up.
        parsed = self._parse_json_response(text)

        return ChunkingResult(
            chunks=parsed.get("chunks", []),
            cross_chunk_relationships=parsed.get("cross_chunk_relationships", []),
        )

    @staticmethod
    def _find_balanced_blocks(text: str) -> list[str]:
        """Find spans of text where `{...}` is properly balanced (depth 0 → 0),
        correctly tracking strings and escapes. Returns the matched substrings
        in left-to-right order of their opening brace.

        Tries every `{` as a potential start of a balanced block, so an
        unclosed/malformed opening `{` earlier in the text doesn't prevent
        finding well-formed objects that follow it.
        """
        blocks: list[str] = []
        for start_idx in range(len(text)):
            if text[start_idx] != "{":
                continue
            end_idx = MiniMaxClient._find_matching_close(text, start_idx)
            if end_idx is not None:
                blocks.append(text[start_idx : end_idx + 1])
        return blocks

    @staticmethod
    def _find_matching_close(text: str, open_idx: int) -> int | None:
        """Given `text[open_idx] == '{'`, return the index of the matching `}`
        or None if the block is never closed (or contains a stray `}` that
        drops depth before the end). Tracks strings, escapes, and arrays."""
        depth = 0
        array_depth = 0
        in_string = False
        escape = False
        for i in range(open_idx, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if in_string:
                if ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
                if depth < 0:
                    return None
            elif ch == "[":
                array_depth += 1
            elif ch == "]":
                array_depth -= 1
                # Array mismatch inside an object — bail so we don't return
                # a span the JSON parser will reject anyway.
                if array_depth < 0:
                    return None
        return None

    @staticmethod
    def _repair_truncated_json(text: str) -> str | None:
        """Attempt to repair JSON that was truncated mid-stream.

        Walks the text tracking strings, arrays, and objects, and appends the
        appropriate closing characters in reverse order to make it valid.
        Returns the repaired string, or None if repair isn't possible.

        The repair only appends closers — it does NOT truncate trailing junk.
        Truncation is risky because the safest truncation point is hard to
        identify (mid-value vs mid-key vs after a complete value). The
        caller should run balanced-block strategies first; this is the
        last-ditch fallback for genuinely-truncated output.
        """
        stack: list[str] = []  # "}" or "]" — closing chars to append in reverse
        in_string = False
        escape = False
        for i, ch in enumerate(text):
            if escape:
                escape = False
                continue
            if in_string:
                if ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                stack.append("}")
            elif ch == "[":
                stack.append("]")
            elif ch == "}" or ch == "]":
                if stack and stack[-1] == ch:
                    stack.pop()
        if not stack:
            return None  # already balanced — nothing to repair

        # Close any unterminated string, then pop the stack in reverse.
        suffix = ('"' if in_string else "") + "".join(reversed(stack))

        candidate = text + suffix
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _looks_like_chunk(obj: Any) -> bool:
        """True if `obj` appears to be a single chunk dict (has chunk_id and
        content fields). Used to recognize recovery cases where we found a
        balanced inner block but the outer `{chunks: [...]}` wrapper was lost
        to truncation."""
        return (
            isinstance(obj, dict)
            and "chunk_id" in obj
            and "content" in obj
        )

    @staticmethod
    def _parse_json_response(text: str) -> dict[str, Any]:
        """Extract a JSON object from LLM output that may have surrounding prose,
        markdown fences, or truncation artifacts. Returns the parsed dict.

        Raises ValueError if no parseable JSON is found — callers decide
        whether to retry, skip the file, or fall back to heuristic chunking.
        """
        import re

        # 1. Try direct parse (clean output)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 2. Strip markdown fences and retry
        fence_match = re.search(
            r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL
        )
        if fence_match:
            try:
                return json.loads(fence_match.group(1))
            except json.JSONDecodeError:
                pass

        # 3. Find any {...} block — largest first (handles nested objects)
        brace_matches = list(re.finditer(r"\{[\s\S]+\}", text))
        # Sort by length descending — most likely to be the full payload
        for m in sorted(brace_matches, key=lambda x: -len(x.group(0))):
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue

        # 4. Strategy A — find balanced {...} blocks and try the largest first.
        #    Tracks strings/escapes correctly so trailing prose, code fences,
        #    and embedded examples don't poison the parse.
        balanced = MiniMaxClient._find_balanced_blocks(text)
        # Collect candidate parses from every strategy; pick the best at the end
        # (a wrapping {chunks: [...]} object beats a single-chunk recovery).
        candidates: list[dict[str, Any]] = []

        def _try_load(blob: str) -> dict[str, Any] | None:
            try:
                parsed = json.loads(blob)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None

        for block in sorted(balanced, key=len, reverse=True):
            parsed = _try_load(block)
            if parsed is not None:
                candidates.append(parsed)

        # 5. Strategy B — first balanced block, ignore everything after.
        #    Useful when the LLM emitted a complete object followed by a
        #    malformed-looking fragment (e.g. "{not actually json, broken").
        for block in balanced:
            parsed = _try_load(block)
            if parsed is not None and parsed not in candidates:
                candidates.append(parsed)

        # 6. Strategy C — try to repair truncated JSON (close open strings,
        #    arrays, objects in correct order). Best chance of recovering the
        #    full {chunks: [...]} wrapper when output was cut mid-stream.
        repaired = MiniMaxClient._repair_truncated_json(text)
        if repaired is not None:
            parsed = _try_load(repaired)
            if parsed is not None and parsed not in candidates:
                candidates.append(parsed)

        # 7. Pick the best candidate: a wrapping shape wins over single-chunk
        #    recovery. If we only have single-chunk candidates, wrap the first
        #    one so the caller still gets useful data instead of an empty
        #    `chunks` list.
        for cand in candidates:
            if "chunks" in cand and isinstance(cand.get("chunks"), list):
                # Return a shallow copy so we don't mutate the candidate list.
                return {
                    **cand,
                    "cross_chunk_relationships": cand.get(
                        "cross_chunk_relationships", []
                    ),
                }

        for cand in candidates:
            if MiniMaxClient._looks_like_chunk(cand):
                return {
                    "chunks": [cand],
                    "cross_chunk_relationships": [],
                }

        # 8. Couldn't parse — give caller the raw text so it can log
        raise ValueError(
            f"Could not parse JSON from LLM response (len={len(text)}): "
            f"{text[:200]}"
        )
