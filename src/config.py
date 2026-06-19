"""
Config — loaded from .env or environment variables.
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
    # [SECURITY] No default API key — must be set via env var or .env file.
    # Hardcoded keys leak via git history.
    gemini_api_key: str | None = field(
        default_factory=lambda: _getenv("GEMINI_API_KEY")
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

    # Ollama embeddings (local chunk embedding — alternative to Gemini)
    # qwen3-embedding:0.6b outputs 1024-dim vectors (verified June 19 2026);
    # nomic-embed-text outputs 768-dim.
    ollama_embedding_model: str = field(
        default_factory=lambda: _getenv("OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    )
    ollama_embedding_dim: int = field(
        default_factory=lambda: int(_getenv("OLLAMA_EMBEDDING_DIM", "1024"))
    )
    # Set to "ollama" to use Ollama for embeddings instead of Gemini.
    embedding_provider: str = field(
        default_factory=lambda: _getenv("EMBEDDING_PROVIDER", "gemini")
    )

    # BitNet (local LLM server, OpenAI-compatible HTTP)
    # Start with: ./scripts/start-llm-server.sh
    bitnet_host: str = field(
        default_factory=lambda: _getenv("BITNET_HOST", "localhost")
    )
    bitnet_port: int = field(
        default_factory=lambda: int(_getenv("BITNET_PORT", "8081"))
    )
    bitnet_model: str = field(
        default_factory=lambda: _getenv("BITNET_MODEL", "bitnet-b1.58-2b-4t")
    )
    bitnet_timeout: int = field(
        default_factory=lambda: int(_getenv("BITNET_TIMEOUT", "60"))
    )
    bitnet_disabled: bool = field(
        default_factory=lambda: _getenv("BITNET_DISABLED", "").lower() in ("1", "true", "yes")
    )


@dataclass
class QdrantConfig:
    """Qdrant vector store configuration."""

    host: str = field(default_factory=lambda: _getenv("QDRANT_HOST", "http://localhost:6333"))
    collection: str = field(default_factory=lambda: _getenv("QDRANT_COLLECTION", "mneme_chunks"))
    # gemini-embedding-2 output dimension. Use `outputDimensionality` in the
    # API request to reduce this (e.g. 768) for cheaper storage if needed.
    vector_size: int = 3072


@dataclass
class Neo4jConfig:
    """Neo4j graph database configuration."""

    uri: str = field(default_factory=lambda: _getenv("NEO4J_URI", "bolt://localhost:7687"))
    user: str = field(default_factory=lambda: _getenv("NEO4J_USER", "neo4j"))
    # [SECURITY] No default password — must be set via env var.
    # The default "password" was a security risk for production deployments.
    password: str | None = field(default_factory=lambda: _getenv("NEO4J_PASSWORD"))


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
