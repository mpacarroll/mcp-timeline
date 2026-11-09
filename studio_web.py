#!/usr/bin/env python3
"""Browser studio for trips, stories, redaction, sharing and agent grants.

    TIMELINE_DB=timeline.db python3 studio_web.py [port]   # default 8010

Then open http://127.0.0.1:8010. Standard library only, no network calls.

It binds to 127.0.0.1 and refuses any other Host header, so a web page
in another tab cannot reach it through DNS rebinding. Every API call must
carry a per-launch token that only the studio's own page receives, so a
cross-site form post cannot drive it either. There is no remote mode on
purpose: this is where the unredacted history is edited.
"""

import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import studio
import studio_export

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "studio_ui")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:;"
       " connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
# Share pages are exported HTML: one inline stylesheet and no scripts at
# all, so they get a policy that forbids every script source outright.
SHARE_CSP = ("default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none';"
             " base-uri 'none'; form-action 'none'")


class NotFound(Exception):
    pass


def make_handler(db_path, session_token, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "mcp-timeline-studio"

        def log_message(self, fmt, *args):  # keep test output quiet
            if os.environ.get("STUDIO_LOG"):
                super().log_message(fmt, *args)

        # ------------------------------------------------ plumbing
        def _send(self, status, body, ctype="application/json", extra=None, csp=CSP):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            if isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise studio.StudioError("Content-Length is not a number")
            if n < 0:
                raise studio.StudioError("Content-Length cannot be negative")
            if n > 1_000_000:
                raise studio.StudioError("request too large")
            if not n:
                return {}
            try:
                data = json.loads(self.rfile.read(n))
            except json.JSONDecodeError:
                raise studio.StudioError("request body is not valid JSON")
            if not isinstance(data, dict):
                raise studio.StudioError("request body must be a JSON object")
            return data

        def _guard(self, api):
            if self.headers.get("Host") not in allowed_hosts:
                self._send(421, {"error": "unexpected Host header"})
                return False
            if api and not secrets.compare_digest(
                    self.headers.get("X-Studio-Token", ""), session_token):
                self._send(403, {"error": "missing or wrong studio token"})
                return False
            return True

        def _dispatch(self, method):
            url = urlparse(self.path)
            path, query = url.path, {k: v[-1] for k, v in parse_qs(url.query).items()}
            api = path.startswith("/api/")
            if not self._guard(api):
                return
            db = studio.connect(db_path)
            try:
                if api:
                    body = self._body() if method in ("POST", "PUT", "PATCH") else {}
                    result = route(db, method, path, query, body)
                    if isinstance(result, tuple):  # (bytes/str, ctype, filename)
                        content, ctype, filename = result
                        self._send(200, content, ctype, {
                            "Content-Disposition": f'attachment; filename="{filename}"'})
                    else:
                        self._send(200, result)
                elif method == "GET":
                    self._static(db, path)
                else:
                    raise NotFound()
            except studio.StudioError as e:
                self._send(400, {"error": str(e)})
            except NotFound:
                self._send(404, {"error": "not found"})
            except sqlite3.Error as e:
                self._send(500, {"error": f"database error: {e}"})
            finally:
                db.close()

        def _static(self, db, path):
            m = re.fullmatch(r"/share/([A-Za-z0-9_\-]+)", path)
            if m:
                try:
                    share = studio.get_share(db, m.group(1))
                except studio.StudioError as e:
                    self._send(410, f"<!doctype html><title>Unavailable</title>"
                                    f"<p>{e}.</p>", "text/html; charset=utf-8")
                    return
                page = studio_export.render(db, share["trip_id"], "html", share["profile"])
                self._send(200, page, "text/html; charset=utf-8", csp=SHARE_CSP)
                return
            name = {"/": "index.html"}.get(path, path.lstrip("/"))
            if name not in ("index.html", "app.js", "app.css"):
                raise NotFound()
            with open(os.path.join(UI_DIR, name), "rb") as f:
                content = f.read()
            if name == "index.html":
                content = content.replace(b"__STUDIO_TOKEN__", session_token.encode())
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, content, ctype)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def editor_view(db, trip_id):
    """The unredacted editor model, annotated with what redaction will do.

    This is the one place unredacted data is served, and only to the
    studio's own page. Nothing here is an export.
    """
    trip = dict(studio.get_trip_row(db, trip_id))
    items = studio.items_for_trip(db, trip_id)
    redactions = studio_export.load_redactions(db, trip_id)
    place_pts = studio_export.place_points(db, redactions)
    stories = {r["local_date"]: r["body"] for r in db.execute(
        "SELECT local_date, body FROM studio_story WHERE trip_id = ?", (trip_id,))}
    owner = studio_export.build_view(db, trip_id, "owner")
    auto = {d["date"]: d.get("story") for d in owner["days"]}
    days = {}
    for it in items:
        matches = studio_export.matching_redactions(redactions, it, place_pts)
        it["redaction"] = (None if not matches else
                           {"action": studio_export._strongest(matches)["action"],
                            "replacement": studio_export._strongest(matches)["replacement"]})
        it["route_points"] = len(it.pop("points"))
        days.setdefault(it["local_date"], []).append(it)
    out_days = []
    for date in sorted(set(days) | set(stories)):
        out_days.append({"date": date, "items": days.get(date, []),
                         "story": stories.get(date),
                         "story_draft": auto.get(date) if date not in stories else None})
    places = [dict(r) for r in db.execute(
        "SELECT DISTINCT i.place_id, i.source_label FROM studio_items i"
        " WHERE i.trip_id = ? AND i.place_id IS NOT NULL ORDER BY i.source_label",
        (trip_id,))]
    return {"trip": trip, "days": out_days, "places": places,
            "redactions": studio.list_redactions(db, trip_id),
            "shares": studio.list_shares(db, trip_id),
            "grants": studio.list_grants(db, trip_id),
            "access_log": studio.access_log(db, trip_id, 50),
            "proposals": studio.list_proposals(db, trip_id, "pending"),
            "exports": studio.list_exports(db, trip_id),
            "profiles": studio.PROFILES,
            "formats": sorted(studio_export.EXPORTERS)}


def route(db, method, path, q, body):
    parts = [p for p in path.split("/") if p][1:]  # drop "api"
    m = (method, *parts)

    if m == ("GET", "trips"):
        return {"trips": studio.list_trips(db)}
    if m == ("POST", "trips"):
        return studio.create_trip(db, body.get("title"), body.get("start_date"),
                                  body.get("end_date"), body.get("summary"))
    if m == ("GET", "history"):
        r = db.execute("SELECT MIN(local_date) lo, MAX(local_date) hi, COUNT(*) n"
                       " FROM visits").fetchone()
        return {"first_date": r["lo"], "last_date": r["hi"], "visits": r["n"]}
    if len(parts) >= 2 and parts[0] == "trips":
        tid = parts[1]
        rest = parts[2:]
        if method == "GET" and not rest:
            return editor_view(db, tid)
        if method == "PATCH" and not rest:
            studio.update_trip(db, tid, body.get("title"), body.get("summary"))
            return {"ok": True}
        if method == "DELETE" and not rest:
            if body.get("confirm") != tid and q.get("confirm") != tid:
                raise studio.StudioError("deleting a trip needs confirm=<trip_id>")
            studio.delete_trip(db, tid)
            return {"ok": True}
        if rest == ["recapture"] and method == "POST":
            return studio.recapture(db, tid)
        if rest == ["notes"] and method == "POST":
            return {"item_id": studio.add_note(db, tid, body.get("date"), body.get("text"),
                                               body.get("time"))}
        if len(rest) == 2 and rest[0] == "story" and method == "PUT":
            studio.set_story(db, tid, rest[1], body.get("body"))
            return {"ok": True}
        if rest == ["preview"] and method == "GET":
            return studio_export.build_view(db, tid, _profile_from(q, body))
        if rest == ["export"] and method == "GET":
            fmt = q.get("format", "md")
            profile = _profile_from(q, body)
            content = studio_export.render(db, tid, fmt, profile)
            name = studio.normalize_profile(profile)["name"]
            studio.log_export(db, tid, fmt, name, "download", content)
            _, ctype, ext = studio_export.EXPORTERS[fmt]
            return content, ctype, f"trip-{tid}-{name}.{ext}"
        if rest == ["shares"] and method == "POST":
            sid = studio.create_share(db, tid, body.get("audience"),
                                      body.get("profile") or "family")
            return {"share_id": sid, "url": f"/share/{sid}"}
        if rest == ["grants"] and method == "POST":
            caps = body.get("capabilities") or ["read"]
            return studio.create_grant(db, tid, body.get("agent"), caps,
                                       body.get("profile") or "agent",
                                       body.get("hours") or 24)
    if len(parts) == 2 and parts[0] == "items":
        iid = parts[1]
        if method == "PATCH":
            return studio.edit_item(db, iid, body.get("label"), body.get("caption"),
                                    body.get("hidden"))
        if method == "DELETE":
            studio.delete_item(db, iid)
            return {"ok": True}
    if len(parts) == 3 and parts[0] == "items" and parts[2] == "source" and method == "GET":
        return studio.inspect_item(db, parts[1])
    if len(parts) == 3 and parts[0] == "places" and parts[2] == "label" and method == "PUT":
        studio.name_place(db, parts[1], body.get("label"))
        return {"ok": True}
    if m == ("POST", "redactions"):
        return {"redaction_id": studio.add_redaction(
            db, body.get("kind"), body.get("action") or "mask", body.get("trip_id"),
            body.get("target"), body.get("lat"), body.get("lng"), body.get("radius_m"),
            body.get("replacement"), body.get("note"))}
    if len(parts) == 2 and parts[0] == "redactions" and method == "DELETE":
        studio.remove_redaction(db, parts[1])
        return {"ok": True}
    if len(parts) == 2 and parts[0] == "shares" and method == "DELETE":
        studio.revoke_share(db, parts[1])
        return {"ok": True}
    if len(parts) == 2 and parts[0] == "grants" and method == "DELETE":
        studio.revoke_grant(db, parts[1])
        return {"ok": True}
    if len(parts) == 3 and parts[0] == "proposals" and method == "POST" \
            and parts[2] in ("accept", "reject"):
        studio.decide_proposal(db, parts[1], parts[2] == "accept")
        return {"ok": True}
    if m == ("POST", "purge"):
        if body.get("confirm") != "delete history":
            raise studio.StudioError('purging needs confirm="delete history"')
        return studio.purge_history(db, body.get("start_date"), body.get("end_date"))
    if m == ("POST", "exports", "remove-stale"):
        return studio.remove_stale_exports(db)
    raise NotFound()


def _profile_from(q, body):
    if body.get("profile"):
        return body["profile"]
    if q.get("custom"):
        try:
            return json.loads(q["custom"])
        except json.JSONDecodeError:
            raise studio.StudioError("custom profile is not valid JSON")
    return q.get("profile", "owner")


def make_server(db_path, port=8010):
    token = secrets.token_urlsafe(24)
    srv = ThreadingHTTPServer(("127.0.0.1", port), None)
    real_port = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(db_path, token, real_port)
    srv.studio_token = token
    return srv


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8010
    db_path = os.environ.get("TIMELINE_DB", "timeline.db")
    studio.connect(db_path).close()
    srv = make_server(db_path, port)
    print(f"history studio on http://127.0.0.1:{srv.server_address[1]}  (db: {db_path})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
