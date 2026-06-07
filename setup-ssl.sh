#!/usr/bin/env bash
# setup-ssl.sh — enable HTTPS and port 80 for the local AI stack
#
# ─────────────────────────────────────────────────────────────────
#  SECURITY NOTICE — read before running
# ─────────────────────────────────────────────────────────────────
#
#  This script uses mkcert to generate a locally-trusted SSL certificate.
#  mkcert works by creating a local Certificate Authority (CA) and adding
#  it to your macOS System Keychain with sudo.
#
#  What that means:
#    • Safari and Chrome will trust HTTPS on 127.0.0.1 and your LOCAL_DOMAIN
#      without any browser warning — because your machine explicitly trusts
#      the mkcert CA.
#    • The CA and its private key are stored in:
#        $(mkcert -CAROOT)/rootCA.pem  and  rootCA-key.pem
#      Guard the key file. Anyone with it can sign certificates that your
#      Mac will trust.
#    • The CA is valid only on THIS machine. It is NOT a public CA and
#      cannot sign certificates for any domain on the public internet.
#
#  To undo completely:
#    mkcert -uninstall
#    sudo security delete-certificate -c mkcert
#
# ─────────────────────────────────────────────────────────────────
#
#  What this script does (this is the ONLY script that needs sudo):
#    1. Checks mkcert and ffmpeg are installed.
#    2. Installs the mkcert root CA into the macOS System Keychain (sudo).
#    3. Generates a certificate for 127.0.0.1, localhost, and LOCAL_DOMAIN.
#    4. Adds "127.0.0.1 <LOCAL_DOMAIN>" to /etc/hosts (sudo) so you can reach
#       the UI by name at https://<LOCAL_DOMAIN>:8443.
#
#  After this, ./start-open-webui.sh runs with NO sudo and serves the stack
#  over HTTPS on port 8443.
#
#  Requirements:
#    brew install mkcert ffmpeg
#
#  Usage:
#    ./setup-ssl.sh

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${BASE_DIR}/.env"
DATA_DIR="${BASE_DIR}/data"
SSL_DIR="${BASE_DIR}/ssl"
CERT_FILE="${SSL_DIR}/cert.pem"
KEY_FILE="${SSL_DIR}/key.pem"

log() { printf '[ssl] %s\n' "$*"; }
die() { printf '[ssl] ERROR: %s\n' "$*" >&2; exit 1; }

# ── Requirements ──────────────────────────────────────────────────
if ! command -v mkcert >/dev/null 2>&1; then
    die "mkcert not found. Install: brew install mkcert"
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
    die "ffmpeg not found. Install: brew install ffmpeg"
fi

# ── Read LOCAL_DOMAIN ─────────────────────────────────────────────
LOCAL_DOMAIN=""
if [ -f "${ENV_FILE}" ]; then
    LOCAL_DOMAIN=$(grep -E '^LOCAL_DOMAIN=' "${ENV_FILE}" 2>/dev/null \
        | cut -d= -f2 | tr -d '"' | tr -d "'" | tr -d ' ' || true)
fi

# ── Confirm before touching the keychain ─────────────────────────
printf '\n[ssl] ─────────────────────────────────────────────────\n'
printf '[ssl]  ABOUT TO INSTALL A ROOT CA INTO YOUR SYSTEM KEYCHAIN\n'
printf '[ssl] ─────────────────────────────────────────────────\n'
printf '[ssl]  This allows Safari/Chrome to trust HTTPS on:\n'
printf '[ssl]    • 127.0.0.1\n'
printf '[ssl]    • localhost\n'
if [ -n "${LOCAL_DOMAIN}" ]; then
printf '[ssl]    • %s\n' "${LOCAL_DOMAIN}"
fi
printf '[ssl]\n'
printf '[ssl]  The CA private key lives at:\n'
printf '[ssl]    %s/rootCA-key.pem\n' "$(mkcert -CAROOT 2>/dev/null || echo '~/.local/share/mkcert')"
printf '[ssl]\n'
printf '[ssl]  To undo: mkcert -uninstall\n'
printf '[ssl] ─────────────────────────────────────────────────\n'
printf '\n  Proceed? [y/N] '
read -r REPLY
case "${REPLY}" in
    y|Y|yes|YES) ;;
    *) die "Aborted." ;;
esac

# ── Install root CA ───────────────────────────────────────────────
log "Installing mkcert root CA into macOS System Keychain (sudo required) ..."
mkcert -install

# ── Generate certificate ──────────────────────────────────────────
mkdir -p "${SSL_DIR}"
DOMAINS="127.0.0.1 localhost"
if [ -n "${LOCAL_DOMAIN}" ]; then
    DOMAINS="${DOMAINS} ${LOCAL_DOMAIN}"
fi
log "Generating certificate for: ${DOMAINS}"
# shellcheck disable=SC2086
mkcert -cert-file "${CERT_FILE}" -key-file "${KEY_FILE}" ${DOMAINS}
chmod 600 "${KEY_FILE}"
log "Certificate written to ${SSL_DIR}/"

# ── Add /etc/hosts entry for LOCAL_DOMAIN ─────────────────────────
if [ -n "${LOCAL_DOMAIN}" ]; then
    if grep -qE "^127\.0\.0\.1[[:space:]]+${LOCAL_DOMAIN}([[:space:]]|$)" /etc/hosts 2>/dev/null; then
        log "/etc/hosts already maps ${LOCAL_DOMAIN} -> 127.0.0.1."
    else
        log "Adding ${LOCAL_DOMAIN} -> 127.0.0.1 to /etc/hosts (sudo) ..."
        printf '\n# macos-local-ai-stack\n127.0.0.1 %s\n' "${LOCAL_DOMAIN}" \
            | sudo tee -a /etc/hosts > /dev/null
        log "/etc/hosts updated."
    fi
fi

log ""
log "Setup complete."
log ""
log "Start the stack with (NO sudo needed):"
log "  ./start-open-webui.sh"
log ""
if [ -n "${LOCAL_DOMAIN}" ]; then
    log "Then open:  https://${LOCAL_DOMAIN}:8443"
    log "       or:  https://127.0.0.1:8443"
else
    log "Then open:  https://127.0.0.1:8443"
fi
log ""
log "Safari and Chrome will trust this certificate with no warning."
