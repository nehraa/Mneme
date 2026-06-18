#!/usr/bin/env bash
# Mneme — One-shot setup script
#
# What this does:
#   1. Checks for required tools (docker, docker compose, uv)
#   2. Creates .env from .env.example if it doesn't exist
#   3. Pulls all Docker images
#   4. Builds the Mneme Docker image
#   5. Starts the stack
#   6. Waits for services to be healthy
#   7. Runs a smoke test against the API
#
# Usage:
#   ./scripts/setup.sh                # full setup + start
#   ./scripts/setup.sh --no-start     # setup without starting
#   ./scripts/setup.sh --pull-only   # just pull images, don't build/start
#
# Idempotent — safe to run multiple times.

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

NO_START=false
PULL_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --no-start)   NO_START=true ;;
        --pull-only)  PULL_ONLY=true; NO_START=true ;;
        *) echo "Unknown arg: $arg"; exit 1 ;;
    esac
done

# ── Helpers ──────────────────────────────────────────────────────────────────
log()  { printf "\033[1;34m[setup]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m %s\n" "$*" >&2; }
fail() { printf "\033[1;31m[fail]\033[0m %s\n" "$*" >&2; exit 1; }

# ── 1. Tool checks ───────────────────────────────────────────────────────────
log "Checking for required tools..."

command -v docker       >/dev/null 2>&1 || fail "docker not found. Install: https://docs.docker.com/get-docker/"
command -v docker compose >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1 \
    || fail "docker compose not found. Install: https://docs.docker.com/compose/install/"
command -v uv          >/dev/null 2>&1 || fail "uv not found. Install: https://github.com/astral-sh/uv"

DOCKER_COMPOSE="docker compose"
command -v docker-compose >/dev/null 2>&1 && DOCKER_COMPOSE="docker-compose"

DOCKER_VERSION=$(docker --version | awk '{print $3}' | tr -d ',')
log "Docker: $DOCKER_VERSION, Compose: $($DOCKER_COMPOSE version --short 2>/dev/null || echo unknown)"

# ── 2. .env setup ────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    log "Creating .env from .env.example..."
    cp .env.example .env
    warn ".env created with default development values."
    warn "  → Edit .env to add real API keys (Anthropic, Gemini, MiniMax) for production."
else
    log ".env already exists — skipping copy."
fi

# ── 3. Pull Docker images ────────────────────────────────────────────────────
log "Pulling Docker images (this may take a few minutes)..."
$DOCKER_COMPOSE pull

# ── 4. Build Mneme image ────────────────────────────────────────────────────
if [ "$PULL_ONLY" = false ]; then
    log "Building Mneme Docker image..."
    $DOCKER_COMPOSE build
fi

# ── 5. Start the stack ───────────────────────────────────────────────────────
if [ "$NO_START" = false ]; then
    log "Starting the stack..."
    $DOCKER_COMPOSE up -d

    # ── 6. Wait for services ────────────────────────────────────────────────
    log "Waiting for services to become healthy..."

    # Neo4j
    log "  Waiting for Neo4j (up to 120s)..."
    for i in $(seq 1 60); do
        if docker exec mneme-neo4j wget --spider -q http://localhost:7474 2>/dev/null; then
            log "  Neo4j is healthy."
            break
        fi
        sleep 2
        if [ "$i" = 60 ]; then fail "Neo4j did not become healthy in 120s."; fi
    done

    # Qdrant
    log "  Waiting for Qdrant (up to 60s)..."
    for i in $(seq 1 30); do
        if docker exec mneme-qdrant wget --spider -q http://localhost:6333/healthz 2>/dev/null; then
            log "  Qdrant is healthy."
            break
        fi
        sleep 2
        if [ "$i" = 30 ]; then fail "Qdrant did not become healthy in 60s."; fi
    done

    # Mneme
    log "  Waiting for Mneme (up to 60s)..."
    for i in $(seq 1 30); do
        if curl -fsS http://localhost:8080/health >/dev/null 2>&1; then
            log "  Mneme is healthy."
            break
        fi
        sleep 2
        if [ "$i" = 30 ]; then fail "Mneme did not become healthy in 60s."; fi
    done

    # ── 7. Smoke test ─────────────────────────────────────────────────────
    log "Running smoke test..."
    HEALTH=$(curl -fsS http://localhost:8080/health)
    echo "  GET /health: $HEALTH"

    MEMORY=$(curl -fsS -X POST http://localhost:8080/memories \
        -H "Content-Type: application/json" \
        -d '{"content":"test","session_id":"setup-smoke-test","tags":["tool=auth"]}')
    echo "  POST /memories: $MEMORY" | head -c 200; echo

    RETRIEVE=$(curl -fsS -X POST http://localhost:8080/retrieve \
        -H "Content-Type: application/json" \
        -d '{"prompt_context":"continue the auth flow"}')
    echo "  POST /retrieve: $RETRIEVE" | head -c 200; echo

    log ""
    log "✅ Setup complete. Stack is running."
    log ""
    log "Next steps:"
    log "  - ./scripts/verify.sh           # run health checks"
    log "  - docker compose logs -f        # follow logs"
    log "  - docker compose down           # stop the stack"
    log "  - curl http://localhost:8080/docs  # OpenAPI docs (Swagger UI)"
else
    log ""
    log "✅ Setup complete (images pulled, build done)."
    if [ "$PULL_ONLY" = true ]; then
        log "  Skipped build and start (--pull-only)."
    else
        log "  Skipped start (--no-start). Run: $DOCKER_COMPOSE up -d"
    fi
fi
