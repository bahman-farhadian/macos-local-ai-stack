#!/usr/bin/env python3
"""
ssl-proxy.py — HTTPS front door for Open WebUI

Terminates TLS on --listen-port (8443) and forwards the decrypted bytes to
Open WebUI's internal HTTP port (8080). The browser always connects over
HTTPS, which is required for secure cookies and other secure-context browser
APIs (e.g. clipboard paste); Open WebUI
itself runs as plain HTTP on loopback and never needs to know about TLS.

WebSocket (chat streaming) works transparently: the browser opens wss:// based
on the page's own origin, and this proxy pipes those bytes straight through.

Concurrency: the accept loop does the bare minimum (accept, then hand off to a
thread). The TLS handshake and the bidirectional copy happen in per-connection
threads, so a single slow client can never block the listener or fill the
kernel accept backlog.

Usage:
    python3 ssl-proxy.py --cert cert.pem --key key.pem \
        --listen-port 8443 --backend-port 8080
"""

import argparse
import socket
import ssl
import threading

HANDSHAKE_TIMEOUT = 10   # seconds to complete the TLS handshake
BACKEND_TIMEOUT = 10     # seconds to open the backend connection


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    """Copy bytes from src to dst until either side closes."""
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _handle(client_raw: socket.socket, ctx: ssl.SSLContext,
            backend_host: str, backend_port: int) -> None:
    """Run in its own thread: TLS handshake, connect backend, pipe both ways."""
    client_ssl = None
    backend = None
    try:
        client_raw.settimeout(HANDSHAKE_TIMEOUT)
        client_ssl = ctx.wrap_socket(client_raw, server_side=True)
        client_ssl.settimeout(None)

        backend = socket.create_connection(
            (backend_host, backend_port), timeout=BACKEND_TIMEOUT
        )
        backend.settimeout(None)

        # One thread each direction; this thread handles backend -> client.
        t = threading.Thread(target=_pipe, args=(client_ssl, backend), daemon=True)
        t.start()
        _pipe(backend, client_ssl)
        t.join()
    except (ssl.SSLError, OSError):
        # Bad handshake, client hung up, or backend not ready — drop quietly.
        pass
    finally:
        for s in (client_ssl, backend, client_raw):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-port", type=int, default=8443)
    parser.add_argument("--backend-port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(args.cert, args.key)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.listen_port))
    srv.listen(256)

    print(
        f"[ssl-proxy] listening on https://{args.host}:{args.listen_port} "
        f"-> http://{args.host}:{args.backend_port}",
        flush=True,
    )

    while True:
        try:
            client_raw, _ = srv.accept()
        except OSError as exc:
            print(f"[ssl-proxy] accept error: {exc}", flush=True)
            continue
        # Hand off immediately — never do blocking work in the accept loop.
        threading.Thread(
            target=_handle,
            args=(client_raw, ctx, args.host, args.backend_port),
            daemon=True,
        ).start()


if __name__ == "__main__":
    main()
