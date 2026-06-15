"""Memory store module — CRUD for Memory Chunks and Graph Edges."""
from src.memory_store.repository import MemoryRepository, MockMemoryRepository

__all__ = ["MemoryRepository", "MockMemoryRepository", "get_repository"]


def get_repository(use_mock: bool = True) -> MemoryRepository:
    """
    Factory — returns the mock by default so Phase 1 can verify without Neo4j running.
    Pass use_mock=False to get the real Neo4j-backed implementation.
    Real implementation → memory_store/repository.py::Neo4jMemoryRepository
    """
    if use_mock:
        return MockMemoryRepository()
    # Real path: return Neo4jMemoryRepository(config=get_config().neo4j)
    raise NotImplementedError(
        "[MOCK] Real Neo4j repository not yet implemented. "
        "Set use_mock=True or implement memory_store/repository.py::Neo4jMemoryRepository"
    )
