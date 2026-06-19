#!/usr/bin/env python3
"""
E2E test — full Mneme pipeline on a demo repository.
Writes results to /tmp/mneme-e2e-result.txt so we can capture it.
"""
import sys, glob, os
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, '.')
from dotenv import load_dotenv
# Load .env from the repo root (where this script lives) rather than a
# hard-coded absolute path so the script works on any checkout.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ['NEO4J_URI'] = 'bolt://localhost:7687'
os.environ['QDRANT_HOST'] = 'http://localhost:6333'

from src.config import get_config
from src.memory_store.neo4j_repository import Neo4jMemoryRepository
from src.retrieval.qdrant_search import QdrantSearch
from src.retrieval.gemini_embeddings import GeminiEmbeddingClient, EMBEDDING_DIM as GEMINI_DIM
from src.retrieval.ollama_embeddings import OllamaEmbeddingClient
from src.retrieval.bitnet_client import detect_intent
from src.ingestion.pipeline import IngestionPipeline
from src.guard.diff_engine import DiffEngine

config = get_config()
repo = Neo4jMemoryRepository(uri=config.neo4j.uri, user=config.neo4j.user, password=config.neo4j.password or "")

# Use the configured embedding provider to determine Qdrant vector size
provider = config.llm.embedding_provider
if provider == "ollama":
    vector_dim = config.llm.ollama_embedding_dim
    embed_client = OllamaEmbeddingClient()
    embed_label = f"Ollama/{config.llm.ollama_embedding_model}"
else:
    vector_dim = GEMINI_DIM
    embed_client = GeminiEmbeddingClient()
    embed_label = f"Gemini/{config.llm.gemini_embedding_model}"

qdrant = QdrantSearch(host="http://localhost:6333", collection="mneme_chunks", vector_size=vector_dim)

print("【0】 Backends...")
print(f"  Neo4j ✓  Qdrant ({vector_dim}D) ✓  {embed_label} ✓")

print("\n【1】 LLM-assisted chunking...")
all_files = sorted([f for f in glob.glob("/tmp/mneme-demo/**/*", recursive=True)
                   if os.path.isfile(f) and (f.endswith('.py') or f.endswith('.md'))])
print(f"  Files: {[os.path.basename(f) for f in all_files]}")
pipeline = IngestionPipeline(repository=repo)
manifest = pipeline.run(all_files, session_id="e2e-demo", project_root="/tmp/mneme-demo")
print(f"  ✓ chunks={manifest.get('chunks_created','?')}  files={manifest.get('files_processed','?')}")

print("\n【2】 Chunks in Neo4j...")
all_chunks = list(repo.list_chunks())
by_f = defaultdict(list)
for c in all_chunks:
    by_f[c['source_file']].append(c)
print(f"  {len(all_chunks)} chunks | {len(by_f)} files")
for fname, chunks in sorted(by_f.items(), key=lambda x: x[0] or ""):
    tags = sorted(set(t.split('=')[0] if '=' in t else t
                       for c in chunks for t in c.get('tags', [])))
    basename = os.path.basename(fname) if fname else "(unknown)"
    print(f"    {basename:28s} {len(chunks):2d} chunks  cats: {tags}")

print("\n【3】 Qdrant vectors...")
results = qdrant.search(query_embedding=[0.0]*vector_dim, limit=10, filter_conditions=None)
print(f"  ✓ {len(results)} vectors in '{qdrant._collection}'")

print("\n【4】 BitNet Intent Detection...")
prompt = "continue the auth token validation flow we were working on"
r = detect_intent(prompt)
print(f"  Prompt: \"{prompt}\"")
print(f"  → Intent: {r.intent}  Tags: {r.detected_tags}  LLM={not r.degraded}")

print("\n【5】 Tag-based retrieval (Neo4j)...")
target = r.detected_tags

def tag_cat(t):
    return t.split('=')[0] if '=' in t else t

matching = [c for c in all_chunks
            if any(tag_cat(t) in {tag_cat(x) for x in c.get('tags', [])} for t in target)]
seen, unique = set(), []
for c in matching:
    if c['chunk_id'] not in seen:
        seen.add(c['chunk_id'])
        unique.append(c)
top_chunks = sorted(
    unique,
    key=lambda x: (0 if x.get('last_accessed') is None else 1, x.get('last_accessed') or ""),
    reverse=True
)[:10]
print(f"  {len(unique)} match → top {len(top_chunks)}:")
for i, c in enumerate(top_chunks, 1):
    preview = c['content'][:85].replace('\n', ' ')
    print(f"  [{i}] {os.path.basename(c['source_file'])}:{c['chunk_id']}  tags={c.get('tags', [])}")
    print(f"       \"{preview}...\"")

print("\n【6】 Semantic re-ranking...")
qemb = embed_client.embed(prompt)
print(f"  Query: {len(qemb)}D vector from {embed_label}")

def cosine(a, b):
    dot = sum(x*y for x, y in zip(a, b))
    return dot / ((sum(x*x for x in a)**0.5) * (sum(x*x for x in b)**0.5) + 1e-10)

reranked = sorted(
    [(cosine(qemb, embed_client.embed(c['content'])), c) for c in top_chunks],
    reverse=True
)
for rank, (score, c) in enumerate(reranked[:5], 1):
    print(f"  #{rank} sim={score:.4f}  {os.path.basename(c['source_file'])}:{c['chunk_id']}  tags={c.get('tags',[])}")

print("\n【7】 Best match → agent context injection")
best = reranked[0][1]
content = best['content']
print(f"  FILE: {best['source_file']}:{best['chunk_id']}")
print(f"  TAGS: {best.get('tags', [])}")
print(f"  CONTENT:\n{'-'*50}\n{content[:1500]}\n{'-'*50}\n  → INJECTED")

print("\n【8】 Memory Guard (semantic diff)...")
g = DiffEngine(repository=repo)
guard = g.check(
    proposed_change=best['content'],
    target_file=best['source_file'],
)
print(f"  triggered={guard.get('triggered','?')}  sim={guard.get('similarity',0.0):.4f}  related={len(guard.get('related_chunks',[]))}")
print(f"  {guard.get('message','')}")

print("\n╔══════════════════════════════════════════════╗")
print("║  ✅  E2E COMPLETE                             ║")
print("╚══════════════════════════════════════════════╝")
