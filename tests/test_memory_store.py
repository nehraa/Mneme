"""
Tests for Phase 1: Memory Store CRUD + API endpoints.

The server backs onto the real in-memory repository (overridden via the
`server.get_repository` factory) so the request/response cycle is exercised
end-to-end without Neo4j.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import server
from src.memory_store.repository import InMemoryMemoryRepository
from src.server import app


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """FastAPI test client with a fresh in-memory repository per test."""
    server._repo = None
    monkeypatch.setattr(
        server, "get_repository", lambda: InMemoryMemoryRepository()
    )
    return TestClient(app)


class TestHealth:
    def test_health(self, client: TestClient):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestCreateMemory:
    def test_create_chunk_returns_correct_schema(self, client: TestClient):
        payload = {
            "content": "Auth flow failed at token_refresh — error: token_expired at src/auth/login.py:42",
            "session_id": "/Users/abhinav/chat/sessions/2024-03-15-auth-flow.md",
            "project_root": "/Users/abhinav/git/myproject",
            "tags": ["tool=auth", "outcome=failed", "error=token_expired"],
            "source_file": "src/auth/login.py",
            "page_order": 3,
            "outcome_tag": "failed",
            "linked_chunks": ["mem_007", "mem_012"],
        }
        resp = client.post("/memories", json=payload)
        assert resp.status_code == 201
        data = resp.json()

        # Schema checks
        assert data["chunk_id"].startswith("mem_")
        assert data["content"] == payload["content"]
        # Tags: caller's tags are preserved + dynamic ones appended (e.g. file=login, language=py)
        for required_tag in payload["tags"]:
            assert required_tag in data["tags"], f"missing required tag: {required_tag}"
        # Dynamic tags should also be present (inferred from content + source_file)
        assert "file=login" in data["tags"]  # inferred from source_file
        assert "language=py" in data["tags"]   # inferred from .py extension
        assert data["outcome_tag"] == "failed"
        assert data["source_file"] == "src/auth/login.py"
        assert data["page_order"] == 3
        assert data["linked_chunks"] == ["mem_007", "mem_012"]
        assert data["session_id"] == payload["session_id"]
        assert data["project_root"] == payload["project_root"]
        assert data["created_at"] is not None
        assert data["last_accessed"] is None

    def test_create_chunk_all_outcome_tags(self, client: TestClient):
        for outcome in ["work_done", "no_tool_called", "successfully_called", "failed", "stopped"]:
            payload = {
                "content": f"Test content for {outcome}",
                "session_id": "/test/session.md",
                "tags": [f"outcome={outcome}"],
                "outcome_tag": outcome,
            }
            resp = client.post("/memories", json=payload)
            assert resp.status_code == 201, f"Failed for outcome={outcome}"
            assert resp.json()["outcome_tag"] == outcome


class TestGetMemory:
    def test_get_chunk_not_found(self, client: TestClient):
        resp = client.get("/memories/mem_99999")
        assert resp.status_code == 404

    def test_get_chunk_updates_last_accessed(self, client: TestClient):
        create_resp = client.post(
            "/memories",
            json={
                "content": "Test chunk",
                "session_id": "/test/session.md",
                "tags": [],
            },
        )
        chunk_id = create_resp.json()["chunk_id"]

        # GET is pure read — last_accessed should NOT be set yet
        get_resp = client.get(f"/memories/{chunk_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["last_accessed"] is None

        # Explicitly touch the chunk — last_accessed should now be set
        touch_resp = client.post(f"/memories/{chunk_id}/touch")
        assert touch_resp.status_code == 200
        get_after = client.get(f"/memories/{chunk_id}")
        assert get_after.json()["last_accessed"] is not None


class TestUpdateTags:
    def test_update_tags_success(self, client: TestClient):
        create_resp = client.post(
            "/memories",
            json={
                "content": "Test chunk",
                "session_id": "/test/session.md",
                "tags": ["tool=auth"],
            },
        )
        chunk_id = create_resp.json()["chunk_id"]

        update_resp = client.patch(
            f"/memories/{chunk_id}/tags",
            json={"tags": ["tool=auth", "outcome=failed", "error=new_error"]},
        )
        assert update_resp.status_code == 200
        assert update_resp.json()["tags"] == [
            "tool=auth",
            "outcome=failed",
            "error=new_error",
        ]

    def test_update_tags_not_found(self, client: TestClient):
        resp = client.patch(
            "/memories/mem_99999/tags",
            json={"tags": ["tool=auth"]},
        )
        assert resp.status_code == 404


class TestListMemories:
    def test_list_all(self, client: TestClient):
        resp = client.get("/memories")
        assert resp.status_code == 200
        data = resp.json()
        assert "chunks" in data
        assert "count" in data

    def test_list_filter_by_tag(self, client: TestClient):
        client.post(
            "/memories",
            json={
                "content": "DB related chunk",
                "session_id": "/test/session.md",
                "tags": ["tool=db", "outcome=failed"],
                "outcome_tag": "failed",
            },
        )
        resp = client.get("/memories?tag=tool=db")
        assert resp.status_code == 200
        chunks = resp.json()["chunks"]
        assert all("tool=db" in c["tags"] for c in chunks)

    def test_list_filter_by_outcome(self, client: TestClient):
        resp = client.get("/memories?outcome=failed")
        assert resp.status_code == 200
        chunks = resp.json()["chunks"]
        assert all(c["outcome_tag"] == "failed" for c in chunks)

    def test_list_filter_by_session(self, client: TestClient):
        resp = client.get("/memories?session_id=/test/session.md")
        assert resp.status_code == 200
        chunks = resp.json()["chunks"]
        assert all(c["session_id"] == "/test/session.md" for c in chunks)
