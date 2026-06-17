#!/usr/bin/env bash
# Mneme — Start the local LLM server (BitNet llama-server, OpenAI-compatible)
#
# Starts llama-server (built by BitNet) hosting the Falcon3-1B-Instruct model.
# Exposes an OpenAI-compatible HTTP API on $BITNET_HOST:$BITNET_PORT so the
# Python client can hit /v1/chat/completions with the same httpx pattern as
# every other LLM provider.
#
# Usage:
#   ./scripts/start-llm-server.sh                    # start in foreground
#   ./scripts/start-llm-server.sh --background       # start, print URL, return
#   BITNET_PORT=9090 ./scripts/start-llm-server.sh   # custom port
#
# Environment variables (all optional — sensible defaults):
#   BITNET_HOST          Bind address         (default: 0.0.0.0)
#   BITNET_PORT          Listen port          (default: 8081)
#   BITNET_MODEL_PATH    GGUF model file      (default: auto-detect f32, fallback i2_s)
#   BITNET_CTX_SIZE      Context window       (default: 2048)
#   BITNET_THREADS       CPU threads          (default: auto-detect, capped at 8)
#   BITNET_ALIAS         Model alias for API  (default: falcon3-1b-instruct)
#   BITNET_MODELS_DIR    Where to look for models (default: ./BitNet/models/...)
#
# Exit codes:
#   0  server started (or is already running)
#   1  no model file found
#   2  llama-server binary missing
#   3  port already in use / server failed to start

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Configuration from env ───────────────────────────────────────────────────
BITNET_HOST="${BITNET_HOST:-0.0.0.0}"
BITNET_PORT="${BITNET_PORT:-8081}"
BITNET_CTX_SIZE="${BITNET_CTX_SIZE:-2048}"
BITNET_ALIAS="${BITNET_ALIAS:-bitnet-b1.58-2b-4t}"
BITNET_MODELS_DIR="${BITNET_MODELS_DIR:-${PROJECT_DIR}/BitNet/models/BitNet-b1.58-2B-4T-gguf}"
BITNET_BIN_DIR="${BITNET_BIN_DIR:-${PROJECT_DIR}/BitNet/build/bin}"

# Auto-detect thread count: nproc if available, else 4
if command -v nproc >/dev/null 2>&1; then
    DETECTED_THREADS=$(nproc)
else
    DETECTED_THREADS=$(sysctl -n hw.ncpu 2>/dev/null || echo 4)
fi
# Cap at 8 — BitNet's i2_s kernels are most stable there
BITNET_THREADS="${BITNET_THREADS:-$([ "$DETECTED_THREADS" -gt 8 ] && echo 8 || echo "$DETECTED_THREADS")}"

# ── Flags ────────────────────────────────────────────────────────────────────
BACKGROUND=false
for arg in "$@"; do
    case "$arg" in
        --background) BACKGROUND=true ;;
        --foreground) BACKGROUND=false ;;
        -h|--help)
            sed -n '2,30p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *) printf "Unknown arg: %s\n" "$arg" >&2; exit 1 ;;
    esac
done

# ── Helpers ──────────────────────────────────────────────────────────────────
log()  { printf "\033[1;34m[llm-server]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[llm-server]\033[0m %s\n" "$*" >&2; }
fail() { printf "\033[1;31m[llm-server]\033[0m %s\n" "$*" >&2; exit "${2:-1}"; }

# ── 1. Validate binary ───────────────────────────────────────────────────────
LLAMA_SERVER="${BITNET_BIN_DIR}/llama-server"
if [ ! -x "${LLAMA_SERVER}" ]; then
    fail "llama-server not found at ${LLAMA_SERVER}
  Build it first:
    cd BitNet && mkdir -p build && cd build && cmake .. && make -j" 2
fi
log "Using binary: ${LLAMA_SERVER}"

# ── 2. Resolve model file ────────────────────────────────────────────────────
# Order of preference:
#   1. $BITNET_MODEL_PATH if explicitly set
#   2. i2_s GGUF (1.58-bit, ~1.2GB) — BitNet-b1.58-2B-4T verified to produce coherent output
#   3. tl1 GGUF (1.58-bit, ~2.2GB) — requires BITNET_ARM_TL1=ON build (not on 8GB RAM)
if [ -n "${BITNET_MODEL_PATH:-}" ]; then
    if [ ! -f "${BITNET_MODEL_PATH}" ]; then
        fail "BITNET_MODEL_PATH='${BITNET_MODEL_PATH}' does not exist." 1
    fi
    MODEL_PATH="${BITNET_MODEL_PATH}"
elif [ -f "${BITNET_MODELS_DIR}/ggml-model-i2_s.gguf" ]; then
    MODEL_PATH="${BITNET_MODELS_DIR}/ggml-model-i2_s.gguf"
elif [ -f "${BITNET_MODELS_DIR}/ggml-model-tl1.gguf" ]; then
    MODEL_PATH="${BITNET_MODELS_DIR}/ggml-model-tl1.gguf"
elif [ -f "${BITNET_MODELS_DIR}/ggml-model-f32.gguf" ]; then
    MODEL_PATH="${BITNET_MODELS_DIR}/ggml-model-f32.gguf"
else
    fail "No model found in ${BITNET_MODELS_DIR}.
  Expected one of:
    - ggml-model-i2_s.gguf  (1.58-bit, ~1.2GB, BitNet-b1.58-2B-4T)
    - ggml-model-tl1.gguf   (1.58-bit, ~2.2GB, requires BITNET_ARM_TL1 build)
    - ggml-model-f32.gguf   (fp32, ~6.6GB)

  Run:  ./scripts/setup-bitnet.sh" 1
fi
log "Using model:  ${MODEL_PATH}"

# ── 3. Check if port is free ─────────────────────────────────────────────────
if command -v lsof >/dev/null 2>&1; then
    if lsof -i :"$BITNET_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
        # If something is already on the port, check whether it's a llama-server
        EXISTING_PID=$(lsof -i :"$BITNET_PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)
        if [ -n "${EXISTING_PID}" ]; then
            warn "Port ${BITNET_PORT} already in use (pid ${EXISTING_PID})."
            warn "Assuming a healthy llama-server is already running."
            # Verify with health check
            if curl -fsS "http://${BITNET_HOST}:${BITNET_PORT}/health" >/dev/null 2>&1; then
                log "Existing server responded healthy."
                log "URL:  http://${BITNET_HOST}:${BITNET_PORT}"
                log "API:  http://${BITNET_HOST}:${BITNET_PORT}/v1"
                exit 0
            fi
            fail "Port ${BITNET_PORT} is in use but /health did not respond. Refusing to start." 3
        fi
    fi
fi

# ── 4. Launch server ─────────────────────────────────────────────────────────
LOG_FILE="${PROJECT_DIR}/logs/llama-server.log"
mkdir -p "$(dirname "${LOG_FILE}")"

CMD=(
    "${LLAMA_SERVER}"
    --model             "${MODEL_PATH}"
    --host              "${BITNET_HOST}"
    --port              "${BITNET_PORT}"
    --ctx-size          "${BITNET_CTX_SIZE}"
    --threads           "${BITNET_THREADS}"
    --alias             "${BITNET_ALIAS}"
    --log-disable
)

log "Command: ${CMD[*]}"

if [ "${BACKGROUND}" = true ]; then
    log "Starting in background. Logs: ${LOG_FILE}"
    nohup "${CMD[@]}" >"${LOG_FILE}" 2>&1 &
    SERVER_PID=$!
    log "Server PID: ${SERVER_PID}"
else
    log "Starting in foreground. Ctrl-C to stop."
    "${CMD[@]}"
    exit $?
fi

# ── 5. Wait for health check (background mode) ────────────────────────────────
HEALTH_URL="http://127.0.0.1:${BITNET_PORT}/health"
DEADLINE=$((SECONDS + 120))  # 2 min — f32 model load is slow

log "Waiting for ${HEALTH_URL} (up to 120s)..."
while [ $SECONDS -lt $DEADLINE ]; do
    if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
        fail "Server process died on startup. Last 30 lines of log:
$(tail -n 30 "${LOG_FILE}" 2>/dev/null || echo 'no log file')" 3
    fi
    if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
        log ""
        log "============================================================"
        log "  LLM server ready"
        log "  URL:  http://${BITNET_HOST}:${BITNET_PORT}"
        log "  API:  http://${BITNET_HOST}:${BITNET_PORT}/v1"
        log "  Model: ${BITNET_ALIAS} (${MODEL_PATH##*/})"
        log "  PID:  ${SERVER_PID}"
        log "  Logs: ${LOG_FILE}"
        log "============================================================"
        log ""
        log "Test with:"
        log "  curl http://127.0.0.1:${BITNET_PORT}/v1/chat/completions \\"
        log "    -H 'Content-Type: application/json' \\"
        log "    -d '{\"model\":\"${BITNET_ALIAS}\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'"
        exit 0
    fi
    sleep 1
done

fail "Server did not become healthy within 120s. Last 30 lines of log:
$(tail -n 30 "${LOG_FILE}" 2>/dev/null || echo 'no log file')" 3
