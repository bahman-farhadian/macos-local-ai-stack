#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# start-ollama.sh
#
# Starts the Ollama server with the settings this deployment requires. Run this
# instead of a bare `ollama serve`. The required environment is set here, at the
# script level, so it is never left to the user to configure.
#
# Enforced settings:
#   OLLAMA_MAX_LOADED_MODELS=2
#     Two models stay resident at once: the chat model (gpt-oss:20b) and the
#     small memory-extraction model (llama3.2:3b). This keeps memory extraction
#     fast without swapping the chat model out on every message.
#
#   OLLAMA_KEEP_ALIVE=5m
#     The two resident models unload after 5 minutes idle, releasing unified
#     memory. This is correct for a 24 GB machine.
#
#   The vision model (llama3.2-vision) is NOT meant to stay resident. It is
#     loaded on demand when an image arrives and unloaded immediately after,
#     via the vision filter's keep_alive=0 request parameter (a per-request
#     value that overrides OLLAMA_KEEP_ALIVE). With MAX_LOADED_MODELS=2, Ollama
#     evicts the least-recently-used resident model to make room for the vision
#     model, then reloads it afterwards. If memory is momentarily insufficient,
#     Ollama queues the request until a model unloads — it does not crash.
#
# Behaviour:
#   - Binds Ollama to 127.0.0.1:11434 (localhost only).
#   - Writes a log to <repo>/data/ollama.log and a PID file.
#   - Safe to call repeatedly — exits cleanly if Ollama is already running.
#
# Usage:
#   ./start-ollama.sh
# ------------------------------------------------------------------------------

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${BASE_DIR}/data"
LOG_FILE="${DATA_DIR}/ollama.log"
PID_FILE="${DATA_DIR}/ollama.pid"

OLLAMA_HOST_ADDR="127.0.0.1:11434"

log() { printf '[ollama] %s\n' "$*"; }
die() { printf '[ollama] ERROR: %s\n' "$*" >&2; exit 1; }

# --- Require the ollama binary ---
if ! command -v ollama >/dev/null 2>&1; then
    die "ollama not found on PATH. Install it from https://ollama.com first."
fi

mkdir -p "${DATA_DIR}"

# --- If Ollama is already serving, do nothing ---
if curl -sf "http://${OLLAMA_HOST_ADDR}/api/tags" -o /dev/null 2>/dev/null; then
    log "Ollama is already running at ${OLLAMA_HOST_ADDR}."
    exit 0
fi

# --- If our PID file points at a live process, do nothing ---
if [ -f "${PID_FILE}" ]; then
    EXISTING_PID=$(cat "${PID_FILE}")
    if kill -0 "${EXISTING_PID}" 2>/dev/null; then
        log "Ollama process already running (PID ${EXISTING_PID})."
        exit 0
    else
        rm -f "${PID_FILE}"
    fi
fi

# --- Required Ollama settings (enforced here, not left to the user) ---
export OLLAMA_HOST="${OLLAMA_HOST_ADDR}"
export OLLAMA_MAX_LOADED_MODELS=2
export OLLAMA_KEEP_ALIVE=5m

log "Starting Ollama ..."
log "  Host                     : ${OLLAMA_HOST_ADDR}"
log "  OLLAMA_MAX_LOADED_MODELS : 2  (chat + memory model resident)"
log "  OLLAMA_KEEP_ALIVE        : 5m"
log "  Log file                 : ${LOG_FILE}"

nohup ollama serve >> "${LOG_FILE}" 2>&1 &
SERVER_PID=$!
echo "${SERVER_PID}" > "${PID_FILE}"

# --- Wait for the API to come up ---
for _ in $(seq 1 30); do
    if curl -sf "http://${OLLAMA_HOST_ADDR}/api/tags" -o /dev/null 2>/dev/null; then
        log "Ollama is ready (PID ${SERVER_PID})."
        exit 0
    fi
    if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
        rm -f "${PID_FILE}"
        die "Ollama exited immediately. Inspect the log: tail -50 ${LOG_FILE}"
    fi
    sleep 1
done

die "Ollama did not become ready within 30s. Inspect the log: tail -50 ${LOG_FILE}"
