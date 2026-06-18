"""
Gemini Embedding 2 client.

Calls Google's `gemini-embedding-2` model to produce dense vector
embeddings. The output dimensionality defaults to the model's native size
(3072) but can be reduced via `output_dimensionality` to cut storage.

API contract:
  POST https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent?key=API_KEY
  Body: {"content": {"parts": [{"text": "..."}]}, "outputDimensionality": 768}
  Returns: {"embedding": {"values": [float, ...]}}

Batch endpoint:
  POST .../models/{model}:batchEmbedContents?key=API_KEY
  Body: {"requests": [{"model": "...", "content": {"parts": [{"text": "..."}]}}, ...]}
  Returns: {"embeddings": [{"values": [...]}, ...]}

Usage:
    client = GeminiEmbeddingClient()
    vec = client.embed("hello world")              # -> list[float], length=768
    vecs = client.embed_batch(["a", "b", "c"])     # -> list[list[float]]

Environment:
    GEMINI_API_KEY            — required
    GEMINI_API_KEY_FALLBACK   — optional second key for rotation
    GEMINI_EMBEDDING_MODEL    — defaults to "gemini-embedding-2"
    EMBEDDING_DIM             — optional, defaults to 768 (cheaper than native 3072)
    EMBEDDING_BATCH_SIZE      — optional, defaults to 50
    EMBEDDING_TIMEOUT         — optional, defaults to 60 seconds
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingResult:
    """Result of embedding one text."""

    text: str
    values: list[float]
    model: str


class GeminiEmbeddingError(Exception):
    """Raised when the embedding API call fails after all retries."""


class GeminiEmbeddingClient:
    """Client for `gemini-embedding-2`.

    Features:
      - Two-key rotation (GEMINI_API_KEY + GEMINI_API_KEY_FALLBACK) so a
        429 on one key transparently rolls over to the second.
      - Batched embed_batch() — single HTTP roundtrip per batch.
      - Automatic retry with exponential backoff on 429 / 5xx.
      - Configurable output dimensionality (768 default, 3072 native).
    """

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
    DEFAULT_MODEL = "gemini-embedding-2"
    DEFAULT_DIM = 768
    DEFAULT_BATCH_SIZE = 50
    DEFAULT_TIMEOUT = 60.0
    MAX_RETRIES = 4

    def __init__(
        self,
        api_key: str | None = None,
        fallback_api_key: str | None = None,
        model: str | None = None,
        output_dimensionality: int | None = None,
        batch_size: int | None = None,
        timeout: float | None = None,
    ) -> None:
        env = os.environ
        self._api_key = api_key or env.get("GEMINI_API_KEY")
        self._fallback_key = fallback_api_key or env.get("GEMINI_API_KEY_FALLBACK")
        if not self._api_key:
            raise GeminiEmbeddingError(
                "GEMINI_API_KEY not set. Set it in .env or pass api_key=..."
            )
        self._model = model or env.get("GEMINI_EMBEDDING_MODEL", self.DEFAULT_MODEL)
        self._dim = output_dimensionality or int(
            env.get("EMBEDDING_DIM", str(self.DEFAULT_DIM))
        )
        self._batch_size = batch_size or int(
            env.get("EMBEDDING_BATCH_SIZE", str(self.DEFAULT_BATCH_SIZE))
        )
        self._timeout = timeout or float(
            env.get("EMBEDDING_TIMEOUT", str(self.DEFAULT_TIMEOUT))
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def embed(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float]:
        """Embed a single string. Returns a list of floats."""
        result = self._call_single(text, task_type=task_type)
        return result.values

    def embed_query(self, text: str) -> list[float]:
        """Embed a query (uses RETRIEVAL_QUERY task type for asymmetric search)."""
        return self.embed(text, task_type="RETRIEVAL_QUERY")

    def embed_batch(
        self,
        texts: list[str],
        task_type: str = "RETRIEVAL_DOCUMENT",
    ) -> list[list[float]]:
        """Embed a list of strings in one batched HTTP call.

        Splits into chunks of `batch_size` and merges results. Returns one
        vector per input string, in input order.
        """
        if not texts:
            return []

        all_vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            chunk = texts[start : start + self._batch_size]
            vectors = self._call_batch(chunk, task_type=task_type)
            if len(vectors) != len(chunk):
                raise GeminiEmbeddingError(
                    f"batch size mismatch: sent {len(chunk)}, got {len(vectors)}"
                )
            all_vectors.extend(vectors)
        return all_vectors

    # ── Internal HTTP ─────────────────────────────────────────────────────────

    def _url(self, op: str, key: str) -> str:
        return f"{self.BASE_URL}/{self._model}:{op}?key={key}"

    def _call_single(self, text: str, task_type: str) -> EmbeddingResult:
        url = self._url("embedContent", self._api_key)
        payload = {
            "content": {"parts": [{"text": text}]},
            "outputDimensionality": self._dim,
            "taskType": task_type,
        }
        data = self._post_with_retry(url, payload, use_fallback_url=False)
        return EmbeddingResult(
            text=text,
            values=data["embedding"]["values"],
            model=self._model,
        )

    def _call_batch(
        self, texts: list[str], task_type: str
    ) -> list[list[float]]:
        url = self._url("batchEmbedContents", self._api_key)
        payload = {
            "requests": [
                {
                    "model": f"models/{self._model}",
                    "content": {"parts": [{"text": t}]},
                    "outputDimensionality": self._dim,
                    "taskType": task_type,
                }
                for t in texts
            ]
        }
        data = self._post_with_retry(url, payload, use_fallback_url=False)
        return [item["values"] for item in data.get("embeddings", [])]

    def _post_with_retry(
        self,
        url: str,
        payload: dict[str, Any],
        use_fallback_url: bool,
    ) -> dict[str, Any]:
        """POST with exponential backoff and key rotation on 429.

        On a 429 from the primary key, swaps to the fallback key for the
        remainder of this call (and future calls in this process).
        """
        keys_to_try = [self._api_key]
        if self._fallback_key and self._fallback_key != self._api_key:
            keys_to_try.append(self._fallback_key)

        last_error: Exception | None = None
        for key_index, key in enumerate(keys_to_try):
            attempt_url = url.replace(self._api_key, key)
            for attempt in range(self.MAX_RETRIES):
                try:
                    with httpx.Client(timeout=self._timeout) as client:
                        response = client.post(attempt_url, json=payload)
                    if response.status_code == 429:
                        # Rate limited — backoff then either retry same key or rotate
                        wait = min(2 ** attempt * 5, 70)
                        logger.warning(
                            "gemini_embed_429 key=%s attempt=%d wait=%ds",
                            key[:8] + "...",
                            attempt + 1,
                            wait,
                        )
                        time.sleep(wait)
                        continue
                    if response.status_code in (500, 502, 503, 504):
                        wait = min(2 ** attempt * 2, 30)
                        logger.warning(
                            "gemini_embed_%d key=%s attempt=%d wait=%ds",
                            response.status_code,
                            key[:8] + "...",
                            attempt + 1,
                            wait,
                        )
                        time.sleep(wait)
                        continue
                    response.raise_for_status()
                    return response.json()
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    logger.error(
                        "gemini_embed_http_error status=%d body=%s",
                        exc.response.status_code,
                        exc.response.text[:300],
                    )
                    if exc.response.status_code in (400, 401, 403):
                        # Don't retry on auth/validation errors — they won't fix
                        raise GeminiEmbeddingError(
                            f"HTTP {exc.response.status_code}: "
                            f"{exc.response.text[:300]}"
                        ) from exc
                    time.sleep(min(2 ** attempt * 2, 30))
                except httpx.RequestError as exc:
                    last_error = exc
                    logger.warning(
                        "gemini_embed_request_error attempt=%d err=%s",
                        attempt + 1,
                        exc,
                    )
                    time.sleep(min(2 ** attempt * 2, 30))

            # All retries on this key exhausted — try next key
            logger.warning(
                "gemini_embed_rotating_key key_index=%d",
                key_index,
            )
            self._api_key = key  # so the URL replacement above stays consistent

        raise GeminiEmbeddingError(
            f"all keys exhausted: {last_error}"
        )


# ── EmbeddingFn type alias used by guard/diff_engine ────────────────────────
# Anything with `def embed(text: str) -> list[float]` satisfies this.
EmbeddingFn = GeminiEmbeddingClient
