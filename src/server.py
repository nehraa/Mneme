"""
[MOCK] Mneme HTTP Server — Phase 1 CRUD endpoints.
Real implementation: all endpoints call Neo4jMemoryRepository (not yet wired).
Phase 1 is "done" when all endpoints return correct JSON and mock records have _mock: True.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.memory_store import get_repository
from src.models import next_chunk_id

# ── Request / Response models ─────────────────────────────────────────────────


class CreateChunkRequest(BaseModel):
    """Payload for POST /memories."""

    content: str = Field(..., description="The chunk text content")
    session_id: str = Field(..., description="Conversation file path (session ID)")
    project_root: str = Field(default="", description="Git repo root for namespace")
    tags: list[str] = Field(default_factory=list)
    source_file: str | None = None
    page_order: int = 0
    outcome_tag: str = Field(default="work_done")
    linked_chunks: list[str] = Field(default_factory=list)


class UpdateTagsRequest(BaseModel):
    """Payload for PATCH /memories/{chunk_id}/tags."""

    tags: list[str]


class IngestRequest(BaseModel):
    """Payload for POST /ingest (Phase 2 stub)."""

    file_paths: list[str]


class RetrieveRequest(BaseModel):
    """Payload for POST /retrieve (Phase 4 stub)."""

    prompt_context: str
    session_id: str | None = None


class GuardRequest(BaseModel):
    """Payload for POST /guard (Phase 5 stub)."""

    proposed_change: str
    target_file: str
    session_id: str | None = None


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Mneme",
    version="0.1.0",
    description="Agentic hybrid memory system with RAG",
)

# Lazy-initialized repository — swap get_repository(use_mock=False) for real Neo4j
_repo: Any = None


def repo() -> Any:
    global _repo
    if _repo is None:
        _repo = get_repository(use_mock=True)
    return _repo


# ── Health ────────────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


# ════════════════════════════════════════════════════════════════════════════
# Phase 1 — Memory Store CRUD
# ════════════════════════════════════════════════════════════════════════════


@app.post("/memories", status_code=201, tags=["Phase 1"])
def create_memory(body: CreateChunkRequest) -> JSONResponse:
    """
    POST /memories — create a new memory chunk.

    [MOCK] Phase 1: stores in-memory dict.
    Real implementation → Neo4jMemoryRepository.create_chunk()
    """
    chunk_id = next_chunk_id()
    now = datetime.now(timezone.utc).isoformat()

    # Parse flat tags into a simple tag_tree dict
    tag_tree = _parse_tags(body.tags)

    record = {
        "_mock": True,
        "chunk_id": chunk_id,
        "session_id": body.session_id,
        "project_root": body.project_root,
        "content": body.content,
        "page_order": body.page_order,
        "tags": body.tags,
        "tag_tree": tag_tree,
        "linked_chunks": body.linked_chunks,
        "outcome_tag": body.outcome_tag,
        "source_file": body.source_file,
        "created_at": now,
        "last_accessed": None,
    }

    repo().create_chunk(record)
    return JSONResponse(content=record, status_code=201)


@app.get("/memories/{chunk_id}", tags=["Phase 1"])
def get_memory(chunk_id: str) -> JSONResponse:
    """
    GET /memories/{chunk_id} — retrieve a chunk by ID.

    [MOCK] Phase 1: looks up in-memory dict.
    Real implementation → Neo4jMemoryRepository.get_chunk()
    """
    chunk = repo().get_chunk(chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_id} not found")
    repo().touch_chunk(chunk_id)
    return JSONResponse(content=chunk)


@app.patch("/memories/{chunk_id}/tags", tags=["Phase 1"])
def update_memory_tags(
    chunk_id: str, body: UpdateTagsRequest
) -> JSONResponse:
    """
    PATCH /memories/{chunk_id}/tags — update tags on an existing chunk.

    [MOCK] Phase 1: updates in-memory dict.
    Real implementation → Neo4jMemoryRepository.update_chunk_tags()
    """
    chunk = repo().update_chunk_tags(chunk_id, body.tags)
    if chunk is None:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_id} not found")
    return JSONResponse(content=chunk)


@app.get("/memories", tags=["Phase 1"])
def list_memories(
    tag: str | None = Query(None, description="Filter by tag (e.g. 'tool=auth')"),
    session_id: str | None = Query(None, description="Filter by session ID"),
    outcome: str | None = Query(None, description="Filter by outcome tag"),
    limit: int = Query(50, ge=1, le=500),
) -> JSONResponse:
    """
    GET /memories — list chunks with optional filters.

    [MOCK] Phase 1: filters in-memory list.
    Real implementation → Neo4jMemoryRepository.list_chunks()
    """
    chunks = repo().list_chunks(
        tag=tag, session_id=session_id, outcome_tag=outcome, limit=limit
    )
    return JSONResponse(content={"chunks": chunks, "count": len(chunks)})


# ════════════════════════════════════════════════════════════════════════════
# Phase 2 — Ingestion Pipeline (stub)
# ════════════════════════════════════════════════════════════════════════════


@app.post("/ingest", tags=["Phase 2"])
def ingest(body: IngestRequest) -> JSONResponse:
    """
    POST /ingest — ingest files and create chunks.

    [MOCK] Phase 2: uses IngestionPipeline with use_mock=True.
    Swap use_mock=False to activate real MiniMax LLM chunking.
    Real implementation → ingestion/pipeline.py::IngestionPipeline.run()
    """
    from src.ingestion.pipeline import IngestionPipeline

    session_id = body.file_paths[0] if body.file_paths else "unknown"
    pipeline = IngestionPipeline(repository=repo(), use_mock=True)
    result = pipeline.run(
        file_paths=body.file_paths,
        session_id=session_id,
        project_root="",
    )
    result["_mock"] = True
    return JSONResponse(content=result)


# ════════════════════════════════════════════════════════════════════════════
# Phase 3 — Graph Index (stub)
# ════════════════════════════════════════════════════════════════════════════


@app.get("/graph/related/{chunk_id}", tags=["Phase 3"])
def get_related_chunks(
    chunk_id: str, depth: int = Query(1, ge=1, le=5)
) -> JSONResponse:
    """
    GET /graph/related/{chunk_id} — get chunks related to this chunk.

    [MOCK STUB] Phase 3: returns mock relationships without Neo4j.
    Real implementation → graph/index.py::GraphIndex.get_related()
    """
    mock_response = {
        "_mock": True,
        "chunk_id": chunk_id,
        "relationships": [
            {
                "chunk_id": "mem_007",
                "type": "same_tool_call",
                "reason": "Both deal with token refresh in auth flow — same OAuth endpoint",
            },
            {
                "chunk_id": "mem_012",
                "type": "prerequisite",
                "reason": "mem_012 sets up the auth token that mem_001 tried to use and failed",
            },
        ],
        "_implementation_note": (
            "Real: graph/index.py::GraphIndex.get_related() — "
            "queries Neo4j for edges from this chunk_id"
        ),
    }
    return JSONResponse(content=mock_response)


# ════════════════════════════════════════════════════════════════════════════
# Phase 4 — Tag-Aware Retrieval Engine (stub)
# ════════════════════════════════════════════════════════════════════════════


@app.post("/retrieve", tags=["Phase 4"])
def retrieve(body: RetrieveRequest) -> JSONResponse:
    """
    POST /retrieve — given a prompt, retrieve relevant memories.

    [MOCK STUB] Phase 4: returns mock injection without doing real retrieval.
    Real implementation → retrieval/engine.py::RetrievalEngine.retrieve()
    """
    mock_response = {
        "_mock": True,
        "detected_tags": ["outcome=failed", "tool=auth", "error=token_expired"],
        "intent": "continue_auth_flow_retry",
        "injected_context": (
            "Relevant memory from last session:\n"
            "[mem_001] Auth flow failed at token_refresh — error: token_expired. "
            "You tried fixing it by adding retry logic but stopped at line 42.\n"
            "[mem_007] Related: same tool call (auth) — successfully called after applying the fix."
        ),
        "chunks_used": ["mem_001", "mem_007"],
        "tag_matches": {
            "outcome=failed": "exact",
            "tool=auth": "exact",
            "error=token_expired": "partial",
        },
        "priority_scores": {"mem_001": 0.94, "mem_007": 0.71},
        "_implementation_note": (
            "Real: retrieval/engine.py::RetrievalEngine.retrieve() — "
            "calls Ollama intent detection + Qdrant search + Gemini tag-sort"
        ),
    }
    return JSONResponse(content=mock_response)


# ════════════════════════════════════════════════════════════════════════════
# Phase 5 — Memory Guard (stub)
# ════════════════════════════════════════════════════════════════════════════


@app.post("/guard", tags=["Phase 5"])
def guard(body: GuardRequest) -> JSONResponse:
    """
    POST /guard — check if proposed change contradicts a past failed attempt.

    [MOCK STUB] Phase 5: returns mock warning without real graph lookup.
    Real implementation → guard/diff_engine.py::DiffEngine.check()
    """
    mock_response = {
        "_mock": True,
        "guard_triggered": True,
        "warning": (
            "You tried JWT in auth/token.py in session sessions/2024-03-10 and it "
            "failed: JWT library was incompatible with the existing session middleware. "
            "mem_042 (failed, tool=auth, error=incompatible_library). "
            "Are you sure you want to retry?"
        ),
        "related_memories": ["mem_042"],
        "override_allowed": True,
        "_implementation_note": (
            "Real: guard/diff_engine.py::DiffEngine.check() — "
            "queries Neo4j for 'contradicts' edges + Qdrant semantic similarity"
        ),
    }
    return JSONResponse(content=mock_response)


# ════════════════════════════════════════════════════════════════════════════
# Phase 6 — Pre-Tool Hook / mneme_inject (stub)
# ════════════════════════════════════════════════════════════════════════════


@app.post("/inject", tags=["Phase 6"])
def inject(message: str, session_id: str | None = None) -> JSONResponse:
    """
    POST /inject — mneme_inject equivalent over HTTP.
    Orchestrates: Phase 4 retrieve + Phase 5 guard, returns injection context.

    [MOCK STUB] Phase 6: returns full mock injection without real backend.
    Real implementation → hook/mcp_tool.py::Mneme MCP tool
    """
    mock_response = {
        "_mock": True,
        "[Mneme] Pre-tool hook fired": True,
        "session": session_id or "default",
        "detected_intent": "continue_auth_flow_retry",
        "retrieved_chunks": ["mem_001", "mem_007"],
        "memory_guard": "PASSED (no contradicting failed attempts)",
        "injected_context": (
            "Relevant memory from last session:\n"
            "[mem_001] Auth flow failed at token_refresh...\n"
            "[mem_007] Same tool call — successfully called after..."
        ),
        "injected_context_length": 187,
        "_implementation_note": (
            "Real: hook/mcp_tool.py::mneme_inject — "
            "MCP tool that fires before every outbound API call in Claude Code"
        ),
    }
    return JSONResponse(content=mock_response)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_tags(tags: list[str]) -> dict[str, Any]:
    """Parse flat tags into a TagTree-like dict."""
    tag_tree: dict[str, Any] = {"category": "tool"}
    for tag in tags:
        if "=" in tag:
            key, val = tag.split("=", 1)
            if key in ("tool", "memory", "skill", "context"):
                tag_tree["category"] = key
            elif key in ("failed", "successfully_called", "no_tool_called", "stopped", "work_done"):
                tag_tree["outcome"] = val
            elif key in ("token_expired", "timeout", "auth_rejected"):
                tag_tree["error"] = val
            else:
                tag_tree[key] = val
    return tag_tree
