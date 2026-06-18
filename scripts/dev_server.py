#!/usr/bin/env python3
"""
dev_server.py — MNEME server in DEV MODE (no Neo4j, no Qdrant).

Uses InMemoryMemoryRepository pre-loaded from /home/Hermes/Mneme/data/skill_chunks.jsonl,
plus MockRetrievalEngine (keyword + tag-based scoring) so /retrieve works without
external services.

Run:  cd /home/Hermes/Mneme && uv run uvicorn scripts.dev_server:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import load_dotenv

# Load .env before any src.* imports
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Reuse the real server's API surface (models, auth, routes) by importing it,
# then swap its `repo()` factory to return our in-memory repo.
import src.server as real_server
from src.memory_store.repository import InMemoryMemoryRepository

REPO = InMemoryMemoryRepository()
JSONL_PATH = Path("/home/Hermes/Mneme/data/skill_chunks.jsonl")
_repo_lock = threading.Lock()
_loaded = False


def _load_jsonl() -> int:
    """Load all chunks from JSONL into REPO. Returns count loaded."""
    if not JSONL_PATH.exists():
        print(f"[dev_server] no JSONL at {JSONL_PATH} yet (ingest may be running)")
        return 0
    count = 0
    edges = 0
    with open(JSONL_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ch = json.loads(line)
                REPO.create_chunk({
                    "chunk_id": ch["chunk_id"],
                    "session_id": ch.get("session_id", "loaded"),
                    "project_root": ch.get("project_root", "/home/Hermes"),
                    "content": ch["content"],
                    "page_order": ch.get("page_order", 0),
                    "tags": ch.get("tags", []),
                    "source_file": ch.get("source_file", ""),
                    "outcome_tag": ch.get("outcome_tag", "work_done"),
                    "linked_chunks": ch.get("linked_chunks", []),
                    # Preserve embedding if present (Gemini Embedding 2, 768-dim)
                    "embedding": ch.get("embedding"),
                    "embedding_model": ch.get("embedding_model"),
                    "embedding_dim": ch.get("embedding_dim"),
                    "source_kind": ch.get("source_kind", "unknown"),
                })
                count += 1
                for tgt in ch.get("linked_chunks", []):
                    try:
                        REPO.create_edge({
                            "source_chunk_id": ch["chunk_id"],
                            "target_chunk_id": tgt,
                            "relationship_type": "linked",
                            "reason": "from JSONL",
                        })
                        edges += 1
                    except Exception:
                        pass
            except Exception as e:
                print(f"[dev_server] skip bad line: {e}")
    print(f"[dev_server] loaded {count} chunks, {edges} edges from {JSONL_PATH}")
    return count


def _dev_repo_factory():
    """Replacement for get_repository() that returns the in-memory repo."""
    global _loaded
    with _repo_lock:
        if not _loaded:
            _load_jsonl()
            _loaded = True
    return REPO


# Monkeypatch: swap get_repository in the server module
real_server.get_repository = _dev_repo_factory
real_server._repo = REPO  # also pre-set the module-level cache

# Also: stub out the Qdrant search so the retrieval engine doesn't try to
# connect to a real Qdrant.
def _no_qdrant():
    return None

real_server.qdrant_search = _no_qdrant


# Lifespan wrapper that just logs; the real one is in real_server
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Pre-load on startup
    _load_jsonl()
    print(f"[dev_server] READY — {REPO._chunks.__len__()} chunks available")
    yield


# Reuse the real app but override its lifespan
real_app = real_server.app
real_app.router.lifespan_context = lifespan

# Add permissive CORS for dev
real_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Expose as `app` for uvicorn
app = real_app


@app.post("/dev/reload")
def dev_reload():
    """DEV-ONLY: re-load the JSONL into the in-memory repo.

    Useful when the ingest script is still running and you want fresh chunks
    without restarting the server. Drops existing data first.
    """
    global _loaded
    with _repo_lock:
        # Clear the existing repo
        REPO._chunks.clear()
        REPO._edges.clear()
        # Reload
        n = _load_jsonl()
        _loaded = True
    return {"reloaded": n, "total_chunks": len(REPO._chunks)}


@app.get("/dev/stats")
def dev_stats():
    """Quick stats for the in-memory repo."""
    return {
        "chunks": len(REPO._chunks),
        "edges": len(REPO._edges),
        "jsonl_exists": JSONL_PATH.exists(),
        "jsonl_size_mb": round(JSONL_PATH.stat().st_size / 1e6, 2) if JSONL_PATH.exists() else 0,
    }
