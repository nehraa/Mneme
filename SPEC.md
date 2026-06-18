# Mneme — Agentic Hybrid Memory System

## Context

**What:** A memory management system with RAG that:
1. Watches directories, chunks all files (including subfolders) using LLM-assisted boundary detection within a 512K context window
2. Links chunks both linearly (page index per file) and across files (graph index via LLM-identified relationships)
3. Tags each chunk with outcome metadata: `work_done / no_tool_called / successfully_called / failed / stopped`
4. On every AI agent prompt (proactively, before every outbound API call), fires a pre-tool hook that detects intent/tags from the incoming context, runs LLM-assisted retrieval filtered by tag priority, and injects relevant memory chunks into the context
5. LLM strategy (three-tier): Anthropic-compatible API (user provides key) for chunking + boundary definition; Gemini (user provides key, tried first) for tag sorting during retrieval with BitNet 1.58-bit (llama-server, OpenAI-compatible HTTP API, local) fallback; BitNet 1.58-bit (llama-server, OpenAI-compatible HTTP API, local) for intent detection
6. Has a "diff against memory" guard: warns before writing code that contradicts a past failed attempt

**Why:** The user wants an AI agent that remembers past work — what failed, what succeeded, what was tried — and surfaces it proactively in every new session, with a session boundary managed by `/new` (new conversation file = new session).

**Architecture:** HTTP server (background daemon) + MCP tool that wraps retrieval. FS events for file watching. Neo4j for graph index + cross-chunk relationships. Qdrant for vector search. Three-tier LLM strategy: Anthropic-compatible API for chunking (user provides key); Gemini for tag-sorting during retrieval (user provides key, fallback to BitNet 1.58-bit local); BitNet 1.58-bit (llama-server, OpenAI-compatible HTTP API, local) for intent detection. No TTL — memories persist until manually deleted.

---

## Confirmed Design Decisions

| Decision | Choice |
|---|---|
| Graph index | Neo4j (local) |
| Vector search | Qdrant (local) |
| LLM strategy | Anthropic API (chunking, user key) → Gemini (tag-sort, user key) → BitNet 1.58-bit local (intent detection) |
| Process model | HTTP server (background daemon) |
| Session boundary | File-based — `/new` creates a new conversation file, that's the session ID |
| File watcher | FS events (filesystem watchers) |
| Memory TTL | Never expire / manual delete |
| Source directory | `/git/Mneme` (the repo itself) |
| Hook mode | Proactive before every outbound API call + manual trigger |
| Memory guard | Yes — block/warn before writing code that contradicts failed attempt |
| Namespace | Project + Session (file path of conversation) |
| First phase | Phase 1: Memory Store |

---

## System Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     Mneme HTTP Server (Daemon)                    │
│  Port: 8080 (localhost) / Unix socket                             │
│                                                                  │
│  ┌──────────────┐     ┌────────────────────────────────────┐    │
│  │ FS Watcher   │────▶│  Ingestion Pipeline (512K ctx LLM) │    │
│  │ (watch dirs)  │     │  LLM: chunk boundaries + edges     │    │
│  └──────────────┘     │  + outcome tags per chunk          │    │
│                       └──────────────┬───────────────────┘    │
│         session file = new session   │                        │
│                       ┌──────────────▼───────────────────┐    │
│                       │  Neo4j (Graph Index)             │    │
│                       │  - nodes: chunks                 │    │
│                       │  - edges: cross-chunk relations  │    │
│                       │  - props: tags, page_order, etc  │    │
│                       └──────────────┬───────────────────┘    │
│                       ┌──────────────▼───────────────────┐    │
│                       │  Qdrant (Vector Index)           │    │
│                       │  - chunk embeddings              │    │
│                       │  - metadata: tags, session, etc  │    │
│                       └──────────────┬───────────────────┘    │
│                       ┌──────────────▼───────────────────┐    │
│                       │  BitNet (Intent Detector)         │    │
│                       │  1.58-bit, llama-server, local   │    │
│                       │  OpenAI-compatible HTTP API      │    │
│                       │  maps prompt → detected tags     │    │
│                       │  ─────────────────────────────  │    │
│                       │  Anthropic API (Chunking LLM)   │    │
│                       │  512K ctx, user-provided key    │    │
│                       │  ─────────────────────────────  │    │
│                       │  Gemini (Tag-sort, if available)│    │
│                       │  user-provided key, fallback     │    │
│                       └──────────────┬───────────────────┘    │
│                       ┌──────────────▼───────────────────┐    │
│                       │  Tag-Aware Retrieval Engine      │    │
│                       │  priority = (tag_match × weight)│    │
│                       │        + recency_boost          │    │
│                       │        + outcome_priority        │    │
│                       └──────────────┬───────────────────┘    │
└──────────────────────────────────────┼───────────────────────┘
                                       │
                              MCP Tool: mneme_inject
                              ┌────────▼────────┐
                              │  Pre-Tool Hook  │
                              │  (Claude Code)  │
                              │  Proactive:     │
                              │  fires before   │
                              │  every API call │
                              └─────────────────┘
```

---

## Data Models

### Memory Chunk
```python
class MemoryChunk:
    chunk_id: str              # e.g., "mem_001"
    session_id: str           # conversation file path
    project_root: str         # git repo root

    content: str               # the actual chunk text
    page_order: int            # position within source file (page index)

    tags: list[str]            # e.g., ["tool=auth", "outcome=failed", "error=token_expired"]
    tag_tree: dict             # parsed tag hierarchy: {"tool": "auth", "outcome": "failed"}

    linked_chunks: list[str]   # graph edges (cross-chunk relationships)

    outcome_tag: str           # "work_done" | "no_tool_called" | "successfully_called" | "failed" | "stopped"
    source_file: str           # absolute path of source file
    created_at: datetime
    last_accessed: datetime
```

### Graph Edge
```python
class ChunkRelationship:
    source_chunk_id: str
    target_chunk_id: str
    relationship_type: str     # "same_tool_call" | "prerequisite" | "follows" | "contradicts" | "same_concept"
    reason: str                # LLM-provided explanation
```

### Tag Tree (generated per chunk by LLM)
```
Level 1 (category):  tool | memory | skill | context
Level 2 (entity):    auth | db | http | file_io | ...
Level 3 (outcome):   failed | successfully_called | no_tool_called | stopped
Level 4 (error):     token_expired | timeout | auth_rejected | ...
```

### Outcome Priority (retrieval weighting)
```python
OUTCOME_PRIORITY = {
    "failed": 1.0,              # highest — these are the most actionable
    "stopped": 0.8,
    "work_done": 0.6,
    "successfully_called": 0.4,
    "no_tool_called": 0.2,
}
```

---

## Phase 1 — Core Memory Store ✅ DONE
**Foundation: the data model and CRUD. No RAG yet.**

**Done signal:** ✅ Verified 2026-06-15 — `uv run pytest tests/test_memory_store.py` → 16/16 PASS

```
$ curl http://localhost:8080/memories/mem_001
→ [MOCK] {
  "chunk_id": "mem_001",
  "content": "Auth flow failed at token_refresh — error: token_expired at src/auth/login.py:42",
  "tags": ["tool=auth", "outcome=failed", "error=token_expired"],
  "tag_tree": {"tool": "auth", "outcome": "failed", "error": "token_expired"},
  "source_file": "src/auth/login.py",
  "page_order": 3,
  "session_id": "/Users/abhinav/chat/sessions/2024-03-15-auth-flow.md",
  "project_root": "/Users/abhinav/git/myproject",
  "linked_chunks": ["mem_007", "mem_012"],
  "outcome_tag": "failed",
  "created_at": "2024-03-15T10:00:00Z",
  "last_accessed": null
}
→ [MOCK LABEL] swap → real Neo4j call in memory_store/repository.py
```

**Phase 1 acceptance criteria:**
- [x] MemoryChunk model defined in `src/models.py`
- [x] ChunkRelationship model defined
- [x] CRUD API: `POST /memories`, `GET /memories/{id}`, `PATCH /memories/{id}/tags`, `GET /memories?tag=X&session=Y`
- [x] Mock prints correct JSON structure; real Neo4j path documented per endpoint
- [x] Tests: 16 tests, all passing (see TEST_DOCUMENTATION.md)

---

## Phase 2 — LLM-Assisted Ingestion Pipeline
**Given files → LLM parses 512K context → chunk boundaries + cross-chunk edges + tags.**

**Done signal:**
```
$ curl -X POST http://localhost:8080/ingest \
  -d '{"file_paths": ["src/auth/*.py", "tests/auth/*.py"]}'
→ [MOCK] {
  "chunks_created": 47,
  "edges_created": 12,
  "session_id": "sessions/2024-03-15-auth-flow.md",
  "tag_tree_summary": {
    "tool=auth": {"failed": 5, "successfully_called": 31, "no_tool_called": 11},
    "tool=db":   {"failed": 2, "successfully_called": 8, "stopped": 1}
  },
  "chunks": [
    { "id": "mem_001", "content": "...", "tags": ["tool=auth","outcome=failed"],
      "linked_chunks": ["mem_007"], "page_order": 3 },
    ...
  ]
}
→ [MOCK LABEL] swap → real Anthropic API call in ingestion/llm_chunker.py (BitNet fallback if API fails)
```

**Phase 2 acceptance criteria:**
- [x] Ingestion manifest schema defined (see `src/ingestion/llm_client.py::ChunkingResult`)
- [x] LLM prompt for chunking+linking+tagging written (single 512K-context pass)
- [x] Chunk boundaries respect semantic units (function, class, test case — not arbitrary token splits)
- [x] Cross-chunk edges generated with relationship_type + reason
- [x] Outcome tags assigned per chunk (work_done/successfully_called/failed/stopped/no_tool_called)
- [x] MockIngestionPipeline returns correct structure; real path documented per method
- [x] Tests: 12 tests covering mock pipeline + endpoint (see tests/test_ingestion.py)

---

## Phase 3 — Graph Index (Neo4j)
**Graph of cross-chunk relationships. Queryable: "what's related to this chunk?"**

**Done signal:**
```
$ curl http://localhost:8080/graph/related/mem_001
→ [MOCK] {
  "chunk_id": "mem_001",
  "relationships": [
    { "chunk_id": "mem_007", "type": "same_tool_call",
      "reason": "Both deal with token refresh in auth flow — same OAuth endpoint" },
    { "chunk_id": "mem_012", "type": "prerequisite",
      "reason": "mem_012 sets up the auth token that mem_001 tried to use and failed" }
  ]
}
→ [MOCK LABEL] swap → real Neo4j traversal in graph/index.py
```

**Phase 3 acceptance criteria:**
- [x] Graph edge schema: (source, target, relationship_type, reason)
- [x] Graph query API: `GET /graph/related/{chunk_id}`, `GET /graph/chains/{chunk_id}?depth=3`
- [x] MockGraphIndex returns correct structure; real Neo4j path documented per method
- [x] Tests: 17 tests covering mock graph + both endpoints (see tests/test_graph.py)

---

## Phase 4 — Tag-Aware Retrieval Engine
**Given prompt context + detected intent → retrieve memories filtered by tag priority.**

**Done signal:**
```
$ curl -X POST http://localhost:8080/retrieve \
  -d '{"prompt_context": "请继续修复auth flow，上次你停在token refresh这里",
       "session_id": "sessions/2024-03-15-auth-flow.md"}'
→ [MOCK] {
  "detected_tags": ["outcome=failed", "tool=auth", "error=token_expired"],
  "intent": "continue_auth_flow_retry",
  "injected_context": "Relevant memory from last session:\n"
    "[mem_001] Auth flow failed at token_refresh — error: token_expired. "
    "You tried fixing it by adding retry logic but stopped at line 42.\n"
    "[mem_007] Related: same tool call (auth) — successfully called after applying the fix.",
  "chunks_used": ["mem_001", "mem_007"],
  "tag_matches": {"outcome=failed": "exact", "tool=auth": "exact", "error=token_expired": "partial"},
  "priority_scores": {"mem_001": 0.94, "mem_007": 0.71}
}
→ [MOCK LABEL] swap → BitNet intent detection + Qdrant search (+ Gemini tag-sort attempt if key provided)
```

**Phase 4 acceptance criteria:**
- [x] Intent detection: IntentDetector maps prompt → detected tags + intent label (BitNet path documented)
- [x] Priority scoring formula: `(tag_match_score × OUTCOME_PRIORITY[outcome_tag]) + recency_boost` implemented
- [x] Recency boost: memories accessed in last 7 days get +0.1 boost
- [x] MockRetrievalEngine returns correct structure; real BitNet + Qdrant + Gemini path documented
- [x] Tests: 18 tests covering intent detection, priority scoring, endpoint (see tests/test_retrieval.py)

---

## Phase 5 — Memory Guard (Diff Against Memory)
**Before AI writes code that contradicts a past failed attempt → warn.**

**Done signal:**
```
$ curl -X POST http://localhost:8080/guard \
  -d '{"proposed_change": "rewrite auth/token.py to use JWT instead of session cookies",
       "target_file": "auth/token.py",
       "session_id": "sessions/2024-03-15-auth-flow.md"}'
→ [MOCK] {
  "guard_triggered": true,
  "warning": "You tried JWT in auth/token.py in session sessions/2024-03-10 and it "
    "failed: JWT library was incompatible with the existing session middleware. "
    "mem_042 (failed, tool=auth, error=incompatible_library). "
    "Are you sure you want to retry?",
  "related_memories": ["mem_042"],
  "override_allowed": true
}
→ [MOCK LABEL] swap → real Neo4j "contradicts" edge lookup + Qdrant semantic check + BitNet intent detection
```

**Phase 5 acceptance criteria:**
- [ ] "contradicts" relationship lookup in graph (past failed attempts at same goal)
- [ ] Semantic similarity check: proposed change vs past failed chunk content
- [ ] Warning returned with memory context + `override_allowed: true` (AI can proceed)
- [ ] Mock fires correctly; real path documented

---

## Phase 6 — Pre-Tool Hook (MCP Interface)
**The MCP tool that wraps everything. Fires proactively before every outbound API call.**

**Done signal:**
```
$ mneme_inject("请继续修复auth flow")
→ [Mneme] Pre-tool hook fired
  Session: sessions/2024-03-15-auth-flow.md
  Detected intent: continue_auth_flow_retry
  Retrieved 2 chunks (1 failed, 1 success)
  Memory guard: PASSED (no contradicting failed attempts)
  Injected context length: 847 chars
  → [MEM_001] Auth flow failed at token_refresh...
  → [MEM_007] Same tool call — successfully called after...
→ [MOCK LABEL] This is where real MCP tool hook fires (Claude Code integration)
```

**Phase 6 acceptance criteria:**
- [ ] MCP tool definition: `mneme_inject(message: string, session_id?: string)`
- [ ] Calls Phase 4 retrieval + Phase 5 guard in sequence
- [ ] Output clearly labeled `[MOCK]` until real implementation
- [ ] MCP tool registration for Claude Code (tool hook)
- [ ] Tests: full injection flow with mock → correct output structure

---

## Critical Files (to be created)

```
/Users/abhinavnehra/git/Mneme/
├── SPEC.md                              # This spec
├── src/
│   ├── __init__.py
│   ├── models.py                        # MemoryChunk, ChunkRelationship, TagTree
│   ├── config.py                        # LLM API keys + provider config
│   ├── server.py                        # FastAPI HTTP server (Phases 1-6)
│   ├── memory_store/
│   │   ├── __init__.py
│   │   ├── repository.py                # Neo4j CRUD operations
│   │   └── mock_repository.py          # Phase 1 mock [MOCK]
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── pipeline.py                 # Ingestion orchestration
│   │   ├── llm_chunker.py              # LLM chunking + linking + tagging
│   │   └── mock_ingestion.py           # Phase 2 mock [MOCK]
│   ├── graph/
│   │   ├── __init__.py
│   │   ├── index.py                    # Neo4j graph operations
│   │   └── mock_graph.py               # Phase 3 mock [MOCK]
│   ├── retrieval/
│   │   ├── __init__.py
│   │   ├── engine.py                   # Tag-aware retrieval + priority scoring
│   │   ├── intent_detector.py           # BitNet intent/tag detection
│   │   └── mock_retrieval.py           # Phase 4 mock [MOCK]
│   ├── guard/
│   │   ├── __init__.py
│   │   ├── diff_engine.py              # "contradicts" lookup + semantic check
│   │   └── mock_guard.py               # Phase 5 mock [MOCK]
│   └── hook/
│       ├── __init__.py
│       ├── mcp_tool.py                  # MCP tool definition
│       └── mock_hook.py                 # Phase 6 mock [MOCK]
├── tests/
│   ├── test_memory_store.py
│   ├── test_ingestion.py
│   ├── test_graph.py
│   ├── test_retrieval.py
│   ├── test_guard.py
│   └── test_hook.py
└── pyproject.toml
```

---

## MOCK Implementation Pattern

Every mock follows this pattern:
```python
def get_memory(chunk_id: str) -> dict:
    """
    [MOCK] Returns a mock memory record.
    Real implementation → memory_store/repository.py::Neo4jMemoryRepository.get()
    """
    return {
        "_mock": True,
        "chunk_id": chunk_id,
        "content": "[MOCK] Auth flow failed — token_expired",
        "tags": ["tool=auth", "outcome=failed"],
        # ... full structure matching real return type
    }
```

Mock functions are in `mock_*.py` files. Real implementations go in `*.py`. Each mock clearly labels:
- `_mock: True` field
- docstring pointing to exact real implementation path
- `[MOCK]` prefix in any print output

Phase is "done" when: mock fires correctly AND real path is documented in docstring AND tests pass.

---

## Verification Plan

| Phase | Verification |
|---|---|
| Phase 1 | `curl http://localhost:8080/memories/mem_001` → mock JSON with correct schema |
| Phase 2 | `POST /ingest` with a Python file → correct chunk count + edges + tags |
| Phase 3 | `GET /graph/related/mem_001` → 2 related chunks with correct relationship types |
| Phase 4 | `POST /retrieve` with auth flow prompt → failed chunk ranked first |
| Phase 5 | `POST /guard` with contradicting change → warning with related memory ID |
| Phase 6 | `mneme_inject(...)` → full output with `[Mneme]` prefix and all metadata |

---

## Open Questions (resolved)

| Question | Answer |
|---|---|
| Storage backend? | Neo4j (graph) + Qdrant (vector), both local |
| Source directory? | `/git/Mneme` |
| First phase? | Phase 1: Memory Store |
| Hook mode? | Proactive before every API call + manual |
| Memory guard? | Yes — block/warn before contradicting failed attempts |
| Namespace? | Project root + session file path |
| LLM strategy? | Anthropic API (chunking) → Gemini (tag-sort) → BitNet local (intent) |
| Watcher type? | FS events |
| Memory TTL? | Never expire / manual delete |
| Process model? | HTTP server (background daemon) |
| Session model? | File-based — `/new` in AI chat = new conversation file = new session |

---

## Substitutions (Spec → Implementation Drift)

This section documents intentional drift between the original spec and the current implementation. Each substitution preserves the original architectural role but changes the specific technology.

### BitNet 1.58-bit (was: Ollama 100-200M)

- **Role:** Local LLM for intent detection (and as a fallback when Gemini is unavailable for tag-sorting during retrieval).
- **Original spec:** Ollama 100-200M local model.
- **Actual implementation:** BitNet 1.58-bit (`microsoft/BitNet-b1.58-2B-4T-gguf`), served locally via `llama-server` exposing an OpenAI-compatible HTTP API.
- **Why substituted:** BitNet provides a significantly smaller memory footprint and faster inference on Apple Silicon while preserving the same architectural role (local, offline-capable, user-controlled LLM). The OpenAI-compatible HTTP API surface keeps the integration drop-in for any client already speaking that protocol.
- **Setup notes:** See `BITNET_KNOWN_ISSUES.md` for the full setup guide, model selection, and pitfalls (TL1 kernel OOM, pretokenizer breakage, etc.).
- **Affected spec sections:** All references to "Ollama" / "Ollama 100-200M" / "Ollama local" throughout this document — including the architecture diagram, LLM strategy summary, phase acceptance criteria, and the file layout (`src/retrieval/intent_detector.py`).
