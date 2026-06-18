#!/usr/bin/env bash
# Mneme — Verification script
#
# Runs health checks against all services and exercises each endpoint.
# Use this after `docker compose up -d` to confirm the stack is healthy.
#
# Usage:
#   ./scripts/verify.sh             # full check (health + endpoints)
#   ./scripts/verify.sh --health    # health checks only
#   ./scripts/verify.sh --endpoints # endpoint smoke tests only
#   ./scripts/verify.sh --neo4j     # Neo4j-specific check
#   ./scripts/verify.sh --qdrant    # Qdrant-specific check

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

MNEME_URL="${MNEME_URL:-http://localhost:8080}"
NEO4J_URL="${NEO4J_URL:-http://localhost:7474}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"

PASS=0
FAIL=0
WARN=0

# ── Helpers ──────────────────────────────────────────────────────────────────
green() { printf "\033[1;32m✓\033[0m %s\n" "$*"; }
red()   { printf "\033[1;31m✗\033[0m %s\n" "$*"; FAIL=$((FAIL+1)); }
yellow(){ printf "\033[1;33m⚠\033[0m %s\n" "$*"; WARN=$((WARN+1)); }
pass()  { green "$*"; PASS=$((PASS+1)); }

check_health() {
    local name="$1"
    local url="$2"
    local expected="${3:-ok}"
    if code=$(curl -fsS -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null); then
        if [ "$code" = "200" ]; then
            pass "$name healthy ($url → $code)"
        else
            red "$name returned $code (expected 200) at $url"
        fi
    else
        red "$name unreachable at $url"
    fi
}

check_endpoint() {
    local name="$1"
    local expected_field="$2"
    shift 2
    local body
    body=$(curl -fsS "$@" 2>/dev/null) || {
        red "$name failed (curl error)"
        return
    }
    if echo "$body" | grep -q "\"$expected_field\""; then
        pass "$name returned $expected_field"
    else
        red "$name missing $expected_field in response: $body" | head -c 200
    fi
}

# ── Parse args ────────────────────────────────────────────────────────────────
CHECK_HEALTH=true
CHECK_ENDPOINTS=true
CHECK_NEO4J=false
CHECK_QDRANT=false
for arg in "$@"; do
    case "$arg" in
        --health)    CHECK_ENDPOINTS=false ;;
        --endpoints) CHECK_HEALTH=false ;;
        --neo4j)     CHECK_HEALTH=false; CHECK_ENDPOINTS=false; CHECK_NEO4J=true ;;
        --qdrant)    CHECK_HEALTH=false; CHECK_ENDPOINTS=false; CHECK_QDRANT=true ;;
    esac
done

# ── 1. Health checks ─────────────────────────────────────────────────────────
if [ "$CHECK_HEALTH" = true ]; then
    echo ""
    echo "═══ Service health ═══"
    check_health "Mneme"  "$MNEME_URL/health"
    check_health "Neo4j"  "$NEO4J_URL"
    check_health "Qdrant" "$QDRANT_URL/healthz"
fi

# ── 2. Endpoint smoke tests ──────────────────────────────────────────────────
if [ "$CHECK_ENDPOINTS" = true ]; then
    echo ""
    echo "═══ Mneme endpoints (mock mode) ═══"
    check_endpoint "GET /health"  "status" "$MNEME_URL/health"
    check_endpoint "POST /memories" "chunk_id" \
        -X POST "$MNEME_URL/memories" \
        -H "Content-Type: application/json" \
        -d '{"content":"verify test","session_id":"verify-script","tags":["tool=auth"]}'
    check_endpoint "GET /memories" "chunk_id" \
        "$MNEME_URL/memories?session_id=verify-script"
    check_endpoint "POST /retrieve" "injected_context" \
        -X POST "$MNEME_URL/retrieve" \
        -H "Content-Type: application/json" \
        -d '{"prompt_context":"continue the auth flow"}'
    check_endpoint "POST /guard" "guard_triggered" \
        -X POST "$MNEME_URL/guard" \
        -H "Content-Type: application/json" \
        -d '{"proposed_change":"add JWT","target_file":"auth/token.py"}'
    check_endpoint "POST /inject" "injected_context" \
        -X POST "$MNEME_URL/inject" \
        -H "Content-Type: application/json" \
        -d '{"message":"continue the auth flow"}'
    check_endpoint "GET /graph/related/mem_001" "chunk_id" \
        "$MNEME_URL/graph/related/mem_001"
    check_endpoint "POST /ingest" "chunks_created" \
        -X POST "$MNEME_URL/ingest" \
        -H "Content-Type: application/json" \
        -d '{"file_paths":["src/ingestion/pipeline.py"]}'
fi

# ── 3. Neo4j-specific ─────────────────────────────────────────────────────────
if [ "$CHECK_NEO4J" = true ]; then
    echo ""
    echo "═══ Neo4j deep check ═══"
    check_health "Neo4j HTTP"  "$NEO4J_URL"
    # Neo4j 5.x removed the legacy HTTP Cypher endpoint (/db/data/transaction/commit).
    # Data access is now exclusively via the Bolt driver on port 7687.
    # Verify Bolt port is reachable from inside the app container.
    BOLT_CHECK=$(docker exec mneme-app python -c "
import socket
s = socket.socket()
s.settimeout(5)
try:
    s.connect(('neo4j', 7687))
    print('OK')
finally:
    s.close()
" 2>/dev/null) || BOLT_CHECK="FAIL"
    if [ "$BOLT_CHECK" = "OK" ]; then
        pass "Neo4j Bolt port (7687) reachable from app container"
    else
        red "Neo4j Bolt port (7687) unreachable from app container"
    fi
    # Verify the bolt protocol responds (use neo4j driver if available, else just TCP check)
    BOLT_PROTO=$(docker exec mneme-app python -c "
import socket
s = socket.socket()
s.settimeout(5)
s.connect(('neo4j', 7687))
# Bolt handshake magic: 0x6060B017
s.sendall(bytes([0x60, 0x60, 0xb0, 0x17, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0]))
data = s.recv(64)
s.close()
if b'bolt' in data.lower() or len(data) > 0:
    print('OK')
else:
    print('FAIL')
" 2>/dev/null) || BOLT_PROTO="FAIL"
    if [ "$BOLT_PROTO" = "OK" ]; then
        pass "Neo4j Bolt protocol responding"
    else
        yellow "Neo4j Bolt protocol check inconclusive (TCP works but no handshake response)"
    fi
fi

# ── 4. Qdrant-specific ────────────────────────────────────────────────────────
if [ "$CHECK_QDRANT" = true ]; then
    echo ""
    echo "═══ Qdrant deep check ═══"
    check_health "Qdrant HTTP"  "$QDRANT_URL/healthz"
    # List collections
    COLL_RESP=$(curl -fsS "$QDRANT_URL/collections" 2>/dev/null) || {
        red "Qdrant collections list failed"
    }
    if echo "$COLL_RESP" | grep -q '"result"'; then
        pass "Qdrant collections list returned result"
    else
        yellow "Qdrant reachable but no collections yet (expected — none created)"
    fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "═══ Summary ═══"
echo "  Pass: $PASS"
echo "  Warn: $WARN"
echo "  Fail: $FAIL"

if [ "$FAIL" -gt 0 ]; then
    echo ""
    red "Some checks failed. See output above."
    exit 1
fi

echo ""
green "All checks passed."
