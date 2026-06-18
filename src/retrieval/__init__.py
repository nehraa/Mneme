"""Retrieval module — tag-aware memory retrieval + intent detection."""
from __future__ import annotations

from src.retrieval.engine import RetrievalEngine
from src.retrieval.qdrant_search import QdrantSearch

# BitNetClient is intentionally NOT imported here. It depends on `llama_cpp`
# (a heavy native dependency that's only available when BitNet is set up).
# Import it directly when needed: `from src.retrieval.bitnet_client import BitNetClient`
__all__ = ["RetrievalEngine", "QdrantSearch"]
