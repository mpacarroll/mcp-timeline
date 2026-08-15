#!/usr/bin/env python3
"""End-to-end MCP protocol tests: spawn server.py over real stdio, call each
tool, assert against the known contents of the synthetic fixture. No real
data anywhere -- ingests tests/fixtures/sample_export.json only.

Run: python3 -m unittest discover -s tests
(requires the mcp package installed, see requirements.txt)
"""

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ingest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_export.json")
SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "server.py")


class ServerToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        ingest.ingest(FIXTURE, cls.db_path)

    @classmethod
    def tearDownClass(cls):
        os.remove(cls.db_path)

    def call(self, tool, args):
        async def run():
            params = StdioServerParameters(
                command=sys.executable, args=[SERVER],
                env={**os.environ, "TIMELINE_DB": self.db_path})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    return await s.call_tool(tool, args)
        return asyncio.run(run()).structured_content

    def test_list_tools(self):
        async def run():
            params = StdioServerParameters(
                command=sys.executable, args=[SERVER],
                env={**os.environ, "TIMELINE_DB": self.db_path})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    return [t.name for t in (await s.list_tools()).tools]
        names = asyncio.run(run())
        self.assertEqual(sorted(names),
                          ["activity_stats", "day_summary", "place_history",
                           "visits_near"])

    def test_activity_stats_walking(self):
        result = self.call("activity_stats", {
            "mode": "walking", "start_date": "2026-01-01",
            "end_date": "2026-01-02", "group_by": "day"})
        self.assertAlmostEqual(result["total_km"], 2.0)  # 1500m + 500m

    def test_activity_stats_rejects_bad_mode(self):
        async def run():
            params = StdioServerParameters(
                command=sys.executable, args=[SERVER],
                env={**os.environ, "TIMELINE_DB": self.db_path})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    return await s.call_tool("activity_stats", {
                        "mode": "teleporting", "start_date": "2026-01-01",
                        "end_date": "2026-01-02"})
        result = asyncio.run(run())
        self.assertTrue(result.is_error)

    def test_day_summary_orders_chronologically(self):
        result = self.call("day_summary", {"date": "2026-01-01"})
        types = [e["type"] for e in result["entries"]]
        self.assertEqual(types, ["visit", "movement", "visit", "movement", "visit"])

    def test_day_summary_empty_day_is_valid_not_error(self):
        result = self.call("day_summary", {"date": "1999-01-01"})
        self.assertEqual(result["entries"], [])
        self.assertIn("note", result)

    def test_visits_near_finds_home(self):
        result = self.call("visits_near", {"lat": 1.0, "lng": 1.0, "radius_m": 50})
        self.assertEqual(result["result"][0]["place_id"], "TESTPLACE_HOME_0001")
        self.assertEqual(result["result"][0]["visit_count"], 2)

    def test_place_history_by_label(self):
        result = self.call("place_history", {"place": "Home"})
        self.assertEqual(result["visit_count"], 2)

    def test_place_history_no_match(self):
        result = self.call("place_history", {"place": "Nowhere At All"})
        self.assertEqual(result["matches"], 0)


if __name__ == "__main__":
    unittest.main()
