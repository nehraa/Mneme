"""
Dynamic tag inference — LLM-based with pattern-extraction fallback.

Strategy:
1. PRIMARY: Call MiniMax API to generate tags. The LLM sees the canonical
   vocabulary + content and decides what tags apply. This is the
   "logistically correct" path — the LLM understands semantics and
   consolidates synonyms.
2. FALLBACK: When LLM is unavailable (no API key, network error, etc.),
   fall back to pattern extraction. Pattern extraction only recognizes
   explicit "Tool: auth" style markers — NO keyword lists, NO if/else.

The canonical vocabulary (see `vocabulary.py`) is the single source of
truth for what tags MEAN. Tags are normalized through it so that
"tool=authentication" and "tool=auth" consolidate to the same tag.

This module is intentionally MINIMAL — the heavy lifting is in
`llm_tagger.py` and `vocabulary.py`.
"""
from __future__ import annotations

import logging
from typing import Iterable

from src.tagging.vocabulary import get_vocabulary


logger = logging.getLogger(__name__)


# ── Tag normalization ────────────────────────────────────────────────────────


def normalize_tag(tag: str) -> str:
    """
    Normalize a tag to its canonical form via the vocabulary.

    Examples:
        "tool=auth" → "tool=auth"  (already canonical)
        "tool=authentication" → "tool=auth"  (synonym → canonical)
        "tool=prisma" → "tool=db"  (prisma synonym → db canonical)
        "tool=weird_unknown_tool" → "tool=other"  (fallback)
        "outcome=didnt_work" → "outcome=failed"  (synonym)
        "language=python" → "language=py"  (synonym)
    """
    if not tag or "=" not in tag:
        return tag

    category, _, value = tag.partition("=")
    vocab = get_vocabulary()

    if category in vocab:
        return f"{category}={vocab[category].resolve(value)}"

    # Unknown category — preserve as-is (user-defined)
    return tag


def normalize_tags(tags: Iterable[str]) -> list[str]:
    """Normalize a list of tags through the vocabulary. Dedupes preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        if not tag or not tag.strip():
            continue
        normalized = normalize_tag(tag.strip())
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


# ── Tag inference ───────────────────────────────────────────────────────────


def infer_tags(
    content: str,
    source_file: str | None = None,
    existing_tags: Iterable[str] | None = None,
    use_llm: bool = True,
) -> list[str]:
    """
    Infer tags for a chunk.

    PRIMARY (use_llm=True, default):
        Calls the MiniMax LLM with the canonical vocabulary. The LLM:
        - Sees existing tags and the vocabulary
        - Decides which tags to apply (canonical preferred)
        - Creates NEW tags only when nothing existing fits
        - Consolidates synonyms (e.g., "auth", "OAuth", "JWT" → tool=auth)

    FALLBACK (use_llm=False OR LLM fails):
        Pattern extraction only. Recognizes "Tool: auth" style markers.
        NO keyword lists, NO if/else matching.

    Returns: list of normalized, deduplicated tags.

    Args:
        content: Chunk text to analyze.
        source_file: Optional source file path (drives language/file tags).
        existing_tags: Tags the caller already provided (always preserved).
        use_llm: If True (default), use the LLM. If False, use pattern fallback.
    """
    existing_list = list(existing_tags) if existing_tags else []

    if use_llm:
        try:
            from src.tagging.llm_tagger import infer_tags_with_llm
            return infer_tags_with_llm(
                content=content,
                source_file=source_file,
                existing_tags=existing_list,
            )
        except Exception as exc:
            logger.warning(
                "[TAGGING] LLM inference failed (%s), falling back to patterns",
                exc,
            )
            # Fall through to pattern fallback

    # Pattern fallback (or explicit use_llm=False)
    from src.tagging.llm_tagger import infer_tags_pattern_fallback
    return infer_tags_pattern_fallback(
        content=content,
        source_file=source_file,
        existing_tags=existing_list,
    )


def merge_tags(*tag_sets: Iterable[str]) -> list[str]:
    """Combine multiple tag sources, normalizing and deduplicating."""
    all_tags: list[str] = []
    for tag_set in tag_sets:
        all_tags.extend(tag_set)
    return normalize_tags(all_tags)
