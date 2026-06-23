#!/usr/bin/env python3
"""
enrich_tags.py — Re-tag heuristic-only chunks via MiniMax (vocabulary-aware).

MNEME plan 2A (pilot v2): the previous pilot at commit 185aa36 used a
free-form prompt and the LLM invented tags like `claude-code`,
`hermes-agent`, `openclaw` — concepts the project already has a structured
canonical vocabulary for in `tag_vocabulary.yaml` / `src/tagging/vocabulary.py`.

This v2 makes the prompt vocabulary-aware: the LLM sees the canonical
values for each category (tool, outcome, error, language) and either
picks from those or creates a new tag explicitly with `category=value`
format. Output is then normalized through `TagCategory.resolve()` so any
synonyms the LLM still emits (e.g. `tool=authentication`) collapse onto
the canonical form (e.g. `tool=auth`).

NEW-TAG TRACKING: any tag whose value is NOT in the canonical list (after
synonym resolution) is recorded in `data/new_tags_proposed.jsonl` so the
user can review and add them to `tag_vocabulary.yaml` later. The
`category=value` format is enforced so a vocabulary editor knows exactly
where to file the new tag.

SAFETY RULES (same as v1):
1. Default mode is --dry-run; real writes need explicit --apply.
2. Atomic write: read all → write to .tmp.<pid> → fsync → os.replace.
3. Backup to data/skill_chunks.jsonl.enrich_pilot_bak BEFORE writing.
4. Log every failure with chunk_id; never silently skip.
5. Print a final summary with sample of new tags for quality spot-check.

Usage:
  # Dry run (default) — show what would happen
  python scripts/enrich_tags.py --limit 5

  # Pilot apply: enrich 200 heuristic chunks
  python scripts/enrich_tags.py --limit 200 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import dotenv_values

# Load .env (workaround for known reader bug)
_cfg = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
for _k, _v in _cfg.items():
    if _v is not None:
        os.environ.setdefault(_k, _v)

import httpx  # noqa: E402

# Ensure project root is on sys.path so we can import src.tagging.* and
# the MiniMax client.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.ingestion.llm_client import MiniMaxClient  # noqa: E402
from src.tagging.vocabulary import get_vocabulary  # noqa: E402

# ── Constants (no magic numbers) ─────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DATA_PATH = DATA_DIR / "skill_chunks.jsonl"
BACKUP_PATH = DATA_DIR / "skill_chunks.jsonl.enrich_pilot_bak"
NEW_TAGS_SIDECAR = DATA_DIR / "new_tags_proposed.jsonl"

# Heuristic tag fingerprint we're replacing.
HEURISTIC_TAG_SET = {"outcome=work_done", "source=heuristic"}

# Default sleep between MiniMax calls. 0.3s is gentle on rate limits but
# still gets ~3.3 chunks/sec; pilot of 200 finishes in ~60s of sleep alone.
DEFAULT_DELAY_SECONDS = 0.3

# Max tokens for the chat completion. M2.7 emits <think>...</think> blocks
# that consume most of the budget — we measured up to 700 reasoning tokens
# before the actual tag list comes out. 1500 leaves headroom for the answer
# even on chunks that make the model think hardest.
MAX_TOKENS = 1500
# Retry budget when the first call was truncated and produced no parseable
# tags. We don't always need this, but it's the only way to recover the
# ~10% of cases where the model thinks hardest.
MAX_TOKENS_RETRY = 3000

# How much of each chunk to send. Tag suggestions don't need the full
# content, and prompt tokens are the bulk of the cost.
MAX_CONTENT_CHARS = 1500

# Per-call HTTP timeout.
HTTP_TIMEOUT_SECONDS = 60.0

# How many example tags to show at the end.
SAMPLE_OUTPUT_SIZE = 10

# Cap on tags per chunk. Used inside the prompt AND enforced post-parse:
# after `resolve_through_vocabulary` we trim to this many to prevent
# over-tagging (M2.7 occasionally emits 25+ tags for chatty chunks).
MAX_TAGS_PER_CHUNK = 15


# ── Prompt construction ──────────────────────────────────────────────────────


def build_prompt(vocabulary: dict, content: str) -> str:
    """Build the vocabulary-aware tagging prompt.

    The LLM sees:
      - The canonical values for each category (tool, outcome, error, language).
      - Instructions to prefer canonical tags, fall back to
        `category=value` format when nothing fits.
      - The chunk content.
    """
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


# ── Heuristic detection ──────────────────────────────────────────────────────

def is_heuristic_chunk(rec: dict) -> bool:
    """True if this chunk's tag set is the placeholder fingerprint."""
    tags = rec.get("tags")
    if not isinstance(tags, list):
        return False
    return set(tags) == HEURISTIC_TAG_SET


# ── MiniMax call ─────────────────────────────────────────────────────────────

def call_minimax_for_tags(client: MiniMaxClient, prompt: str) -> str:
    """Send the vocabulary-aware prompt to MiniMax, return raw response text.

    The M2.7 model emits <think>...</think> before the actual answer; the
    parser below strips those blocks. We do not try to use MiniMaxClient's
    chunk_content() because that's tied to a chunking system prompt — this
    script needs a free-form chat completion, so we hit the same endpoint
    directly with the same auth the client set up.
    """
    url = f"{client._base_url}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {client._api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": client._model,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "user", "content": prompt},
        ],
    }
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as http:
        response = http.post(url, headers=headers, json=payload)
        response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def call_minimax_with_retry(client: MiniMaxClient, prompt: str) -> str:
    """Call MiniMax once; if the response was truncated (finish_reason=length)
    and no parseable tags came back, retry once with a much higher budget.

    Truncation happens when M2.7's <think> block eats all the tokens. The
    retry nudges max_tokens via a fresh request — we can't change it after
    the first call, so this is two distinct HTTP calls.
    """
    text = call_minimax_for_tags(client, prompt)
    if parse_tags_from_response(text):
        return text
    # Truncation recovery: bigger budget.
    url = f"{client._base_url}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {client._api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": client._model,
        "max_tokens": MAX_TOKENS_RETRY,
        "messages": [
            {"role": "user", "content": prompt},
        ],
    }
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as http:
        response = http.post(url, headers=headers, json=payload)
        response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


# ── Tag parsing ──────────────────────────────────────────────────────────────

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
# Split on commas, newlines, semicolons — covers the shapes M2.7 emits.
_DELIMITER_RE = re.compile(r"[,\n;]")
# Strip surrounding quotes, brackets, and whitespace per tag.
_TAG_TRIM_RE = re.compile(r'^[\s"\'`\[\(\)\]]+|[\s"\'`\[\(\)\]]+$')
# Drop tags that are too long to be useful.
_MAX_TAG_LEN = 64
# Drop pure noise tokens.
_MIN_TAG_LEN = 2


def parse_tags_from_response(raw: str) -> list[str]:
    """Extract a clean list of tags from MiniMax's free-form text response.

    Strips <think>...</think>, splits on common delimiters, trims, dedupes
    (preserving first-seen order), and drops empty/short/long tokens.
    Returns [] if nothing usable came back.
    """
    # 1. Remove <think>...</think> blocks (M2.7 always emits these).
    text = _THINK_BLOCK_RE.sub("", raw)

    # 2. Split on common delimiters.
    pieces = _DELIMITER_RE.split(text)

    seen: set[str] = set()
    out: list[str] = []
    for piece in pieces:
        tag = _TAG_TRIM_RE.sub("", piece).strip()
        if not tag:
            continue
        if len(tag) < _MIN_TAG_LEN or len(tag) > _MAX_TAG_LEN:
            continue
        # Skip "tags:" / "Tag1:" / numeric-only noise.
        if tag.lower() in {"tags", "tag", "here", "are", "the", "is"}:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


# ── Tag resolution and new-tag detection ─────────────────────────────────────


def resolve_through_vocabulary(
    raw_tags: list[str],
    vocabulary: dict,
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Normalize raw tags through the vocabulary and report any new ones.

    For each tag of the form `category=value`:
      - Look up the category in the vocabulary.
      - If found, replace `value` with the canonical form (synonyms collapse).
      - If the value was NOT canonical AND NOT a known synonym, record it
        as a proposed new tag — even if it then falls back to `other`.

    The new-tag detection happens BEFORE fallback: "the LLM tried to
    propose `tool=hermes-agent`" is meaningful information even though
    resolve() collapses it to `tool=other`. We want to surface the
    ORIGINAL intent for the vocabulary editor to review.

    Tags that don't have `category=value` format are kept as-is (the LLM
    might emit bare tokens if it ignored the format rule) but are NOT
    recorded as new tags — those are noise.

    Returns:
        (normalized_tags, new_tag_records)
        - normalized_tags: list of normalized tag strings (canonical
          categories preserved; non-canonical tokens preserved as-is).
        - new_tag_records: list of (chunk_category, original_value,
          normalized_value) tuples for proposed new vocabulary entries.
    """
    normalized: list[str] = []
    new_records: list[tuple[str, str, str]] = []

    for tag in raw_tags:
        tag = tag.strip()
        if not tag or "=" not in tag:
            # No category prefix — keep as-is but don't propose as new vocab.
            normalized.append(tag)
            continue

        category, _, value = tag.partition("=")
        category_lower = category.strip().lower()
        value_lower = value.strip().lower()

        if category_lower not in vocabulary:
            # Unknown category — keep as-is.
            normalized.append(tag)
            continue

        cat = vocabulary[category_lower]
        resolved_value = cat.resolve(value_lower)
        normalized_tag = f"{category_lower}={resolved_value}"
        normalized.append(normalized_tag)

        # Was this a new tag? Check whether the LLM's original value
        # matched anything we already know (canonical OR synonym). If
        # not, the LLM was proposing something new — record it.
        if (
            value_lower not in cat.canonical_values
            and value_lower not in cat.synonyms
        ):
            new_records.append((category_lower, value_lower, resolved_value))

    return normalized, new_records


# ── Tag composition ──────────────────────────────────────────────────────────

def build_new_tags(enriched: list[str], outcome_tag: str) -> list[str]:
    """Compose the final tag list for a chunk.

    Keep the original outcome= tag (typically work_done) and drop the
    source=heuristic marker (the whole point of enrichment is to leave
    that behind). Prepend the new descriptive tags.

    Also enforces MAX_TAGS_PER_CHUNK: if the LLM overshot, we trim to
    the cap (priority is order from the LLM since resolve+dedupe already
    happened upstream). Returns a tuple of (tags, trimmed: bool).
    """
    # Filter out any heuristic/system tags the LLM might mirror back.
    cleaned = [
        t for t in enriched
        if t not in HEURISTIC_TAG_SET
        and not t.startswith("source=")
    ]
    final = list(cleaned)
    trimmed = False
    # Reserve 1 slot for outcome_tag if it isn't already present.
    budget = MAX_TAGS_PER_CHUNK - (0 if outcome_tag in final else 1)
    if budget < 0:
        budget = 0
    if len(final) > MAX_TAGS_PER_CHUNK:
        final = final[:MAX_TAGS_PER_CHUNK]
        trimmed = True
    if outcome_tag and outcome_tag not in final:
        final.append(outcome_tag)
    return final, trimmed


# ── I/O helpers ──────────────────────────────────────────────────────────────

def load_chunks(path: Path) -> tuple[list[dict], int]:
    """Load all chunks. Returns (chunks, total_lines_scanned)."""
    chunks: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                chunks.append(rec)
    return chunks, len(chunks)


def atomic_write_with_backup(chunks: list[dict], data_path: Path, backup_path: Path) -> None:
    """Write chunks to data_path via temp+rename, with backup first."""
    # 1. Backup.
    if data_path.exists():
        backup_bytes = data_path.read_bytes()
        backup_path.write_bytes(backup_bytes)
        print(f"  Backup: {backup_path} ({len(backup_bytes):,} bytes)")

    # 2. Temp file with PID suffix to avoid collisions.
    pid = os.getpid()
    tmp_path = data_path.with_suffix(f".jsonl.tmp.{pid}")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for rec in chunks:
            f.write(json.dumps(rec))
            f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    print(f"  Temp:   {tmp_path} ({tmp_path.stat().st_size:,} bytes)")

    # 3. Atomic rename.
    os.replace(tmp_path, data_path)
    print(f"  Atomic: {tmp_path} -> {data_path}")


def append_new_tags_sidecar(
    sidecar_path: Path,
    chunk_id: str,
    new_records: list[tuple[str, str, str]],
) -> None:
    """Append new-tag proposals to the sidecar JSONL file.

    Each line: {"chunk_id", "tag", "category", "original_value",
                "normalized_value"}.
    """
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    with open(sidecar_path, "a", encoding="utf-8") as f:
        for category, original, normalized in new_records:
            f.write(json.dumps({
                "chunk_id": chunk_id,
                "tag": f"{category}={normalized}",
                "category": category,
                "original_value": original,
                "normalized_value": normalized,
            }) + "\n")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="max number of heuristic chunks to enrich (default: all)")
    ap.add_argument("--offset", type=int, default=0,
                    help="skip this many heuristic chunks from the start (default 0). "
                         "Use to avoid re-processing chunks from a prior pilot.")
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is dry-run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit dry-run flag (default behavior; provided for clarity)")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS,
                    help=f"seconds between MiniMax calls (default {DEFAULT_DELAY_SECONDS})")
    ap.add_argument("--output", type=Path, default=DEFAULT_DATA_PATH,
                    help=f"output JSONL path (default {DEFAULT_DATA_PATH})")
    args = ap.parse_args()

    data_path: Path = args.output
    if not data_path.exists():
        print(f"ERROR: {data_path} not found")
        return 1

    # ── Load vocabulary (single source of truth) ───────────────────────────
    vocabulary = get_vocabulary()
    n_tool = len(vocabulary["tool"].canonical_values)
    n_outcome = len(vocabulary["outcome"].canonical_values)
    n_error = len(vocabulary["error"].canonical_values)
    n_language = len(vocabulary["language"].canonical_values)

    # ── 1. Load and identify heuristic chunks ───────────────────────────────
    print(f"Loading {data_path}...")
    chunks, total = load_chunks(data_path)
    print(f"  Read {total} chunks")

    heuristic_indices = [i for i, c in enumerate(chunks) if is_heuristic_chunk(c)]
    heuristic_count = len(heuristic_indices)
    print(f"  Heuristic-only chunks: {heuristic_count}")

    if args.limit is not None:
        target_indices = heuristic_indices[args.offset : args.offset + args.limit]
    else:
        target_indices = heuristic_indices[args.offset :]
    target_count = len(target_indices)
    print(f"  Will enrich: {target_count}")
    print(f"  Vocabulary: tool={n_tool} outcome={n_outcome} "
          f"error={n_error} language={n_language} canonical values")

    if target_count == 0:
        print("Nothing to enrich.")
        return 0

    # ── 2. Dry-run by default ───────────────────────────────────────────────
    if not args.apply:
        est_seconds = target_count * args.delay + target_count * 2  # ~2s per call
        print()
        print("=" * 60)
        print("DRY RUN — pass --apply to actually enrich and write")
        print(f"Would call MiniMax for {target_count} chunks (max_tokens={MAX_TOKENS})")
        print(f"Delay between calls: {args.delay}s")
        print(f"Estimated wall time: ~{est_seconds:.0f}s ({est_seconds/60:.1f} min)")
        print(f"Backup would be written to: {BACKUP_PATH}")
        print(f"Atomic-rename: <data_path>.tmp.<pid> -> {data_path}")
        print(f"New-tag sidecar would be appended to: {NEW_TAGS_SIDECAR}")
        print()
        print("Vocab-aware prompt preview (first 600 chars):")
        sample_content = "(chunk content placeholder)"
        print(build_prompt(vocabulary, sample_content)[:600])
        return 0

    # ── 3. Initialize MiniMax ───────────────────────────────────────────────
    print(f"\nInitializing MiniMaxClient...")
    try:
        client = MiniMaxClient()
    except Exception as exc:
        print(f"ERROR: client init failed: {exc}")
        return 1
    print(f"  model={client._model} base={client._base_url}")

    # ── 4. Enrich in sequence ───────────────────────────────────────────────
    print(f"\nEnriching {target_count} heuristic chunks (vocab-aware)...")
    started = time.time()
    enriched = 0
    failed = 0
    skipped = 0
    samples: list[tuple[str, list[str], list[str]]] = []  # (chunk_id, old, new)

    for n, idx in enumerate(target_indices, 1):
        rec = chunks[idx]
        chunk_id = rec.get("chunk_id", f"<no-id-{idx}>")
        content = rec.get("content") or ""
        old_tags = list(rec.get("tags", []))
        outcome_tag = next(
            (t for t in old_tags if t.startswith("outcome=")),
            "outcome=work_done",
        )

        if not content.strip():
            print(f"  [{n}/{target_count}] {chunk_id}: SKIP (empty content)")
            skipped += 1
            continue

        text_to_send = content[:MAX_CONTENT_CHARS]
        prompt = build_prompt(vocabulary, text_to_send)

        try:
            raw = call_minimax_with_retry(client, prompt)
            raw_tags = parse_tags_from_response(raw)
            if not raw_tags:
                # Don't overwrite a chunk with no tags — leave it heuristic.
                print(f"  [{n}/{target_count}] {chunk_id}: FAIL (no parseable tags)")
                failed += 1
                continue

            # Resolve through vocabulary: collapse synonyms, detect new tags.
            normalized_tags, new_records = resolve_through_vocabulary(
                raw_tags, vocabulary
            )
            if not normalized_tags:
                print(f"  [{n}/{target_count}] {chunk_id}: FAIL (resolution yielded empty)")
                failed += 1
                continue

            final_tags, trimmed = build_new_tags(normalized_tags, outcome_tag)
            if trimmed:
                print(f"  [{n}/{target_count}] {chunk_id}: TRIM (>{MAX_TAGS_PER_CHUNK} tags)")
            rec["tags"] = final_tags
            enriched += 1

            # Record new-tag proposals.
            if new_records:
                append_new_tags_sidecar(NEW_TAGS_SIDECAR, chunk_id, new_records)

            if len(samples) < SAMPLE_OUTPUT_SIZE:
                samples.append((chunk_id, old_tags, final_tags))
        except Exception as exc:
            print(f"  [{n}/{target_count}] {chunk_id}: FAIL ({type(exc).__name__}: {exc})")
            failed += 1
            continue

        if n % 10 == 0 or n == target_count:
            elapsed = time.time() - started
            rate = enriched / elapsed if elapsed > 0 else 0
            eta = (target_count - n) / rate if rate > 0 else 0
            print(
                f"  [{n}/{target_count}] enriched={enriched} failed={failed} "
                f"skipped={skipped} elapsed={elapsed:.1f}s rate={rate:.2f}/s eta={eta:.0f}s"
            )

        time.sleep(args.delay)

    # ── 5. Atomic write ─────────────────────────────────────────────────────
    print(f"\nWriting enriched file atomically...")
    atomic_write_with_backup(chunks, data_path, BACKUP_PATH)

    # ── 6. Final report ─────────────────────────────────────────────────────
    elapsed = time.time() - started
    print()
    print("=" * 60)
    print(f"DONE enriched={enriched} failed={failed} skipped={skipped} "
          f"elapsed={elapsed:.1f}s")
    print(f"Total chunks now: {total}")
    remaining_heuristic = sum(1 for c in chunks if is_heuristic_chunk(c))
    print(f"Remaining heuristic-only chunks: {remaining_heuristic} "
          f"(was {heuristic_count})")
    if NEW_TAGS_SIDECAR.exists():
        with open(NEW_TAGS_SIDECAR) as f:
            n_new = sum(1 for _ in f)
        print(f"New-tag proposals recorded: {n_new} ({NEW_TAGS_SIDECAR})")
    print()
    print("Sample of new tags (first {}):".format(min(len(samples), SAMPLE_OUTPUT_SIZE)))
    for cid, old, new in samples:
        print(f"  {cid}")
        print(f"    old: {old}")
        print(f"    new: {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())