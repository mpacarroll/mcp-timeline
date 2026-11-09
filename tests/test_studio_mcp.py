#!/usr/bin/env python3
"""Agent access over the real MCP protocol: scope and consent.

Spawns server.py over stdio the way an MCP client does, with the grant
token in the server's environment, and checks that an agent can read only
the granted trip, only through its disclosure view and redactions, and
only while the grant is active. Synthetic data only.

Run: python3 -m unittest discover -s tests
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import demo_studio  # noqa: E402
import studio  # noqa: E402

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

SERVER = os.path.join(ROOT, "server.py")


class StudioMcpTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.demo = demo_studio.build(self.db_path, quiet=True)
        self.token = self.demo["grant"]["token"]

    def tearDown(self):
        for p in (self.db_path, self.db_path + ".grant"):
            if os.path.exists(p):
                os.remove(p)

    def session(self, calls, toolset="studio", token="__default__"):
        env = {**os.environ, "TIMELINE_DB": self.db_path, "TIMELINE_TOOLS": toolset}
        env.pop("TIMELINE_GRANT", None)
        if token == "__default__":
            token = self.token
        if token:
            env["TIMELINE_GRANT"] = token

        async def run():
            params = StdioServerParameters(command=sys.executable, args=[SERVER], env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    out = []
                    for name, args in calls:
                        if name == "__list__":
                            out.append([t.name for t in (await s.list_tools()).tools])
                        else:
                            out.append((await s.call_tool(name, args)).structured_content)
                    return out
        return asyncio.run(run())

    def test_studio_toolset_exposes_no_whole_history_tools(self):
        (names,) = self.session([("__list__", {})])
        self.assertEqual(sorted(names), ["studio_day", "studio_propose", "studio_scope",
                                         "studio_trip"])

    def test_agent_reads_the_redacted_disclosed_trip(self):
        scope, trip = self.session([("studio_scope", {}), ("studio_trip", {})])
        self.assertTrue(scope["allowed"])
        self.assertEqual(scope["trip"]["title"], "Lisbon long weekend")
        self.assertEqual(scope["capabilities"], ["read", "suggest"])
        text = json.dumps(trip)
        for word in ("Lumen", "rash", "Ines", "42.39012", "38.72265"):
            self.assertNotIn(word, text)
        self.assertEqual(trip["disclosure"]["coordinates"], "neighborhood")
        self.assertIn("Appointment", text)

    def test_day_outside_the_share_is_refused(self):
        ok, outside = self.session([("studio_day", {"date": "2026-05-15"}),
                                    ("studio_day", {"date": "2026-05-13"})])
        self.assertTrue(ok["allowed"])
        self.assertEqual(ok["day"]["date"], "2026-05-15")
        self.assertFalse(outside["allowed"])

    def test_no_grant_and_revoked_grant_are_refused(self):
        (no_grant,) = self.session([("studio_trip", {})], token=None)
        self.assertFalse(no_grant["allowed"])
        self.assertIn("TIMELINE_GRANT", no_grant["reason"])
        db = studio.connect(self.db_path)
        studio.revoke_grant(db, self.demo["grant"]["grant_id"])
        db.close()
        (revoked,) = self.session([("studio_trip", {})])
        self.assertEqual(revoked, {"allowed": False, "reason": "this grant was revoked"})
        db = studio.connect(self.db_path)
        outcomes = [r["outcome"] for r in studio.access_log(db)]
        db.close()
        self.assertEqual(outcomes[0], "refused: revoked")

    def test_proposals_are_held_and_cannot_target_redacted_items(self):
        (trip,) = self.session([("studio_trip", {})])
        items = [e for d in trip["days"] for e in d["items"]]
        tower = next(e for e in items if e["label"] == "Belem Tower")
        masked = next(e for e in items if e.get("redacted"))
        ok, refused = self.session([
            ("studio_propose", {"field": "caption", "value": "Iconic.", "ref": tower["ref"]}),
            ("studio_propose", {"field": "caption", "value": "x", "ref": masked["ref"]})])
        self.assertEqual(ok["status"], "pending")
        self.assertFalse(refused["allowed"])
        db = studio.connect(self.db_path)
        pending = studio.list_proposals(db, self.demo["trip_id"], "pending")
        self.assertEqual([p["value"] for p in pending], ["Iconic."])
        db.close()

    def test_read_only_grant_cannot_propose(self):
        db = studio.connect(self.db_path)
        g = studio.create_grant(db, self.demo["trip_id"], "Reader", ["read"], "public", 1)
        db.close()
        (out,) = self.session([("studio_propose", {"field": "story", "value": "x",
                                                   "date": "2026-05-15"})],
                              token=g["token"])
        self.assertFalse(out["allowed"])

    def test_access_log_records_every_allowed_call(self):
        self.session([("studio_scope", {}), ("studio_trip", {})])
        db = studio.connect(self.db_path)
        log = studio.access_log(db, self.demo["trip_id"])
        db.close()
        self.assertEqual([(r["tool"], r["outcome"]) for r in log],
                         [("studio_trip", "allowed"), ("studio_scope", "allowed")])


if __name__ == "__main__":
    unittest.main()
