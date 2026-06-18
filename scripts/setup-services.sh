#!/usr/bin/env bash
# Mneme — All-in-one service manager
#
# Checks, downloads, and starts ALL backend services that Mneme needs:
#   - Neo4j (graph database)
#   - Qdrant (vector search)
#   - BitNet llama-server (local LLM intent detection)
#
# Usage:
#   ./scripts/setup-services.sh              # check + start missing
#   ./scripts/setup-services.sh --install   # install missing Docker images + start
#   ./scripts/setup-services.sh --status    # show status of all services
#   ./scripts/setup-services.sh --stop       # stop all services
#   ./scripts/setup-services.sh --restart    # restart all
#
# Environment (override before running):
#   NEO4J_PASSWORD=mneme-dev-password
#   BITNET_PORT=8081

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

# ── Config ──────────────────────────────────────────────────────────────
NEO4J_PASSWORD="${NEO4J_PASSWORD:-mneme-dev-password}"
NEO4J_HTTP_PORT="${NEO4J_HTTP_PORT:-7474}"
NEO4J_BOLT_PORT="${NEO4J_BOLT_PORT:-7687}"
QDRANT_PORT="${QDRANT_PORT:-6333}"
BITNET_PORT="${BITNET_PORT:-8081}"
BITNET_SCRIPT="${SCRIPT_DIR}/setup-bitnet.sh"

# ── Colour helpers ─────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; RESET='\033[0m'
info()  { echo -e "${BLUE}[service]${RESET}  $*"; }
ok()    { echo -e "${GREEN}[✓]${RESET}   $*"; }
warn()  { echo -e "${YELLOW}[!]${RESET}   $*" >&2; }
fail()  { echo -e "${RED}[✗]${RESET}   $*" >&2; exit 1; }

# ── Service checks ────────────────────────────────────────────────────
_is_docker_running() {
    docker info >/dev/null 2>&1
}

_is_docker_installed() {
    command -v docker >/dev/null 2>&1
}

_is_neo4j_running() {
    curl -sf http://localhost:${NEO4J_HTTP_PORT}/ >/dev/null 2>&1
}

_is_qdrant_running() {
    curl -sf http://localhost:${QDRANT_PORT}/ >/dev/null 2>&1
}

_is_bitnet_running() {
    curl -sf http://localhost:${BITNET_PORT}/health >/dev/null 2>&1
}

_is_neo4j_container_running() {
    docker ps --filter "name=mneme-neo4j" --filter "ancestor=neo4j:5.12" --format '{{.Names}}' 2>/dev/null | grep -q mneme-neo4j
}

_is_qdrant_container_running() {
    docker ps --filter "name=mneme-qdrant" --filter "ancestor=qdrant/qdrant" --format '{{.Names}}' 2>/dev/null | grep -q mneme-qdrant
}

# ── Docker install ─────────────────────────────────────────────────────
_ensure_docker() {
    if ! _is_docker_installed; then
        fail "Docker is not installed. Install Docker Desktop from https://docker.com"
    fi
    if ! _is_docker_running; then
        warn "Docker is not running. Starting Docker Desktop..."
        open -a Docker 2>/dev/null || warn "Could not open Docker Desktop. Start it manually."
        local i=0
        while [ $i -lt 60 ]; do
            if _is_docker_running; then
                ok "Docker started."
                return 0
            fi
            sleep 2; i=$((i+2))
            echo -n "."
        done
        echo ""
        fail "Docker did not start within 120s. Start Docker Desktop manually."
    fi
}

# ── Neo4j ──────────────────────────────────────────────────────────────
start_neo4j() {
    _ensure_docker
    info "Starting Neo4j on bolt://localhost:${NEO4J_BOLT_PORT} ..."

    # Stop existing container if present
    docker stop mneme-neo4j 2>/dev/null || true
    docker rm mneme-neo4j 2>/dev/null || true

    docker run -d \
        --name mneme-neo4j \
        --publish ${NEO4J_BOLT_PORT}:7687 \
        --publish ${NEO4J_HTTP_PORT}:7474 \
        --env NEO4J_AUTH=neo4j/"${NEO4J_PASSWORD}" \
        --env NEO4J_PLUGINS='["apoc"]' \
        neo4j:5.12 \
        >/dev/null

    # Wait for Neo4j to be ready
    info "Waiting for Neo4j (up to 60s)..."
    local i=0
    while [ $i -lt 60 ]; do
        if curl -sf http://localhost:${NEO4J_HTTP_PORT}/ >/dev/null 2>&1; then
            ok "Neo4j ready on http://localhost:${NEO4J_HTTP_PORT} (bolt://localhost:${NEO4J_BOLT_PORT})"
            return 0
        fi
        sleep 2; i=$((i+2))
    done
    docker logs mneme-neo4j | tail -10
    fail "Neo4j did not become ready within 60s."
}

install_neo4j() {
    _ensure_docker
    info "Pulling Neo4j 5.12 image..."
    docker pull neo4j:5.12
    ok "Neo4j image ready."
}

# ── Qdrant ─────────────────────────────────────────────────────────────
start_qdrant() {
    _ensure_docker
    info "Starting Qdrant on http://localhost:${QDRANT_PORT} ..."

    docker stop mneme-qdrant 2>/dev/null || true
    docker rm mneme-qdrant 2>/dev/null || true

    docker run -d \
        --name mneme-qdrant \
        --publish ${QDRANT_PORT}:6333 \
        qdrant/qdrant \
        >/dev/null

    info "Waiting for Qdrant (up to 30s)..."
    local i=0
    while [ $i -lt 30 ]; do
        if curl -sf http://localhost:${QDRANT_PORT}/ >/dev/null 2>&1; then
            ok "Qdrant ready on http://localhost:${QDRANT_PORT}"
            return 0
        fi
        sleep 2; i=$((i+2))
    done
    docker logs mneme-qdrant | tail -10
    fail "Qdrant did not become ready within 60s."
}

install_qdrant() {
    _ensure_docker
    info "Pulling Qdrant image..."
    docker pull qdrant/qdrant
    ok "Qdrant image ready."
}

# ── BitNet ─────────────────────────────────────────────────────────────
start_bitnet() {
    info "Starting BitNet llama-server on localhost:${BITNET_PORT} ..."
    if _is_bitnet_running; then
        ok "BitNet already running on localhost:${BITNET_PORT}"
        return 0
    fi
    if [ ! -x "${PROJECT_DIR}/BitNet/build/bin/llama-server" ]; then
        warn "llama-server not found. Running setup..."
        "${BITNET_SCRIPT}" || fail "BitNet setup failed."
    fi
    "${SCRIPT_DIR}/start-llm-server.sh" --background
    ok "BitNet started."
}

install_bitnet() {
    info "Setting up BitNet..."
    "${BITNET_SCRIPT}" || fail "BitNet setup failed."
}

# ── Status ────────────────────────────────────────────────────────────
show_status() {
    echo ""
    echo "  Service          Status                      Endpoint"
    echo "  ─────────────────────────────────────────────────────────────"

    # Docker
    if _is_docker_installed && _is_docker_running; then
        echo -e "  Docker           ${GREEN}running${RESET}                         docker"
    elif _is_docker_installed; then
        echo -e "  Docker           ${YELLOW}not running${RESET}                    start with: open -a Docker"
    else
        echo -e "  Docker           ${RED}not installed${RESET}                    https://docker.com"
    fi

    # Neo4j
    if _is_neo4j_running; then
        echo -e "  Neo4j            ${GREEN}running${RESET}                         bolt://localhost:${NEO4J_BOLT_PORT}"
    elif _is_docker_installed && _is_docker_running && _is_neo4j_container_running; then
        echo -e "  Neo4j            ${YELLOW}container running, not ready${RESET}   bolt://localhost:${NEO4J_BOLT_PORT}"
    else
        echo -e "  Neo4j            ${RED}not running${RESET}                     ./scripts/setup-services.sh --install"
    fi

    # Qdrant
    if _is_qdrant_running; then
        echo -e "  Qdrant           ${GREEN}running${RESET}                         http://localhost:${QDRANT_PORT}"
    elif _is_docker_installed && _is_docker_running && _is_qdrant_container_running; then
        echo -e "  Qdrant           ${YELLOW}container running, not ready${RESET}   http://localhost:${QDRANT_PORT}"
    else
        echo -e "  Qdrant           ${RED}not running${RESET}                     ./scripts/setup-services.sh --install"
    fi

    # BitNet
    if _is_bitnet_running; then
        echo -e "  BitNet           ${GREEN}running${RESET}                         http://localhost:${BITNET_PORT}"
    else
        echo -e "  BitNet           ${RED}not running${RESET}                     ./scripts/setup-services.sh --install"
    fi

    echo ""
}

# ── Stop ──────────────────────────────────────────────────────────────
stop_all() {
    info "Stopping all services..."
    docker stop mneme-neo4j mneme-qdrant 2>/dev/null && ok "Neo4j, Qdrant stopped" || true
    if _is_bitnet_running; then
        local pid=$(lsof -ti :${BITNET_PORT} 2>/dev/null || true)
        [ -n "${pid}" ] && kill ${pid} 2>/dev/null && ok "BitNet stopped" || true
    fi
    ok "All services stopped."
}

restart_all() {
    stop_all
    sleep 2
    start_neo4j
    start_qdrant
    start_bitnet
}

# ── Install all ───────────────────────────────────────────────────────
install_all() {
    info "Installing all services (pull Docker images + build BitNet)..."
    _ensure_docker
    install_neo4j
    install_qdrant
    install_bitnet
    ok "All images/images ready."
}

# ── Main ──────────────────────────────────────────────────────────────
ACTION="${1:-start}"

case "${ACTION}" in
    --status)  show_status ;;
    --install)
        install_all
        echo ""
        info "All installed. Starting..."
        start_neo4j
        start_qdrant
        start_bitnet
        echo ""
        show_status
        ;;
    --stop)    stop_all ;;
    --restart) restart_all; show_status ;;
    --start|"")
        # Start missing services
        _ensure_docker 2>/dev/null || true
        if ! _is_neo4j_running; then
            if _is_docker_running; then
                if ! docker image ls neo4j:5.12 --format '{{.Repository}}' 2>/dev/null | grep -q neo4j; then
                    install_neo4j
                fi
                start_neo4j
            else
                warn "Docker not available. Neo4j not started."
            fi
        else
            ok "Neo4j already running."
        fi

        if ! _is_qdrant_running; then
            if _is_docker_running; then
                if ! docker image ls qdrant/qdrant --format '{{.Repository}}' 2>/dev/null | grep -q qdrant; then
                    install_qdrant
                fi
                start_qdrant
            else
                warn "Docker not available. Qdrant not started."
            fi
        else
            ok "Qdrant already running."
        fi

        if ! _is_bitnet_running; then
            if [ ! -x "${PROJECT_DIR}/BitNet/build/bin/llama-server" ]; then
                install_bitnet
            fi
            start_bitnet
        else
            ok "BitNet already running."
        fi

        echo ""
        show_status
        ;;
    *)
        echo "Usage: $0 [--status|--install|--stop|--restart]"
        exit 1
        ;;
esac
