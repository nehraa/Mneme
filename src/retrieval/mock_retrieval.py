"""
[MOCK] Mock Retrieval Engine — returns deterministic mock retrieval results.
Phase 4 is "done" when this mock fires AND the real Ollama+Qdrant path is documented.
Real implementation → retrieval/engine.py::RetrievalEngine
"""
from __future__ import annotations

from typing import Any


class MockRetrievalEngine:
    """
    [MOCK] Returns deterministic mock retrieval for Phase 4 verification.
    Real backend: Ollama (intent) + Qdrant (vector search) + Gemini (tag-sort).
    """

    def retrieve(
        self,
        prompt_context: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        [MOCK] Return a deterministic mock retrieval result.
        Real implementation → retrieval/engine.py::RetrievalEngine.retrieve()
        """
        return {
            "_mock": True,
            "detected_tags": [
                "outcome=failed",
                "tool=auth",
                "error=token_expired",
            ],
            "intent": "continue_auth_flow_retry",
            "injected_context": (
                "Relevant memory from last session:\n"
                "[mem_001] Auth flow failed at token_refresh — "
                "error: token_expired. You tried fixing it by adding "
                "retry logic but stopped at line 42.\n"
                "[mem_007] Related: same tool call (auth) — "
                "successfully called after applying the fix."
            ),
            "chunks_used": ["mem_001", "mem_007"],
            "tag_matches": {
                "outcome=failed": "exact",
                "tool=auth": "exact",
                "error=token_expired": "partial",
            },
            "priority_scores": {"mem_001": 0.94, "mem_007": 0.71},
            "_implementation_note": (
                "Real: retrieval/engine.py::RetrievalEngine.retrieve() — "
                "calls Ollama intent detection + Qdrant search + Gemini tag-sort"
            ),
        }
