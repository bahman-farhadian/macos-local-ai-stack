#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# stop-open-webui.sh
#
# Stops the Open WebUI background process, then stops ChromaDB.
#
# Shutdown order matters for memory safety:
#   1. Open WebUI is stopped first. It is the client of ChromaDB; any in-flight
#      memory write from the Adaptive Memory plugin must complete (or be
#      rejected cleanly) before the vector store disappears.
#   2. ChromaDB is stopped second. By then no new write requests can arrive.
#      ChromaDB receives SIGTERM and closes its SQLite handles cleanly,
#      checkpointing the WAL. All previously written vectors are already on disk.
#
# No memory data is held only in RAM awaiting this sequence. ChromaDB persists
# every write to its WAL before responding. This sequence ensures clean
# file-handle closure, not data flushing.
#
# Usage:
#   ./stop-open-webui.sh
# ------------------------------------------------------------------------------

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${BASE_DIR}/data"
PID_FILE="${DATA_DIR}/webui.pid"

SHUTDOWN_TIMEOUT=10

log() { printf '[stop] %s\n' "$*"; }

# --- Stop the HTTPS proxy first (the browser-facing entry point) ---
PROXY_PID_FILE="${DATA_DIR}/ssl-proxy.pid"
if [ -f "${PROXY_PID_FILE}" ]; then
    PROXY_PID=$(cat "${PROXY_PID_FILE}")
    if kill -0 "${PROXY_PID}" 2>/dev/null; then
        kill "${PROXY_PID}" 2>/dev/null || true
        log "HTTPS proxy stopped (PID ${PROXY_PID})."
    fi
    rm -f "${PROXY_PID_FILE}"
fi

# --- Stop Open WebUI ---
if [ ! -f "${PID_FILE}" ]; then
    log "Open WebUI is not running (no PID file)."
else
    PID=$(cat "${PID_FILE}")
    if ! kill -0 "${PID}" 2>/dev/null; then
        log "Open WebUI already stopped (removing stale PID file)."
        rm -f "${PID_FILE}"
    else
        log "Stopping Open WebUI (PID ${PID}) ..."
        kill -TERM "${PID}"
        ELAPSED=0
        while kill -0 "${PID}" 2>/dev/null; do
            if [ "${ELAPSED}" -ge "${SHUTDOWN_TIMEOUT}" ]; then
                log "Did not stop within ${SHUTDOWN_TIMEOUT}s — forcing (SIGKILL)."
                kill -KILL "${PID}" 2>/dev/null || true
                break
            fi
            sleep 1
            ELAPSED=$((ELAPSED + 1))
        done
        rm -f "${PID_FILE}"
        log "Open WebUI stopped."
    fi
fi

# --- Stop ChromaDB (after Open WebUI, its client, is down) ---
if [ -x "${BASE_DIR}/stop-chromadb.sh" ]; then
    "${BASE_DIR}/stop-chromadb.sh"
else
    log "WARNING: stop-chromadb.sh not found — ChromaDB (if running) was left up."
fi

log "All services stopped."

