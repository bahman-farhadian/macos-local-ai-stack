#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# stop-ollama.sh
#
# Stops the Ollama server started by start-ollama.sh.
#
# Note: this only stops an Ollama instance launched via start-ollama.sh (tracked
# by data/ollama.pid). If Ollama was started by the macOS app instead, quit it
# from the menu bar.
#
# Usage:
#   ./stop-ollama.sh
# ------------------------------------------------------------------------------

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${BASE_DIR}/data"
PID_FILE="${DATA_DIR}/ollama.pid"

SHUTDOWN_TIMEOUT=10

log() { printf '[ollama] %s\n' "$*"; }

if [ ! -f "${PID_FILE}" ]; then
    log "No PID file at ${PID_FILE}. Ollama was not started by start-ollama.sh."
    log "If the macOS Ollama app is running, quit it from the menu bar."
    exit 0
fi

PID=$(cat "${PID_FILE}")

if ! kill -0 "${PID}" 2>/dev/null; then
    log "Process ${PID} is not running. Removing stale PID file."
    rm -f "${PID_FILE}"
    exit 0
fi

log "Sending SIGTERM to Ollama (PID ${PID}) ..."
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
log "Ollama stopped."
