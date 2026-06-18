"""
Test configuration — fixtures and environment setup.

This conftest.py:
  - Resets the vocabulary cache between tests so YAML/built-in changes
    don't leak

Note on API keys: Tests that require real API calls (LLM tagging,
Gemini embeddings, MiniMax chunking) DO NOT skip when keys are missing
or invalid. They fail loudly. Skipping tests hides real failures.
If a test fails due to API issues, that IS the test result — fix the
integration, don't hide it with a skip.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def reset_vocabulary_cache():
    """Reset the vocabulary cache between tests so tests are isolated."""
    from src.tagging import vocabulary

    vocabulary.reset_vocabulary()
    yield
    vocabulary.reset_vocabulary()
