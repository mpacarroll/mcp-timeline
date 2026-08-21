#!/usr/bin/env python3
"""Minimal HTTP receiver for automated Health Auto Export pushes.

Health Auto Export (iOS app) can be configured with a REST API automation
that POSTs your Health/Workout data to a URL on a schedule, with no manual
export tap. This script listens for that POST, checks a shared-secret
bearer token, and saves the raw payload to disk for inspection.

This is deliberately capture-only for now: the app's exact JSON shape
isn't independently documented anywhere reliable, so the first real
payload gets inspected before ingest_healthkit_auto.py (a separate,
not-yet-written script) is built to parse it -- same discipline as
ingest_healthkit.py itself, built and tested against a real export
before being trusted.

Usage:
    WEBHOOK_TOKEN=<shared secret> python3 webhook_receiver.py [port]

Configure the same token in Health Auto Export's automation as a
custom header: Authorization: Bearer <shared secret>.

This process only ever writes to <capture_dir>; it never touches
timeline.db. Run it behind the same tunnel as server.py but on its own
path/port -- Cloudflare Access's browser login can't gate this, since
the Health Auto Export app can't complete an interactive SSO redirect,
so this script does its own bearer-token check instead.
"""

import http.server
import os
import sys
from datetime import datetime, timezone

CAPTURE_DIR = os.environ.get("WEBHOOK_CAPTURE_DIR", "captures")
TOKEN = os.environ.get("WEBHOOK_TOKEN")

if not TOKEN:
    sys.exit("Set WEBHOOK_TOKEN to a shared secret before starting this "
              "receiver -- an unauthenticated write endpoint is not safe "
              "to expose through the tunnel.")


class Handler(http.server.BaseHTTPRequestHandler):
    # Declaring Content-Length on every response (below) makes the response
    # boundary explicit instead of relying on connection-close to signal
    # end-of-body -- the latter looked fine to curl directly, but produced
    # an "HTTP/2 stream not closed cleanly" warning once Cloudflare's tunnel
    # was translating HTTP/2 (client-facing) to HTTP/1.0 (this process's
    # default). HTTP/1.1 plus an explicit length is unambiguous either way.
    protocol_version = "HTTP/1.1"

    def _respond(self, status, body: bytes):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {TOKEN}":
            self._respond(401, b"unauthorized")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        os.makedirs(CAPTURE_DIR, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        content_type = self.headers.get("Content-Type", "")
        ext = "csv" if "csv" in content_type else "json"
        out_path = os.path.join(CAPTURE_DIR, f"healthauto_{stamp}.{ext}")
        with open(out_path, "wb") as f:
            f.write(body)

        print(f"captured {len(body)} bytes -> {out_path}")
        self._respond(200, b"ok")

    def log_message(self, fmt, *args):
        pass  # default logs to stderr per-request; the capture print above is enough


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    print(f"listening on 127.0.0.1:{port}, saving captures to {CAPTURE_DIR}/")
    server.serve_forever()
