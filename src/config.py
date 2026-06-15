"""
[MOCK] Config — loaded from .env or environment variables.
Real implementation: values come from env vars; mock uses hardcoded defaults.
Real path: this file stays, values are loaded from os.environ / python-dotenv.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _getenv(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


@dataclass
class LLMConfig:
    """LLM provider configuration."""

    # Anthropic-compatible API (for chunking + boundary definition)
    anthropic_api_key: str | None = field(
        default_factory=lambda: _getenv("ANTHROPIC_API_KEY")
    )
    anthropic_base_url: str = field(
        default_factory=lambda: _getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    )

    # Gemini (for embedding + tag-sort during retrieval)
    gemini_api_key: str = field(
        default_factory=lambda: _getenv(
            "GEMINI_API_KEY", "AIzaSyAQR7zEAhU5Gp_q2JXNkokN0c8AOYlWgQI"
        )
    )
    gemini_embedding_model: str = field(
        default_factory=lambda: _getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-2")
    )

    # Ollama (local intent detection)
    ollama_base_url: str = field(
        default_factory=lambda: _getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    )
    ollama_model: str = field(
        default_factory=lambda: _getenv("OLLAMA_MODEL", "phi-4-mini")
    )


@dataclass
class QdrantConfig:
    """Qdrant vector store configuration."""

    host: str = field(default_factory=lambda: _getenv("QDRANT_HOST", "http://localhost:6333"))
    collection: str = field(default_factory=lambda: _getenv("QDRANT_COLLECTION", "mneme_chunks"))
    vector_size: int = 768  # gemini-embedding-2 dimension


@dataclass
class Neo4jConfig:
    """Neo4j graph database configuration."""

    uri: str = field(default_factory=lambda: _getenv("NEO4J_URI", "bolt://localhost:7687"))
    user: str = field(default_factory=lambda: _getenv("NEO4J_USER", "neo4j"))
    password: str = field(default_factory=lambda: _getenv("NEO4J_PASSWORD", "password"))


@dataclass
class ServerConfig:
    """HTTP server configuration."""

    host: str = field(default_factory=lambda: _getenv("MNEME_HOST", "localhost"))
    port: int = field(default_factory=lambda: int(_getenv("MNEME_PORT", "8080")))


@dataclass
class MnemeConfig:
    """Top-level configuration container."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    qdrant: QdrantConfig = field(default_factory=QdrantConfig)
    neo4j: Neo4jConfig = field(default_factory=Neo4jConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


# Global singleton — lazily initialized
_config: MnemeConfig | None = None


def get_config() -> MnemeConfig:
    global _config
    if _config is None:
        _config = MnemeConfig()
    return _config
