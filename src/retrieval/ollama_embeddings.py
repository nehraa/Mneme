"""
[REAL] Ollama Embeddings Client — local embedding generation via Ollama API.

Provides dense embeddings for semantic similarity using locally-hosted models
(e.g. qwen3:0.6b, nomic-embed-text). No API key required; runs entirely offline.

Ollama API endpoint:
    POST {base_url}/api/embeddings
    Body: {"model": "...", "prompt": "..."}
    Response: {"embedding": [float, ...]}

The client is a thin wrapper. It does NOT cache results — caching is
the caller's responsibility if needed.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen3-embedding:0.6b"
DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_TIMEOUT = 120.0


def _cosine(v1: list[float], v2: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = sum(a * a for a in v1) ** 0.5
    norm2 = sum(b * b for b in v2) ** 0.5
    return dot / (norm1 * norm2 + 1e-10)


class OllamaEmbeddingClient:
    """
    Real Ollama embedding client for local embedding generation.

    Reads configuration from environment variables or constructor parameters.
    """

    def __init__(
        self,
        api_key: str | None = None,  # accepted for interface compat; Ollama has no key
        model: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        env_model = os.environ.get("OLLAMA_EMBEDDING_MODEL")
        self._model = model or env_model or DEFAULT_MODEL
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def embed(self, text: str) -> list[float]:
        """
        Generate an embedding for the given text.

        Returns a dense vector of dimension matching the configured model
        (e.g. 3584 for qwen3:0.6b).

        Raises:
            RuntimeError: If the API call fails or returns invalid data.
            ValueError: If the input text is empty.
        """
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")

        url = f"{self._base_url}/api/embeddings"
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": text[:8000],  # truncate to fit context window
        }
        headers = {"Content-Type": "application/json"}

        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"[OLLAMA] Embedding request failed (HTTP {exc.response.status_code}): "
                f"{exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"[OLLAMA] Embedding request failed: {exc}") from exc

        data = resp.json()

        try:
            embedding: list[float] = data["embedding"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(
                f"[OLLAMA] Unexpected response shape: {data}"
            ) from exc

        if not isinstance(embedding, list):
            raise RuntimeError(
                f"[OLLAMA] Expected embedding to be a list, got {type(embedding).__name__}"
            )

        return embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts.

        Sends each text in a separate request (Ollama's batch endpoint shares
        the same shape, so per-text is safer and avoids truncation issues).

        Args:
            texts: List of strings to embed.

        Returns:
            List of embedding vectors (one per input text).

        Raises:
            RuntimeError: If any embedding request fails.
        """
        if not texts:
            return []
        return [self.embed(text) for text in texts]

    def batch_embed_with_similarity(
        self,
        texts: list[str],
        similarity_threshold: float = 0.95,
    ) -> tuple[list[list[float]], list[tuple[int, int, float]]]:
        """
        Embed multiple texts and compute pairwise cosine similarities.

        Useful during ingestion to detect near-duplicate / same-content chunks
        and create graph relationships in Neo4j without a second pass.

        Args:
            texts: List of strings to embed.
            similarity_threshold: Minimum cosine similarity to flag a pair
                as a duplicate (default 0.95 — very high fidelity).

        Returns:
            A 2-tuple of:
              - List of embedding vectors (same order as input texts)
              - List of (i, j, similarity) tuples for all pairs where
                similarity >= threshold, i < j (deduplicated).
        """
        if not texts:
            return [], []

        embeddings = self.embed_batch(texts)
        duplicates: list[tuple[int, int, float]] = []

        for i in range(len(embeddings)):
            for j in range(i + 1, len(embeddings)):
                sim = _cosine(embeddings[i], embeddings[j])
                if sim >= similarity_threshold:
                    duplicates.append((i, j, sim))

        return embeddings, duplicates
