"""
Mneme — pre-tool hook orchestrator that builds injection context for Claude Code.

Orchestrates: RetrievalEngine (intent + candidate retrieval) + DiffEngine
(per-chunk contradiction check). The hook fires before every outbound API
call in Claude Code, retrieves relevant memories, checks for contradictions
with past failed attempts, and builds an "injection context" that gets
prepended to the prompt.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.guard.diff_engine import DiffEngine
    from src.memory_store import MemoryRepository
    from src.retrieval.engine import RetrievalEngine


# ── Module-level constants ────────────────────────────────────────────────────

# Status text written to `memory_guard` when no guard was triggered.
_GUARD_PASSED = "PASSED (no contradicting failed attempts)"

# Implementation note used when the real path returns the combined manifest.
_REAL_IMPL_NOTE = (
    "Real: hook/mneme.py::Mneme.inject() — "
    "orchestrates RetrievalEngine.retrieve() + DiffEngine.check()"
)


class Mneme:
    """
    Pre-tool hook: build an injection context for the next Claude Code prompt.

    Backend path:
        1. RetrievalEngine.retrieve(prompt_context=message, session_id)
           → keyword-based intent detection + repository scan + tag scoring.
           Returns a dict with `intent`, `injected_context`, `chunks_used`,
           `detected_tags`, `tag_matches`, `priority_scores`.
        2. For each retrieved chunk, DiffEngine.check(proposed_change=message,
           target_file=chunk.source_file, session_id) → repository
           'contradicts' edge lookup + Jaccard similarity. Returns a dict with
           `guard_triggered`, `warning`, `related_memories`.
        3. Aggregate guard results — if any guard fired, prefix the
           `injected_context` with a warning describing the past failure.
        4. Return the combined manifest containing intent, retrieved_chunks,
           memory_guard status, injected_context, and its length.
    """

    def __init__(
        self,
        repository: MemoryRepository,
        retrieval_engine: RetrievalEngine | None = None,
        diff_engine: DiffEngine | None = None,
    ) -> None:
        """
        Initialize Mneme.

        Args:
            repository: MemoryRepository instance for chunk/edge access.
                        Required.
            retrieval_engine: Optional RetrievalEngine override (for tests).
                              If None, a new RetrievalEngine is built using
                              `repository`.
            diff_engine: Optional DiffEngine override (for tests).
                         If None, a new DiffEngine is built using `repository`.
        """
        self._repo = repository
        self._retrieval = retrieval_engine
        self._diff = diff_engine

    def inject(
        self,
        message: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Build the injection context for a Claude Code prompt.

        Args:
            message: The user/agent message about to be sent. Treated as the
                     retrieval query AND the proposed_change for the guard.
            session_id: Optional session filter (defaults to cross-session).

        Returns:
            dict with keys:
              - session: resolved session_id (or "default")
              - detected_intent: intent label from the retrieval engine
              - retrieved_chunks: list of chunk_ids the retrieval engine returned
              - memory_guard: human-readable guard status (PASSED or warning)
              - injected_context: final context string (with warnings prepended
                                  if any guard fired)
              - injected_context_length: length of the injected_context string
              - _implementation_note: documents the real backend path
        """
        # Lazily build engines if they were not injected. This keeps the
        # public surface clean (a single `repository` is enough) while still
        # allowing tests to pass custom engines in.
        retrieval = self._build_retrieval_engine()
        diff = self._build_diff_engine()

        # 1. Retrieval — get intent + candidate chunks + base injected context.
        retrieval_result = retrieval.retrieve(
            prompt_context=message,
            session_id=session_id,
        )
        detected_intent = retrieval_result.get("intent", "")
        retrieved_chunks = retrieval_result.get("chunks_used", [])
        injected_context = retrieval_result.get("injected_context", "")

        # 2. Guard — for each chunk, check whether the proposed change
        #    contradicts a past failure. We need the full chunk dict (not
        #    just the id) to read `source_file`, so we look each one up.
        warnings: list[str] = []
        related_memories: list[str] = []
        for chunk_id in retrieved_chunks:
            chunk = self._repo.get_chunk(chunk_id) if self._repo is not None else None
            target_file = (chunk or {}).get("source_file", "")
            guard_result = diff.check(
                proposed_change=message,
                target_file=target_file,
                session_id=session_id,
            )
            if guard_result.get("guard_triggered"):
                warning = guard_result.get("warning")
                if warning:
                    warnings.append(warning)
                related_memories.extend(guard_result.get("related_memories", []))

        # 3. Aggregate guard results into the final injected_context and
        #    a human-readable memory_guard status.
        if warnings:
            memory_guard = " | ".join(warnings)
            injected_context = (
                "WARNING — past failed attempt(s) for this change:\n"
                + "\n".join(warnings)
                + "\n\n"
                + injected_context
            )
        else:
            memory_guard = _GUARD_PASSED

        return {
            "hook_fired": True,
            "session": session_id or "default",
            "detected_intent": detected_intent,
            "retrieved_chunks": retrieved_chunks,
            "memory_guard": memory_guard,
            "injected_context": injected_context,
            "injected_context_length": len(injected_context),
            "_implementation_note": _REAL_IMPL_NOTE,
        }

    # ── Private helpers ────────────────────────────────────────────────────────

    def _build_retrieval_engine(self) -> RetrievalEngine:
        """Return the configured retrieval engine, building one if needed."""
        if self._retrieval is not None:
            return self._retrieval
        from src.retrieval.engine import RetrievalEngine

        self._retrieval = RetrievalEngine(repository=self._repo)
        return self._retrieval

    def _build_diff_engine(self) -> DiffEngine:
        """Return the configured diff engine, building one if needed."""
        if self._diff is not None:
            return self._diff
        from src.guard.diff_engine import DiffEngine

        self._diff = DiffEngine(repository=self._repo)
        return self._diff
