#!/usr/bin/env python3
"""
alter_tags.py — Modify the `tags` field of chunks without re-ingesting.

SAFETY RULES (learned from June 22 2026 sweep_embeddings.py destruction):
1. NEVER in-place rewrite. Write to .new, then atomic rename.
2. NEVER silently skip lines on read. Log every skip.
3. Backup to .bak4 BEFORE writing.
4. Dry-run by default. --apply required to write.
5. Print summary: read N, kept K, modified M.

Two modes:
  --add-tag TAG              Add TAG to every chunk matching --filter
  --remove-tag TAG           Remove TAG from every chunk matching --filter
  --set-tags tag1 tag2 ...   REPLACE the tags list (use with care)
  --filter-source-kind KIND  Only modify chunks where source_kind=KIND
  --filter-source PATH       Only modify chunks where source_file contains PATH
  --filter-content TEXT      Only modify chunks where content contains TEXT
  --dry-run (default)        Show what would change
  --apply                    Actually write the changes

Examples:
  # Add topic=mneme tag to all skill chunks
  python scripts/alter_tags.py --add-tag topic=mneme --filter-source-kind skill --dry-run
  python scripts/alter_tags.py --add-tag topic=mneme --filter-source-kind skill --apply

  # Remove source=heuristic from all chunks
  python scripts/alter_tags.py --remove-tag source=heuristic --apply

  # Replace all tags on chunks that mention "OpenMAIC"
  python scripts/alter_tags.py --set-tags topic=openmaic --filter-content "OpenMAIC" --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

_cfg = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
for _k, _v in _cfg.items():
    if _v is not None:
        os.environ.setdefault(_k, _v)

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "skill_chunks.jsonl"
BACKUP_PATH = Path(__file__).resolve().parent.parent / "data" / "skill_chunks.jsonl.bak4"
TMP_PATH = Path(__file__).resolve().parent.parent / "data" / "skill_chunks.jsonl.new"


def main() -> int:
    ap = argparse.ArgumentParser()
    op = ap.add_mutually_exclusive_group(required=True)
    op.add_argument("--add-tag", help="Tag to add to matching chunks (e.g. topic=foo)")
    op.add_argument("--remove-tag", help="Tag to remove from matching chunks")
    op.add_argument("--set-tags", nargs="+", help="Replace tags list with these")
    ap.add_argument("--filter-source-kind", help="Only chunks where source_kind matches")
    ap.add_argument("--filter-source", help="Only chunks where source_file contains PATH")
    ap.add_argument("--filter-content", help="Only chunks where content contains TEXT")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="Show what would change (default)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the changes")
    args = ap.parse_args()

    if not DATA_PATH.exists():
        print(f"ERROR: {DATA_PATH} not found")
        return 1

    if args.apply:
        args.dry_run = False

    print(f"Loading {DATA_PATH}...")
    raw_lines = DATA_PATH.read_text().splitlines()

    # Parse all chunks
    chunks: list[dict] = []
    skip_reasons: list[tuple[int, str]] = []

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

    print(f"Read {len(raw_lines)} lines")
    print(f"  Valid chunks: {len(chunks)}")
    if skip_reasons:
        print(f"  Preserved verbatim: {len(skip_reasons)}")
        for ln, reason in skip_reasons[:3]:
            print(f"    line {ln}: {reason}")

    # Determine which chunks to modify
    modified = 0
    matches: list[tuple[str, list[str], list[str]]] = []  # (cid, before, after)

    for chunk in chunks:
        # Apply filters
        if args.filter_source_kind and chunk.get("source_kind") != args.filter_source_kind:
            continue
        if args.filter_source and args.filter_source not in chunk.get("source_file", ""):
            continue
        if args.filter_content and args.filter_content not in chunk.get("content", ""):
            continue

        old_tags = list(chunk.get("tags", []))
        new_tags = list(old_tags)

        if args.add_tag:
            if args.add_tag not in new_tags:
                new_tags.append(args.add_tag)
        elif args.remove_tag:
            new_tags = [t for t in new_tags if t != args.remove_tag]
        elif args.set_tags is not None:
            new_tags = list(args.set_tags)

        if new_tags != old_tags:
            chunk["tags"] = new_tags
            modified += 1
            matches.append((chunk.get("chunk_id", ""), old_tags, new_tags))

    print()
    print(f"Would modify: {modified} chunks")
    if matches:
        print("Sample changes (first 5):")
        for cid, before, after in matches[:5]:
            print(f"  {cid[:40]}...")
            print(f"    before: {before}")
            print(f"    after:  {after}")

    if args.dry_run:
        print()
        print("=" * 60)
        print("DRY RUN - pass --apply to actually write")
        print(f"Backup would be: {BACKUP_PATH}")
        print(f"Temp file: {TMP_PATH}")
        print(f"Atomic rename: {TMP_PATH} -> {DATA_PATH}")
        return 0

    # Write atomically
    print("\nWriting atomically...")
    backup_data = DATA_PATH.read_bytes()
    BACKUP_PATH.write_bytes(backup_data)
    print(f"Backup: {BACKUP_PATH} ({len(backup_data):,} bytes)")

    # Build output: all original lines, with modified chunks rewritten
    skip_line_set = {ln for ln, _ in skip_reasons}
    output_lines: list[str] = []
    chunk_iter = iter(chunks)

    for i, raw_line in enumerate(raw_lines, 1):
        if i in skip_line_set:
            output_lines.append(raw_line)
            continue
        try:
            rec = next(chunk_iter)
        except StopIteration:
            break
        output_lines.append(json.dumps(rec))

    new_content = "\n".join(output_lines)
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"

    TMP_PATH.write_text(new_content)
    print(f"Temp: {TMP_PATH} ({len(new_content):,} bytes)")

    os.replace(TMP_PATH, DATA_PATH)
    print(f"Renamed: {TMP_PATH} -> {DATA_PATH}")

    print()
    print("=" * 60)
    print(f"DONE modified={modified} chunks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
