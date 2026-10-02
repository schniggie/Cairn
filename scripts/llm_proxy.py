#!/usr/bin/env python3
"""Path-stripping LLM proxy for the DasCTF gateway.

The DasCTF competition LLM gateway expects requests at its *exact* URL — the
whitelisted upstream endpoint (e.g. ``https://api.deepseek.com/anthropic/v1/messages``)
is baked into the gateway URL, and the gateway appends whatever path the client
adds. Clients that append their own path (Claude Code CLI appends
``/v1/messages``) would hit a doubled path and 404.

This proxy sits in front of the gateway: it strips the request path entirely
and forwards the raw method/headers/body (auth token included) to the gateway
URL. It runs on the host (host network) so both the dispatcher healthcheck and
the host-networked worker containers can reach it at the same address.

Env:
    LLM_UPSTREAM   gateway URL to forward to (required)
    LLM_BIND       bind address, default "0.0.0.0"
    LLM_PORT       listen port, default 8888
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("LLM_UPSTREAM", "").rstrip("/")
BIND = os.environ.get("LLM_BIND", "0.0.0.0")
PORT = int(os.environ.get("LLM_PORT", "8888"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep noise down
        pass

    def _forward(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        url = UPSTREAM + (("?" + query) if query else "")

        req = urllib.request.Request(url, data=body, method=method)
        for key, value in self.headers.items():
            if key.lower() in ("host", "content-length", "connection"):
                continue
            req.add_header(key, value)
        req.add_header("Content-Length", str(len(body) if body else 0))

        try:
            resp = urllib.request.urlopen(req, timeout=300)
            data = resp.read()
        except urllib.error.HTTPError as exc:
            data = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except Exception as exc:  # noqa: BLE001
            try:
                self.send_error(502, f"proxy error: {exc}"[:200])
            except Exception:  # noqa: BLE001
                pass
            return

        self.send_response(resp.status)
        for key, value in resp.headers.items():
            if key.lower() in ("content-length", "connection", "transfer-encoding"):
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        self._forward("POST")

    def do_GET(self) -> None:
        self._forward("GET")


if __name__ == "__main__":
    if not UPSTREAM:
        sys.exit("LLM_UPSTREAM is not set")
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"llm_proxy: {BIND}:{PORT} -> {UPSTREAM}", flush=True)
    server.serve_forever()
