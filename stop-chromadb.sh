#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# stop-chromadb.sh
#
# Stops the ChromaDB background process started by start-chromadb.sh.
#
# Shutdown safety: ChromaDB writes every memory vector to disk via SQLite WAL
# before returning a response. No data is held only in RAM awaiting a shutdown
# flush. SIGTERM gives SQLite time to close file handles and checkpoint the WAL
# cleanly, which is what this script does.
#
# Invoked automatically by stop-open-webui.sh *after* Open WebUI has stopped.
# Ordering is intentional: Open WebUI is the client of ChromaDB and must stop
# first so no in-flight memory write fails.
#
# Usage:
#   ./stop-chromadb.sh
# ------------------------------------------------------------------------------

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${BASE_DIR}/data"
PID_FILE="${DATA_DIR}/chromadb.pid"

SHUTDOWN_TIMEOUT=10

log() { printf '[chromadb] %s\n' "$*"; }

if [ ! -f "${PID_FILE}" ]; then
    log "No PID file found at ${PID_FILE}. ChromaDB does not appear to be running."
    exit 0
fi

PID=$(cat "${PID_FILE}")

if ! kill -0 "${PID}" 2>/dev/null; then
    log "Process ${PID} is not running. Removing stale PID file."
    rm -f "${PID_FILE}"
    exit 0
fi

log "Sending SIGTERM to ChromaDB (PID ${PID}) — waiting for clean SQLite checkpoint ..."
kill -TERM "${PID}"

ELAPSED=0
while kill -0 "${PID}" 2>/dev/null; do
    if [ "${ELAPSED}" -ge "${SHUTDOWN_TIMEOUT}" ]; then
        log "Process did not stop within ${SHUTDOWN_TIMEOUT}s. Sending SIGKILL ..."
        kill -KILL "${PID}" 2>/dev/null || true
        break
    fi
    sleep 1
    ELAPSED=$((ELAPSED + 1))
done

rm -f "${PID_FILE}"
log "ChromaDB stopped. All memory vectors safely persisted on disk."
