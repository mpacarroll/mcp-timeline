#!/usr/bin/env python3
"""HTTP tests for the browser studio: its guards and its main flows,
against a real server on a background thread. Synthetic data only.

Run: python3 -m unittest discover -s tests
"""

import http.client
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import demo_studio  # noqa: E402
import studio_web  # noqa: E402


class StudioWebTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.demo = demo_studio.build(self.db_path, quiet=True)
        self.srv = studio_web.make_server(self.db_path, 0)
        self.port = self.srv.server_address[1]
        self.token = self.srv.studio_token
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for p in (self.db_path, self.db_path + ".grant"):
            if os.path.exists(p):
                os.remove(p)

    def req(self, method, path, body=None, token=True, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["X-Studio-Token"] = self.token if token is True else token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        raw = r.read()
        c.close()
        ctype = r.getheader("Content-Type") or ""
        return r.status, (json.loads(raw) if "json" in ctype and raw else raw.decode()), r

    # ---------------------------------------------------- guards
    def test_foreign_host_header_is_refused(self):
        status, _, _ = self.req("GET", "/api/trips", host="evil.example:80")
        self.assertEqual(status, 421)
        status, _, _ = self.req("GET", "/", host="evil.example:80", token=False)
        self.assertEqual(status, 421)

    def test_api_needs_the_session_token(self):
        self.assertEqual(self.req("GET", "/api/trips", token=False)[0], 403)
        self.assertEqual(self.req("GET", "/api/trips", token="guess")[0], 403)
        self.assertEqual(self.req("GET", "/api/trips")[0], 200)

    def test_page_carries_token_and_strict_csp(self):
        status, page, r = self.req("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.token, page)
        self.assertNotIn("__STUDIO_TOKEN__", page)
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertIsNone(re.search(r"<script>[^<]", page), "no inline scripts")
        for asset in ("/app.js", "/app.css"):
            self.assertEqual(self.req("GET", asset, token=False)[0], 200)
        self.assertEqual(self.req("GET", "/../studio.py", token=False)[0], 404)

    def test_bad_input_is_a_clear_400(self):
        status, body, _ = self.req("POST", "/api/trips", {"title": "x", "start_date": "nope",
                                                          "end_date": "2026-05-17"})
        self.assertEqual(status, 400)
        self.assertIn("YYYY-MM-DD", body["error"])
        self.assertEqual(self.req("GET", "/api/nothing")[0], 404)

    # ---------------------------------------------------- flows
    def test_capture_edit_inspect_flow(self):
        status, out, _ = self.req("POST", "/api/trips", {
            "title": "Castle day", "start_date": "2026-05-16", "end_date": "2026-05-16"})
        self.assertEqual(status, 200)
        tid = out["trip_id"]
        _, trip, _ = self.req("GET", f"/api/trips/{tid}")
        castle = next(i for d in trip["days"] for i in d["items"]
                      if i["source_label"] == "Sao Jorge Castle")
        status, _, _ = self.req("PATCH", f"/api/items/{castle['item_id']}",
                                {"caption": "Peacocks everywhere"})
        self.assertEqual(status, 200)
        _, insp, _ = self.req("GET", f"/api/items/{castle['item_id']}/source")
        self.assertEqual(insp["your_changes"]["caption"], "Peacocks everywhere")
        self.assertEqual(insp["source_status"], "present")
        friend = next(i for d in trip["days"] for i in d["items"]
                      if i["source_label"] == "Ines's apartment")
        # The demo's friend redaction is scoped to the demo trip, so a new
        # trip over the same day does not inherit it; the global home
        # redaction would apply here if home were in range.
        self.assertIsNone(friend["redaction"])

    def test_export_download_is_redacted_and_logged(self):
        tid = self.demo["trip_id"]
        status, body, r = self.req("GET", f"/api/trips/{tid}/export?format=md&profile=family")
        self.assertEqual(status, 200)
        self.assertIn("attachment", r.getheader("Content-Disposition"))
        self.assertNotIn("Lumen", body)
        _, trip, _ = self.req("GET", f"/api/trips/{tid}")
        self.assertEqual(trip["exports"][0]["destination"], "download")
        custom = quote(json.dumps({"base": "public", "days": ["2026-05-16"]}))
        status, view, _ = self.req("GET", f"/api/trips/{tid}/preview?custom={custom}")
        self.assertEqual([d["date"] for d in view["days"]], ["2026-05-16"])

    def test_share_page_then_revoke(self):
        sid = self.demo["share_id"]
        status, page, r = self.req("GET", f"/share/{sid}", token=False)
        self.assertEqual(status, 200)
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("script-src", csp)
        self.assertIn("Riverside Market", page)
        self.assertNotIn("Lumen", page)
        self.assertEqual(self.req("DELETE", f"/api/shares/{sid}")[0], 200)
        self.assertEqual(self.req("GET", f"/share/{sid}", token=False)[0], 410)

    def test_grant_token_returned_once_and_never_listed(self):
        tid = self.demo["trip_id"]
        status, g, _ = self.req("POST", f"/api/trips/{tid}/grants",
                                {"agent": "Test agent", "hours": 1})
        self.assertEqual(status, 200)
        self.assertTrue(g["token"].startswith("tlg_"))
        _, trip, _ = self.req("GET", f"/api/trips/{tid}")
        self.assertNotIn(g["token"], json.dumps(trip))
        self.assertNotIn("token_sha256", json.dumps(trip))

    def test_destructive_actions_need_explicit_confirmation(self):
        tid = self.demo["trip_id"]
        self.assertEqual(self.req("DELETE", f"/api/trips/{tid}")[0], 400)
        status, _, _ = self.req("POST", "/api/purge", {"start_date": "2026-05-16",
                                                       "end_date": "2026-05-16"})
        self.assertEqual(status, 400)
        status, counts, _ = self.req("POST", "/api/purge", {
            "start_date": "2026-05-16", "end_date": "2026-05-16",
            "confirm": "delete history"})
        self.assertEqual(status, 200)
        self.assertGreater(counts["visits"], 0)
        self.assertEqual(self.req("DELETE", f"/api/trips/{tid}?confirm={tid}")[0], 200)
        self.assertEqual(self.req("GET", f"/api/trips/{tid}")[0], 400)
        self.assertEqual(self.req("GET", f"/share/{self.demo['share_id']}",
                                  token=False)[0], 410)

    def test_redaction_and_stale_export_flow(self):
        tid = self.demo["trip_id"]
        self.req("GET", f"/api/trips/{tid}/export?format=csv&profile=owner")
        status, _, _ = self.req("POST", "/api/redactions", {
            "kind": "place", "target": "SYNTH_MARKET_0007", "trip_id": tid,
            "replacement": "Lunch"})
        self.assertEqual(status, 200)
        _, trip, _ = self.req("GET", f"/api/trips/{tid}")
        self.assertTrue(trip["exports"][0]["stale_reason"])
        _, out, _ = self.req("POST", "/api/exports/remove-stale")
        self.assertEqual(len(out["outside_studio"]), 1)


if __name__ == "__main__":
    unittest.main()
