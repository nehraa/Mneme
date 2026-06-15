"""Ingestion module — LLM-assisted chunking, linking, and tagging."""
from __future__ import annotations

from src.ingestion.pipeline import IngestionPipeline
from src.ingestion.llm_client import MiniMaxClient

__all__ = ["IngestionPipeline", "MiniMaxClient"]
