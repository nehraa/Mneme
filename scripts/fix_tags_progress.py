#!/usr/bin/env python3
"""
fix_tags_progress.py — Report fix_tags.py progress as a percentage.

Reads:
  - /tmp/fix_tags.log (current run's log)
  - data/skill_chunks.jsonl (current state)
  - data/skill_chunks.jsonl.fix_tags_bak (pre-run state)

Computes:
  - heuristic chunks remaining (by comparing current vs backup)
  - processed-so-far count from the most recent [FIX_TAGS] log line
  - total selected at start of run
  - elapsed wall-clock
  - rate (chunks/sec) and ETA
  - process status (PID 1397059 alive?)

Sends to Telegram home channel via send_message.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

LOG_PATH = Path("/tmp/fix_tags.log")
DATA_PATH = Path("/home/Hermes/Mneme/data/skill_chunks.jsonl")
BACKUP_PATH = Path("/home/Hermes/Mneme/data/skill_chunks.jsonl.fix_tags_bak")
PID = 1397059

IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now() -> str:
    return datetime.now(IST).strftime("%H:%M IST")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _latest_progress_line() -> tuple[int, int] | None:
    """Return (processed, total) from the most recent [FIX_TAGS] log line."""
    if not LOG_PATH.exists():
        return None
    pattern = re.compile(r"\[FIX_TAGS\]\s+(\d+)/(\d+)\s+processed")
    latest: tuple[int, int] | None = None
    # Read backwards — file is small (~21KB), but tail the last ~50 lines is enough
    try:
        with LOG_PATH.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 16384)
            f.seek(size - chunk)
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        m = pattern.search(line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def _heuristic_remaining() -> int | None:
    """Count chunks still tagged with source=heuristic in the current jsonl."""
    if not DATA_PATH.exists():
        return None
    count = 0
    try:
        with DATA_PATH.open() as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    c = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "source=heuristic" in (c.get("tags") or []):
                    count += 1
    except OSError:
        return None
    return count


def _started_at() -> str | None:
    """Parse the first [FIX_TAGS] log line's timestamp."""
    if not LOG_PATH.exists():
        return None
    try:
        with LOG_PATH.open() as f:
            for line in f:
                if "[FIX_TAGS]" in line:
                    m = re.match(r"^([\d\-]+ [\d:]+),", line)
                    if m:
                        return m.group(1)
    except OSError:
        return None
    return None


def _started_at_et() -> float | None:
    """Return process start time from ps, as epoch seconds."""
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(PID), "-o", "lstart="], text=True
        ).strip()
        if not out:
            return None
        # lstart format: "Tue Jun 23 20:32:00 2026"  (LOCAL time)
        dt = datetime.strptime(out, "%a %b %d %H:%M:%S %Y")
        # Treat as IST (this server's local time) and convert to epoch
        return dt.replace(tzinfo=IST).timestamp()
    except (subprocess.CalledProcessError, ValueError):
        return None


def build_report() -> str:
    alive = _pid_alive(PID)
    if not alive:
        return (
            f"⚠️ *fix_tags: process 1397059 is NOT running*\n"
            f"_checked at {_ist_now()}_"
        )

    progress = _latest_progress_line()
    heuristic_remaining = _heuristic_remaining()

    # Process elapsed from ps lstart
    started_epoch = _started_at_et()
    elapsed_str = "?"
    if started_epoch:
        elapsed_sec = max(0, int(datetime.now(timezone.utc).timestamp() - started_epoch))
        h, rem = divmod(elapsed_sec, 3600)
        m, s = divmod(rem, 60)
        elapsed_str = f"{h}h {m}m"

    if progress is None and heuristic_remaining is None:
        return (
            f"🟡 *fix_tags: no progress data yet*\n"
            f"pid {PID} alive • started {elapsed_str} ago • {_ist_now()}"
        )

    if progress is not None:
        processed, total = progress
        pct = (processed / total) * 100 if total else 0.0
        rate = processed / max(1, _elapsed_seconds(started_epoch)) if started_epoch else 0
        eta_sec = (total - processed) / rate if rate > 0 else 0
        eta_str = _fmt_eta(eta_sec)
        bar = _bar(pct)
        # Remaining heuristic count from log progress (more current than jsonl count)
        msg = (
            f"🔄 *fix_tags: {processed:,} / {total:,}  ({pct:.2f}%)*\n"
            f"{bar}\n"
            f"• elapsed: {elapsed_str}\n"
            f"• rate: {rate:.3f} chunks/s\n"
            f"• ETA: ~{eta_str}\n"
            f"• pid: {PID} ✓ alive\n"
            f"_checked at {_ist_now()}_"
        )
        if heuristic_remaining is not None:
            msg += f"\n• heuristic chunks left in jsonl: {heuristic_remaining:,}"
        return msg

    # Fallback: only jsonl count
    if heuristic_remaining is not None:
        msg = (
            f"🟡 *fix_tags: log progress unreadable*\n"
            f"• heuristic chunks in jsonl: {heuristic_remaining:,}\n"
            f"• pid {PID} alive • started {elapsed_str} ago\n"
            f"_checked at {_ist_now()}_"
        )
        return msg

    return (
        f"🟡 *fix_tags: partial data*\n"
        f"• pid {PID} alive • started {elapsed_str} ago • {_ist_now()}"
    )


def _elapsed_seconds(started_epoch: float | None) -> int:
    if not started_epoch:
        return 1
    return max(1, int(datetime.now(timezone.utc).timestamp() - started_epoch))


def _fmt_eta(eta_sec: float) -> str:
    if eta_sec <= 0 or eta_sec > 1e9:
        return "—"
    s = int(eta_sec)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def _bar(pct: float, width: int = 20) -> str:
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled) + f" {pct:.2f}%"


if __name__ == "__main__":
    print(build_report())
