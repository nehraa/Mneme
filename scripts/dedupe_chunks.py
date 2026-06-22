#!/usr/bin/env python3
"""
dedupe_chunks.py — Remove near-duplicate chunks from data/skill_chunks.jsonl.

SAFETY RULES (learned from June 22 2026 sweep_embeddings.py destruction):
1. NEVER in-place rewrite. Write to .new, then atomic rename.
2. NEVER silently skip lines on read. Log every skip.
3. Backup to .bak3 BEFORE writing.
4. Default mode is dry-run. --apply required to write.
5. Print summary: read N, kept K, removed R.

Strategy: For each chunk with an embedding, compute cosine similarity to
already-seen chunks in the same source_file. If > threshold (default 0.95),
drop the new chunk. First occurrence wins.

Usage:
  python scripts/dedupe_chunks.py                       # dry run
  python scripts/dedupe_chunks.py --apply               # actually write
  python scripts/dedupe_chunks.py --apply --threshold 0.90  # more aggressive
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import dotenv_values

_cfg = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
for _k, _v in _cfg.items():
    if _v is not None:
        os.environ.setdefault(_k, _v)

import numpy as np  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = PROJECT_ROOT / "data" / "skill_chunks.jsonl"
BACKUP_PATH = PROJECT_ROOT / "data" / "skill_chunks.jsonl.bak3"
TMP_PATH = PROJECT_ROOT / "data" / "skill_chunks.jsonl.new"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is dry-run)")
    ap.add_argument("--threshold", type=float, default=0.95,
                    help="cosine similarity threshold for dedup (default 0.95)")
    args = ap.parse_args()

    if not DATA_PATH.exists():
        print(f"ERROR: {DATA_PATH} not found")
        return 1

    print(f"Loading {DATA_PATH}...")
    raw_lines = DATA_PATH.read_text().splitlines()

    # Parse all chunks; preserve verbatim any unparseable lines
    chunks: list[dict] = []
    skip_reasons: list[tuple[int, str]] = []
    null_idx_in_chunks: list[int] = []

    for i, line in enumerate(raw_lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            skip_reasons.append((i, f"JSONDecodeError: {exc}"))
            continue
        if not isinstance(rec, dict) or not rec.get("chunk_id"):
            skip_reasons.append((i, "missing chunk_id or not a dict"))
            continue
        chunks.append(rec)
        if rec.get("embedding") is None:
            null_idx_in_chunks.append(len(chunks) - 1)

    print(f"Read {len(raw_lines)} lines")
    print(f"  Valid chunks: {len(chunks)}")
    print(f"  Null embeddings (skipped from dedup): {len(null_idx_in_chunks)}")
    print(f"  With embeddings (dedup candidates): {len(chunks) - len(null_idx_in_chunks)}")
    if skip_reasons:
        print(f"  Preserved verbatim: {len(skip_reasons)}")
        for ln, reason in skip_reasons[:3]:
            print(f"    line {ln}: {reason}")

    # ── Dedup: per source_file, group embeddings, drop near-duplicates ─────
    print(f"\nDeduping (threshold={args.threshold}) per source_file...")
    started = time.time()

    # Group: source_file -> list of (chunk_idx, embedding_vector)
    by_source: dict[str, list[tuple[int, list[float]]]] = {}
    for idx in range(len(chunks)):
        emb = chunks[idx].get("embedding")
        if not emb or not isinstance(emb, list):
            continue
        src = chunks[idx].get("source_file", "<unknown>")
        by_source.setdefault(src, []).append((idx, emb))

    # Track which chunk indices to remove (keep first occurrence per group)
    to_remove: set[int] = set()
    dup_pairs: list[tuple[str, str, float]] = []  # (kept_id, removed_id, sim)

    for src, items in by_source.items():
        # Convert to numpy for fast cosine
        vecs = np.asarray([e for _, e in items], dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1)
        norms[norms == 0] = 1.0  # avoid div-by-zero

        kept_indices: list[int] = []  # indices into items list (local)
        kept_vecs: list[np.ndarray] = []
        kept_norms: list[float] = []

        for local_i, (chunk_idx, _) in enumerate(items):
            if not kept_vecs:
                # First chunk in this source — always keep
                kept_indices.append(local_i)
                kept_vecs.append(vecs[local_i])
                kept_norms.append(norms[local_i])
                continue

            # Cosine similarity to all kept chunks
            v = vecs[local_i]
            dots = np.array([(v @ kv) / (norms[local_i] * kn)
                             for kv, kn in zip(kept_vecs, kept_norms)])
            max_sim = float(dots.max()) if len(dots) else 0.0
            if max_sim >= args.threshold:
                # Duplicate — remove this one
                to_remove.add(chunk_idx)
                # Find which kept chunk it matched
                match_local = int(np.argmax(dots))
                kept_chunk_idx = items[kept_indices[match_local]][0]
                dup_pairs.append((chunks[kept_chunk_idx]["chunk_id"],
                                  chunks[chunk_idx]["chunk_id"],
                                  max_sim))
            else:
                kept_indices.append(local_i)
                kept_vecs.append(v)
                kept_norms.append(norms[local_i])

    elapsed = time.time() - started
    print(f"  Dedup scan: {elapsed:.1f}s")
    print(f"  Duplicates found: {len(to_remove)}")
    print(f"  Final chunks after dedup: {len(chunks) - len(to_remove)}")

    if dup_pairs:
        print(f"  Sample duplicate pairs (kept → removed, sim):")
        for kept, removed, sim in dup_pairs[:5]:
            print(f"    sim={sim:.4f}  {kept[:40]}... → {removed[:40]}...")

    if not args.apply:
        print()
        print("=" * 60)
        print("DRY RUN — pass --apply to actually remove duplicates")
        print(f"Backup would be: {BACKUP_PATH}")
        print(f"New file would be: {TMP_PATH}")
        print(f"Then atomic rename: {TMP_PATH} -> {DATA_PATH}")
        return 0

    # ── Write atomically: backup → tmp → rename ─────────────────────────────
    print("\nWriting results atomically...")

    backup_data = DATA_PATH.read_bytes()
    BACKUP_PATH.write_bytes(backup_data)
    print(f"Backup: {BACKUP_PATH} ({len(backup_data):,} bytes)")

    # Build output: all chunks except removed, in original order
    skip_line_set = {ln for ln, _ in skip_reasons}
    new_content_parts: list[str] = []
    chunk_iter = iter(chunks)

    for i, raw_line in enumerate(raw_lines, 1):
        if i in skip_line_set:
            new_content_parts.append(raw_line)
            continue
        try:
            rec = next(chunk_iter)
        except StopIteration:
            break
        rec_idx = chunks.index(rec)
        if rec_idx in to_remove:
            continue
        new_content_parts.append(json.dumps(rec))

    new_content = "\n".join(new_content_parts)
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"

    print(f"Writing {TMP_PATH} ({len(new_content):,} bytes, {len(new_content_parts)} lines)...")
    with open(TMP_PATH, "w") as f:
        f.write(new_content)
    print(f"Temp: {TMP_PATH}")

    os.replace(TMP_PATH, DATA_PATH)
    print(f"Renamed: {TMP_PATH} -> {DATA_PATH}")

    print()
    print("=" * 60)
    print(f"DONE removed={len(to_remove)} kept={len(chunks) - len(to_remove)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
