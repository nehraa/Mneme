#!/usr/bin/env python3
"""
backfill_embeddings.py — SAFE backfill of missing embeddings.

SAFETY RULES (learned from June 22 2026 sweep_embeddings.py destruction):
1. NEVER in-place rewrite. Write to data/skill_chunks.jsonl.new, then atomic rename.
2. NEVER silently skip lines on read. Log every skip with line number + reason.
3. Backup to data/skill_chunks.jsonl.bak2 BEFORE writing.
4. Default mode is --dry-run. Real writes need explicit --apply flag.
5. Print summary at end: read N, skipped M, embedded K, failed F.

Usage:
  # Dry run first — show what would happen
  python scripts/backfill_embeddings.py --dry-run

  # Actually write (requires --apply)
  python scripts/backfill_embeddings.py --apply

  # With small batch size to avoid Ollama OOM
  python scripts/backfill_embeddings.py --apply --batch-size 3
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from dotenv import dotenv_values

# Load .env (workaround for known bug)
_cfg = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
for _k, _v in _cfg.items():
    if _v is not None:
        os.environ.setdefault(_k, _v)

from src.retrieval.ollama_embeddings import OllamaEmbeddingClient  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = PROJECT_ROOT / "data" / "skill_chunks.jsonl"
BACKUP_PATH = PROJECT_ROOT / "data" / "skill_chunks.jsonl.bak2"
TMP_PATH = PROJECT_ROOT / "data" / "skill_chunks.jsonl.new"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is dry-run)")
    ap.add_argument("--batch-size", type=int, default=5,
                    help="embeddings per Ollama call (default 5; smaller = safer)")
    ap.add_argument("--delay", type=float, default=0.3,
                    help="seconds between batches (default 0.3)")
    args = ap.parse_args()

    if not DATA_PATH.exists():
        print(f"ERROR: {DATA_PATH} not found")
        return 1

    # ── Phase 1: Load + validate ALL lines (don't skip, don't drop) ──────────
    print(f"Loading {DATA_PATH}...")
    raw_lines = DATA_PATH.read_text().splitlines()
    chunks: list[dict] = []
    skip_reasons: list[tuple[int, str]] = []  # (line_num, reason)
    null_indices: list[int] = []

    for i, line in enumerate(raw_lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            skip_reasons.append((i, f"JSONDecodeError: {exc}"))
            continue
        if not isinstance(rec, dict):
            skip_reasons.append((i, f"not a dict, got {type(rec).__name__}"))
            continue
        if not rec.get("chunk_id"):
            skip_reasons.append((i, "missing chunk_id"))
            continue
        chunks.append(rec)
        if rec.get("embedding") is None and rec.get("content"):
            null_indices.append(len(chunks) - 1)

    print(f"Read {len(raw_lines)} lines")
    print(f"  Valid chunks loaded: {len(chunks)}")
    print(f"  Skipped (preserved verbatim): {len(skip_reasons)}")
    print(f"  Need embeddings: {len(null_indices)}")

    if skip_reasons[:5]:
        print("  First 5 skip reasons:")
        for ln, reason in skip_reasons[:5]:
            print(f"    line {ln}: {reason}")

    if not null_indices:
        print("Nothing to embed — all chunks already have vectors")
        return 0

    # ── Phase 2: Dry-run by default; --apply required to write ──────────────
    if not args.apply:
        print()
        print("=" * 60)
        print("DRY RUN — pass --apply to actually write embeddings")
        print(f"Would embed {len(null_indices)} chunks via Ollama qwen3-embedding:0.6b")
        print(f"Batch size: {args.batch_size}, delay between batches: {args.delay}s")
        print(f"Estimated time: ~{len(null_indices) * args.delay / args.batch_size:.0f}s ({len(null_indices) * args.delay / args.batch_size / 60:.1f} min)")
        print(f"Backup would be written to: {BACKUP_PATH}")
        print(f"New file would be written to: {TMP_PATH}")
        print(f"Then atomic rename: {TMP_PATH} -> {DATA_PATH}")
        return 0

    # ── Phase 3: Embed ───────────────────────────────────────────────────────
    embedder = OllamaEmbeddingClient()
    print(f"\nEmbedding with model={embedder._model} base={embedder._base_url}")
    print(f"Batch size: {args.batch_size}, delay: {args.delay}s")

    started = time.time()
    embedded = 0
    failed = 0

    for batch_start in range(0, len(null_indices), args.batch_size):
        batch = null_indices[batch_start:batch_start + args.batch_size]
        texts = [chunks[i].get("content", "")[:2000] for i in batch]

        try:
            vectors = embedder.embed_batch(texts)
            for idx, vec in zip(batch, vectors):
                if vec:
                    chunks[idx]["embedding"] = vec
                    chunks[idx]["embedding_model"] = embedder._model
                    chunks[idx]["embedding_dim"] = len(vec)
                    embedded += 1
                else:
                    failed += 1
        except Exception as exc:
            print(f"  batch {batch_start}-{batch_start+len(batch)} failed: {exc}")
            # Try one at a time to maximize recovery
            for idx, text in zip(batch, texts):
                try:
                    vec = embedder.embed(text)
                    if vec:
                        chunks[idx]["embedding"] = vec
                        chunks[idx]["embedding_model"] = embedder._model
                        chunks[idx]["embedding_dim"] = len(vec)
                        embedded += 1
                    else:
                        failed += 1
                except Exception as exc2:
                    print(f"    single {idx} failed: {exc2}")
                    failed += 1

        elapsed = time.time() - started
        rate = embedded / elapsed if elapsed > 0 else 0
        eta = (len(null_indices) - batch_start - len(batch)) / rate if rate > 0 else 0
        print(
            f"  [{batch_start + len(batch)}/{len(null_indices)}] "
            f"embedded={embedded} failed={failed} "
            f"elapsed={elapsed:.1f}s rate={rate:.2f}/s eta={eta:.0f}s"
        )
        time.sleep(args.delay)

    # ── Phase 4: Write atomically (backup → tmp → rename) ───────────────────
    print("\nWriting results atomically...")

    # Preserve skipped lines verbatim from original
    skipped_text = "\n".join(raw_lines[i - 1] for i, _ in skip_reasons)
    if skipped_text and not skipped_text.endswith("\n"):
        skipped_text += "\n"

    backup_data = DATA_PATH.read_bytes()
    BACKUP_PATH.write_bytes(backup_data)
    print(f"Backup written: {BACKUP_PATH} ({len(backup_data):,} bytes)")

    output_lines = []
    chunk_iter = iter(chunks)
    skip_iter = iter(skip_reasons)
    next_skip = next(skip_iter, None)
    skip_line_set = {ln for ln, _ in skip_reasons}
    next_chunk_idx = 0

    for i, raw_line in enumerate(raw_lines, 1):
        if i in skip_line_set:
            output_lines.append(raw_line)
        else:
            try:
                output_lines.append(json.dumps(next(chunk_iter)))
                next_chunk_idx += 1
            except StopIteration:
                # Edge case: shouldn't happen given our indexing
                pass

    # Stream-write line-by-line to avoid building an ~780MB string in memory
    with open(TMP_PATH, "w", encoding="utf-8") as out_f:
        for line in output_lines:
            out_f.write(line)
            out_f.write("\n")
        out_f.flush()
        os.fsync(out_f.fileno())
    TMP_PATH_SIZE = TMP_PATH.stat().st_size
    print(f"Temp file written: {TMP_PATH} ({TMP_PATH_SIZE:,} bytes)")

    # Atomic rename
    os.replace(TMP_PATH, DATA_PATH)
    print(f"Atomic rename: {TMP_PATH} -> {DATA_PATH}")

    elapsed = time.time() - started
    print()
    print("=" * 60)
    print(f"DONE embedded={embedded} failed={failed} elapsed={elapsed:.1f}s")
    print(f"Final: {len(chunks)} chunks in {DATA_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
