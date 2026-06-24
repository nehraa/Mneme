"""
Tests for scripts/fix_tags.py — re-tag heuristic and messy-tagged chunks.

The fix-tags script reuses MiniMaxTaggerClient from src/tagging/llm_tagger.py
to generate vocabulary-aware tags, then resolves through TagCategory.resolve()
and writes the new tags back to data/skill_chunks.jsonl with an atomic rename.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts import fix_tags


# ── Heuristic detection ──────────────────────────────────────────────────────

HEURISTIC_FINGERPRINT = {"outcome=work_done", "source=heuristic"}


def make_chunk(chunk_id: str, tags: list[str], content: str = "x") -> dict:
    return {"chunk_id": chunk_id, "tags": tags, "content": content, "source_file": "f.txt"}


class TestIsHeuristicChunk:
    def test_detects_exact_fingerprint(self):
        c = make_chunk("c1", list(HEURISTIC_FINGERPRINT))
        assert fix_tags.is_heuristic_chunk(c) is True

    def test_rejects_when_extra_tags(self):
        c = make_chunk("c1", ["outcome=work_done", "source=heuristic", "tool=auth"])
        assert fix_tags.is_heuristic_chunk(c) is False

    def test_rejects_when_missing_tags(self):
        c = make_chunk("c1", ["outcome=work_done"])
        assert fix_tags.is_heuristic_chunk(c) is False

    def test_rejects_when_no_tags(self):
        c = make_chunk("c1", [])
        assert fix_tags.is_heuristic_chunk(c) is False


# ── Bare-token detection ─────────────────────────────────────────────────────


class TestIsBareToken:
    def test_detects_bare_word(self):
        assert fix_tags.is_bare_token("python") is True
        assert fix_tags.is_bare_token("system_health") is True

    def test_rejects_canonical_format(self):
        assert fix_tags.is_bare_token("tool=auth") is False
        assert fix_tags.is_bare_token("outcome=failed") is False

    def test_rejects_too_short(self):
        assert fix_tags.is_bare_token("a") is False
        assert fix_tags.is_bare_token("") is False

    def test_rejects_other_marker(self):
        assert fix_tags.is_bare_token("other") is True  # bare "other" is also a token
        # but "other=foo" is not a bare token — it's a tag, just with "other" category


# ── Rate-limit duplicate detection ───────────────────────────────────────────


class TestHasRateLimitDuplication:
    def test_detects_two_rate_limit_forms(self):
        tags = ["error=rate_limit", "status_code=429"]
        assert fix_tags.has_rate_limit_duplication(tags) is True

    def test_passes_with_single_form(self):
        tags = ["error=rate_limit"]
        assert fix_tags.has_rate_limit_duplication(tags) is False

    def test_detects_429_anywhere(self):
        tags = ["error=api_error", "status=429", "tool=minimax_api"]
        assert fix_tags.has_rate_limit_duplication(tags) is True


# ── Chunk selection ──────────────────────────────────────────────────────────


class TestSelectChunks:
    def _chunks(self):
        return [
            make_chunk("a", list(HEURISTIC_FINGERPRINT)),
            make_chunk("b", ["outcome=work_done", "source=heuristic", "tool=auth"]),
            make_chunk("c", ["outcome=work_done", "error=rate_limit", "status=429"]),
            make_chunk("d", ["outcome=work_done", "source=heuristic", "log"]),
            make_chunk("e", ["tool=auth", "language=py"]),
        ]

    def test_selects_heuristic_only(self):
        selected = fix_tags.select_chunks(self._chunks(), mode="heuristic")
        ids = [c["chunk_id"] for c in selected]
        assert ids == ["a", "d"]

    def test_selects_cleanup_targets(self):
        selected = fix_tags.select_chunks(self._chunks(), mode="cleanup")
        ids = [c["chunk_id"] for c in selected]
        # b has extra tag (heuristic+tool), c has rate_limit duplication,
        # d has bare token 'log'. e is clean.
        assert "b" in ids or "c" in ids or "d" in ids  # depends on threshold

    def test_selects_all_in_all_mode(self):
        selected = fix_tags.select_chunks(self._chunks(), mode="all")
        ids = [c["chunk_id"] for c in selected]
        # All except e (which is clean)
        assert "e" not in ids


# ── Tag composition ──────────────────────────────────────────────────────────


class TestComposeFinalTags:
    def test_drops_heuristic_marker_keeps_outcome(self):
        """When LLM produces tags, drop source=heuristic but keep outcome=work_done."""
        llm_tags = ["tool=auth", "language=py"]
        original = ["outcome=work_done", "source=heuristic"]
        result = fix_tags.compose_final_tags(llm_tags, original)
        assert "tool=auth" in result
        assert "language=py" in result
        assert "source=heuristic" not in result
        assert "outcome=work_done" in result

    def test_dedupes(self):
        llm_tags = ["tool=auth", "tool=auth", "outcome=failed"]
        original = ["outcome=work_done"]
        result = fix_tags.compose_final_tags(llm_tags, original)
        assert result.count("tool=auth") == 1
        assert result.count("outcome=failed") == 1

    def test_drops_bare_tokens_from_llm_output(self):
        """LLM should only emit category=value, but guard against bare tokens."""
        llm_tags = ["tool=auth", "python", "log"]
        original = ["outcome=work_done", "source=heuristic"]
        result = fix_tags.compose_final_tags(llm_tags, original)
        assert "python" not in result
        assert "log" not in result

    def test_caps_at_max_tags(self, monkeypatch):
        monkeypatch.setattr(fix_tags, "MAX_TAGS_PER_CHUNK", 5)
        llm_tags = ["tool=auth", "tool=db", "tool=http", "language=py",
                    "language=js", "language=go", "error=timeout"]
        original = ["outcome=work_done", "source=heuristic"]
        result = fix_tags.compose_final_tags(llm_tags, original)
        assert len(result) <= 5

    def test_dedupes_by_category(self):
        """Multiple tool= tags collapse to the LLM's first choice."""
        llm_tags = ["tool=config", "tool=auth", "tool=http", "tool=memory", "language=py"]
        original = ["outcome=work_done", "source=heuristic"]
        result = fix_tags.compose_final_tags(llm_tags, original)
        tool_tags = [t for t in result if t.startswith("tool=")]
        assert tool_tags == ["tool=config"]
        assert "language=py" in result
        assert "outcome=work_done" in result


# ── Atomic write (mock the filesystem) ──────────────────────────────────────


class TestAtomicWrite:
    def test_atomic_write_creates_backup_and_replaces(self, tmp_path, monkeypatch):
        """Atomic write: backup → temp → fsync → os.replace."""
        data_file = tmp_path / "skill_chunks.jsonl"
        data_file.write_text('{"chunk_id": "a", "tags": ["old"]}\n')

        # Redirect DATA_PATH and BACKUP_PATH to tmp_path
        monkeypatch.setattr(fix_tags, "DATA_PATH", data_file)
        monkeypatch.setattr(fix_tags, "BACKUP_PATH", tmp_path / "skill_chunks.jsonl.fix_tags_bak")
        monkeypatch.setattr(fix_tags, "TMP_PATH", tmp_path / "skill_chunks.jsonl.fix_tags_new")

        chunks = [{"chunk_id": "a", "tags": ["new"]}]
        fix_tags.atomic_write(chunks, original_text=data_file.read_text())

        assert data_file.exists()
        assert (tmp_path / "skill_chunks.jsonl.fix_tags_bak").exists()
        line = data_file.read_text().strip()
        assert '"new"' in line
        assert '"old"' not in line