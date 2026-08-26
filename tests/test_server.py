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

    def test_activity_stats_unknown_mode_is_valid_empty_not_error(self):
        # A mode with no data (typo, or a data source not yet ingested) is
        # not a protocol error: other importers add modes over time, so the
        # server can't validate against a fixed list. It comes back as a
        # zero-total result with a note naming modes that do have data.
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
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["total_km"], 0)
        self.assertIn("walking", result.structured_content["note"])

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



class DerivedDayTests(unittest.TestCase):
    """day_summary must reconstruct a day from raw fixes when no source
    recorded visits or activities for it, which is every day after the
    last Timeline export."""

    @classmethod
    def setUpClass(cls):
        import sqlite3
        from datetime import datetime, timedelta, timezone
        fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        ingest.ingest(FIXTURE, cls.db_path)

        # A day covered only by raw fixes: an hour at the fixture's known
        # Home coordinates, then a walk away from it.
        db = sqlite3.connect(cls.db_path)
        base = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
        rows = []
        for m in range(0, 61, 10):
            rows.append((base + timedelta(minutes=m), 1.0, 1.0))
        for i, m in enumerate(range(70, 111, 10)):
            rows.append((base + timedelta(minutes=m), 1.0 + 0.004 * (i + 1), 1.0))
        for ts, lat, lng in rows:
            db.execute("INSERT INTO path_points (source, ts_utc, lat, lng)"
                       " VALUES (?,?,?,?)",
                       ("owntracks", ts.strftime("%Y-%m-%dT%H:%M:%SZ"), lat, lng))
        db.commit()
        db.close()
        cls.local_date = base.astimezone().strftime("%Y-%m-%d")

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

    def test_recorded_day_is_labeled_as_recorded(self):
        result = self.call("day_summary", {"date": "2026-01-01"})
        self.assertEqual(result["derived_from"], "recorded visits and activities")

    def test_day_with_only_raw_fixes_is_reconstructed(self):
        result = self.call("day_summary", {"date": self.local_date})
        self.assertNotIn("note", result)
        self.assertIn("raw location fixes", result["derived_from"])
        kinds = [e["type"] for e in result["entries"]]
        self.assertEqual(kinds, ["visit", "movement"])

    def test_derived_stay_is_matched_to_a_known_place(self):
        result = self.call("day_summary", {"date": self.local_date})
        stay = result["entries"][0]
        # The fixture's Home sits at 1.0, 1.0, which is where the fixes are.
        self.assertEqual(stay["place"], "Home")
        self.assertEqual(stay["place_id"], "TESTPLACE_HOME_0001")

    def test_derived_movement_does_not_invent_a_transport_mode(self):
        result = self.call("day_summary", {"date": self.local_date})
        movement = result["entries"][1]
        self.assertEqual(movement["mode"], "unknown")
        self.assertGreater(movement["km"], 0)

    def test_day_with_nothing_at_all_still_returns_a_note(self):
        result = self.call("day_summary", {"date": "1999-01-01"})
        self.assertEqual(result["entries"], [])
        self.assertIn("note", result)


if __name__ == "__main__":
    unittest.main()
