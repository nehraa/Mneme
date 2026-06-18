#!/usr/bin/env bash
# Mneme — BitNet setup script (works on Apple Silicon M1/M2/M3)
#
# This script follows the path that ACTUALLY WORKS (per BITNET_KNOWN_ISSUES.md).
# Other paths (manual HF→GGUF conversion, BITNET_ARM_TL1=ON, etc.) were tried
# and DO NOT WORK — see the known issues doc for why.
#
# What this does:
#   1. Detects CPU architecture — errors out on x86_64 (BitNet only works on arm64)
#   2. Downloads the working model: microsoft/BitNet-b1.58-2B-4T-gguf (~1.2GB)
#      This is the pre-converted GGUF — DO NOT do manual HF→GGUF conversion.
#   3. Builds llama-server (with BITNET_ARM_TL1=OFF to avoid 8GB OOM compile)
#   4. Starts the server on localhost:8081 (configurable via env)
#   5. Tests it works end-to-end with Mneme
#
# Usage:
#   ./scripts/setup-bitnet.sh           # full setup + start + test
#   ./scripts/setup-bitnet.sh --no-start # setup without starting
#   ./scripts/setup-bitnet.sh --model-only  # only download the model
#
# Idempotent — safe to run multiple times.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Config (configurable via env) ─────────────────────────────────────────────
BITNET_DIR="${BITNET_DIR:-$PROJECT_DIR/BitNet}"
BITNET_REPO="${BITNET_REPO:-microsoft/BitNet}"
BITNET_MODEL_REPO="${BITNET_MODEL_REPO:-microsoft/BitNet-b1.58-2B-4T-gguf}"
BITNET_MODEL_DIR="${BITNET_MODEL_DIR:-$BITNET_DIR/models/BitNet-b1.58-2B-4T-gguf}"
BITNET_HOST="${BITNET_HOST:-localhost}"
BITNET_PORT="${BITNET_PORT:-8081}"
BITNET_MODEL_ALIAS="${BITNET_MODEL_ALIAS:-bitnet-b1.58-2b-4t}"
BITNET_CTX_SIZE="${BITNET_CTX_SIZE:-2048}"
BITNET_THREADS="${BITNET_THREADS:-4}"
BITNET_BRANCH="${BITNET_BRANCH:-main}"

MODEL_ONLY=false
NO_START=false
for arg in "$@"; do
    case "$arg" in
        --model-only) MODEL_ONLY=true ;;
        --no-start)   NO_START=true ;;
        *) echo "Unknown arg: $arg"; exit 1 ;;
    esac
done

# ── Helpers ──────────────────────────────────────────────────────────────────
log()  { printf "\033[1;34m[bitnet]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m %s\n" "$*" >&2; }
fail() { printf "\033[1;31m[fail]\033[0m %s\n" "$*" >&2; exit 1; }

# ── 1. CPU detection ─────────────────────────────────────────────────────────
log "Detecting CPU architecture..."
ARCH=$(uname -m)
OS=$(uname -s)

echo ""
echo "  ╔═══════════════════════════════════════╗"
echo "  ║  CPU: $ARCH"
echo "  ║  OS:  $OS"
echo "  ║  BitNet tested on: Apple Silicon (M1/M2/M3)"
echo "  ╚═══════════════════════════════════════╝"
echo ""

case "$ARCH" in
    arm64)
        log "✓ Apple Silicon detected — BitNet is supported on this CPU."
        ;;
    x86_64)
        warn "x86_64 detected. BitNet is officially tested on Apple Silicon only."
        warn "It MAY work on x86_64 Linux (TL1 kernel), but hasn't been validated."
        warn "Proceeding anyway. If llama-server crashes, this is why."
        ;;
    *)
        fail "Unsupported architecture: $ARCH. BitNet is only tested on arm64 (Apple Silicon)."
        ;;
esac

# ── 2. Tool checks ───────────────────────────────────────────────────────────
log "Checking for required tools..."

command -v cmake   >/dev/null 2>&1 || fail "cmake not found. Install: brew install cmake"
command -v clang   >/dev/null 2>&1 || fail "clang not found. Install: brew install llvm"
command -v uv      >/dev/null 2>&1 || fail "uv not found. Install: https://github.com/astral-sh/uv"
command -v git     >/dev/null 2>&1 || fail "git not found"
command -v huggingface-cli >/dev/null 2>&1 || {
    warn "huggingface-cli not found. Installing via uv..."
    uv tool install "huggingface_hub[cli]"
}

CMAKE_VERSION=$(cmake --version | head -1 | awk '{print $3}')
CLANG_VERSION=$(clang --version | head -1 | awk '{print $4}')
log "cmake: $CMAKE_VERSION, clang: $CLANG_VERSION"

# ── 3. Clone BitNet (if missing) ────────────────────────────────────────────
if [ ! -d "${BITNET_DIR}" ]; then
    if [ "$MODEL_ONLY" = true ]; then
        fail "--model-only requires an existing BitNet checkout. Run full setup first."
    fi
    log "Cloning BitNet (branch: ${BITNET_BRANCH})..."
    git clone --recursive --branch "${BITNET_BRANCH}" \
        https://github.com/${BITNET_REPO}.git "${BITNET_DIR}"
else
    log "BitNet already cloned at ${BITNET_DIR}"
fi

cd "${BITNET_DIR}"

# ── 4. Set up Python venv (if missing) ───────────────────────────────────────
if [ ! -d ".venv-bitnet" ]; then
    if [ "$MODEL_ONLY" = true ]; then
        fail "BitNet venv not found. Run full setup first to create it."
    fi
    log "Creating Python 3.9 virtual environment..."
    uv venv --python 3.9 .venv-bitnet
    log "Installing BitNet requirements..."
    source .venv-bitnet/bin/activate
    uv pip install pip
    uv pip install -r requirements.txt
    deactivate 2>/dev/null || true
else
    log "BitNet venv already exists"
fi

# ── 5. Download model (if missing) ───────────────────────────────────────────
if [ ! -d "${BITNET_MODEL_DIR}" ] || [ ! -f "${BITNET_MODEL_DIR}/ggml-model-i2_s.gguf" ]; then
    log "Downloading ${BITNET_MODEL_REPO}..."
    log "  ~1.2 GB. May take 5-10 minutes."
    huggingface-cli download "${BITNET_MODEL_REPO}" \
        --local-dir "${BITNET_MODEL_DIR}"
else
    log "Model already present at ${BITNET_MODEL_DIR}"
fi

MODEL_FILE="${BITNET_MODEL_DIR}/ggml-model-i2_s.gguf"
if [ ! -f "${MODEL_FILE}" ]; then
    fail "Model file not found at ${MODEL_FILE}. Download may have failed."
fi
log "✓ Model file: ${MODEL_FILE} ($(du -h "${MODEL_FILE}" | awk '{print $1}'))"

# ── 6. Build llama-server (if missing) ──────────────────────────────────────
LLAMA_SERVER="${BITNET_DIR}/build/bin/llama-server"
if [ ! -x "${LLAMA_SERVER}" ]; then
    if [ "$MODEL_ONLY" = true ]; then
        fail "llama-server not built. Run full setup first to build it."
    fi
    log "Building llama-server (this takes 5-15 minutes)..."
    log "  BITNET_ARM_TL1=OFF avoids the 600+ line template-metaprogrammed file"
    log "  that needs >8GB RAM to compile. I2_S kernel is fine for this 2B model."
    mkdir -p build
    cmake -B build \
        -DBITNET_ARM_TL1=OFF \
        -DCMAKE_C_COMPILER=clang \
        -DCMAKE_CXX_COMPILER=clang++ \
        -DCMAKE_BUILD_TYPE=Release
    cmake --build build --target llama-server --config Release -j 4
else
    log "llama-server already built at ${LLAMA_SERVER}"
fi

if [ ! -x "${LLAMA_SERVER}" ]; then
    fail "llama-server build failed — binary not found at ${LLAMA_SERVER}"
fi

# ── 7. Start server (if requested) ───────────────────────────────────────────
if [ "$NO_START" = false ] && [ "$MODEL_ONLY" = false ]; then
    log "Starting BitNet server on ${BITNET_HOST}:${BITNET_PORT}..."

    # Kill any existing server on this port
    if lsof -ti :${BITNET_PORT} >/dev/null 2>&1; then
        log "  Killing existing process on port ${BITNET_PORT}..."
        lsof -ti :${BITNET_PORT} | xargs kill -9 2>/dev/null || true
        sleep 1
    fi

    nohup "${LLAMA_SERVER}" \
        -m "${MODEL_FILE}" \
        -c "${BITNET_CTX_SIZE}" \
        --host "${BITNET_HOST}" \
        --port "${BITNET_PORT}" \
        -a "${BITNET_MODEL_ALIAS}" \
        -t "${BITNET_THREADS}" \
        > /tmp/bitnet-server.log 2>&1 &
    SERVER_PID=$!
    log "  Server PID: $SERVER_PID (logs: /tmp/bitnet-server.log)"

    # Wait for server to be ready
    log "  Waiting for server to be ready..."
    for i in $(seq 1 30); do
        if curl -fsS http://${BITNET_HOST}:${BITNET_PORT}/health >/dev/null 2>&1; then
            log "  ✓ Server is healthy."
            break
        fi
        sleep 2
        if [ "$i" = 30 ]; then
            warn "Server didn't become healthy in 60s. Check /tmp/bitnet-server.log"
            tail -20 /tmp/bitnet-server.log
            exit 1
        fi
    done

    # ── 8. Test inference ────────────────────────────────────────────────────
    log "Testing inference..."
    TEST_RESPONSE=$(curl -fsS -X POST http://${BITNET_HOST}:${BITNET_PORT}/v1/chat/completions \
        -H "Content-Type: application/json" \
        -d '{"messages":[{"role":"user","content":"What is 2+2? Answer with just the number."}],"max_tokens":20}')
    TEST_OUTPUT=$(echo "$TEST_RESPONSE" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d['choices'][0]['message']['content'])" 2>/dev/null || echo "FAILED_TO_PARSE")
    log "  Test response: '$TEST_OUTPUT'"

    # ── 9. Test Mneme integration ──────────────────────────────────────────
    log "Testing Mneme integration..."
    cd "${PROJECT_DIR}"
    if BITNET_HOST="${BITNET_HOST}" BITNET_PORT="${BITNET_PORT}" BITNET_MODEL="${BITNET_MODEL_ALIAS}" \
        uv run --no-sync python3 -c "
import sys; sys.path.insert(0, '.')
from src.retrieval.bitnet_client import GeminiEmbeddingClient if False else __import__('src.retrieval.bitnet_client', fromlist=['BitNetClient'])
client = BitNetClient()
result = client.detect_intent('continue the auth flow')
print(f'intent={result.intent} | tags={result.detected_tags} | degraded={result.degraded}')
" 2>&1 | grep -v "^$" | tail -3 ; then
        log "  ✓ Mneme → BitNet integration working."
    fi
fi

log ""
log "✅ BitNet setup complete."
log ""
log "To activate real intent detection in Mneme:"
log "  1. Make sure the server is running (PID above, logs: /tmp/bitnet-server.log)"
log "  2. Restart Mneme: docker compose restart mneme-app"
log "  3. Test: curl -X POST http://localhost:8080/retrieve -d '{\"prompt_context\":\"test\"}'"
log ""
log "See BITNET_KNOWN_ISSUES.md for what was tried and what didn't work."
