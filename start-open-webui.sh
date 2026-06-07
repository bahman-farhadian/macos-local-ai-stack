#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# start-open-webui.sh
#
# Starts the full local AI stack and serves it over HTTPS only.
#
# Architecture:
#   - Open WebUI runs on http://127.0.0.1:8080 (loopback, INTERNAL only).
#   - ssl-proxy.py terminates TLS on https://127.0.0.1:8443 and forwards to 8080.
#   - The browser ONLY ever uses https://<domain>:8443. Port 8080 is internal
#     and must not be used by browsers — secure cookies and other secure-context
#     APIs require an HTTPS origin.
#
# This script requires a TLS certificate. Run ./setup-ssl.sh once before the
# first start. It needs NO sudo (setup-ssl.sh handles the one sudo step).
#
# What it does, in order:
#   1. Loads .env (admin credentials, optional settings).
#   2. Verifies the TLS certificate exists (fails fast with instructions if not).
#   3. Starts Ollama (if needed), ChromaDB.
#   4. Starts Open WebUI on the internal HTTP port.
#   5. Starts the HTTPS proxy on port 8443.
#   6. Launches the provisioner in the background (auto-configures everything).
#
# Safe to call repeatedly. Run ./stop-open-webui.sh first if a previous run
# left processes behind.
# ------------------------------------------------------------------------------

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${BASE_DIR}/.env"

# --- Load .env if present ---
# .env must define WEBUI_ADMIN_EMAIL and WEBUI_ADMIN_PASSWORD.
# Copy .env.example to .env, fill in credentials, then run this script.
if [ -f "${ENV_FILE}" ]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
else
    printf '[start] WARNING: %s not found.\n' "${ENV_FILE}"
    printf '[start]          Copy .env.example to .env and set your admin\n'
    printf '[start]          credentials. Auto-configuration will be skipped.\n'
fi
VENV_DIR="${BASE_DIR}/.venv"
DATA_DIR="${BASE_DIR}/data"
LOG_FILE="${DATA_DIR}/webui.log"
PID_FILE="${DATA_DIR}/webui.pid"
SECRET_KEY_FILE="${DATA_DIR}/.webui_secret_key"

OLLAMA_BASE_URL="http://127.0.0.1:11434"
BIND_HOST="127.0.0.1"
PORT="8080"                 # Open WebUI default port

CHROMA_HOST="127.0.0.1"
CHROMA_PORT="8000"          # ChromaDB default port

# Define helpers before first use
log() { printf '[start] %s\n' "$*"; }
die() { printf '[start] ERROR: %s\n' "$*" >&2; exit 1; }

# --- TLS certificate is required (HTTPS-only access) ---
# The stack is served over HTTPS only. The certificate is created once by
# ./setup-ssl.sh. This script needs no sudo.
SSL_DIR="${BASE_DIR}/ssl"
CERT_FILE="${SSL_DIR}/cert.pem"
KEY_FILE="${SSL_DIR}/key.pem"
PROXY_PORT="8443"

if [ ! -f "${CERT_FILE}" ] || [ ! -f "${KEY_FILE}" ]; then
    printf '[start] ERROR: TLS certificate not found.\n' >&2
    printf '[start]\n' >&2
    printf '[start] This stack is served over HTTPS only. Set it up once:\n' >&2
    printf '[start]   1. brew install mkcert ffmpeg\n' >&2
    printf '[start]   2. ./setup-ssl.sh\n' >&2
    printf '[start]\n' >&2
    printf '[start] Then run ./start-open-webui.sh again.\n' >&2
    exit 1
fi

# --- Local DNS check (informational only — no sudo here) ---
# setup-ssl.sh adds the /etc/hosts entry for LOCAL_DOMAIN. If it is missing,
# warn the user but continue (127.0.0.1 access still works).
if [ -n "${LOCAL_DOMAIN:-}" ]; then
    if ! grep -qE "^127\.0\.0\.1[[:space:]]+${LOCAL_DOMAIN}([[:space:]]|$)" /etc/hosts 2>/dev/null; then
        log "NOTE: ${LOCAL_DOMAIN} is not in /etc/hosts yet."
        log "      Run ./setup-ssl.sh to add it, or use https://127.0.0.1:${PROXY_PORT}."
    fi
fi

# --- Pre-flight checks ---
log "Checking Ollama at ${OLLAMA_BASE_URL} ..."
if ! curl -sf "${OLLAMA_BASE_URL}/api/tags" -o /dev/null; then
    # Ollama is not running. Start it via our launcher so the required settings
    # (OLLAMA_MAX_LOADED_MODELS=2, keep-alive) are enforced at the script level.
    if [ -x "${BASE_DIR}/start-ollama.sh" ]; then
        log "Ollama not reachable — starting it via start-ollama.sh ..."
        "${BASE_DIR}/start-ollama.sh"
    else
        die "Ollama is not reachable and start-ollama.sh is missing. Run: ${BASE_DIR}/start-ollama.sh"
    fi
fi
log "Ollama is up."

[ -d "${VENV_DIR}" ] || die "Virtual environment not found at ${VENV_DIR}. See README.md."
[ -f "${VENV_DIR}/bin/open-webui" ] || die "open-webui binary missing. Run: uv pip install -r ${BASE_DIR}/requirements.txt --python ${VENV_DIR}/bin/python"
[ -x "${BASE_DIR}/start-chromadb.sh" ] || die "start-chromadb.sh not found or not executable in ${BASE_DIR}."

# --- Guard against duplicate start ---
if [ -f "${PID_FILE}" ]; then
    EXISTING_PID=$(cat "${PID_FILE}")
    if kill -0 "${EXISTING_PID}" 2>/dev/null; then
        log "Open WebUI is already running (PID ${EXISTING_PID})."
        log "Access: https://${LOCAL_DOMAIN:-127.0.0.1}:${PROXY_PORT}"
        log "To restart, run ./stop-open-webui.sh first."
        exit 0
    else
        log "Found a stale PID file (process ${EXISTING_PID} is gone). Cleaning up."
        rm -f "${PID_FILE}"
    fi
fi

# --- Reset per-run logs so each start shows only its own output ---
# These are status logs for a single run; keeping them fresh avoids confusion
# from stale lines left by previous starts.
mkdir -p "${DATA_DIR}"
: > "${LOG_FILE}"                    2>/dev/null || true
: > "${DATA_DIR}/provision.log"      2>/dev/null || true
: > "${DATA_DIR}/ssl-proxy.log"      2>/dev/null || true

# --- Start ChromaDB and wait for readiness ---
log "Starting ChromaDB ..."
"${BASE_DIR}/start-chromadb.sh"

log "Waiting for ChromaDB to become ready on ${CHROMA_HOST}:${CHROMA_PORT} ..."
CHROMA_READY=0
for _ in $(seq 1 30); do
    if curl -sf "http://${CHROMA_HOST}:${CHROMA_PORT}/api/v2/heartbeat" -o /dev/null 2>/dev/null \
        || curl -sf "http://${CHROMA_HOST}:${CHROMA_PORT}/api/v1/heartbeat" -o /dev/null 2>/dev/null; then
        CHROMA_READY=1
        break
    fi
    sleep 1
done
if [ "${CHROMA_READY}" -eq 1 ]; then
    log "ChromaDB is ready."
else
    log "WARNING: ChromaDB heartbeat not confirmed after 30s — continuing anyway."
    log "         If memory features misbehave, check: tail -50 ${DATA_DIR}/chromadb.log"
fi

# --- Environment ---
mkdir -p "${DATA_DIR}"

# Ollama
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL}"

# Default model shown in the model selector on new chats.
# Change this to any model name from `ollama list`.
export DEFAULT_MODELS="${DEFAULT_MODELS:-gpt-oss:20b}"

# Disable unused background schedulers to speed up lifespan startup.
# These default to True in Open WebUI 0.9.5 and cause the asyncio scheduler
# task to make DB queries on every tick, delaying the lifespan yield.
export ENABLE_AUTOMATIONS=false
export ENABLE_CALENDAR=false

# Web search — DuckDuckGo by default (no API key required).
# To use Google PSE instead, add GOOGLE_PSE_API_KEY and GOOGLE_PSE_ENGINE_ID
# to .env — the block below picks them up automatically.
# URL fetch (paste any URL into chat) works with no extra configuration.
export ENABLE_RAG_WEB_SEARCH=true
export RAG_WEB_SEARCH_RESULT_COUNT=5
export RAG_WEB_SEARCH_CONCURRENT_REQUESTS=5

# Use Google PSE when keys are present; fall back to DuckDuckGo.
if [ -n "${GOOGLE_PSE_API_KEY:-}" ] && [ -n "${GOOGLE_PSE_ENGINE_ID:-}" ]; then
    export RAG_WEB_SEARCH_ENGINE=google_pse
    export GOOGLE_PSE_API_KEY="${GOOGLE_PSE_API_KEY}"
    export GOOGLE_PSE_ENGINE_ID="${GOOGLE_PSE_ENGINE_ID}"
    log "Web search engine : Google PSE"
else
    export RAG_WEB_SEARCH_ENGINE=duckduckgo
    log "Web search engine : DuckDuckGo (set GOOGLE_PSE_API_KEY + GOOGLE_PSE_ENGINE_ID to switch to Google)"
fi

# OLLAMA_KEEP_ALIVE is intentionally NOT set here. The Ollama default of
# 5 minutes is correct for this machine's 24 GB unified memory. See README.md
# ("Model keep-alive and memory") for the full explanation.

# Storage
export DATA_DIR="${DATA_DIR}"

# Vector store (T2 long-term memory) — point Open WebUI at local ChromaDB
export VECTOR_DB="chroma"
export CHROMA_HTTP_HOST="${CHROMA_HOST}"
export CHROMA_HTTP_PORT="${CHROMA_PORT}"

# Public address — Open WebUI uses this for generated links and as the
# canonical origin. The browser reaches the stack here (through the proxy).
export WEBUI_URL="https://${LOCAL_DOMAIN:-127.0.0.1}:${PROXY_PORT}"

# CORS — Open WebUI splits this list by SEMICOLON, not comma.
# Only HTTPS origins are allowed. Port 8080 is internal and never browser-facing.
_CORS="https://127.0.0.1:${PROXY_PORT}"
if [ -n "${LOCAL_DOMAIN:-}" ]; then
    _CORS="${_CORS};https://${LOCAL_DOMAIN}:${PROXY_PORT}"
fi
export CORS_ALLOW_ORIGIN="${_CORS}"

# --- Secret key management ---
PLACEHOLDER="local-only-change-me"
CURRENT_KEY="${WEBUI_SECRET_KEY:-}"

if [ -n "${CURRENT_KEY}" ] && [ "${CURRENT_KEY}" != "${PLACEHOLDER}" ]; then
    log "Using WEBUI_SECRET_KEY from shell environment."
    export WEBUI_SECRET_KEY="${CURRENT_KEY}"
elif [ -f "${SECRET_KEY_FILE}" ]; then
    STORED_KEY=$(cat "${SECRET_KEY_FILE}")
    [ -n "${STORED_KEY}" ] || die "Secret key file ${SECRET_KEY_FILE} is empty. Delete it and restart to regenerate."
    log "Loaded persistent WEBUI_SECRET_KEY from ${SECRET_KEY_FILE}."
    export WEBUI_SECRET_KEY="${STORED_KEY}"
else
    log "No secret key found. Generating a new persistent key ..."
    NEW_KEY=$(openssl rand -hex 32)
    printf '%s' "${NEW_KEY}" > "${SECRET_KEY_FILE}"
    chmod 600 "${SECRET_KEY_FILE}"
    log "New key written to ${SECRET_KEY_FILE} (readable by owner only)."
    log "It will be reloaded automatically on every subsequent start."
    export WEBUI_SECRET_KEY="${NEW_KEY}"
fi

# HuggingFace token (optional; avoids first-boot rate limits)
export HF_TOKEN="${HF_TOKEN:-}"

# Telemetry off
export SCARF_NO_ANALYTICS=true
export DO_NOT_TRACK=true
export ANONYMIZED_TELEMETRY=false

# --- TLS certificate expiry check (informational) ---
# The certificate's presence was already verified at the top of the script.
CERT_EXPIRY=$(openssl x509 -noout -enddate -in "${CERT_FILE}" 2>/dev/null | sed 's/notAfter=//')
CERT_EXPIRY_EPOCH=$(date -j -f "%b %d %T %Y %Z" "${CERT_EXPIRY}" "+%s" 2>/dev/null || echo 0)
DAYS_LEFT=$(( (CERT_EXPIRY_EPOCH - $(date "+%s")) / 86400 ))
if [ "${DAYS_LEFT}" -le 0 ]; then
    log "WARNING: TLS certificate has EXPIRED (${CERT_EXPIRY}). Run ./setup-ssl.sh to renew."
elif [ "${DAYS_LEFT}" -le 30 ]; then
    log "TLS certificate expires in ${DAYS_LEFT} days (${CERT_EXPIRY}) — consider renewing soon."
else
    log "TLS certificate valid until ${CERT_EXPIRY} (${DAYS_LEFT} days left)."
fi

# --- Launch Open WebUI (internal HTTP backend) ---
log "Starting Open WebUI (internal backend on http://${BIND_HOST}:${PORT}) ..."

nohup "${VENV_DIR}/bin/open-webui" serve \
    --host "${BIND_HOST}" \
    --port "${PORT}" \
    >> "${LOG_FILE}" 2>&1 &

SERVER_PID=$!
echo "${SERVER_PID}" > "${PID_FILE}"

sleep 2
if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    rm -f "${PID_FILE}"
    die "Open WebUI exited immediately. Inspect the log: tail -50 ${LOG_FILE}"
fi
log "Open WebUI process started (PID ${SERVER_PID})."

# --- Launch HTTPS proxy (the only browser-facing entry point) ---
PROXY_PID_FILE="${DATA_DIR}/ssl-proxy.pid"
PROXY_LOG="${DATA_DIR}/ssl-proxy.log"
# Clean up any stale proxy from a previous run.
if [ -f "${PROXY_PID_FILE}" ]; then
    OLD_PROXY=$(cat "${PROXY_PID_FILE}")
    kill "${OLD_PROXY}" 2>/dev/null || true
    rm -f "${PROXY_PID_FILE}"
fi
PYTHON="${VENV_DIR}/bin/python"
[ -x "${PYTHON}" ] || PYTHON="python3"
nohup "${PYTHON}" "${BASE_DIR}/ssl-proxy.py" \
    --cert "${CERT_FILE}" --key "${KEY_FILE}" \
    --listen-port "${PROXY_PORT}" --backend-port "${PORT}" \
    >> "${PROXY_LOG}" 2>&1 &
PROXY_PID=$!
echo "${PROXY_PID}" > "${PROXY_PID_FILE}"
sleep 1
if ! kill -0 "${PROXY_PID}" 2>/dev/null; then
    rm -f "${PROXY_PID_FILE}"
    die "HTTPS proxy failed to start. Inspect the log: tail -20 ${PROXY_LOG}"
fi
log "HTTPS proxy started (PID ${PROXY_PID}) — https://${LOCAL_DOMAIN:-127.0.0.1}:${PROXY_PORT} -> http://${BIND_HOST}:${PORT}"

# --- Start provisioner in the background ---
# Provisioning configures the admin account, filters, and model settings.
# It runs in the background so this script returns immediately. It waits for
# Open WebUI to finish first-boot setup (model-weight downloads can take a minute),
# then applies everything. Follow it with: tail -f ${DATA_DIR}/provision.log
if [ -n "${WEBUI_ADMIN_EMAIL:-}" ] && [ -n "${WEBUI_ADMIN_PASSWORD:-}" ]; then
    if [ -x "${BASE_DIR}/.venv/bin/python" ] && [ -f "${BASE_DIR}/provision.py" ]; then
        nohup "${BASE_DIR}/.venv/bin/python" "${BASE_DIR}/provision.py" \
            >> "${DATA_DIR}/provision.log" 2>&1 &
        log "Provisioner running in background (PID $!)."
    else
        log "WARNING: provision.py or .venv not found — skipping auto-configuration."
    fi
else
    log "WARNING: WEBUI_ADMIN_EMAIL / WEBUI_ADMIN_PASSWORD not set in .env."
    log "         Auto-configuration skipped. Set them and restart."
fi

# --- Final summary ---
log ""
log "================================================================"
log "  Stack is starting up."
log ""
log "  Open the UI at:   https://${LOCAL_DOMAIN:-127.0.0.1}:${PROXY_PORT}"
log "  Sign in with the email/password from your .env file."
log ""
log "  First boot downloads embedding weights (~1 min). Follow with:"
log "    tail -f ${DATA_DIR}/webui.log        (server startup)"
log "    tail -f ${DATA_DIR}/provision.log    (auto-configuration)"
log ""
log "  The UI is ready when provision.log shows: Provisioning complete."
log "  Stop everything with: ./stop-open-webui.sh"
log "================================================================"
