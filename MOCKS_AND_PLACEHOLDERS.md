# Mocks, Stubs, and Placeholders — Mneme Project

This document catalogs every mock, stub, placeholder, and `[MOCK]`-marked component in the codebase. Use it as a roadmap for what needs real implementation when external dependencies (Neo4j, Qdrant, Ollama/BitNet, sentence embeddings) come online.

---

## Quick Summary

| Category | Count | Status |
|---|---|---|
| Mock classes (return deterministic data) | 7 | Production-ready, swap behind `use_mock` flag |
| Stub endpoints (return hardcoded response) | 0 | ✅ All phases now have class-based implementations |
| Real backends pending (Neo4j, Qdrant, BitNet) | 4 | Documented paths, not yet wired |
| Placeholder algorithms (Jaccard, keyword, aggregation) | 6 | Working but need replacement with real models |
| API keys hardcoded in source | 1 | Security issue — needs to be addressed |

---

## 1. Mock Classes (deterministic data, used for testing/verification)

These are real code — they implement the contract, return valid data, and are tested. The "mock" is the role they play: they stand in for the real backend.

### `MockMemoryRepository` — `src/memory_store/repository.py`
- **Role:** Stand-in for `Neo4jMemoryRepository`
- **Pattern:** In-memory dict for chunks, list for edges
- **Status:** ✅ Production-quality, all CRUD tested
- **Swap point:** `get_repository(use_mock=False)` — raises `NotImplementedError` (line 16-19 of `__init__.py`)
- **Tests:** 16 in `tests/test_memory_store.py`
- **Methods:** `create_chunk`, `get_chunk`, `update_chunk_tags`, `list_chunks`, `touch_chunk`, `create_edge`, `get_related_chunks`, `get_contradicting_chunks`

### `MockIngestionPipeline` — `src/ingestion/mock_ingestion.py`
- **Role:** Stand-in for real LLM-based chunking
- **Pattern:** Returns hardcoded chunks + relationships
- **Status:** ✅ Production-quality, tested
- **Swap point:** `IngestionPipeline(use_mock=True)` defaults to mock; `use_mock=False` uses real `MiniMaxClient` (which is real, see §4)
- **Tests:** `tests/test_ingestion.py`

### `MockGraphIndex` — `src/graph/mock_graph.py`
- **Role:** Stand-in for `Neo4jGraphIndex`
- **Methods:** `get_related()`, `get_chains()` — return mock relationships
- **Status:** ✅ Production-quality, tested
- **Swap point:** `GraphIndex(use_mock=False)` activates real path
- **Tests:** `tests/test_graph.py`

### `MockRetrievalEngine` — `src/retrieval/mock_retrieval.py`
- **Role:** Stand-in for real retrieval (Ollama intent + Qdrant + Gemini)
- **Pattern:** Returns fixed manifest with `_mock: True`
- **Status:** ✅ Production-quality, tested
- **Swap point:** `RetrievalEngine(use_mock=False)` activates real path
- **Tests:** 4 in `tests/test_retrieval.py::TestMockRetrievalEngine`
- **Side effect:** Prints `[MOCK]` to stderr on invocation

### `MockDiffEngine` — `src/guard/mock_diff_engine.py`
- **Role:** Stand-in for real graph-based contradiction lookup
- **Pattern:** Returns fixed manifest
- **Status:** ✅ Production-quality, tested
- **Swap point:** `DiffEngine(use_mock=False)` activates real path
- **Tests:** 6 in `tests/test_guard.py::TestMockDiffEngine`
- **Side effect:** Prints `[MOCK]` to stderr on invocation

### `IntentDetector` mock path — `src/retrieval/intent_detector.py`
- **Role:** Stand-in for real Ollama/BitNet intent detection
- **Pattern:** Keyword matching on prompt text
- **Status:** ✅ Production-quality, tested
- **Swap point:** `IntentDetector(use_mock=False)` calls `_detect_real()` (currently stubbed, see §4)
- **Tests:** 6 in `tests/test_retrieval.py::TestIntentDetector`

### `MockMneme` — `src/hook/mock_mneme.py` *(Phase 6)*
- **Role:** Stand-in for the real pre-tool hook orchestrator
- **Pattern:** Returns fixed manifest with `detected_intent="continue_auth_flow_retry"`, `retrieved_chunks=["mem_001", "mem_007"]`, `memory_guard="PASSED ..."`, hardcoded `injected_context`
- **Status:** ✅ Production-quality, tested
- **Swap point:** `Mneme(use_mock=False)` activates real orchestration
- **Tests:** 10 in `tests/test_hook.py::TestMockMneme`
- **Side effect:** Prints `[MOCK]` to stderr on invocation

### `Mneme` — `src/hook/mneme.py` *(Phase 6 — real orchestrator)*
- **Role:** Orchestrates Phase 4 (RetrievalEngine) + Phase 5 (DiffEngine) into a single pre-tool invocation
- **Pattern:** Calls `RetrievalEngine.retrieve()` → for each chunk, `DiffEngine.check()` → aggregate guard results
- **Status:** ✅ Production-quality, tested
- **Tests:** 8 in `tests/test_hook.py::TestMneme`
- **Notes:** This is a real orchestrator — no business logic of its own, just composition

---

## 2. Stub Endpoints (hardcoded responses, not class-based)

### `/inject` endpoint — `src/server.py:308-330` (Phase 6) ✅ **IMPLEMENTED**
- **State:** No longer a stub — uses `Mneme` orchestrator
- **Accepts:** `use_mock` query param (`?use_mock=false` to activate real path)
- **Tests:** 5 in `tests/test_hook.py::TestInjectEndpoint`

### Other endpoint comments still say "stub"
- `server.py:41` — `IngestRequest` docstring: "Payload for POST /ingest (Phase 2 stub)" *(outdated — Phase 2 is implemented)*
- `server.py:47` — `RetrieveRequest` docstring: "Payload for POST /retrieve (Phase 4 stub)" *(outdated — Phase 4 is implemented)*
- `server.py:54` — `GuardRequest` docstring: "Payload for POST /guard (Phase 5 stub)" *(outdated — Phase 5 is implemented)*

**Note:** These Pydantic model docstrings still say "stub" but the endpoints are fully implemented now. Cosmetic cleanup needed.

---

## 3. Real Backends Pending (documented paths, not implemented)

### `Neo4jMemoryRepository` — `src/memory_store/`
- **Status:** Stub — `get_repository(use_mock=False)` raises `NotImplementedError`
- **What needs to be built:** All methods currently in `MockMemoryRepository`, but using Neo4j driver
- **Files to create:** `src/memory_store/neo4j_repository.py`
- **Wired in:** `src/memory_store/__init__.py:15-19` (the `else` branch)

### `Ollama`/`BitNet` real intent detection — `src/retrieval/intent_detector.py:_detect_real`
- **Status:** Calls `BitNetClient` which calls `BitNet/build/bin/llama-cli` with Falcon3-1B-Instruct
- **Issue:** Falcon3 0.8B at 1.58bit quantization produces gibberish (verified by test runs)
- **Workaround:** Client falls back to f32 model (3.8GB, works but heavy)
- **Resolution:** Try larger Falcon3 (3B+) or different quantization
- **Files involved:** `src/retrieval/bitnet_client.py`, `BitNet/` directory (kept dormant)

### `Qdrant` vector search
- **Status:** Not started
- **Where it would go:** `RetrievalEngine` real path — `_score_chunks()` uses `self._repo.list_chunks()` (in-memory filter). Real impl would do vector similarity search
- **What's there now:** Jaccard on tag sets as a placeholder (see §4)

### `Gemini` tag-sort
- **Status:** Documented only
- **Where it would go:** `RetrievalEngine` real path — post-scoring tag re-ranking

---

## 4. Placeholder Algorithms (working but crude)

### Jaccard word similarity — `src/guard/diff_engine.py:_jaccard_similarity`
- **What it is:** `|words_a ∩ words_b| / |words_a ∪ words_b|`
- **Purpose:** DiffEngine real-path similarity scoring
- **Limitation:** No semantic understanding — "auth" and "oauth" don't match
- **Replacement:** Sentence-transformer embeddings + cosine similarity (when a model works)

### Jaccard tag-set similarity — `src/retrieval/engine.py:_compute_embedding_similarity`
- **What it is:** Jaccard over tag sets
- **Purpose:** `RetrievalEngine` scoring component
- **Limitation:** Treats tags as opaque tokens
- **Replacement:** Dense embedding similarity (when Qdrant is wired)

### Keyword-based intent detection — `src/retrieval/intent_detector.py`
- **What it is:** Hardcoded `INTENT_KEYWORDS` and `TAG_PREFIX_MAP` dicts
- **Purpose:** Mock intent detection
- **Limitation:** Doesn't generalize beyond the dictionary
- **Replacement:** LLM-based intent detection (when BitNet/Ollama works)

### Mneme orchestration aggregations — `src/hook/mneme.py` *(Phase 6)*
Several `[PLACEHOLDER]` annotations in the Mneme code flag crude aggregation logic:

- **`_GUARD_PASSED = "PASSED (no contradicting failed attempts)"`** — Single string for "nothing wrong." Doesn't distinguish "no contradictions found" from "no relevant memories retrieved." A more sophisticated version would emit different statuses for these cases.

- **`_GUARD_MAX_CHUNKS = 10`** — Hard cap on how many retrieved chunks to run the guard on. Currently set to 10 for latency. A real production version might dynamically size this based on retrieval confidence or a token budget. Currently NOT used in the code (limit is implicitly 0 since the Mneme defaults to mock engines).

- **Naive guard aggregation** — `if warnings: memory_guard = " | ".join(warnings)` — Any guard trigger is reported as a single concatenated string. A more sophisticated version would rank by severity, weight by retrieval score, and possibly batch multiple guard hits into a structured message.

- **Engine use_mock defaults to True in Mneme orchestrator** — `Mneme._build_retrieval_engine()` and `_build_diff_engine()` create engines with `use_mock=True` even when `Mneme.use_mock=False`. The orchestrator's "real path" is real orchestration, but the underlying engines still use mocks. When real backends (BitNet, Qdrant, Neo4j) are available, flip the engine defaults to `use_mock=False`.

- **Hardcoded `injected_context` in mock** — MockMneme returns a hardcoded context string about auth flow retry. The real path derives it from the actual retrieval result.

---

## 5. Real Backends Wired and Working

### `MiniMaxClient` — `src/ingestion/llm_client.py`
- **Status:** ✅ **Real implementation** (not mock) — sends real requests to MiniMax API
- **Used by:** `IngestionPipeline(use_mock=False)`
- **Note:** The file's docstring says `[MOCK]` but the code IS the real implementation. The "mock" tag is on the call site (`pipeline.py`).
- **Test coverage:** Limited — depends on MiniMax API being reachable

---

## 6. Security Issues (related to "real" deployment)

### ~~Hardcoded API key in `llm_client.py`~~ — ✅ FIXED
- **Status:** Fixed. `MiniMaxClient` now reads `MINIMAX_API_KEY` from env var and raises a clear `RuntimeError` if missing.
- **File:** `src/ingestion/llm_client.py:116-136`

### ~~Default Gemini API key in `config.py`~~ — ✅ FIXED
- **Status:** Fixed. `gemini_api_key` is now `str | None` with no default — must be set via `GEMINI_API_KEY` env var.

### ~~Default Neo4j password in `config.py`~~ — ✅ FIXED
- **Status:** Fixed. `password` is now `str | None` with no default — must be set via `NEO4J_PASSWORD` env var.

### Hardcoded API key in `.env.example` — ✅ FIXED
- **Status:** Fixed. Replaced `GEMINI_API_KEY=AIzaSyAQR7zEAhU5Gp_q2JXNkokN0c8AOYlWgQI` with `your_gemini_api_key_here` placeholder.

### Thread safety on `next_chunk_id()` — ✅ FIXED
- **Status:** Fixed. Added `threading.Lock` around the counter increment.
- **File:** `src/models.py:147-167`
- **Note:** Lock protects within a single process. For multi-process deployments (e.g., `uvicorn --workers 4`), use UUIDs or a database sequence to avoid cross-process collisions.

### GET endpoint mutates state — ✅ FIXED
- **Status:** Fixed. `touch_chunk` no longer called inside `GET /memories/{chunk_id}`. Added separate `POST /memories/{chunk_id}/touch` endpoint for recency tracking.
- **Files:** `src/server.py` (endpoint split), `src/memory_store/repository.py` (unchanged — `touch_chunk` is now only called via the new endpoint)

---

## 7. `BitNet` External Dependency

### Status
- **Models:** DELETED (8.8GB freed) — can be regenerated via `python setup_env.py`
- **Code:** KEPT (9.7MB) — `src/retrieval/bitnet_client.py`, `BitNet/` directory
- **Real path:** Wired but dormant — `use_mock=False` will fail until models are restored

### To restore
```bash
cd BitNet
source .venv-bitnet/bin/activate
python setup_env.py -hr tiiuae/Falcon3-1B-Instruct-1.58bit -q i2_s
```

---

## 8. Missing Documentation

- **README.md** — none exists. Project has no top-level orientation document.
- **ADRs (Architecture Decision Records)** — none exist. Key decisions (mock-first strategy, scoring weights, embedding placeholders) are not documented.
- **Inline `_mock_warning` mentions** — both guard and retrieval modules have these, but the pattern isn't documented anywhere central.

---

## Priority for Real Implementations

1. **✅ DONE:** Move API key to env var
2. **✅ DONE:** Remove outdated "stub" docstrings on Pydantic models
3. **🟡 Quick wins:** Add `README.md` (1-2 hours) — *exists but is being updated*
4. **🟡 Clean up:** Fix the duplicated `_implementation_note` string between `diff_engine.py` and `mock_diff_engine.py` (2 min)
5. **🟠 Medium:** Build `Neo4jMemoryRepository` (1-2 days, needs Neo4j)
6. **🟠 Medium:** Get a working intent detection model (Falcon3-3B at 1.58bit? Different quantization? Different model entirely?)
7. **🔴 Hard:** Wire Qdrant for vector search (needs Qdrant deployment)
8. **🔴 Hard:** Wire Gemini for tag-sort (needs API access)

---

## How Mocks Are Wired (the pattern)

Every module follows the same pattern:

```python
class XxxEngine:
    def __init__(self, repository=None, use_mock: bool = True):
        self._repo = repository
        self._use_mock = use_mock

    def do_something(self, ...):
        if self._use_mock:
            from src.xxx.mock_xxx import MockXxxEngine
            return MockXxxEngine().do_something(...)
        # Real implementation
        ...
```

And server.py endpoints accept a `use_mock` query param:

```python
@app.post("/xxx")
def xxx_endpoint(..., use_mock: bool = Query(True, ...)):
    engine = XxxEngine(repository=repo(), use_mock=use_mock)
    result = engine.do_something(...)
    return JSONResponse(content=result)
```

**To activate real path:** pass `?use_mock=false` to the endpoint. Default is always `use_mock=true` for safety.
