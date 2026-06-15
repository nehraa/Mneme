# Test Documentation — Mneme

This document explains what each test verifies and why it exists.

**Philosophy:** Every test validates a specific behavior described in SPEC.md. Tests are structured using **AAA** (Arrange → Act → Assert). Each test verifies one specific thing.

---

## Phase 1 Tests — Memory Store (12 tests)

These tests cover `src/memory_store/repository.py` and `src/server.py`.

### `TestHealth::test_health`
```python
resp = client.get("/health")
assert resp.status_code == 200
assert resp.json()["status"] == "ok"
```
**What it verifies:** The FastAPI server is reachable and responsive. This is the most basic sanity check — if this fails, the app is broken at the framework level.

---

### `TestCreateMemory::test_create_chunk_returns_mock_with_correct_schema`
```python
resp = client.post("/memories", json={full MemoryChunk payload})
assert resp.status_code == 201
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
```
**What it verifies:** The `POST /memories` endpoint accepts a full `MemoryChunk` payload and returns the exact JSON schema from SPEC.md's Phase 1 done signal. This is the most important test — every downstream phase depends on this schema being correct.

**Why `last_accessed` is `null`:** A newly created chunk has never been read, so `last_accessed` is `None`. It gets set on `GET /memories/{id}` (see `test_get_chunk_updates_last_accessed`).

---

### `TestCreateMemory::test_create_chunk_all_outcome_tags`
```python
for outcome in ["work_done", "no_tool_called", "successfully_called", "failed", "stopped"]:
    resp = client.post("/memories", json={..., "outcome_tag": outcome})
    assert resp.json()["outcome_tag"] == outcome
```
**What it verifies:** All five `OutcomeTag` values (`work_done / no_tool_called / successfully_called / failed / stopped`) serialize correctly through the API. These tags drive the **priority scoring** in Phase 4 retrieval — `failed` chunks should score 1.0, `stopped` 0.8, etc.

---

### `TestGetMemory::test_get_chunk_not_found`
```python
resp = client.get("/memories/mem_99999")
assert resp.status_code == 404
```
**What it verifies:** Requesting a non-existent chunk returns 404. This is standard REST behavior and prevents silent failures.

---

### `TestGetMemory::test_get_chunk_updates_last_accessed`
```python
create_resp = client.post("/memories", json={...})
chunk_id = create_resp.json()["chunk_id"]
get_resp = client.get(f"/memories/{chunk_id}")
assert get_resp.json()["last_accessed"] is not None
```
**What it verifies:** `GET /memories/{id}` calls `repo().touch_chunk()`, which updates the `last_accessed` timestamp. This timestamp is used for **recency boost** in Phase 4's priority scoring formula: `priority = (tag_match × outcome_priority) + recency_boost`, where recent memories get +0.1.

---

### `TestUpdateTags::test_update_tags_success`
```python
create_resp = client.post("/memories", json={"tags": ["tool=auth"]})
chunk_id = create_resp.json()["chunk_id"]
update_resp = client.patch(f"/memories/{chunk_id}/tags", json={"tags": ["tool=auth", "outcome=failed"]})
assert update_resp.json()["tags"] == ["tool=auth", "outcome=failed"]
```
**What it verifies:** `PATCH /memories/{id}/tags` correctly updates the tag list. This is used after ingestion when tags need to be refined, or by the AI agent when it wants to re-tag a chunk.

---

### `TestUpdateTags::test_update_tags_not_found`
```python
resp = client.patch("/memories/mem_99999/tags", json={"tags": [...]})
assert resp.status_code == 404
```
**What it verifies:** 404 is returned when updating a non-existent chunk.

---

### `TestListMemories::test_list_all`
```python
resp = client.get("/memories")
assert "chunks" in resp.json() and "count" in resp.json()
```
**What it verifies:** `GET /memories` returns the envelope `{chunks: [...], count: N}`. The count allows clients to know total size without loading all chunks.

---

### `TestListMemories::test_list_filter_by_tag`
```python
client.post("/memories", json={"tags": ["tool=db", "outcome=failed"]})
resp = client.get("/memories?tag=tool=db")
assert all("tool=db" in c["tags"] for c in resp.json()["chunks"])
```
**What it verifies:** The `?tag=` filter correctly scopes results to only chunks containing that tag. Used in Phase 4 retrieval when narrowing down candidates.

---

### `TestListMemories::test_list_filter_by_outcome`
```python
resp = client.get("/memories?outcome=failed")
assert all(c["outcome_tag"] == "failed" for c in resp.json()["chunks"])
```
**What it verifies:** The `?outcome=` filter correctly scopes to chunks with that `outcome_tag`. Critical for Phase 4's priority scoring — we need to be able to specifically retrieve `failed` chunks when retrying.

---

### `TestListMemories::test_list_filter_by_session`
```python
resp = client.get("/memories?session_id=/test/session.md")
assert all(c["session_id"] == "/test/session.md" for c in ...)
```
**What it verifies:** Memory namespace isolation by session. Each AI agent conversation creates a new session file — memories from one session should not leak into another unless explicitly shared.

---

## Phase 2–6 Stub Tests (4 tests)

These verify that the stub endpoints are **wired** correctly — returning the right structure — even though the real implementation doesn't exist yet.

---

### `TestIngestStub::test_ingest_returns_mock_manifest`
```python
resp = client.post("/ingest", json={"file_paths": ["src/auth/*.py"]})
assert resp.json()["_mock"] is True
assert resp.json()["chunks_created"] == 47
assert resp.json()["edges_created"] == 12
assert "tag_tree_summary" in resp.json()
```
**What it verifies:** Phase 2's `/ingest` endpoint returns the `IngestionManifest` schema. When the real LLM integration is wired, this test will need to be replaced with a test that calls the actual LLM chunker.

---

### `TestGraphStub::test_get_related_returns_mock`
```python
resp = client.get("/graph/related/mem_001")
assert resp.json()["_mock"] is True
assert len(resp.json()["relationships"]) == 2
assert resp.json()["relationships"][0]["type"] == "same_tool_call"
```
**What it verifies:** Phase 3's graph endpoint returns `relationships[]` with `chunk_id`, `type`, and `reason`. Also verifies that two relationship types are returned (same_tool_call + prerequisite).

---

### `TestRetrieveStub::test_retrieve_returns_mock_injection`
```python
resp = client.post("/retrieve", json={...})
assert resp.json()["_mock"] is True
assert resp.json()["intent"] == "continue_auth_flow_retry"
assert resp.json()["priority_scores"]["mem_001"] > resp.json()["priority_scores"]["mem_007"]
```
**What it verifies:** Phase 4's retrieval endpoint returns the correct structure, AND validates the key priority rule: **`failed` chunks rank higher than `successfully_called` chunks** (mem_001=failed scores 0.94, mem_007=success scores 0.71). This is the core business logic of Mneme.

---

### `TestGuardStub::test_guard_returns_mock_warning`
```python
resp = client.post("/guard", json={"proposed_change": "rewrite auth/token.py to JWT"})
assert resp.json()["_mock"] is True
assert resp.json()["guard_triggered"] is True
assert resp.json()["override_allowed"] is True
assert "mem_042" in resp.json()["warning"]
```
**What it verifies:** Phase 5's guard returns `guard_triggered: true` with a specific memory ID in the warning. The `override_allowed: true` field is important — Mneme warns but does not block; the AI agent decides whether to proceed.

---

### `TestInjectStub::test_inject_returns_full_mock`
```python
resp = client.post("/inject", params={"message": "请继续修复auth flow", "session_id": "..."})
assert resp.json()["_mock"] is True
assert "[Mneme] Pre-tool hook fired" in resp.json()
assert resp.json()["memory_guard"] == "PASSED"
assert len(resp.json()["retrieved_chunks"]) == 2
```
**What it verifies:** Phase 6's full orchestration returns the complete injection context that would be prepended to the AI agent's prompt — including session ID, detected intent, retrieved chunks, and guard status.

---

## How to Add a Test

1. Add a test class in `tests/test_memory_store.py`
2. Use AAA pattern: create data → call endpoint → assert results
3. Assert against **specific values** from SPEC.md, not just type checks
4. If testing a mock stub, include `assert resp.json()["_mock"] is True`
5. Run: `uv run pytest tests/test_memory_store.py -v`

## Running Tests

```bash
# All tests
uv run pytest tests/ -v

# One test class
uv run pytest tests/test_memory_store.py::TestCreateMemory -v

# With coverage
uv run pytest tests/ --cov=src --cov-report=term-missing
```
