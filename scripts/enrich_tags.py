#!/usr/bin/env python3
"""
enrich_tags.py — Re-tag heuristic-only chunks via MiniMax.

MNEME plan 2A: 81.9% of corpus (21,292 of 26,005) is tagged only
['outcome=work_done', 'source=heuristic']. This script asks MiniMax-M2.7
to produce real descriptive tags and replaces the heuristic tag set in place.

PILOT MODE: by default --limit is None (would process all 21,292). The
pilot run uses --limit 200 to validate tag quality before scaling up.

SAFETY RULES (per project patterns from backfill_embeddings.py / reingest_failed.py):
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

  # Full corpus (only after pilot confirms quality)
  python scripts/enrich_tags.py --apply
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

# Ensure project root is on sys.path so we can import MiniMaxClient.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.ingestion.llm_client import MiniMaxClient  # noqa: E402

# ── Constants (no magic numbers) ─────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DATA_PATH = DATA_DIR / "skill_chunks.jsonl"
BACKUP_PATH = DATA_DIR / "skill_chunks.jsonl.enrich_pilot_bak"

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

PROMPT_TEMPLATE = "Reply with comma-separated tags for this text: {content}"


# ── Heuristic detection ──────────────────────────────────────────────────────

def is_heuristic_chunk(rec: dict) -> bool:
    """True if this chunk's tag set is the placeholder fingerprint."""
    tags = rec.get("tags")
    if not isinstance(tags, list):
        return False
    return set(tags) == HEURISTIC_TAG_SET


# ── MiniMax call ─────────────────────────────────────────────────────────────

def call_minimax_for_tags(client: MiniMaxClient, content: str) -> str:
    """Send content to MiniMax, return raw response text.

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
            {"role": "user", "content": PROMPT_TEMPLATE.format(content=content)},
        ],
    }
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as http:
        response = http.post(url, headers=headers, json=payload)
        response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def call_minimax_with_retry(client: MiniMaxClient, content: str) -> str:
    """Call MiniMax once; if the response was truncated (finish_reason=length)
    and no parseable tags came back, retry once with a much higher budget.

    Truncation happens when M2.7's <think> block eats all the tokens. The
    retry nudges max_tokens via a fresh request — we can't change it after
    the first call, so this is two distinct HTTP calls.
    """
    text = call_minimax_for_tags(client, content)
    if parse_tags_from_response(text):
        return text
    # Truncation recovery: bigger budget.
    import httpx as _httpx
    url = f"{client._base_url}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {client._api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": client._model,
        "max_tokens": MAX_TOKENS_RETRY,
        "messages": [
            {"role": "user", "content": PROMPT_TEMPLATE.format(content=content)},
        ],
    }
    with _httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as http:
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


# ── Tag composition ──────────────────────────────────────────────────────────

def build_new_tags(enriched: list[str], outcome_tag: str) -> list[str]:
    """Compose the final tag list for a chunk.

    Keep the original outcome= tag (typically work_done) and drop the
    source=heuristic marker (the whole point of enrichment is to leave
    that behind). Prepend the new descriptive tags.
    """
    # Filter out any heuristic/system tags the LLM might mirror back.
    cleaned = [
        t for t in enriched
        if t not in HEURISTIC_TAG_SET
        and not t.startswith("source=")
    ]
    final = list(cleaned)
    if outcome_tag and outcome_tag not in final:
        final.append(outcome_tag)
    return final


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


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="max number of heuristic chunks to enrich (default: all)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is dry-run)")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS,
                    help=f"seconds between MiniMax calls (default {DEFAULT_DELAY_SECONDS})")
    ap.add_argument("--output", type=Path, default=DEFAULT_DATA_PATH,
                    help=f"output JSONL path (default {DEFAULT_DATA_PATH})")
    args = ap.parse_args()

    data_path: Path = args.output
    if not data_path.exists():
        print(f"ERROR: {data_path} not found")
        return 1

    # ── 1. Load and identify heuristic chunks ────────────────────────────────
    print(f"Loading {data_path}...")
    chunks, total = load_chunks(data_path)
    print(f"  Read {total} chunks")

    heuristic_indices = [i for i, c in enumerate(chunks) if is_heuristic_chunk(c)]
    heuristic_count = len(heuristic_indices)
    print(f"  Heuristic-only chunks: {heuristic_count}")

    if args.limit is not None:
        target_indices = heuristic_indices[: args.limit]
    else:
        target_indices = heuristic_indices
    target_count = len(target_indices)
    print(f"  Will enrich: {target_count}")

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
    print(f"\nEnriching {target_count} heuristic chunks...")
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

        try:
            raw = call_minimax_with_retry(client, text_to_send)
            new_tags_raw = parse_tags_from_response(raw)
            if not new_tags_raw:
                # Don't overwrite a chunk with no tags — leave it heuristic.
                print(f"  [{n}/{target_count}] {chunk_id}: FAIL (no parseable tags)")
                failed += 1
                continue
            final_tags = build_new_tags(new_tags_raw, outcome_tag)
            rec["tags"] = final_tags
            enriched += 1
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
    print()
    print("Sample of new tags (first {}):".format(min(len(samples), SAMPLE_OUTPUT_SIZE)))
    for cid, old, new in samples:
        print(f"  {cid}")
        print(f"    old: {old}")
        print(f"    new: {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
