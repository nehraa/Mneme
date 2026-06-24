#!/usr/bin/env python3
"""
fix_tags.py — Re-tag heuristic chunks and clean up messy tags.

Modes:
  --mode heuristic  Chunks with source=heuristic (and only bare-token extras)
  --mode cleanup    Chunks with bare tokens, rate_limit duplication, or
                    heuristic + extra canonical tag
  --mode all        Any chunk that needs re-tagging

For each selected chunk, calls MiniMax (via MiniMaxTaggerClient) with the
canonical vocabulary, then composes the final tag list:
  - drop source=heuristic
  - drop bare tokens (no category=)
  - prefer LLM's outcome over the original outcome
  - dedup
  - cap at MAX_TAGS_PER_CHUNK

SAFETY (same as alter_tags.py / enrich_tags.py):
  1. --dry-run is the default; --apply required to write.
  2. Atomic write: backup → tmp → os.replace.
  3. Backup written BEFORE any destructive step.
  4. Print summary: read N, selected S, modified M.

Usage:
  python scripts/fix_tags.py --mode heuristic --limit 5
  python scripts/fix_tags.py --mode all --limit 100 --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

_cfg = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
for _k, _v in _cfg.items():
    if _v is not None:
        os.environ.setdefault(_k, _v)

# Ensure project root on sys.path so `src.*` resolves when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingestion.llm_client import MiniMaxClient  # noqa: E402
from src.tagging.vocabulary import get_vocabulary  # noqa: E402

# Reuse the proven parser + resolver from the enrichment pilot. They strip
# M2.7's  think blocks and collapse synonyms via TagCategory.resolve().
from scripts.enrich_tags import (  # noqa: E402
    HTTP_TIMEOUT_SECONDS,
    MAX_CONTENT_CHARS,
    MAX_TOKENS,
    MAX_TOKENS_RETRY,
    parse_tags_from_response,
    resolve_through_vocabulary,
)

logger = logging.getLogger(__name__)


# ── Paths (constants — no magic paths) ────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_PATH = DATA_DIR / "skill_chunks.jsonl"
BACKUP_PATH = DATA_DIR / "skill_chunks.jsonl.fix_tags_bak"
TMP_PATH = DATA_DIR / "skill_chunks.jsonl.fix_tags_new"


# ── Tagging constants ─────────────────────────────────────────────────────────

MAX_TAGS_PER_CHUNK = 8
HEURISTIC_FINGERPRINT = frozenset({"outcome=work_done", "source=heuristic"})


# ── Pure helpers (covered by tests) ──────────────────────────────────────────


def is_heuristic_chunk(chunk: dict) -> bool:
    """True iff tags are exactly the placeholder fingerprint."""
    return set(chunk.get("tags", [])) == set(HEURISTIC_FINGERPRINT)


def is_bare_token(tag: str) -> bool:
    """True if `tag` has no category= prefix and is at least 2 chars."""
    if not tag:
        return False
    if len(tag.strip()) < 2:
        return False
    return "=" not in tag


def has_rate_limit_duplication(tags: list[str]) -> bool:
    """True if any tag carries a 429 indicator (sign of messy rate-limit tagging)."""
    return any("429" in t for t in tags)


def _extras_outside_fingerprint(tags: list[str]) -> list[str]:
    return [t for t in tags if t not in HEURISTIC_FINGERPRINT]


def needs_heuristic_fix(chunk: dict) -> bool:
    """source=heuristic present, and any extras are bare tokens only."""
    tags = chunk.get("tags", [])
    if "source=heuristic" not in tags:
        return False
    extras = _extras_outside_fingerprint(tags)
    return all(is_bare_token(t) for t in extras)


def needs_cleanup(chunk: dict) -> bool:
    """Bare tokens, 429 duplication, or heuristic + extra canonical tag."""
    tags = chunk.get("tags", [])
    if any(is_bare_token(t) for t in tags):
        return True
    if has_rate_limit_duplication(tags):
        return True
    if "source=heuristic" in tags:
        extras = _extras_outside_fingerprint(tags)
        if any(not is_bare_token(t) for t in extras):
            return True
    return False


def select_chunks(chunks: list[dict], mode: str) -> list[dict]:
    if mode == "heuristic":
        return [c for c in chunks if needs_heuristic_fix(c)]
    if mode == "cleanup":
        return [c for c in chunks if needs_cleanup(c)]
    if mode == "all":
        return [c for c in chunks if needs_heuristic_fix(c) or needs_cleanup(c)]
    raise ValueError(f"Unknown mode: {mode!r}")


def compose_final_tags(llm_tags: list[str], original: list[str]) -> list[str]:
    """Merge LLM tags with original, applying cleanup rules.

    - drop bare tokens from LLM output
    - drop source=heuristic from original
    - prefer LLM's outcome over original
    - dedup exact matches, then dedup by category (keep first occurrence)
    - cap at MAX_TAGS_PER_CHUNK
    """
    clean_llm = [t.strip() for t in llm_tags if t and not is_bare_token(t)]
    clean_orig = [t for t in original if t != "source=heuristic"]

    if any(t.startswith("outcome=") for t in clean_llm):
        clean_orig = [t for t in clean_orig if not t.startswith("outcome=")]

    seen_strings: set[str] = set()
    seen_categories: set[str] = set()
    combined: list[str] = []
    for tag in (clean_llm + clean_orig):
        if tag in seen_strings:
            continue
        category = tag.split("=", 1)[0] if "=" in tag else None
        if category and category in seen_categories:
            continue
        if category:
            seen_categories.add(category)
        seen_strings.add(tag)
        combined.append(tag)

    return combined[:MAX_TAGS_PER_CHUNK]


def atomic_write(chunks: list[dict], original_text: str | None = None) -> None:
    """Backup → write tmp → os.replace."""
    # ponytail: original_text is accepted for future "preserve verbatim lines"
    # parity with alter_tags.py; current implementation rewrites every chunk.
    del original_text

    if DATA_PATH.exists():
        BACKUP_PATH.write_bytes(DATA_PATH.read_bytes())

    output = "\n".join(json.dumps(c) for c in chunks)
    if output and not output.endswith("\n"):
        output += "\n"

    TMP_PATH.write_text(output)
    os.replace(TMP_PATH, DATA_PATH)


# ── LLM re-tagging ────────────────────────────────────────────────────────────


class RateLimitHit(Exception):
    """MiniMax returned HTTP 429. The run stops cleanly; resume later."""


def _vocab_dict() -> dict:
    vocab = get_vocabulary()
    return {
        name: {
            "canonical": list(info.canonical_values),
            "synonyms": dict(info.synonyms),
        }
        for name, info in vocab.items()
    }


def retag_chunk(
    client: MiniMaxClient,
    chunk: dict,
    vocabulary: dict,
) -> tuple[list[str] | None, list[tuple[str, str, str]], dict]:
    """Call MiniMax for vocabulary-aware tags.

    Returns (final_tags, new_tag_records, usage). If the LLM returns no
    parseable tags, final_tags is None so the caller leaves the chunk alone.
    usage is the MiniMax usage dict from the call(s) made (prompt_tokens,
    completion_tokens, total_tokens).
    """
    content = (chunk.get("content") or "")[:MAX_CONTENT_CHARS]
    if not content.strip():
        return None, [], {}

    original = list(chunk.get("tags", []))
    outcome_tag = next(
        (t for t in original if t.startswith("outcome=")),
        "outcome=work_done",
    )

    prompt = _build_prompt(vocabulary, content)
    raw, usage = _call_minimax_with_usage(client, prompt)
    raw_tags = parse_tags_from_response(raw)
    if not raw_tags:
        return None, [], usage

    normalized, new_records = resolve_through_vocabulary(raw_tags, vocabulary)
    if not normalized:
        return None, [], usage

    final = compose_final_tags(normalized, original)
    # Preserve outcome if LLM didn't supply one.
    if outcome_tag and not any(t.startswith("outcome=") for t in final):
        final.append(outcome_tag)
    final = final[:MAX_TAGS_PER_CHUNK]
    return final, new_records, usage


# ponytail: inlined call_minimax_with_retry from scripts.enrich_tags so we
# can capture the API usage dict per request. The proven retry-and-parse
# logic is unchanged; only the return type grew from `str` to
# `(str, usage_dict)`.
def _call_minimax_with_usage(
    client: MiniMaxClient,
    prompt: str,
) -> tuple[str, dict]:
    """Call MiniMax with retry; also return total usage across attempts."""
    url = f"{client._base_url}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {client._api_key}",
        "Content-Type": "application/json",
    }

    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def _post(payload):
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as http:
            response = http.post(url, headers=headers, json=payload)
        if response.status_code == 429:
            raise RateLimitHit(f"MiniMax rate limit (429): {response.text[:200]}")
        response.raise_for_status()
        return response.json()

    payload = {
        "model": client._model,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = _post(payload)
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    for k in total_usage:
        total_usage[k] += usage.get(k, 0)

    if parse_tags_from_response(text):
        return text, total_usage

    # Truncation recovery — bigger budget.
    payload["max_tokens"] = MAX_TOKENS_RETRY
    data = _post(payload)
    retry_usage = data.get("usage", {})
    for k in total_usage:
        total_usage[k] += retry_usage.get(k, 0)

    return data["choices"][0]["message"]["content"], total_usage


def _build_prompt(vocabulary: dict, content: str) -> str:
    """Vocabulary-aware prompt — same shape as enrich_tags.build_prompt."""
    tool_vals = ", ".join(vocabulary["tool"].canonical_values)
    outcome_vals = ", ".join(vocabulary["outcome"].canonical_values)
    error_vals = ", ".join(vocabulary["error"].canonical_values)
    language_vals = ", ".join(vocabulary["language"].canonical_values)

    return f"""You are tagging a chunk of text from a developer's local AI/RAG memory.

Existing canonical vocabulary (prefer these where they fit):

tool: {tool_vals}
outcome: {outcome_vals}
error: {error_vals}
language: {language_vals}

For each chunk:
1. Pick canonical tags from the lists above if they fit.
2. If no canonical fits a concept, create a new tag using `category=value` format (lowercase snake_case).
3. Aim for 5-10 tags total. Cap at {MAX_TAGS_PER_CHUNK}.
4. Skip tags you can't justify.

Format: comma-separated tags, one line, no markdown. Example:
tool=auth, language=py, error=timeout

Chunk:
{content}"""


# ── CLI ───────────────────────────────────────────────────────────────────────


def _load_chunks(path: Path) -> list[dict]:
    chunks: list[dict] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            chunks.append(json.loads(line))
        except json.JSONDecodeError as exc:
            logger.warning("[FIX_TAGS] Skipping malformed line: %s", exc)
    return chunks


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        force=True,
    )

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--mode", choices=["heuristic", "cleanup", "all"], default="heuristic",
    )
    ap.add_argument("--limit", type=int, default=0, help="Max chunks to process (0 = no limit)")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="Seconds between MiniMax calls (default 0 = no artificial delay)")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.apply:
        args.dry_run = False

    if not DATA_PATH.exists():
        print(f"ERROR: {DATA_PATH} not found")
        return 1

    print(f"Loading {DATA_PATH}...")
    chunks = _load_chunks(DATA_PATH)
    print(f"  Loaded {len(chunks):,} chunks")

    selected = select_chunks(chunks, args.mode)
    if args.limit > 0:
        selected = selected[: args.limit]
    print(f"Selected {len(selected):,} chunks (mode={args.mode})")

    if not selected:
        print("Nothing to do.")
        return 0

    if args.dry_run:
        print()
        print("First 5 selected chunks:")
        for c in selected[:5]:
            cid = c.get("chunk_id", "?")[:40]
            print(f"  {cid}: tags={c.get('tags', [])}")
        print()
        print("=" * 60)
        print("DRY RUN - pass --apply to actually re-tag")
        return 0

    print("Connecting to MiniMax...")
    try:
        client = MiniMaxClient()
    except Exception as exc:
        print(f"ERROR: MiniMax client init failed: {exc}")
        return 1
    print(f"  model={client._model} base={client._base_url}")

    vocabulary = get_vocabulary()

    import time
    modified = 0
    skipped = 0
    new_records_total = 0
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    api_calls = 0
    retries = 0
    started = time.time()
    rate_limited = False

    for i, chunk in enumerate(selected, 1):
        old_tags = list(chunk.get("tags", []))
        try:
            new_tags, new_records, usage = retag_chunk(client, chunk, vocabulary)
        except RateLimitHit as exc:
            logger.error(
                "[FIX_TAGS] Rate limit hit at chunk %d/%d (%s). "
                "Saving progress and stopping — rerun after 1h to resume.",
                i, len(selected), exc,
            )
            rate_limited = True
            break
        except Exception as exc:
            logger.warning("[FIX_TAGS] %s: LLM call failed (%s)", chunk.get("chunk_id"), exc)
            skipped += 1
            continue

        for k in total_usage:
            total_usage[k] += usage.get(k, 0)
        # Count calls: first call always happens, retry if total > MAX_TOKENS.
        api_calls += 1
        if usage.get("completion_tokens", 0) >= MAX_TOKENS - 10:
            retries += 1

        if new_tags is None or new_tags == old_tags:
            skipped += 1
        else:
            chunk["tags"] = new_tags
            modified += 1
            new_records_total += len(new_records)

        if args.delay > 0:
            time.sleep(args.delay)
        if i % 10 == 0 or i == len(selected):
            elapsed = time.time() - started
            rate = i / elapsed if elapsed > 0 else 0
            logger.info(
                "[FIX_TAGS] %d/%d processed, %d modified, %d skipped, "
                "tokens=%d (prompt=%d completion=%d) calls=%d retries=%d "
                "rate=%.2f/s",
                i, len(selected), modified, skipped,
                total_usage["total_tokens"], total_usage["prompt_tokens"],
                total_usage["completion_tokens"], api_calls, retries, rate,
            )

    elapsed = time.time() - started
    logger.info(
        "[FIX_TAGS] Done: modified=%d skipped=%d new_tag_proposals=%d "
        "tokens=%d (prompt=%d completion=%d) calls=%d retries=%d elapsed=%.1fs "
        "rate_limited=%s",
        modified, skipped, new_records_total,
        total_usage["total_tokens"], total_usage["prompt_tokens"],
        total_usage["completion_tokens"], api_calls, retries, elapsed,
        rate_limited,
    )

    if modified > 0:
        print()
        print(f"Writing atomically to {DATA_PATH}...")
        print(f"  Backup: {BACKUP_PATH}")
        atomic_write(chunks)
    else:
        print()
        print("No chunks modified — no write performed.")

    print()
    print("=" * 60)
    print(f"DONE: modified={modified} skipped={skipped} new_proposals={new_records_total}")
    print(f"TOKEN USAGE: total={total_usage['total_tokens']:,} "
          f"(prompt={total_usage['prompt_tokens']:,} completion={total_usage['completion_tokens']:,})")
    print(f"API CALLS: {api_calls} (retries={retries}) elapsed={elapsed:.1f}s "
          f"rate={(api_calls / elapsed if elapsed > 0 else 0):.2f}/s")
    if rate_limited:
        remaining = len(selected) - modified - skipped
        print(f"RATE LIMITED at chunk {modified + skipped}. "
              f"{remaining} chunks still heuristic. Re-run after 1h.")
        return 2  # distinct exit code so wrapper scripts can detect rate-limit
    return 0


if __name__ == "__main__":
    sys.exit(main())
