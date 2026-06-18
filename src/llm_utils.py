"""
LLM client utilities — shared HTTP retry logic for rate limiting.

Gemini's free tier returns HTTP 429 when the per-minute quota is exhausted.
This module provides a single retry-on-429 helper so all Gemini-touching
clients (embeddings, LLM tagger) behave consistently.

Note: This is for Gemini only. The chunking client
(src/ingestion/llm_client.py) uses MiniMax, which has a different rate
limit policy and is not retried here.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable

import httpx


logger = logging.getLogger(__name__)


def get_rate_limit_wait_seconds() -> float:
    """Read GEMINI_RATE_LIMIT_WAIT_SECONDS env var, defaulting to 70s."""
    return float(os.environ.get("GEMINI_RATE_LIMIT_WAIT_SECONDS", "70"))


def get_rate_limit_max_retries() -> int:
    """Read GEMINI_RATE_LIMIT_MAX_RETRIES env var, defaulting to 3."""
    return int(os.environ.get("GEMINI_RATE_LIMIT_MAX_RETRIES", "3"))


def post_with_rate_limit_retry(
    request_fn: Callable[[], httpx.Response],
    *,
    max_retries: int | None = None,
    wait_seconds: float | None = None,
) -> httpx.Response:
    """
    Call request_fn() until it succeeds or we exhaust retries on HTTP 429.

    On any non-429 HTTP error, raise_for_status propagates immediately.
    On HTTP 429, sleep `wait_seconds` and retry, up to `max_retries` times.

    Args:
        request_fn: Zero-arg callable that performs the HTTP request and
            returns an httpx.Response. Called repeatedly on 429.
        max_retries: Max retry count (default from env). Total attempts
            = max_retries + 1.
        wait_seconds: Seconds to wait between retries (default from env).

    Returns:
        The successful httpx.Response.

    Raises:
        httpx.HTTPStatusError: If a non-429 HTTP error occurs.
        RuntimeError: If max retries exhausted on 429.
    """
    if max_retries is None:
        max_retries = get_rate_limit_max_retries()
    if wait_seconds is None:
        wait_seconds = get_rate_limit_wait_seconds()

    last_response: httpx.Response | None = None
    total_attempts = max_retries + 1

    for attempt in range(total_attempts):
        response = request_fn()
        if response.status_code != 429:
            # Non-429: let raise_for_status surface any other HTTP error.
            response.raise_for_status()
            return response

        # 429 — record and (maybe) retry.
        last_response = response
        if attempt < max_retries:
            logger.warning(
                "[LLM] 429 rate limited on attempt %d/%d — waiting %.1fs before retry",
                attempt + 1,
                total_attempts,
                wait_seconds,
            )
            time.sleep(wait_seconds)

    # Exhausted retries — surface a typed error with the last response attached.
    raise RuntimeError(
        f"[LLM] Rate limited after {total_attempts} attempts "
        f"({wait_seconds:.1f}s wait between each) — giving up. "
        f"Last response body: {last_response.text[:300] if last_response else '<none>'}"
    )