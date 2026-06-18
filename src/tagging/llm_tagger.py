"""
LLM-based dynamic tagging — uses MiniMax API to generate tags intelligently.

The LLM sees:
  - The chunk content
  - The canonical vocabulary (existing tags and their meanings)
  - The user's existing tags (if any)

The LLM then decides which tags to apply. It can:
  - Use existing canonical tags (preferred)
  - Create NEW tags when nothing existing fits
  - Refuse to create duplicates when an existing tag already covers the meaning

This is the "logistically correct" approach: the LLM understands semantics,
so "JWT auth", "OAuth flow", "session login" all get the same `tool=auth`
instead of creating three different tags.

Why LLM and not keyword lists:
  - Keyword lists miss synonyms and word forms
  - Hardcoded lists don't adapt to new domains
  - Keyword lists can produce duplicates (e.g., "logged" → tool=log vs
    "login" → tool=auth when both mean authentication)

Why we still have a vocabulary:
  - Defines the CANONICAL form of each tag
  - Prevents "authneticate" → tool=authneticate (typo consolidation)
  - Lets users extend via tag_vocabulary.yaml without code changes
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

import httpx

from src.tagging.vocabulary import get_vocabulary


logger = logging.getLogger(__name__)


# ── MiniMax client (lightweight, only for tagging) ───────────────────────────


class MiniMaxTaggerClient:
    """Minimal MiniMax client just for the tagging task.

    Reuses the same MiniMax-Text-01 / OpenAI-compatible API as
    src.ingestion.llm_client, but with a tagging-specific prompt.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.minimax.io",
        model: str = "MiniMax-Text-01",
        timeout: float = 30.0,
    ):
        # Read API key from parameter, then env var, then fail loudly
        env_key = os.environ.get("MINIMAX_API_KEY")
        if api_key:
            self._api_key = api_key
        elif env_key:
            self._api_key = env_key
        else:
            raise RuntimeError(
                "[MINIMAX] API key not found. "
                "Set MINIMAX_API_KEY environment variable or pass api_key=..."
            )
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def generate_tags(
        self,
        content: str,
        vocabulary: dict[str, Any],
        existing_tags: list[str] | None = None,
        max_tags: int = 10,
    ) -> list[str]:
        """
        Ask the LLM to generate tags for the given content.

        Args:
            content: The chunk text to tag.
            vocabulary: The canonical vocabulary (from get_vocabulary()).
            existing_tags: Tags the user already provided (preserve these).
            max_tags: Maximum number of tags to generate.

        Returns:
            List of canonical tag strings (e.g., ["tool=auth", "outcome=failed"]).

        Raises:
            RuntimeError: If the LLM call fails.
        """
        system_prompt = self._build_system_prompt(vocabulary)
        user_prompt = self._build_user_prompt(content, existing_tags or [], max_tags)

        url = f"{self._base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "max_tokens": 200,
            "temperature": 0.0,  # deterministic for consistent tagging
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"[MINIMAX] Tagging request failed (HTTP {exc.response.status_code}): "
                f"{exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"[MINIMAX] Tagging request failed: {exc}") from exc

        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()

        # Parse JSON from response (handle markdown fences)
        return self._parse_tags_response(text)

    def _build_system_prompt(self, vocabulary: dict[str, Any]) -> str:
        """Build the system prompt that explains the vocabulary to the LLM."""
        # Format vocabulary as a YAML-like reference for the LLM
        vocab_lines = ["# Tag Vocabulary", ""]
        for category, info in vocabulary.items():
            canonical = info.canonical_values if hasattr(info, "canonical_values") else info.get("canonical", [])
            synonyms = info.synonyms if hasattr(info, "synonyms") else info.get("synonyms", {})
            vocab_lines.append(f"## {category}")
            vocab_lines.append(f"canonical: {', '.join(canonical)}")
            if synonyms:
                syn_str = ", ".join(f"{k}={v}" for k, v in synonyms.items())
                vocab_lines.append(f"synonyms: {syn_str}")
            vocab_lines.append("")

        vocab_text = "\n".join(vocab_lines)

        return f"""You are a tag generation assistant for a memory system.

Your job: analyze the user's content and return tags that classify it.

CRITICAL RULES:
1. Use the EXISTING canonical vocabulary below. If a tag fits, use it.
2. DO NOT create duplicate tags. If `tool=auth` already exists for "authentication",
   use it — don't create `tool=authentication` or `tool=login`.
3. Only create NEW tags when nothing in the vocabulary fits.
4. Tags use `category=value` format. Categories: tool, outcome, error, language, file.
5. Return ONLY valid JSON: {{"tags": ["tag1", "tag2", ...]}}

{vocab_text}

GUIDANCE:
- "auth", "OAuth", "JWT", "login", "session", "authentication" → tool=auth
- "postgres", "MySQL", "Redis", "Prisma" → tool=db
- "HTTP", "REST API", "GraphQL" → tool=http
- "failed", "broken", "didn't work", "exception" → outcome=failed
- "timeout", "401", "rejected" → error tags
- ALWAYS prefer canonical tags over creating new synonyms."""

    def _build_user_prompt(
        self,
        content: str,
        existing_tags: list[str],
        max_tags: int,
    ) -> str:
        """Build the user prompt with content and context."""
        existing_str = (
            f"\n\nCaller-provided tags (KEEP THESE): {', '.join(existing_tags)}"
            if existing_tags else ""
        )

        return f"""Tag the following content. Return at most {max_tags} tags.

CONTENT:
{content[:2000]}
{existing_str}

Return JSON only."""

    def _parse_tags_response(self, text: str) -> list[str]:
        """Parse the LLM's JSON response."""
        # Try to extract JSON from markdown fences
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            text = match.group(1)

        # Find any JSON object
        match = re.search(r"\{[\s\S]+\}", text)
        if match:
            text = match.group(0)

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"[MINIMAX] Failed to parse tags response: {text[:200]}")
            return []

        tags = parsed.get("tags", [])
        if not isinstance(tags, list):
            logger.warning(f"[MINIMAX] Tags field is not a list: {tags}")
            return []

        # Validate each tag has category=value format
        valid_tags = []
        for tag in tags:
            if isinstance(tag, str) and "=" in tag:
                valid_tags.append(tag.strip())

        return valid_tags


# ── Main tag inference ───────────────────────────────────────────────────────


def infer_tags_with_llm(
    content: str,
    source_file: str | None = None,
    existing_tags: list[str] | None = None,
) -> list[str]:
    """
    Generate tags using the MiniMax LLM.

    Falls back to pattern extraction if:
    - LLM client can't initialize (no API key)
    - LLM call fails
    - Response can't be parsed

    Returns a list of tags. Caller tags are preserved and added to the result.
    """
    try:
        client = MiniMaxTaggerClient()
        vocab = get_vocabulary()
        # Convert TagCategory objects to dicts for the prompt
        vocab_dict = {
            name: {
                "canonical": list(info.canonical_values),
                "synonyms": dict(info.synonyms),
            }
            for name, info in vocab.items()
        }
        llm_tags = client.generate_tags(
            content=content,
            vocabulary=vocab_dict,
            existing_tags=existing_tags,
        )
    except Exception as exc:
        logger.warning(
            f"[TAGGING] LLM tagging failed ({exc}), falling back to patterns"
        )
        # Fallback: just return existing tags + simple file tags
        fallback = list(existing_tags or [])
        if source_file:
            ext = source_file.rsplit(".", 1)[-1].lower() if "." in source_file else ""
            if ext:
                fallback.append(f"language={ext}")
        return list(dict.fromkeys(fallback))

    # Add file tags from source_file (the LLM doesn't see the filename well)
    if source_file:
        ext = source_file.rsplit(".", 1)[-1].lower() if "." in source_file else ""
        if ext:
            llm_tags.append(f"language={ext}")
        basename = source_file.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if basename:
            llm_tags.append(f"file={basename}")

    # Normalize through vocabulary (consolidates any synonyms the LLM created)
    from src.tagging.infer import normalize_tags

    return normalize_tags(llm_tags)


def infer_tags_pattern_fallback(
    content: str,
    source_file: str | None = None,
    existing_tags: list[str] | None = None,
) -> list[str]:
    """
    Pattern-based tag inference — used as fallback when LLM is unavailable.

    ONLY extracts tags from explicit pattern markers in content:
      - "Tool: auth" → tool=auth
      - "Outcome: failed" → outcome=failed
      - "Error: timeout" → error=timeout

    This is intentionally MINIMAL — no keyword lists, no if-statements.
    Use the LLM for proper tagging.
    """
    from src.tagging.infer import normalize_tags

    raw: list[str] = []

    if existing_tags:
        raw.extend(t for t in existing_tags if t and t.strip())

    if content:
        # Only explicit pattern markers — no keyword matching
        pattern = re.compile(
            r"(?:^|[\n,;])\s*(tool|outcome|error|file|tag)[\s:]+([a-zA-Z0-9_\-\.]+)",
            re.IGNORECASE | re.MULTILINE,
        )
        for match in pattern.finditer(content):
            category = match.group(1).lower()
            value = match.group(2).lower()
            if category == "tag":
                raw.append(value)
            else:
                raw.append(f"{category}={value}")

    if source_file:
        ext = source_file.rsplit(".", 1)[-1].lower() if "." in source_file else ""
        if ext:
            raw.append(f"language={ext}")
        basename = source_file.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if basename:
            raw.append(f"file={basename}")

    return normalize_tags(raw)
