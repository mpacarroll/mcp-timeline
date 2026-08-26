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
import json
import os
import sys
from datetime import datetime, timezone

CAPTURE_DIR = os.environ.get("WEBHOOK_CAPTURE_DIR", "captures")
TOKEN = os.environ.get("WEBHOOK_TOKEN")

if not TOKEN:
    sys.exit("Set WEBHOOK_TOKEN to a shared secret before starting this "
              "receiver -- an unauthenticated write endpoint is not safe "
              "to expose through the tunnel.")


def describe_payload(body: bytes, content_type: str = "", max_lines: int = 40):
    """Summarize the shape of a captured payload as lines of text.

    The point of this receiver is to learn an undocumented payload format,
    and a byte count teaches nothing. This prints the structure (keys,
    types, list lengths, one sample leaf value per path) so the format can
    usually be read off a single capture instead of several round trips.

    Sample values are truncated and only one is shown per path, since the
    payload is real health data and this output goes to a log file.
    """
    if "csv" in content_type.lower():
        text = body.decode("utf-8", "replace")
        rows = text.splitlines()
        out = [f"CSV, {len(rows)} lines"]
        if rows:
            out.append(f"header: {rows[0][:200]}")
        return out

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        preview = body[:120].decode("utf-8", "replace").replace("\n", " ")
        return [f"not JSON. first bytes: {preview!r}"]

    lines = []

    def walk(node, path="", depth=0):
        if len(lines) >= max_lines:
            return
        if isinstance(node, dict):
            lines.append(f"{path or '<root>'}: object with {len(node)} keys "
                         f"({', '.join(list(node)[:8])}"
                         f"{', ...' if len(node) > 8 else ''})")
            for key, value in list(node.items())[:12]:
                walk(value, f"{path}.{key}" if path else key, depth + 1)
        elif isinstance(node, list):
            lines.append(f"{path}: array of {len(node)}")
            if node:
                # One element is enough to reveal the element shape, and
                # avoids dumping an entire day of samples into the log.
                walk(node[0], f"{path}[0]", depth + 1)
        else:
            sample = repr(node)
            if len(sample) > 60:
                sample = sample[:57] + "..."
            lines.append(f"{path}: {type(node).__name__} = {sample}")

    walk(data)
    if len(lines) >= max_lines:
        lines.append(f"... truncated at {max_lines} lines; full payload is on disk")
    return lines


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
        for line in describe_payload(body, content_type):
            print(f"  {line}")
        self._respond(200, b"ok")

    def log_message(self, fmt, *args):
        pass  # default logs to stderr per-request; the capture print above is enough


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    print(f"listening on 127.0.0.1:{port}, saving captures to {CAPTURE_DIR}/")
    server.serve_forever()
