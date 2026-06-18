# Mneme — Agentic Hybrid Memory System

> An AI agent that remembers what it tried, what failed, what succeeded — and surfaces that context proactively before every API call.

## What is Mneme?

Mneme is a **memory management system with RAG** built for AI agents. When an AI agent works on a project, Mneme:

1. **Watches** directories and chunks all files (including subfolders) using LLM-assisted boundary detection
2. **Links** chunks linearly (page index) and across files (graph index via LLM-identified relationships)
3. **Tags** each chunk with outcome metadata: `failed / successfully_called / stopped / work_done / no_tool_called`
4. **Fires proactively** before every outbound API call — detecting intent, retrieving relevant memories, injecting context
5. **Warns** before writing code that contradicts a past failed attempt (memory guard)

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     Mneme HTTP Server (Daemon)                     │
│  Port: 8080 (localhost)                                           │
│                                                                   │
│  ┌──────────────┐     ┌────────────────────────────────────┐     │
│  │ FS Watcher   │────▶│  Ingestion Pipeline (512K ctx LLM) │     │
│  └──────────────┘     │  Anthropic API (chunk boundaries)  │     │
│                       └──────────────┬───────────────────┘     │
│                       ┌──────────────▼───────────────────┐     │
│                       │  Neo4j (Graph Index)              │     │
│                       │  Qdrant (Vector Index)            │     │
│                       └──────────────┬───────────────────┘     │
│                       ┌──────────────▼───────────────────┐     │
│                       │  Tag-Aware Retrieval Engine      │     │
│                       │  Ollama (intent) + Gemini (tags)│     │
│                       └──────────────┬───────────────────┘     │
└──────────────────────────────────────┼───────────────────────────┘
                                       │ MCP Tool: mneme_inject
                                       ▼
                               Claude Code Pre-Tool Hook
```

## Phase Status

| Phase | Description | Status |
|---|---|---|
| 1 | Core Memory Store (CRUD + data model) | ✅ Done |
| 2 | LLM-Assisted Ingestion Pipeline | ✅ Done (mock-first, MiniMax real impl) |
| 3 | Graph Index (Neo4j) | ✅ Done (mock-first, Neo4j real impl) |
| 4 | Tag-Aware Retrieval Engine | ✅ Done (mock + 4-component scoring) |
| 5 | Memory Guard (diff against memory) | ✅ Done (mock-first, Neo4j + Qdrant real impl) |
| 6 | Pre-Tool Hook (MCP interface) | ✅ Done (Mneme orchestrator) |

**All phases implemented in mock-first architecture.** See `MOCKS_AND_PLACEHOLDERS.md` for swap points.

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (package manager)
- Neo4j (local or cloud)
- Qdrant (local or cloud)
- Ollama (for intent detection — optional for Phase 1)

### Setup

```bash
# Clone
git clone https://github.com/your-org/Mneme.git
cd Mneme

# Install dependencies
uv sync

# Copy and fill in API keys
cp .env.example .env
# Edit .env with your:
#   - ANTHROPIC_API_KEY
#   - GEMINI_API_KEY
#   - NEO4J_PASSWORD
#   - OLLAMA_MODEL (optional)

# Run the server
uv run uvicorn src.server:app --port 8080 --reload
```

### Running Tests

```bash
uv run pytest tests/ -v
```

## API Endpoints

### Phase 1 — Memory Store (✅ Done)

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/memories` | Create a memory chunk |
| `GET` | `/memories/{chunk_id}` | Get a chunk by ID |
| `PATCH` | `/memories/{chunk_id}/tags` | Update tags on a chunk |
| `GET` | `/memories?tag=X&session=Y&outcome=Z` | List chunks with filters |

### Phase 2–6 — Stubs (🔜 / ⏳)

| Method | Endpoint | Phase | Description |
|---|---|---|---|
| `POST` | `/ingest` | 2 | Ingest files → chunks (LLM-assisted) |
| `GET` | `/graph/related/{chunk_id}` | 3 | Get related chunks (graph traversal) |
| `POST` | `/retrieve` | 4 | Tag-aware memory retrieval |
| `POST` | `/guard` | 5 | Diff against past failed attempts |
| `POST` | `/inject` | 6 | Full pre-tool hook injection |

All stubs return `{"_mock": true, ...}` until the real implementation is wired.

## Project Structure

```
Mneme/
├── SPEC.md                  # Architecture & phase plan
├── README.md                # This file
├── TEST_DOCUMENTATION.md   # Test guide
├── .env.example             # API key template
├── pyproject.toml            # Dependencies
└── src/
    ├── models.py             # MemoryChunk, ChunkRelationship, TagTree
    ├── config.py             # Env var configuration
    ├── server.py             # FastAPI server (all phases)
    └── memory_store/
        ├── __init__.py      # Repository factory
        └── repository.py     # MockMemoryRepository (Neo4j path documented)
```

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Description | Required |
|---|---|---|
| `ANTHROPIC_API_KEY` | For chunking + boundary definition | Yes (Phase 2+) |
| `GEMINI_API_KEY` | For embeddings + tag-sort | Yes (Phase 2+) |
| `GEMINI_EMBEDDING_MODEL` | Embedding model name | Default: `gemini-embedding-2` |
| `OLLAMA_MODEL` | Local intent detection model | Yes (Phase 4+) |
| `NEO4J_PASSWORD` | Neo4j connection | Yes (Phase 3+) |
| `QDRANT_HOST` | Qdrant connection | Default: `http://localhost:6333` |
