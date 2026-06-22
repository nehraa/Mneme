# MNEME Retrieval Quality Fix Plan

**Status:** June 22 2026, post-ingest, post-dedup
**Current state:** 26,002 chunks, 25,158 with embeddings (97%), 844 nulls
**Both servers up:** MNEME :8080, BitNet :8081 (BitNet stays — user explicit)

---

## Diagnosis

**What's working:**
- Pipeline runs end-to-end: Ollama embed → BitNet intent → cosine search → ranking
- Cosine similarity is strong (0.7-0.85 on good queries)
- Tag detection from BitNet returns useful terms (`['course','student','IELTS','OpenMAIC']`)
- Dedup removed 5,321 within-source duplicates; corpus cleaner

**What's broken (ranked by impact on retrieval quality):**

### 1. **BitNet intent JSON parser fails silently** — HIGH impact
- **Symptom:** Response has `intent: <continue_previous_work|retry_previous_attempt|fix_previous_failure|general>` — the raw template string, not a picked label
- **Cause:** `bitnet_client.py:_parse_response()` expects clean JSON. BitNet outputs things like `{"intent": "general", "detected_tags": ["tag1", "linux"]}` which the regex catches, but when tags list is malformed it falls back to the placeholder
- **Effect:** `detected_tags` ends up empty, so tag-match in scoring contributes 0
- **User constraint:** BitNet stays — we fix the parser, not remove BitNet

### 2. **Heuristic chunks have no real tags** — HIGH impact
- **Symptom:** ~50% of chunks (those where MiniMax returned prose) are tagged only `["outcome=work_done", "source=heuristic"]`
- **Cause:** When MiniMax M2.7 returns think-block prose instead of JSON, we fall back to paragraph chunking with no tag inference
- **Effect:** These chunks score the same in tag-match (0) so they only rank by cosine
- **Fix:** Tag enrichment pass — re-process heuristic chunks through MiniMax with a tag-only prompt (no chunking, just tags)

### 3. **MiniMax JSON parser misses ~5% of responses** — MEDIUM impact
- **Symptom:** `WARNING minimax_fallback_heuristic path=... err=Could not parse JSON from LLM response`
- **Cause:** MiniMax sometimes wraps JSON in ```json ... ``` fences, or puts prose around it. Current parser tries 4 strategies but still misses when output has trailing prose
- **Effect:** Files where parse fails become heuristic chunks (problem #2 above)
- **Fix:** Improve parser with more aggressive regex strategies + try the largest balanced `{...}` block

### 4. **Sessions dominate the corpus (88% of chunks)** — MEDIUM impact
- **Symptom:** Top 5 results often come from session files (chat logs)
- **Cause:** 23K session chunks vs 3K skill chunks. Cosine matching naturally picks the most-chunked content
- **Effect:** Skills content (cleaner, more authoritative) gets buried under chat log noise
- **Fix:** Boost `source_kind=skill` chunks in scoring formula (×1.5 on outcome_weight or similar)

### 5. **Cross-source duplicates persist** — LOW impact
- **Symptom:** Top 5 results sometimes include 5 copies of the same content from different sessions
- **Cause:** Dedup was per-source-file; same content from different sessions survives
- **Effect:** Reduces diversity of returned chunks, not relevance
- **Fix:** Cross-source dedup (O(N²), ~10-15 min for 26K chunks)

### 6. **Embedding model is small (0.6B params)** — LOW impact
- **Symptom:** Cosine similarities cluster in 0.5-0.8 range instead of having strong 0.9+ matches
- **Cause:** qwen3-embedding:0.6b is a small embedding model
- **Effect:** Hard to get clear "exact match" signal from cosine
- **Fix:** Swap to a larger embedding model (mxbai-embed-large 1024-dim, or nomic-embed-text 768-dim). Requires re-embedding all 25K chunks

---

## Fix Plan (ordered by ROI)

### Phase 1: Fix what's broken in code (low risk, 2-3 hours)

**1A. Fix BitNet intent JSON parser** (`src/retrieval/bitnet_client.py`)
- Add fallback: if no JSON found, look for first quoted intent label
- If still nothing, extract intent from prose (`"intent is general"` → `general`)
- Add `degraded=True` flag to IntentResult so we know it's a fallback
- Test: verify on 5 sample prompts that we get a real intent label, not the placeholder

**1B. Fix MiniMax JSON parser** (`src/ingestion/llm_client.py`)
- Add strategy: find the largest balanced `{...}` block (handles trailing prose)
- Add strategy: strip everything after first complete JSON object
- Add strategy: try to repair truncated JSON (close open quotes/braces)
- Re-ingest files where parse failed (read from `ingest_full_manifest.json` where status != "done")

**1C. Boost skill chunks in scoring** (`src/retrieval/engine.py:_score_chunks`)
- Multiply outcome_weight by 1.5 when `source_kind == "skill"`
- Test: query "Aidutech" should rank skill chunks above session chunks

**Estimated impact:** Phase 1 alone should bring relevance from ~70% to ~85%

### Phase 2: Backfill what was missed (medium risk, 1-2 hours)

**2A. Tag enrichment pass** — `scripts/enrich_tags.py` (NEW)
- Read JSONL, find chunks with `tags` containing only `source=heuristic`
- For each, call MiniMax M2.7 with prompt: `"Reply with comma-separated tags for this text: <chunk_content>"`
- Update tags in place, write back via atomic temp+rename
- Run during off-hours (MiniMax has rate limits even at higher tiers)

**2B. Backfill 844 null embeddings** — use existing `scripts/backfill_embeddings.py --apply`
- Run as soon as free RAM allows (Ollama needs 1.3GB)

**2C. Re-ingest files that MiniMax parse failed on**
- Read manifest, find any path with status != "done"
- Re-run those files through the updated parser
- Expected: 200-500 files would re-process correctly with the improved parser

**Estimated impact:** Phase 2 brings relevance to ~90%+

### Phase 3: Deduplicate across sources (low risk, 1-2 hours)

**3A. Cross-source dedup** — `scripts/dedupe_chunks.py` enhancement
- Add `--cross-source` flag (default off — it's O(N²))
- For 26K chunks: 676M comparisons, ~10-15 min wall time
- Use same numpy vectorized cosine as the in-memory retrieval
- Only run after Phase 1+2 are done (so we don't dedup before enrich)

**Estimated impact:** Removes the 5-copies-of-same-content issue. Doesn't increase relevance, increases diversity.

### Phase 4: Better embeddings (high risk, multi-day)

**4A. Try mxbai-embed-large** (1024-dim, stronger than qwen3-embedding:0.6b)
- `ollama pull mxbai-embed-large`
- Re-embed all 25K chunks (~2 hours wall time, $0 cost)
- Re-run test queries, compare relevance scores
- If better: commit the model switch; if worse: keep qwen3

**4B. Try nomic-embed-text** (768-dim, popular for semantic search)
- `ollama pull nomic-embed-text` (already pulled, 274MB)
- Same re-embed + test workflow

**Estimated impact:** Could bring relevance to 95%+ if a better model actually helps

### Phase 5: Intent improvements (optional)

**5A. Improve BitNet intent prompt** — see if better wording produces more reliable JSON
- Try giving BitNet concrete examples in the system prompt
- Cap temperature at 0.0 (deterministic) for intent calls

**5B. Add cross-prompt intent validation** — if BitNet returns intent that's not in the taxonomy, default to "general" instead of returning the template string

**Estimated impact:** Marginal — the parser fix in Phase 1A already handles this

---

## Success Metrics

**Before any fix:**
- Average cosine similarity on relevant queries: 0.7-0.85
- Tag-match contribution to ranking: 0 (BitNet broken + heuristic chunks empty)
- Duplicate results: 4-5 of 5 top hits often same content

**After Phase 1:**
- Cosine: unchanged
- Tag-match: working (BitNet parses, tags populated)
- Duplicates: reduced (skill chunks boosted, less session noise)

**After Phase 2:**
- Cosine: 0.8-0.9 (heuristic chunks now have real embeddings + tags)
- Tag-match: high (every chunk has 3-5 tags)
- Duplicates: same as Phase 1

**After Phase 3:**
- All above + no cross-source duplicates in top 5

**After Phase 4 (if model swap helps):**
- Cosine: 0.85-0.95 (clear strong matches)
- Tag-match: high
- Duplicates: same

---

## Risk Profile

| Phase | Risk | Failure Mode | Mitigation |
|-------|------|--------------|------------|
| 1A | Low | Parser still misses | Add more fallback strategies; eventually disable BitNet for intent (NO — user said keep) |
| 1B | Low | Parser still misses | Same |
| 1C | Very low | Wrong boost amount | Test on 10 queries, tune coefficient |
| 2A | Medium | MiniMax API rate limit | Batch with delays, run in background |
| 2B | Low | Ollama OOM | Wait for free RAM, run during quiet hours |
| 2C | Low | Same as 1B | Same |
| 3A | Low | O(N²) too slow | Run overnight, or limit to top-N cosine |
| 4A | Medium | Different model is worse | A/B test with both before committing |
| 4B | Medium | Same | Same |
| 5A | Very low | BitNet still hallucinates | Accept it, parser handles fallback |

---

## What We Are NOT Doing

- ❌ Removing BitNet from the pipeline (user constraint)
- ❌ Switching to cloud-only LLM providers (privacy + cost)
- ❌ Replacing the JSONL-on-disk storage with a database (out of scope)
- ❌ Re-ingesting from scratch (would lose 26K chunks of work)

---

## Estimated Total Effort

| Phase | Time | Can Run |
|-------|------|---------|
| 1A | 1 hour | Now |
| 1B | 1 hour | Now |
| 1C | 30 min | Now |
| 2A | 1-2 hours (script) + 2-3 hours (run) | Tonight |
| 2B | 5 min (script) + 5 min (run) | After free RAM |
| 2C | 30 min (re-run) | After 1B |
| 3A | 30 min (script) + 15 min (run) | Tomorrow |
| 4A | 1 hour (switch) + 2 hours (re-embed) | This week |
| 4B | 1 hour + 2 hours | This week |

**Realistic order:** 1C → 1A → 1B → 2C → 2B → 2A → 3A → 4A → 4B

**Stop and re-evaluate after Phase 2.** By then we should be at 85-90% relevance. Phase 3+4 only if needed.
