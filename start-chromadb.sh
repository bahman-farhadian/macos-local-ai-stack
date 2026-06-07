#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# start-chromadb.sh
#
# Starts a local ChromaDB server as a background process.
#
# ChromaDB is the vector store backing the long-term (T2) memory layer of this
# deployment. Open WebUI's memory subsystem stores memory vectors here and
# answers semantic-similarity queries from it before each response.
#
# Persistence model: ChromaDB writes every change to a SQLite WAL (write-ahead
# log) on disk before returning an HTTP 200. There is no in-RAM buffer that
# needs flushing. A clean SIGTERM shutdown (which this script produces) lets
# SQLite close its file handles and checkpoint the WAL. A hard crash leaves the
# WAL un-checkpointed but not corrupt — ChromaDB replays it on the next start.
#
# Behaviour:
#   - Binds exclusively to 127.0.0.1 — not accessible from any other host.
#   - Serves over HTTP only. This is intentional for local use.
#   - Persists all data to <repo>/data/chromadb.
#   - Disables ChromaDB's anonymous telemetry.
#   - Safe to call repeatedly — exits cleanly if already running.
#
# Invoked automatically by start-open-webui.sh before Open WebUI starts.
#
# Usage:
#   ./start-chromadb.sh
# ------------------------------------------------------------------------------

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${BASE_DIR}/.venv"
DATA_DIR="${BASE_DIR}/data"
CHROMA_DIR="${DATA_DIR}/chromadb"
LOG_FILE="${DATA_DIR}/chromadb.log"
PID_FILE="${DATA_DIR}/chromadb.pid"

CHROMA_HOST="127.0.0.1"
CHROMA_PORT="8000"          # ChromaDB default port

log() { printf '[chromadb] %s\n' "$*"; }
die() { printf '[chromadb] ERROR: %s\n' "$*" >&2; exit 1; }

if [ ! -x "${VENV_DIR}/bin/chroma" ]; then
    die "chroma CLI not found at ${VENV_DIR}/bin/chroma. Run: uv pip install -r ${BASE_DIR}/requirements.txt --python ${VENV_DIR}/bin/python"
fi

if [ -f "${PID_FILE}" ]; then
    EXISTING_PID=$(cat "${PID_FILE}")
    if kill -0 "${EXISTING_PID}" 2>/dev/null; then
        log "ChromaDB is already running (PID ${EXISTING_PID})."
        log "Endpoint: http://${CHROMA_HOST}:${CHROMA_PORT}"
        exit 0
    else
        log "Stale PID file found (PID ${EXISTING_PID} no longer running). Removing."
        rm -f "${PID_FILE}"
    fi
fi

mkdir -p "${CHROMA_DIR}"
export ANONYMIZED_TELEMETRY=False

log "Starting ChromaDB ..."
log "  Bind address : ${CHROMA_HOST}:${CHROMA_PORT}  (localhost only, HTTP)"
log "  Data dir     : ${CHROMA_DIR}"
log "  Log file     : ${LOG_FILE}"

nohup "${VENV_DIR}/bin/chroma" run \
    --host "${CHROMA_HOST}" \
    --port "${CHROMA_PORT}" \
    --path "${CHROMA_DIR}" \
    >> "${LOG_FILE}" 2>&1 &

SERVER_PID=$!
echo "${SERVER_PID}" > "${PID_FILE}"

sleep 2
if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    rm -f "${PID_FILE}"
    die "ChromaDB exited immediately. Inspect the log: tail -50 ${LOG_FILE}"
fi

log "ChromaDB process started (PID ${SERVER_PID})."
