"""Tests for Phase 1: Memory Store CRUD + API endpoints."""
from __future__ import annotations

from fastapi.testclient import TestClient

from src.server import app

client = TestClient(app)


class TestHealth:
    def test_health(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestCreateMemory:
    def test_create_chunk_returns_mock_with_correct_schema(self):
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
        assert data["_mock"] is True
        assert data["chunk_id"].startswith("mem_")
        assert data["content"] == payload["content"]
        assert data["tags"] == payload["tags"]
        assert data["outcome_tag"] == "failed"
        assert data["source_file"] == "src/auth/login.py"
        assert data["page_order"] == 3
        assert data["linked_chunks"] == ["mem_007", "mem_012"]
        assert data["session_id"] == payload["session_id"]
        assert data["project_root"] == payload["project_root"]
        assert data["created_at"] is not None
        assert data["last_accessed"] is None

    def test_create_chunk_all_outcome_tags(self):
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
    def test_get_chunk_not_found(self):
        resp = client.get("/memories/mem_99999")
        assert resp.status_code == 404

    def test_get_chunk_updates_last_accessed(self):
        # Create
        create_resp = client.post(
            "/memories",
            json={
                "content": "Test chunk",
                "session_id": "/test/session.md",
                "tags": [],
            },
        )
        chunk_id = create_resp.json()["chunk_id"]

        # Get
        get_resp = client.get(f"/memories/{chunk_id}")
        assert get_resp.status_code == 200
        # last_accessed should now be set
        assert get_resp.json()["last_accessed"] is not None


class TestUpdateTags:
    def test_update_tags_success(self):
        # Create
        create_resp = client.post(
            "/memories",
            json={
                "content": "Test chunk",
                "session_id": "/test/session.md",
                "tags": ["tool=auth"],
            },
        )
        chunk_id = create_resp.json()["chunk_id"]

        # Update
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

    def test_update_tags_not_found(self):
        resp = client.patch(
            "/memories/mem_99999/tags",
            json={"tags": ["tool=auth"]},
        )
        assert resp.status_code == 404


class TestListMemories:
    def test_list_all(self):
        resp = client.get("/memories")
        assert resp.status_code == 200
        data = resp.json()
        assert "chunks" in data
        assert "count" in data

    def test_list_filter_by_tag(self):
        # Create chunk with known tag
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

    def test_list_filter_by_outcome(self):
        resp = client.get("/memories?outcome=failed")
        assert resp.status_code == 200
        chunks = resp.json()["chunks"]
        assert all(c["outcome_tag"] == "failed" for c in chunks)

    def test_list_filter_by_session(self):
        resp = client.get("/memories?session_id=/test/session.md")
        assert resp.status_code == 200
        chunks = resp.json()["chunks"]
        assert all(c["session_id"] == "/test/session.md" for c in chunks)


class TestIngestStub:
    def test_ingest_returns_mock_manifest(self):
        resp = client.post(
            "/ingest",
            json={"file_paths": ["src/auth/*.py", "tests/auth/*.py"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["_mock"] is True
        assert data["chunks_created"] == 47
        assert data["edges_created"] == 12
        assert "tag_tree_summary" in data
        assert "_implementation_note" in data


class TestGraphStub:
    def test_get_related_returns_mock(self):
        resp = client.get("/graph/related/mem_001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["_mock"] is True
        assert "relationships" in data
        assert len(data["relationships"]) == 2
        assert data["relationships"][0]["type"] == "same_tool_call"
        assert "_implementation_note" in data


class TestRetrieveStub:
    def test_retrieve_returns_mock_injection(self):
        resp = client.post(
            "/retrieve",
            json={
                "prompt_context": "请继续修复auth flow，上次你停在token refresh这里",
                "session_id": "sessions/2024-03-15-auth-flow.md",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["_mock"] is True
        assert data["intent"] == "continue_auth_flow_retry"
        assert "chunks_used" in data
        assert "priority_scores" in data
        assert data["priority_scores"]["mem_001"] > data["priority_scores"]["mem_007"]


class TestGuardStub:
    def test_guard_returns_mock_warning(self):
        resp = client.post(
            "/guard",
            json={
                "proposed_change": "rewrite auth/token.py to use JWT instead of session cookies",
                "target_file": "auth/token.py",
                "session_id": "sessions/2024-03-15-auth-flow.md",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["_mock"] is True
        assert data["guard_triggered"] is True
        assert data["override_allowed"] is True
        assert "mem_042" in data["warning"]


class TestInjectStub:
    def test_inject_returns_full_mock(self):
        resp = client.post(
            "/inject",
            params={
                "message": "请继续修复auth flow",
                "session_id": "sessions/2024-03-15-auth-flow.md",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["_mock"] is True
        assert "[Mneme] Pre-tool hook fired" in data
        assert data["memory_guard"] == "PASSED (no contradicting failed attempts)"
        assert len(data["retrieved_chunks"]) == 2
