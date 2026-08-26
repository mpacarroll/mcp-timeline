#!/usr/bin/env python3
"""HTTP receiver for OwnTracks location reports.

OwnTracks (free, open source, iOS and Android) can POST location updates
to an HTTP endpoint you control, continuously, with no manual step ever.
This receiver checks a shared bearer token, parses the documented
OwnTracks JSON payload, and writes location fixes straight into the same
path_points table ingest.py writes, tagged source='owntracks'.

Why this exists: a Google Timeline export is a frozen one-time snapshot
with no automation path (Timeline has been on-device and encrypted since
2025, with no API and no Shortcuts action on the export button). This
keeps accumulating for as long as the process runs. Location not
captured is lost permanently, so having this running matters more than
any downstream analysis of the points it collects.

Usage:
    OWNTRACKS_TOKEN=<secret> TIMELINE_DB=<path> \
        python3 owntracks_receiver.py [port]

Then in the OwnTracks app, Settings:
    Mode:      HTTP
    URL:       https://<your-public-host>/
    UserID:    anything (it identifies you, it is not the secret)
    Password:  the same secret
    Authentication and Password toggles: on

The iOS app authenticates with HTTP Basic and cannot send a custom
Authorization header, so the secret goes in the Password field. Basic
credentials are only base64, not encrypted, which is fine here because
the tunnel is HTTPS end to end. Bearer is also accepted, for curl,
tests, and any client that prefers it.

Unlike server.py (read-only) this process writes to the database, so it
gets its own bearer-token check rather than relying on a network-layer
gate: an unattended phone app cannot complete an interactive SSO login.
"""

import base64
import hmac
import http.server
import json
import os
import socketserver
import sqlite3
import sys
from datetime import datetime, timezone

import schema

SOURCE = "owntracks"

DB_PATH = os.environ.get("TIMELINE_DB", "timeline.db")
TOKEN = os.environ.get("OWNTRACKS_TOKEN")

# A phone reporting from indoors or off a cell tower can produce fixes
# accurate to hundreds of meters. Those are worse than useless for
# "where was I": they invent movement that never happened. Points less
# accurate than this are counted and dropped rather than silently stored.
MAX_ACCURACY_M = int(os.environ.get("OWNTRACKS_MAX_ACCURACY_M", "500"))

if not TOKEN:
    sys.exit("Set OWNTRACKS_TOKEN to a shared secret before starting this "
             "receiver. This endpoint writes to the database; it must not "
             "be exposed unauthenticated.")


def init_db():
    db = sqlite3.connect(DB_PATH)
    migrated = schema.ensure_schema(db)
    if migrated:
        # Altering someone's database is worth saying out loud, especially
        # here, where it happens unattended at service start.
        print(f"migrated schema: added a source column to "
              f"{', '.join(migrated)} (existing rows backfilled as "
              f"{schema.LEGACY_SOURCE!r})", flush=True)
    db.commit()
    db.close()


def record_location(payload):
    """Store one OwnTracks location fix. Returns a short status string
    describing what happened, for the operator watching the log."""
    lat, lon, tst = payload.get("lat"), payload.get("lon"), payload.get("tst")
    if lat is None or lon is None or tst is None:
        return "skipped (missing lat/lon/tst)"

    acc = payload.get("acc")
    if acc is not None and acc > MAX_ACCURACY_M:
        return f"skipped (accuracy {acc}m worse than {MAX_ACCURACY_M}m)"

    ts_iso = datetime.fromtimestamp(int(tst), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    db = sqlite3.connect(DB_PATH)
    try:
        # The phone resends on reconnect, so the same fix can arrive twice.
        # (source, ts_utc) identifies a fix well enough to drop repeats.
        dup = db.execute(
            "SELECT 1 FROM path_points WHERE source = ? AND ts_utc = ? LIMIT 1",
            (SOURCE, ts_iso)).fetchone()
        if dup:
            return f"duplicate {ts_iso}"
        db.execute(
            "INSERT INTO path_points (source, ts_utc, lat, lng) VALUES (?,?,?,?)",
            (SOURCE, ts_iso, float(lat), float(lon)))
        db.commit()
    finally:
        db.close()
    trigger = payload.get("t", "?")
    return f"stored {ts_iso} ({lat:.5f},{lon:.5f}) acc={acc} trigger={trigger}"


def _authorized(header):
    """True when the request carries the shared secret.

    Accepts two schemes because clients differ. The OwnTracks iOS app
    authenticates with HTTP Basic and cannot send a custom Authorization
    header at all, so its secret arrives as the Basic password. Bearer is
    kept for curl, the tests, and anything that prefers it.

    The Basic username is ignored on purpose: OwnTracks' UserID identifies
    a device, it is not a second secret, and demanding a particular value
    would add a failure mode without adding security.

    Compared with compare_digest rather than ==, so the comparison does not
    leak the secret's prefix through timing.
    """
    if not header:
        return False
    scheme, _, value = header.partition(" ")
    scheme = scheme.lower()
    if scheme == "bearer":
        return hmac.compare_digest(value, TOKEN)
    if scheme == "basic":
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        _user, _, password = decoded.partition(":")
        return hmac.compare_digest(password, TOKEN)
    return False


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self, status, body: bytes, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not _authorized(self.headers.get("Authorization")):
            # Logged on purpose. Without this, a phone posting with a stale
            # token looks exactly like a phone that is not posting at all:
            # both leave an empty log and an empty table. Never log the
            # value that was sent, only that one arrived and was rejected.
            supplied = self.headers.get("Authorization")
            if not supplied:
                reason = "no Authorization header"
            else:
                scheme = supplied.partition(" ")[0].lower()
                reason = (f"wrong secret ({scheme} scheme)"
                          if scheme in ("basic", "bearer")
                          else f"unsupported auth scheme {scheme!r}")
            print(f"rejected a POST from {self.client_address[0]}: {reason}",
                  flush=True)
            self._respond(401, b'{"error":"unauthorized"}')
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._respond(400, b'{"error":"invalid json"}')
            return

        # OwnTracks posts several message types on the same endpoint.
        # Only location fixes are stored, but everything else still gets a
        # clean 200 so the app does not queue and retry them forever.
        msg_type = payload.get("_type")
        if msg_type == "location":
            try:
                status = record_location(payload)
            except (ValueError, TypeError, sqlite3.Error) as exc:
                print(f"error storing location: {exc}", flush=True)
                self._respond(500, b'{"error":"storage failed"}')
                return
            print(status, flush=True)
        else:
            print(f"ignored _type={msg_type}", flush=True)

        # An empty JSON array is the documented "nothing to send back"
        # response; OwnTracks uses the response body to deliver friend
        # locations, which this deployment does not use.
        self._respond(200, b"[]")

    def log_message(self, fmt, *args):
        pass  # per-request stderr logging is noise; the prints above are the log


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # Threaded so one slow or half-open phone connection cannot block the
    # next report. Each request opens its own SQLite connection, so no
    # connection is shared across threads.


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8002
    init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"owntracks receiver listening on 127.0.0.1:{port}, writing to {DB_PATH}",
          flush=True)
    srv.serve_forever()
