"""
[REAL] Gemini Embeddings Client — Google's text-embedding-004 model.

Provides 768-dimensional embeddings for semantic similarity.

The Gemini API uses Google's Generative AI endpoint:
    POST https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent

API key is read from GEMINI_API_KEY env var. Fails loudly if not set.

The client is a thin wrapper around the embedContent endpoint. It does
NOT cache results — caching is the caller's responsibility if needed.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

import httpx

from src.llm_utils import post_with_rate_limit_retry


logger = logging.getLogger(__name__)


# Gemini embedding API constants
DEFAULT_MODEL = "gemini-embedding-2"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
# gemini-embedding-2 default output dimension. Use `outputDimensionality`
# in the request body to get a different size (e.g. 768 for cheaper storage).
EMBEDDING_DIM = 3072
DEFAULT_TIMEOUT = 30.0


class GeminiEmbeddingClient:
    """
    Real Gemini embedding client for text-embedding-004.

    Uses the Google Generative AI embedContent endpoint.
    Reads API key from GEMINI_API_KEY env var (no hardcoding).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        # Read API key from parameter, then env var, then fail loudly
        env_key = os.environ.get("GEMINI_API_KEY")
        if api_key:
            self._api_key = api_key
        elif env_key:
            self._api_key = env_key
        else:
            raise RuntimeError(
                "[GEMINI] API key not found. "
                "Set GEMINI_API_KEY environment variable or pass api_key=..."
            )

        # Model selection: parameter > env var > default
        env_model = os.environ.get("GEMINI_EMBEDDING_MODEL")
        self._model = model or env_model or DEFAULT_MODEL
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def embed(self, text: str) -> list[float]:
        """
        Generate an embedding for the given text.

        Returns a 768-dimensional vector (for gemini-embedding-2).

        Raises:
            RuntimeError: If the API call fails or returns invalid data.
        """
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")

        # Embedding models are only available via v1, not v1beta.
        url = f"{self._base_url}/v1/models/{self._model}:embedContent"
        params = {"key": self._api_key}
        payload = {
            "model": f"models/{self._model}",
            "content": {
                "parts": [{"text": text[:8000]}]  # Truncate to fit context window
            },
        }
        headers = {"Content-Type": "application/json"}

        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = post_with_rate_limit_retry(
                    lambda: client.post(
                        url, params=params, headers=headers, json=payload
                    )
                )
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"[GEMINI] Embedding request failed (HTTP {exc.response.status_code}): "
                f"{exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"[GEMINI] Embedding request failed: {exc}") from exc

        data = resp.json()

        # Response shape: {"embedding": {"values": [float, ...]}}
        try:
            embedding = data["embedding"]["values"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(
                f"[GEMINI] Unexpected response shape: {data}"
            ) from exc

        if not isinstance(embedding, list) or len(embedding) != EMBEDDING_DIM:
            raise RuntimeError(
                f"[GEMINI] Expected {EMBEDDING_DIM}-dim embedding, got {len(embedding) if isinstance(embedding, list) else 'invalid'}"
            )

        return embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts.

        Sends each text in a separate request (Gemini's batch endpoint
        has different shapes per model, so per-text is safer).

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
