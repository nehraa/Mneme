#!/usr/bin/env python3
"""
reingest_failed.py — Re-ingest files that previously failed parsing.

MNEME plan 2C: Re-process files marked "failed" in the manifest using the
improved MiniMax JSON parser (Phase 1B). Writes new chunks to
skill_chunks.jsonl atomically (read → write .tmp.<pid> → os.replace),
dedupes by chunk_id, and updates the manifest.

SAFETY RULES (per user emphasis on careful work after prior write failures):
1. NEVER overwrite skill_chunks.jsonl without a backup.
2. Atomic write: read all → write to .tmp.<pid> → fsync → os.replace.
3. Dedup by chunk_id (safe_path__raw_id prefix, matches ingest_full.py).
4. Dry-run by default; --apply required for actual write.
5. Log every step with print() since this is a script.

Usage:
  python scripts/reingest_failed.py           # dry-run
  python scripts/reingest_failed.py --apply   # actually write + update manifest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import dotenv_values

# Load .env (same workaround as ingest_full.py)
_cfg = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
for _k, _v in _cfg.items():
    if _v is not None:
        os.environ.setdefault(_k, _v)

# Ensure project root is on sys.path so `from src...` works regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Reuse MiniMax (improved parser) + helpers from existing scripts
from src.ingestion.llm_client import MiniMaxClient  # noqa: E402

# Add scripts/ to path so we can import ingest_full helpers
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ingest_full as _ingest  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CHUNKS_PATH = DATA_DIR / "skill_chunks.jsonl"
BACKUP_PATH = DATA_DIR / "skill_chunks.jsonl.reingest_bak"
MANIFEST_PATH = PROJECT_ROOT / "scripts" / "ingest_full_manifest.json"


def _safe_path_prefix(path: str) -> str:
    """Match ingest_full.py:459-460 — same dedup key."""
    return str(path).replace("/", "_").replace(".", "_")[:60]


def _build_record(rec: dict, source_file: str, existing_ids: set) -> dict | None:
    """Add metadata fields + apply dedup key. Returns None if duplicate."""
    raw_id = rec.get("chunk_id", "")
    unique_id = f"{_safe_path_prefix(source_file)}__{raw_id}"
    if unique_id in existing_ids:
        return None
    rec["chunk_id"] = unique_id
    rec["source_file"] = source_file
    rec["session_id"] = "mneme-full-corpus-2026-06-19"
    rec["project_root"] = "/home/Hermes"
    rec["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tags = rec.get("tags", [])
    rec["outcome_tag"] = next(
        (t.split("=", 1)[1] for t in tags if t.startswith("outcome=")),
        "work_done",
    )
    existing_ids.add(unique_id)
    return rec


def find_failed(manifest: dict[str, str]) -> list[str]:
    """Return paths with status != 'done'."""
    return [p for p, s in manifest.items() if s != "done"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is dry-run)")
    args = ap.parse_args()

    # ── 1. Load manifest, find failed entries ───────────────────────────────
    if not MANIFEST_PATH.exists():
        print(f"ERROR: manifest not found at {MANIFEST_PATH}")
        return 1
    manifest = json.loads(MANIFEST_PATH.read_text())
    failed_paths = find_failed(manifest)

    print(f"Manifest: {len(manifest)} entries, {len(failed_paths)} failed")
    for p in failed_paths:
        print(f"  FAILED: {manifest[p]!r}  {p}")

    if not failed_paths:
        print("Nothing to re-ingest — all files marked done.")
        return 0

    # ── 2. Validate files still exist on disk ───────────────────────────────
    missing = [p for p in failed_paths if not Path(p).exists()]
    if missing:
        print(f"ERROR: {len(missing)} failed files no longer exist on disk:")
        for p in missing:
            print(f"  MISSING: {p}")
        return 1

    if not args.apply:
        print()
        print("=" * 60)
        print("DRY RUN — pass --apply to actually re-ingest and write")
        print(f"Would process {len(failed_paths)} file(s) via MiniMaxClient (improved parser)")
        print(f"Would write backup to: {BACKUP_PATH}")
        print(f"Would atomic-rename: {CHUNKS_PATH}.tmp.<pid> -> {CHUNKS_PATH}")
        return 0

    # ── 3. Initialize MiniMax + embeddings (re-using ingest_full client init)
    print(f"\nInitializing MiniMaxClient (improved parser from Phase 1B)...")
    try:
        minimax = MiniMaxClient()
        embeddings, provider_kind = _ingest.make_embedding_client()
    except Exception as exc:
        print(f"ERROR: client init failed: {exc}")
        return 1
    print(f"  minimax model={minimax._model} base={minimax._base_url}")
    print(f"  embeddings provider={provider_kind} model={getattr(embeddings, '_model', '?')}")

    # ── 4. Load existing chunks for dedup ───────────────────────────────────
    print(f"\nLoading existing chunks from {CHUNKS_PATH} for dedup...")
    existing_ids: set[str] = set()
    if CHUNKS_PATH.exists():
        with CHUNKS_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    cid = rec.get("chunk_id")
                    if cid:
                        existing_ids.add(cid)
                except json.JSONDecodeError:
                    continue
    print(f"  {len(existing_ids)} existing chunk_ids loaded")

    # ── 5. Re-ingest each failed file ────────────────────────────────────────
    new_records: list[dict] = []
    for fp in failed_paths:
        path = Path(fp)
        # Session JSON (matches ingest_full.py collect_sessions ext filter)
        source_kind = "session"
        cap = _ingest.MAX_CHARS_SESSION
        print(f"\nProcessing: {fp}")
        print(f"  source_kind={source_kind} cap={cap}")

        try:
            records = _ingest.process_file(
                path, source_kind, minimax, embeddings, provider_kind, cap
            )
        except Exception as exc:
            print(f"  ERROR: process_file raised: {exc}")
            print(f"  Manifest status remains 'failed' for this file.")
            continue

        added = 0
        dupes = 0
        for rec in records:
            built = _build_record(rec, fp, existing_ids)
            if built is None:
                dupes += 1
            else:
                new_records.append(built)
                added += 1
        print(f"  produced {len(records)} chunks, added {added}, dupes_skipped {dupes}")
        if len(records) > 0:
            sample_tags = new_records[-1].get("tags", [])[:5] if new_records else []
            print(f"  sample tags from last chunk: {sample_tags}")

    if not new_records:
        print("\nNo new chunks produced — leaving manifest and data file unchanged.")
        return 0

    # ── 6. Backup + atomic write ─────────────────────────────────────────────
    print(f"\nWriting {len(new_records)} new chunks atomically...")

    # Read existing content
    existing_text = CHUNKS_PATH.read_text() if CHUNKS_PATH.exists() else ""
    # Ensure existing content ends with newline before we append
    if existing_text and not existing_text.endswith("\n"):
        existing_text += "\n"

    # New chunk lines
    new_lines = [json.dumps(rec) for rec in new_records]
    final_text = existing_text + "\n".join(new_lines) + "\n"

    # Write backup first
    if CHUNKS_PATH.exists():
        backup_bytes = CHUNKS_PATH.read_bytes()
        BACKUP_PATH.write_bytes(backup_bytes)
        print(f"  Backup: {BACKUP_PATH} ({len(backup_bytes):,} bytes)")

    # Write to temp file with PID suffix to avoid collisions
    pid = os.getpid()
    tmp_path = CHUNKS_PATH.with_suffix(f".jsonl.tmp.{pid}")
    with open(tmp_path, "w") as f:
        f.write(final_text)
        f.flush()
        os.fsync(f.fileno())
    print(f"  Temp: {tmp_path} ({len(final_text):,} bytes)")

    # Atomic rename
    os.replace(tmp_path, CHUNKS_PATH)
    print(f"  Renamed: {tmp_path} -> {CHUNKS_PATH}")

    # ── 7. Update manifest for files we successfully produced chunks for ─────
    # We mark "done" if we produced >=1 chunk from the file. Files that produced
    # 0 chunks (e.g. empty content) stay as "failed" in the manifest.
    produced_files: set[str] = set()
    for rec in new_records:
        sf = rec.get("source_file")
        if sf:
            produced_files.add(sf)

    for fp in failed_paths:
        if fp in produced_files:
            manifest[fp] = "done"

    # Atomic manifest write too
    manifest_tmp = MANIFEST_PATH.with_suffix(f".json.tmp.{pid}")
    manifest_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    os.replace(manifest_tmp, MANIFEST_PATH)
    print(f"  Manifest updated: {len(produced_files)} file(s) marked done")

    # ── 8. Verification ──────────────────────────────────────────────────────
    print(f"\nVerification:")
    final_count = sum(1 for _ in CHUNKS_PATH.open())
    print(f"  Final chunk count in {CHUNKS_PATH.name}: {final_count}")
    print(f"  Expected delta: +{len(new_records)}")
    # Sanity: re-read first new chunk
    with CHUNKS_PATH.open() as f:
        lines = f.readlines()
    new_first = lines[-len(new_records)] if len(lines) >= len(new_records) else None
    if new_first:
        sample = json.loads(new_first)
        print(f"  First new chunk sample:")
        print(f"    chunk_id: {sample.get('chunk_id')}")
        print(f"    source_file: {sample.get('source_file')}")
        print(f"    tags[:5]: {sample.get('tags', [])[:5]}")
        print(f"    outcome_tag: {sample.get('outcome_tag')}")
        print(f"    embedding_dim: {sample.get('embedding_dim')}")
        print(f"    content_preview: {(sample.get('content') or '')[:120]!r}")

    print(f"\nDONE reingest: {len(produced_files)}/{len(failed_paths)} file(s) succeeded, "
          f"{len(new_records)} chunks added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
