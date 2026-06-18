"""
Integration tests for Neo4jMemoryRepository.

These tests require a running Neo4j instance at bolt://localhost:7687
with credentials neo4j:mneme-dev-password.

Tests are automatically skipped when Neo4j is not available so that the
rest of the test suite (InMemoryMemoryRepository tests) can still run.
"""
from __future__ import annotations

import uuid

import pytest

from src.memory_store.neo4j_repository import Neo4jMemoryRepository

# Module-level check: skip all tests in this file if Neo4j is unreachable.
_neo4j_available: bool | None = None


def _check_neo4j() -> bool:
    """Check Neo4j connectivity. Result is cached for the duration of the test run."""
    global _neo4j_available
    if _neo4j_available is None:
        try:
            driver = Neo4jMemoryRepository(
                uri="bolt://localhost:7687",
                user="neo4j",
                password="mneme-dev-password",
            )
            driver._driver.verify_connectivity()
            driver.close()
            _neo4j_available = True
        except Exception:
            _neo4j_available = False
    return _neo4j_available


skip_if_no_neo4j = pytest.mark.skipif(
    not _check_neo4j(),
    reason="Neo4j not available at bolt://localhost:7687",
)


@pytest.fixture(scope="module")
def _driver():
    """Module-scoped driver: created once, shared by all tests, closed at end."""
    driver = Neo4jMemoryRepository(
        uri="bolt://localhost:7687",
        user="neo4j",
        password="mneme-dev-password",
    )
    yield driver
    driver.close()


@pytest.fixture
def repo(_driver: Neo4jMemoryRepository) -> Neo4jMemoryRepository:
    """Provide a Neo4jMemoryRepository backed by the module-scoped driver."""
    yield _driver


@pytest.fixture(scope="function", autouse=True)
def _cleanup_test_chunks(_driver: Neo4jMemoryRepository):
    """Delete all test-prefixed chunks before and after each test."""
    def _purge() -> None:
        if _driver._driver._closed:
            return
        _driver._run(
            "MATCH (c:Chunk) WHERE c.chunk_id STARTS WITH 'test_' DETACH DELETE c"
        )

    _purge()
    yield
    _purge()


@pytest.fixture
def chunk_a(repo: Neo4jMemoryRepository) -> dict:
    """Create and return a test chunk; caller must NOT clean up (handled by test)."""
    return repo.create_chunk({
        "chunk_id": f"test_a_{uuid.uuid4().hex[:8]}",
        "session_id": "/test/session.md",
        "project_root": "/test/project",
        "content": "Auth flow failed at token_refresh",
        "page_order": 0,
        "outcome_tag": "failed",
        "source_file": "src/auth/token.py",
        "tags": ["tool=auth", "outcome=failed", "error=token_expired"],
        "tag_tree": {"category": "tool", "outcome": "failed", "error": "token_expired"},
        "linked_chunks": [],
        "created_at": "2024-03-15T10:00:00",
        "last_accessed": None,
    })


@pytest.fixture
def chunk_b(repo: Neo4jMemoryRepository) -> dict:
    return repo.create_chunk({
        "chunk_id": f"test_b_{uuid.uuid4().hex[:8]}",
        "session_id": "/test/session.md",
        "project_root": "/test/project",
        "content": "DB query succeeded on second attempt",
        "page_order": 1,
        "outcome_tag": "work_done",
        "source_file": "src/db/query.py",
        "tags": ["tool=db", "outcome=work_done"],
        "tag_tree": {"category": "tool", "outcome": "work_done"},
        "linked_chunks": [],
        "created_at": "2024-03-15T11:00:00",
        "last_accessed": None,
    })


def _cleanup_chunk(repo: Neo4jMemoryRepository, chunk_id: str) -> None:
    """Delete a test chunk and all its edges."""
    repo._run(
        "MATCH (c:Chunk {chunk_id: $chunk_id}) DETACH DELETE c",
        chunk_id=chunk_id,
    )


# ── Tests ──────────────────────────────────────────────────────────────────────


@skip_if_no_neo4j
class TestNeo4jChunkCRUD:
    def test_create_chunk(self, repo: Neo4jMemoryRepository, chunk_a: dict):
        assert chunk_a["chunk_id"].startswith("test_a_")
        assert chunk_a["content"] == "Auth flow failed at token_refresh"
        assert chunk_a["outcome_tag"] == "failed"
        assert chunk_a["tags"] == ["tool=auth", "outcome=failed", "error=token_expired"]
        assert chunk_a["source_file"] == "src/auth/token.py"
        assert chunk_a["page_order"] == 0
        _cleanup_chunk(repo, chunk_a["chunk_id"])

    def test_get_chunk_existing(self, repo: Neo4jMemoryRepository, chunk_a: dict):
        found = repo.get_chunk(chunk_a["chunk_id"])
        assert found is not None
        assert found["chunk_id"] == chunk_a["chunk_id"]
        assert found["content"] == chunk_a["content"]
        _cleanup_chunk(repo, chunk_a["chunk_id"])

    def test_get_chunk_not_found(self, repo: Neo4jMemoryRepository):
        assert repo.get_chunk("nonexistent_id_12345") is None

    def test_update_chunk_tags(self, repo: Neo4jMemoryRepository, chunk_a: dict):
        new_tags = ["tool=auth", "outcome=failed", "error=new_error", "extra=tag"]
        updated = repo.update_chunk_tags(chunk_a["chunk_id"], new_tags)
        assert updated is not None
        assert updated["tags"] == new_tags
        _cleanup_chunk(repo, chunk_a["chunk_id"])

    def test_update_chunk_tags_not_found(self, repo: Neo4jMemoryRepository):
        assert repo.update_chunk_tags("nonexistent_id_12345", ["tag=a"]) is None

    def test_list_chunks_no_filter(self, repo: Neo4jMemoryRepository, chunk_a: dict, chunk_b: dict):
        # Use a high limit since the database may have leftover chunks from
        # previous test runs. We filter to our session to verify list_chunks
        # returns the chunks we created.
        chunks = repo.list_chunks(session_id=chunk_a["session_id"], limit=100)
        ids = [c["chunk_id"] for c in chunks]
        assert chunk_a["chunk_id"] in ids
        assert chunk_b["chunk_id"] in ids
        _cleanup_chunk(repo, chunk_a["chunk_id"])
        _cleanup_chunk(repo, chunk_b["chunk_id"])

    def test_list_chunks_filter_by_tag(self, repo: Neo4jMemoryRepository, chunk_a: dict, chunk_b: dict):
        chunks = repo.list_chunks(tag="tool=auth", limit=10)
        assert all("tool=auth" in c.get("tags", []) for c in chunks)
        _cleanup_chunk(repo, chunk_a["chunk_id"])
        _cleanup_chunk(repo, chunk_b["chunk_id"])

    def test_list_chunks_filter_by_session(self, repo: Neo4jMemoryRepository, chunk_a: dict, chunk_b: dict):
        chunks = repo.list_chunks(session_id="/test/session.md", limit=10)
        assert all(c["session_id"] == "/test/session.md" for c in chunks)
        _cleanup_chunk(repo, chunk_a["chunk_id"])
        _cleanup_chunk(repo, chunk_b["chunk_id"])

    def test_list_chunks_filter_by_outcome(self, repo: Neo4jMemoryRepository, chunk_a: dict, chunk_b: dict):
        chunks = repo.list_chunks(outcome_tag="failed", limit=10)
        assert all(c["outcome_tag"] == "failed" for c in chunks)
        _cleanup_chunk(repo, chunk_a["chunk_id"])
        _cleanup_chunk(repo, chunk_b["chunk_id"])

    def test_list_chunks_limit(self, repo: Neo4jMemoryRepository):
        # Create 5 chunks with unique IDs
        ids = []
        for i in range(5):
            cid = f"test_limit_{uuid.uuid4().hex[:8]}"
            repo.create_chunk({
                "chunk_id": cid,
                "session_id": "/test/session.md",
                "project_root": "/test/project",
                "content": f"Content {i}",
                "page_order": i,
                "outcome_tag": "work_done",
                "source_file": "src/test.py",
                "tags": [],
                "tag_tree": {},
                "linked_chunks": [],
                "created_at": "2024-03-15T10:00:00",
                "last_accessed": None,
            })
            ids.append(cid)
        chunks = repo.list_chunks(limit=3)
        assert len(chunks) == 3
        for cid in ids:
            _cleanup_chunk(repo, cid)

    def test_touch_chunk(self, repo: Neo4jMemoryRepository, chunk_a: dict):
        repo.touch_chunk(chunk_a["chunk_id"])
        updated = repo.get_chunk(chunk_a["chunk_id"])
        assert updated is not None
        assert updated["last_accessed"] is not None
        _cleanup_chunk(repo, chunk_a["chunk_id"])


@skip_if_no_neo4j
class TestNeo4jEdges:
    def test_create_edge(self, repo: Neo4jMemoryRepository, chunk_a: dict, chunk_b: dict):
        edge = repo.create_edge({
            "source_chunk_id": chunk_a["chunk_id"],
            "target_chunk_id": chunk_b["chunk_id"],
            "relationship_type": "follows",
            "reason": "Second attempt succeeded after first failed",
        })
        assert edge["source_chunk_id"] == chunk_a["chunk_id"]
        assert edge["target_chunk_id"] == chunk_b["chunk_id"]
        assert edge["relationship_type"] == "follows"
        assert edge["reason"] == "Second attempt succeeded after first failed"
        _cleanup_chunk(repo, chunk_a["chunk_id"])
        _cleanup_chunk(repo, chunk_b["chunk_id"])

    def test_get_related_chunks(self, repo: Neo4jMemoryRepository, chunk_a: dict, chunk_b: dict):
        repo.create_edge({
            "source_chunk_id": chunk_a["chunk_id"],
            "target_chunk_id": chunk_b["chunk_id"],
            "relationship_type": "follows",
            "reason": "test",
        })
        related = repo.get_related_chunks(chunk_a["chunk_id"], depth=1)
        related_ids = [r["chunk_id"] for r in related]
        assert chunk_b["chunk_id"] in related_ids
        _cleanup_chunk(repo, chunk_a["chunk_id"])
        _cleanup_chunk(repo, chunk_b["chunk_id"])

    def test_get_related_chunks_depth_2(self, repo: Neo4jMemoryRepository):
        # a -> b -> c chain
        cid_a = f"test_depth2_a_{uuid.uuid4().hex[:8]}"
        cid_b = f"test_depth2_b_{uuid.uuid4().hex[:8]}"
        cid_c = f"test_depth2_c_{uuid.uuid4().hex[:8]}"
        for cid, content in [(cid_a, "Chunk A"), (cid_b, "Chunk B"), (cid_c, "Chunk C")]:
            repo.create_chunk({
                "chunk_id": cid,
                "session_id": "/test/session.md",
                "project_root": "/test/project",
                "content": content,
                "page_order": 0,
                "outcome_tag": "work_done",
                "source_file": "src/test.py",
                "tags": [],
                "tag_tree": {},
                "linked_chunks": [],
                "created_at": "2024-03-15T10:00:00",
                "last_accessed": None,
            })
        repo.create_edge({
            "source_chunk_id": cid_a, "target_chunk_id": cid_b,
            "relationship_type": "follows", "reason": "a to b",
        })
        repo.create_edge({
            "source_chunk_id": cid_b, "target_chunk_id": cid_c,
            "relationship_type": "follows", "reason": "b to c",
        })

        # depth=1: should only see b from a
        related = repo.get_related_chunks(cid_a, depth=1)
        assert cid_b in [r["chunk_id"] for r in related]
        assert cid_c not in [r["chunk_id"] for r in related]

        # depth=2: should see both b and c from a
        related2 = repo.get_related_chunks(cid_a, depth=2)
        assert cid_b in [r["chunk_id"] for r in related2]
        assert cid_c in [r["chunk_id"] for r in related2]

        for cid in [cid_a, cid_b, cid_c]:
            _cleanup_chunk(repo, cid)

    def test_get_contradicting_chunks(self, repo: Neo4jMemoryRepository):
        # Create two chunks with CONTRADICTS edge
        cid_1 = f"test_contra_1_{uuid.uuid4().hex[:8]}"
        cid_2 = f"test_contra_2_{uuid.uuid4().hex[:8]}"
        repo.create_chunk({
            "chunk_id": cid_1,
            "session_id": "/test/session.md",
            "project_root": "/test/project",
            "content": "First auth attempt failed",
            "page_order": 0,
            "outcome_tag": "failed",
            "source_file": "auth/token.py",
            "tags": ["tool=auth", "outcome=failed"],
            "tag_tree": {},
            "linked_chunks": [],
            "created_at": "2024-03-15T10:00:00",
            "last_accessed": None,
        })
        repo.create_chunk({
            "chunk_id": cid_2,
            "session_id": "/test/session.md",
            "project_root": "/test/project",
            "content": "Second auth attempt succeeded",
            "page_order": 1,
            "outcome_tag": "work_done",
            "source_file": "auth/token.py",
            "tags": ["tool=auth", "outcome=work_done"],
            "tag_tree": {},
            "linked_chunks": [],
            "created_at": "2024-03-15T11:00:00",
            "last_accessed": None,
        })
        repo.create_edge({
            "source_chunk_id": cid_1,
            "target_chunk_id": cid_2,
            "relationship_type": "contradicts",
            "reason": "Second attempt succeeded after first failed",
        })

        contradicting = repo.get_contradicting_chunks("auth/token.py")
        assert cid_1 in [c["chunk_id"] for c in contradicting]

        # With session filter
        contradicting_sess = repo.get_contradicting_chunks(
            "auth/token.py", session_id="/test/session.md"
        )
        assert cid_1 in [c["chunk_id"] for c in contradicting_sess]

        # Different session returns nothing
        contradicting_other = repo.get_contradicting_chunks(
            "auth/token.py", session_id="/nonexistent/session.md"
        )
        assert cid_1 not in [c["chunk_id"] for c in contradicting_other]

        for cid in [cid_1, cid_2]:
            _cleanup_chunk(repo, cid)

    def test_get_contradicting_chunks_no_match(self, repo: Neo4jMemoryRepository):
        assert repo.get_contradicting_chunks("nonexistent/file.py") == []


@skip_if_no_neo4j
class TestNeo4jLifecycle:
    def test_close_then_query_raises(self, repo: Neo4jMemoryRepository):
        repo.close()
        with pytest.raises(RuntimeError, match="Neo4j connection failed"):
            repo.list_chunks()
