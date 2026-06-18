"""Memory store module — CRUD for Memory Chunks and Graph Edges."""
from src.memory_store.neo4j_repository import Neo4jMemoryRepository
from src.memory_store.repository import InMemoryMemoryRepository
from src.config import get_config

# Type alias: the production repository is Neo4j-backed. Tests and local
# dev environments may swap in InMemoryMemoryRepository instead.
MemoryRepository = Neo4jMemoryRepository

__all__ = [
    "InMemoryMemoryRepository",
    "MemoryRepository",
    "Neo4jMemoryRepository",
    "get_repository",
]


def get_repository() -> Neo4jMemoryRepository:
    """
    Factory — returns a Neo4jMemoryRepository using configured credentials.

    For tests and local dev environments without a running Neo4j instance,
    instantiate InMemoryMemoryRepository directly.

    Returns
    -------
    Neo4jMemoryRepository
    """
    cfg = get_config().neo4j
    if not cfg.password:
        raise RuntimeError(
            "Neo4j password is not configured. Set MNEME_NEO4J_PASSWORD "
            "(or the relevant env var) in the environment. For local dev "
            "without Neo4j, instantiate InMemoryMemoryRepository() directly."
        )
    return Neo4jMemoryRepository(uri=cfg.uri, user=cfg.user, password=cfg.password)
