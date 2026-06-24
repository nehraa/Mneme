"""
Mneme HTTP Server — exposes Mneme's memory and retrieval API over HTTP.

All endpoints back onto the real, Neo4j-backed implementations.
"""
from __future__ import annotations

import atexit
import glob
import hmac
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, TypeVar

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.memory_store import get_repository
from src.models import next_chunk_id
from src.config import get_config

logger = logging.getLogger(__name__)

# ── API key authentication ────────────────────────────────────────────────────
#
# When MNEME_API_KEY is set, all endpoints except /health require the
# `X-Mneme-Key` header to match. Custom header (not Authorization) keeps
# the CORS allowlist narrow and reserves Authorization for future JWT work.
#
# When MNEME_API_KEY is unset, the server stays open and logs a loud warning
# at startup so the operator knows endpoints are unauthenticated.
_API_KEY = os.environ.get("MNEME_API_KEY")
if not _API_KEY:
    logger.warning(
        "SECURITY WARNING: MNEME_API_KEY is unset - all endpoints except "
        "/health are UNAUTHENTICATED. This is intended for local development "
        "only. Set MNEME_API_KEY to a strong random value before exposing "
        "this server to any network."
    )
else:
    logger.info(
        "API key auth enabled - clients must send X-Mneme-Key header "
        "(/health is exempt)."
    )


def verify_api_key(
    x_mneme_key: str | None = Header(default=None, alias="X-Mneme-Key"),
) -> None:
    """FastAPI dependency that enforces MNEME_API_KEY.

    Constant-time comparison via `hmac.compare_digest` to prevent timing
    attacks against the configured key.

    No-op when MNEME_API_KEY is unset (backward-compatible dev mode).
    Raises HTTPException(403) when the configured key does not match the
    header value.
    """
    if not _API_KEY:
        return None
    if x_mneme_key is None or not hmac.compare_digest(x_mneme_key, _API_KEY):
        raise HTTPException(status_code=403, detail="Invalid or missing API key")
    return None


# ── Ingest path allowlist ────────────────────────────────────────────────────
#
# MNEME_INGEST_ALLOWED_ROOTS is a comma-separated list of directories that
# define where POST /ingest is allowed to read files from. This blocks the
# trivial path-traversal attack where a client POSTs file_paths=["/etc/passwd"]
# or file_paths=["../../sensitive.txt"] and the pipeline dutifully reads it.
#
# Behavior:
#   - When MNEME_INGEST_ALLOWED_ROOTS is UNSET → log a loud warning and allow
#     all paths (dev mode, same shape as MNEME_API_KEY).
#   - When MNEME_INGEST_ALLOWED_ROOTS is set → only files resolved to a path
#     that lives under one of the configured roots are accepted. Any other
#     path is rejected with HTTPException(403) by the /ingest handler.
#
# Resolution uses Path.resolve() so symlinked traversal is also defeated:
# a symlink inside an allowed root that points outside it still resolves to
# the outside location, and the prefix check then rejects it.
#
# Glob patterns are accepted, but every file matched by the glob must also
# resolve inside an allowed root — a pattern like "/etc/**/*.conf" will
# expand into many matches, each individually validated.
_ALLOWED_INGEST_ROOTS: list[Path] | None = None
_allowed_roots_env = os.environ.get("MNEME_INGEST_ALLOWED_ROOTS")
if not _allowed_roots_env or _allowed_roots_env.strip() == "":
    _ALLOWED_INGEST_ROOTS = None
    logger.warning(
        "SECURITY WARNING: MNEME_INGEST_ALLOWED_ROOTS is unset - POST /ingest "
        "will accept ANY file path from any client. This is intended for local "
        "development only. Set MNEME_INGEST_ALLOWED_ROOTS to a comma-separated "
        "list of directories (e.g. '/home/user/projects,/srv/code') before "
        "exposing this server to any network."
    )
else:
    _ALLOWED_INGEST_ROOTS = [
        Path(p).expanduser().resolve()
        for p in _allowed_roots_env.split(",")
        if p.strip()
    ]
    logger.info(
        "Ingest path allowlist active: %s",
        [str(p) for p in _ALLOWED_INGEST_ROOTS],
    )


def _is_path_under_allowed_roots(resolved: Path) -> bool:
    """Return True iff `resolved` lives under one of the configured allowlist roots.

    Comparison walks the resolved path's parents and checks whether any
    configured allowlist root is an ancestor (or matches exactly). This
    handles the case where `resolved` itself is one of the allowed roots,
    and treats ancestry via `root in resolved.parents` so symlinks inside
    an allowed root that point outside it still produce a `resolved` value
    the prefix check rejects. Resolving once on the caller side ensures
    symlinked paths are normalized before the ancestry check.
    """
    if _ALLOWED_INGEST_ROOTS is None:
        return True
    for root in _ALLOWED_INGEST_ROOTS:
        if resolved == root or root in resolved.parents:
            return True
    return False


def _validate_ingest_paths(file_paths: list[str]) -> None:
    """Validate every entry in `file_paths` against the ingest allowlist.

    Two-layer check:
      1. Validate the literal input pattern itself — resolved via
         Path.resolve(strict=False) — so that traversal attempts like
         "/etc/passwd" or "/tmp/allowed/../etc/passwd" are rejected even
         when the filesystem glob returns zero matches.
      2. Expand glob patterns (mirrors what the ingestion pipeline does)
         and validate every match.

    Raises HTTPException(403) on the first violation; returns None on
    success. When MNEME_INGEST_ALLOWED_ROOTS is unset, validation is a
    no-op and the warning logged at startup is the operator's only signal.
    """
    if _ALLOWED_INGEST_ROOTS is None:
        return None

    if not file_paths:
        return None

    for pattern in file_paths:
        # Layer 1: reject literal patterns that resolve outside the
        # allowlist, regardless of whether the file currently exists. This
        # blocks `/etc/passwd`, `../../sensitive.txt`, and any other
        # probing-style payload that wouldn't survive glob expansion.
        try:
            pattern_resolved = Path(pattern).expanduser().resolve(strict=False)
        except OSError as exc:
            raise HTTPException(
                status_code=403,
                detail=f"Cannot resolve ingest path '{pattern}': {exc}",
            ) from exc
        if not _is_path_under_allowed_roots(pattern_resolved):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Ingest path '{pattern}' (resolved to '{pattern_resolved}') "
                    f"is outside the configured MNEME_INGEST_ALLOWED_ROOTS"
                ),
            )

        # Layer 2: validate every glob match. A glob like "/srv/code/**/*.py"
        # could expand to many files, all of which must individually live
        # under the allowlist (Path.resolve() handles symlinked matches).
        for match in glob.glob(str(Path(pattern).expanduser()), recursive=True):
            try:
                resolved = Path(match).expanduser().resolve(strict=False)
            except OSError as exc:
                raise HTTPException(
                    status_code=403,
                    detail=f"Cannot resolve ingest path '{match}': {exc}",
                ) from exc
            if not _is_path_under_allowed_roots(resolved):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Ingest path '{match}' (resolved to '{resolved}') "
                        f"is outside the configured MNEME_INGEST_ALLOWED_ROOTS"
                    ),
                )
    return None


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
    """Payload for POST /ingest (Phase 2 endpoint)."""

    file_paths: list[str]


class RetrieveRequest(BaseModel):
    """Payload for POST /retrieve (Phase 4 endpoint)."""

    prompt_context: str
    session_id: str | None = None


class GuardRequest(BaseModel):
    """Payload for POST /guard (Phase 5 endpoint)."""

    proposed_change: str
    target_file: str
    session_id: str | None = None


class InjectRequest(BaseModel):
    """Payload for POST /inject (Phase 6 endpoint).

    Used by the pre-tool hook (mneme_inject) — fires before every outbound
    API call in Claude Code. The `message` is both the retrieval query
    AND the proposed_change for the memory guard.
    """

    message: str = Field(..., description="The user/agent message to retrieve context for")
    session_id: str | None = Field(default=None, description="Optional session filter")


# ── App ────────────────────────────────────────────────────────────────────────


def _close_clients() -> None:
    """Close Qdrant and Neo4j client connections if they were initialized.

    Idempotent: safe to call multiple times. Catches and logs errors so that
    a failure in one client's close() does not prevent the other from
    closing.
    """
    global _qdrant_search, _repo

    qs = _qdrant_search
    if qs is not None:
        _qdrant_search = None
        try:
            close = getattr(qs, "close", None)
            if callable(close):
                close()
                logger.info("Closed Qdrant client connection")
        except Exception as exc:
            logger.warning("Error closing Qdrant client: %s", exc)

    repo_obj = _repo
    if repo_obj is not None:
        _repo = None
        try:
            close = getattr(repo_obj, "close", None)
            if callable(close):
                close()
                logger.info("Closed Neo4j driver connection")
        except Exception as exc:
            logger.warning("Error closing Neo4j driver: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan: graceful cleanup of client connections on shutdown."""
    yield
    _close_clients()


# Register atexit backup so connections close even on hard exit (Ctrl+C,
# SIGTERM not handled by uvicorn, etc.). atexit fires at interpreter shutdown
# regardless of how the process terminates.
atexit.register(_close_clients)


app = FastAPI(
    title="Mneme",
    version="0.1.0",
    description="Agentic hybrid memory system with RAG",
    lifespan=lifespan,
)

# CORS middleware — secure-by-default.
#
# Behavior:
#   - If MNEME_CORS_ORIGINS is UNSET → allow no cross-origin requests (empty list).
#     The server logs a warning at startup so operators know restrictive mode is active.
#   - If MNEME_CORS_ORIGINS is set to "*" → log a loud warning and proceed. A
#     wildcard origin combined with allow_credentials=True is unsafe: any site
#     can issue credentialed cross-origin requests against this API. We still
#     honor the operator's explicit choice rather than refusing to start, so
#     the warning is the loud feedback they get.
#   - If MNEME_CORS_ORIGINS is set to a comma-separated list of origins → use them.
#
# Set MNEME_CORS_ORIGINS in production to the exact origin(s) that need access,
# e.g. MNEME_CORS_ORIGINS="https://app.example.com" or a CSV for multiple.
_cors_env = os.environ.get("MNEME_CORS_ORIGINS")
if _cors_env is None or _cors_env.strip() == "":
    _cors_origins: list[str] = []
    logger.warning(
        "CORS: MNEME_CORS_ORIGINS is unset — restrictive mode active. "
        "No cross-origin requests will be allowed. "
        "Set MNEME_CORS_ORIGINS to a comma-separated list of origins to enable CORS."
    )
else:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
    if _cors_origins == ["*"]:
        # allow_credentials=True + wildcard origin is unsafe. The operator set
        # this explicitly, so honor it but make the security risk impossible
        # to miss: log a loud, specific warning and continue.
        logger.warning(
            "SECURITY WARNING: MNEME_CORS_ORIGINS='*' allows ANY origin to "
            "make credentialed cross-origin requests to this API. This is a "
            "known unsafe combination (allow_credentials=True + wildcard). "
            "Set MNEME_CORS_ORIGINS to a comma-separated list of explicit "
            "origins (e.g. 'https://app.example.com') or unset it to deny "
            "all cross-origin requests. Proceeding with '*' as requested."
        )
    else:
        logger.info(
            "CORS: allowing cross-origin requests from %s", _cors_origins
        )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    # Explicit allowlist: combined with allow_credentials=True, "*" is
    # unsafe (any cross-origin header from a permitted origin). Mneme's API
    # only needs Authorization + Content-Type for all current endpoints.
    allow_headers=["Authorization", "Content-Type"],
)

# ── Security response headers ──────────────────────────────────────────────────
#
# Adds defensive headers to every response:
#   - Strict-Transport-Security: pin HTTPS for 1 year (no preload by default)
#   - X-Frame-Options: DENY — never framed
#   - X-Content-Type-Options: nosniff — block MIME sniffing
#   - Content-Security-Policy: lock down by default (server returns no HTML)
#   - Referrer-Policy: no-referrer — never leak the request URL
#
# Set MNEME_SECURITY_HEADERS=false to disable (e.g. local dev behind a proxy
# that already sets these headers).
_SECURITY_HEADERS_ENABLED = os.environ.get("MNEME_SECURITY_HEADERS", "true").lower() not in (
    "false",
    "0",
    "no",
    "off",
)
if not _SECURITY_HEADERS_ENABLED:
    logger.warning(
        "MNEME_SECURITY_HEADERS=false — security response headers are disabled. "
        "This is unsafe for production. Enable it unless running behind a proxy "
        "that already sets Strict-Transport-Security, X-Frame-Options, "
        "X-Content-Type-Options, Content-Security-Policy, and Referrer-Policy."
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject defensive security headers into every response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        # HSTS: always pin HTTPS for 1 year on all subdomains. No `preload`
        # so operators can opt in deliberately after auditing their setup.
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Mneme serves only JSON; no scripts, no frames, no subresources.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


if _SECURITY_HEADERS_ENABLED:
    app.add_middleware(SecurityHeadersMiddleware)

# Lazy-initialized repository — back onto Neo4j (or fallback at construction time).
_repo: Any = None

# Lazy-initialized QdrantSearch client — None if Qdrant is unavailable.
# Initialized on first use to avoid crashing the server when Qdrant is down.
_qdrant_search: Any = None

# FastAPI runs endpoints in a threadpool, so the lazy-init helpers below can
# race: two concurrent requests can both observe `_repo is None`, both call
# `get_repository()`, and the second silently overwrites the first's
# connection. Per-resource locks + double-checked locking keep init
# exclusive while leaving the steady-state (already-initialized) fast path
# lock-free.
_repo_lock = threading.Lock()
_qdrant_search_lock = threading.Lock()

_T = TypeVar("_T")


def _get_or_init(
    attr_name: str, lock: threading.Lock, factory: Callable[[], _T | None]
) -> _T | None:
    """Double-checked-locking lazy init.

    Returns the module-level attribute named ``attr_name`` if already set;
    otherwise calls ``factory`` under ``lock`` to initialize it. The factory
    may return ``None`` to signal that init failed; that ``None`` is cached so
    subsequent callers do not retry on every request.
    """
    current = globals().get(attr_name)
    if current is not None:
        return current
    with lock:
        current = globals().get(attr_name)
        if current is not None:
            return current
        current = factory()
        globals()[attr_name] = current
    return current


def repo() -> Any:
    """Return the lazily-initialized memory repository, or None."""
    return _get_or_init("_repo", _repo_lock, get_repository)


def qdrant_search() -> Any:
    """Return the lazily-initialized QdrantSearch instance, or None."""
    def _init() -> QdrantSearch | None:
        try:
            from src.config import get_config
            cfg = get_config()
            from src.retrieval.qdrant_search import QdrantSearch

            instance = QdrantSearch(
                host=cfg.qdrant.host,
                collection=cfg.qdrant.collection,
                vector_size=cfg.qdrant.vector_size,
            )
            logger.info(
                "QdrantSearch initialized: host=%s collection=%s",
                cfg.qdrant.host, cfg.qdrant.collection,
            )
            return instance
        except Exception as exc:
            logger.warning("Failed to initialize QdrantSearch: %s", exc)
            return None

    return _get_or_init("_qdrant_search", _qdrant_search_lock, _init)


# ── Health ────────────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


# ════════════════════════════════════════════════════════════════════════════
# Phase 1 — Memory Store CRUD
# ════════════════════════════════════════════════════════════════════════════


@app.post(
    "/memories",
    status_code=201,
    tags=["Phase 1"],
    dependencies=[Depends(verify_api_key)],
)
def create_memory(body: CreateChunkRequest) -> JSONResponse:
    """
    POST /memories — create a new memory chunk.

    When Qdrant is configured, also indexes the chunk's embedding for
    semantic retrieval. This is best-effort: if Qdrant is unavailable,
    the chunk is still stored and the error is logged without failing
    the request.
    """
    chunk_id = next_chunk_id()
    now = datetime.now(timezone.utc).isoformat()

    # Dynamic tagging: if caller didn't provide tags, infer them from content.
    # If tags were provided, merge them with inferred ones (caller's tags win).
    from src.tagging.infer import infer_tags, merge_tags
    inferred_tags = infer_tags(body.content, source_file=body.source_file)
    final_tags = merge_tags(body.tags, inferred_tags)

    # Parse flat tags into a simple tag_tree dict
    tag_tree = _parse_tags(final_tags)

    # Derive outcome_tag from final tags if caller didn't set one explicitly
    final_outcome_tag = body.outcome_tag
    if body.outcome_tag == "work_done":  # default
        for tag in final_tags:
            if tag.startswith("outcome="):
                final_outcome_tag = tag.split("=", 1)[1]
                break

    record = {
        "chunk_id": chunk_id,
        "session_id": body.session_id,
        "project_root": body.project_root,
        "content": body.content,
        "page_order": body.page_order,
        "tags": final_tags,
        "tag_tree": tag_tree,
        "linked_chunks": body.linked_chunks,
        "outcome_tag": final_outcome_tag,
        "source_file": body.source_file,
        "created_at": now,
        "last_accessed": None,
    }

    repo().create_chunk(record)

    # Real embeddings via configured provider (Gemini or Ollama): embed and index in Qdrant.
    # Best-effort — if the embedding provider or Qdrant is unavailable, the chunk is
    # still stored and the error is logged without failing the request.
    qs = qdrant_search()
    cfg = get_config()
    if qs is not None and cfg.llm.embedding_provider == "ollama":
        try:
            from src.retrieval.ollama_embeddings import OllamaEmbeddingClient

            ollama_emb = OllamaEmbeddingClient()
            embedding = ollama_emb.embed(body.content)

            qs.upsert_chunk(
                chunk_id=chunk_id,
                content=body.content,
                embedding=embedding,
                metadata={
                    "session_id": body.session_id,
                    "tags": final_tags,
                    "outcome_tag": final_outcome_tag,
                    "source_file": body.source_file,
                    "created_at": now,
                },
            )
            logger.info(
                "Indexed chunk %s in Qdrant (ollama, dim=%d)",
                chunk_id, len(embedding),
            )
        except Exception as exc:
            # Don't fail the request — chunk is already stored in Neo4j
            logger.warning(
                "Failed to index chunk %s in Qdrant (ollama): %s",
                chunk_id, exc,
            )
    elif qs is not None and cfg.llm.embedding_provider != "ollama":
        # Gemini (default when embedding_provider is not "ollama")
        try:
            from src.retrieval.gemini_embeddings import GeminiEmbeddingClient

            gemini = GeminiEmbeddingClient()
            embedding = gemini.embed(body.content)

            qs.upsert_chunk(
                chunk_id=chunk_id,
                content=body.content,
                embedding=embedding,
                metadata={
                    "session_id": body.session_id,
                    "tags": final_tags,
                    "outcome_tag": final_outcome_tag,
                    "source_file": body.source_file,
                    "created_at": now,
                },
            )
            logger.info(
                "Indexed chunk %s in Qdrant (gemini, dim=%d)",
                chunk_id, len(embedding),
            )
        except Exception as exc:
            # Don't fail the request — chunk is already stored in Neo4j
            logger.warning(
                "Failed to index chunk %s in Qdrant (gemini): %s",
                chunk_id, exc,
            )

    return JSONResponse(content=record, status_code=201)


@app.get(
    "/memories/{chunk_id}",
    tags=["Phase 1"],
    dependencies=[Depends(verify_api_key)],
)
def get_memory(chunk_id: str) -> JSONResponse:
    """
    GET /memories/{chunk_id} — retrieve a chunk by ID.

    Pure read (safe + idempotent per HTTP semantics). Recency tracking
    happens via the separate POST /memories/{id}/touch endpoint.
    """
    chunk = repo().get_chunk(chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_id} not found")
    return JSONResponse(content=chunk)


@app.post(
    "/memories/{chunk_id}/touch",
    tags=["Phase 1"],
    dependencies=[Depends(verify_api_key)],
)
def touch_memory(chunk_id: str) -> JSONResponse:
    """
    POST /memories/{chunk_id}/touch — update last_accessed timestamp.

    Called after retrieval to update recency boost in scoring.
    Kept separate from GET so the read path stays safe + idempotent.
    """
    chunk = repo().get_chunk(chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_id} not found")
    repo().touch_chunk(chunk_id)
    return JSONResponse(content={"chunk_id": chunk_id, "touched": True})


@app.patch(
    "/memories/{chunk_id}/tags",
    tags=["Phase 1"],
    dependencies=[Depends(verify_api_key)],
)
def update_memory_tags(chunk_id: str, body: UpdateTagsRequest) -> JSONResponse:
    """
    PATCH /memories/{chunk_id}/tags — update tags on an existing chunk.
    """
    chunk = repo().update_chunk_tags(chunk_id, body.tags)
    if chunk is None:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_id} not found")
    return JSONResponse(content=chunk)


@app.get(
    "/memories",
    tags=["Phase 1"],
    dependencies=[Depends(verify_api_key)],
)
def list_memories(
    tag: str | None = Query(None, description="Filter by tag (e.g. 'tool=auth')"),
    session_id: str | None = Query(None, description="Filter by session ID"),
    outcome: str | None = Query(None, description="Filter by outcome tag"),
    limit: int = Query(50, ge=1, le=500),
) -> JSONResponse:
    """
    GET /memories — list chunks with optional filters.
    """
    chunks = repo().list_chunks(
        tag=tag, session_id=session_id, outcome_tag=outcome, limit=limit
    )
    return JSONResponse(content={"chunks": chunks, "count": len(chunks)})


# ════════════════════════════════════════════════════════════════════════════
# Phase 2 — Ingestion Pipeline
# ════════════════════════════════════════════════════════════════════════════


@app.post(
    "/ingest",
    tags=["Phase 2"],
    dependencies=[Depends(verify_api_key)],
)
def ingest(body: IngestRequest) -> JSONResponse:
    """
    POST /ingest — ingest files and create chunks.

    Validates every requested path against MNEME_INGEST_ALLOWED_ROOTS before
    invoking the pipeline. Rejects with HTTPException(403) if any path
    (literal or glob match) resolves outside the configured allowlist.
    """
    _validate_ingest_paths(body.file_paths)

    from src.ingestion.pipeline import IngestionPipeline

    session_id = body.file_paths[0] if body.file_paths else "unknown"
    pipeline = IngestionPipeline(repository=repo())
    result = pipeline.run(
        file_paths=body.file_paths,
        session_id=session_id,
        project_root="",
    )
    return JSONResponse(content=result)


# ════════════════════════════════════════════════════════════════════════════
# Phase 3 — Graph Index
# ════════════════════════════════════════════════════════════════════════════


@app.get(
    "/graph/related/{chunk_id}",
    tags=["Phase 3"],
    dependencies=[Depends(verify_api_key)],
)
def get_related_chunks(
    chunk_id: str, depth: int = Query(1, ge=1, le=5)
) -> JSONResponse:
    """
    GET /graph/related/{chunk_id} — get chunks related to this chunk.
    """
    from src.graph.index import GraphIndex

    index = GraphIndex(repository=repo())
    result = index.get_related(chunk_id=chunk_id, depth=depth)
    return JSONResponse(content=result)


@app.get(
    "/graph/chains/{chunk_id}",
    tags=["Phase 3"],
    dependencies=[Depends(verify_api_key)],
)
def get_chunk_chains(
    chunk_id: str, depth: int = Query(3, ge=2, le=5)
) -> JSONResponse:
    """
    GET /graph/chains/{chunk_id} — get multi-hop traversal chains.
    """
    from src.graph.index import GraphIndex

    index = GraphIndex(repository=repo())
    result = index.get_chains(chunk_id=chunk_id, depth=depth)
    return JSONResponse(content=result)


# ════════════════════════════════════════════════════════════════════════════
# Phase 4 — Tag-Aware Retrieval Engine
# ════════════════════════════════════════════════════════════════════════════


@app.post(
    "/retrieve",
    tags=["Phase 4"],
    dependencies=[Depends(verify_api_key)],
)
def retrieve(body: RetrieveRequest) -> JSONResponse:
    """
    POST /retrieve — given a prompt, retrieve relevant memories.

    Three-tier strategy (June 19 2026):
      1. Embed the prompt via Ollama qwen3-embedding:0.6b (LOCAL)
      2. Score candidates using tag match + outcome priority + recency
      3. Intent detection via BitNet b1.58-2B-4T (LOCAL, :8081)
      4. Re-rank using BitNet's intent + chunk similarity

    Gemini is NOT used anywhere — we use Ollama for embeddings and
    MiniMax M2.7 only for LLM chunking (offline at retrieve time).
    """
    from src.retrieval.engine import RetrievalEngine
    from src.retrieval.intent_detector import IntentDetector

    # Step 1: Embed the query via Ollama (LOCAL, no rate limits)
    query_embedding = None
    try:
        from src.retrieval.ollama_embeddings import OllamaEmbeddingClient

        ollama_emb = OllamaEmbeddingClient()
        query_embedding = ollama_emb.embed(body.prompt_context)
        logger.info("ollama_query_embedding dim=%d", len(query_embedding))
    except Exception as exc:
        logger.warning("ollama_query_embed_failed err=%s — falling back to tag-only", exc)

    # Step 2: Intent detection.
    # Default: keyword-based intent detection (<10ms, no LLM roundtrip).
    # Set BITNET_INTENT=1 to enable the BitNet → MiniMax → keyword cascade.
    # When the cascade is on, each call increments a counter that surfaces
    # in /dev/stats so operators can see which path is serving traffic.
    intent: str = "general"
    detected_tags: list[str] = []
    use_cascade = os.environ.get("BITNET_INTENT", "").lower() in ("1", "true", "yes")
    if use_cascade:
        from src.retrieval.bitnet_client import detect_intent_cascade

        intent_result = detect_intent_cascade(body.prompt_context)
        intent = intent_result.intent
        detected_tags = list(intent_result.detected_tags)
    else:
        intent_detector = IntentDetector()
        intent_result = intent_detector.detect(body.prompt_context)
        intent = intent_result.get("intent", "general")
        detected_tags = intent_result.get("detected_tags", [])

    # Step 3: Retrieve using engine (tag match + outcome priority)
    engine = RetrievalEngine(
        repository=repo(),
        qdrant_search=qdrant_search(),
    )
    result = engine.retrieve(
        prompt_context=body.prompt_context,
        session_id=body.session_id,
        query_embedding=query_embedding,
    )

    # Merge BitNet intent into response
    if isinstance(result, dict):
        result["intent"] = intent
        result["detected_tags"] = detected_tags

    return JSONResponse(content=result)


# ════════════════════════════════════════════════════════════════════════════
# Phase 5 — Memory Guard
# ════════════════════════════════════════════════════════════════════════════


@app.post(
    "/guard",
    tags=["Phase 5"],
    dependencies=[Depends(verify_api_key)],
)
def guard(body: GuardRequest) -> JSONResponse:
    """
    POST /guard — check if proposed change contradicts a past failed attempt.
    """
    from src.guard.diff_engine import DiffEngine

    # Wire Gemini as the embedding service. When GEMINI_API_KEY is set,
    # the guard uses real semantic similarity (cosine over Gemini embeddings)
    # instead of Jaccard word overlap.
    embedding_service = None
    if os.environ.get("GEMINI_API_KEY"):
        try:
            from src.retrieval.gemini_embeddings import GeminiEmbeddingClient

            _gemini_for_guard = GeminiEmbeddingClient()

            def _embed_for_guard(text: str) -> list[float]:
                return _gemini_for_guard.embed(text)

            embedding_service = _embed_for_guard
        except Exception as exc:
            logger.warning(
                "Failed to init Gemini for guard, falling back to Jaccard: %s",
                exc,
            )

    engine = DiffEngine(
        repository=repo(),
        qdrant_search=qdrant_search(),
        embedding_service=embedding_service,
    )
    result = engine.check(
        proposed_change=body.proposed_change,
        target_file=body.target_file,
        session_id=body.session_id,
    )
    return JSONResponse(content=result)


# ════════════════════════════════════════════════════════════════════════════
# Phase 6 — Pre-Tool Hook / mneme_inject
# ════════════════════════════════════════════════════════════════════════════


@app.post(
    "/inject",
    tags=["Phase 6"],
    dependencies=[Depends(verify_api_key)],
)
def inject(body: InjectRequest) -> JSONResponse:
    """
    POST /inject — mneme_inject equivalent over HTTP.
    Orchestrates: Phase 4 retrieve + Phase 5 guard, returns injection context.
    """
    from src.hook.mneme import Mneme

    engine = Mneme(repository=repo())
    result = engine.inject(message=body.message, session_id=body.session_id)
    return JSONResponse(content=result)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_tags(tags: list[str]) -> dict[str, Any]:
    """Parse flat tags into a TagTree-like dict."""
    tag_tree: dict[str, Any] = {"category": "tool"}
    for tag in tags:
        if "=" in tag:
            key, val = tag.split("=", 1)
            if key in ("tool", "memory", "skill", "context"):
                tag_tree["category"] = key
            elif key in (
                "failed",
                "successfully_called",
                "no_tool_called",
                "stopped",
                "work_done",
            ):
                tag_tree["outcome"] = val
            elif key in ("token_expired", "timeout", "auth_rejected"):
                tag_tree["error"] = val
            else:
                tag_tree[key] = val
    return tag_tree
