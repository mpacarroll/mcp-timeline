#!/usr/bin/env python3
"""Ingest tests against a synthetic fixture. No real data anywhere in this file
or in tests/fixtures/sample_export.json -- coordinates and place IDs are made up.

Run: python3 -m unittest discover -s tests
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ingest

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_export.json")


class IngestTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        ingest.ingest(FIXTURE, self.db_path)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row

    def tearDown(self):
        self.db.close()
        os.remove(self.db_path)

    def q(self, sql, params=()):
        return self.db.execute(sql, params).fetchall()

    def test_row_counts(self):
        self.assertEqual(self.q("SELECT COUNT(*) n FROM places")[0]["n"], 3)
        self.assertEqual(self.q("SELECT COUNT(*) n FROM visits")[0]["n"], 4)
        self.assertEqual(self.q("SELECT COUNT(*) n FROM activities")[0]["n"], 3)
        self.assertEqual(self.q("SELECT COUNT(*) n FROM path_points")[0]["n"], 3)

    def test_place_aggregation(self):
        home = self.q("SELECT * FROM places WHERE place_id = ?",
                       ("TESTPLACE_HOME_0001",))[0]
        self.assertEqual(home["visit_count"], 2)
        self.assertEqual(home["semantic_type"], "Home")
        self.assertAlmostEqual(home["lat"], 1.0)

    def test_local_date_uses_own_offset_not_utc(self):
        # The last visit of day 1 starts 17:25 -05:00 (still Jan 1 locally)
        # even though its UTC equivalent has already rolled to Jan 2.
        row = self.q(
            "SELECT local_date FROM visits WHERE start_local = '17:25:00'")[0]
        self.assertEqual(row["local_date"], "2026-01-01")

    def test_string_numbers_cast_to_real(self):
        row = self.q("SELECT distance_m FROM activities WHERE mode = 'walking'"
                      " ORDER BY start_utc LIMIT 1")[0]
        self.assertIsInstance(row["distance_m"], float)
        self.assertAlmostEqual(row["distance_m"], 1500.0)

    def test_rebuild_is_idempotent(self):
        ingest.ingest(FIXTURE, self.db_path)  # run again
        self.assertEqual(self.q("SELECT COUNT(*) n FROM visits")[0]["n"], 4)


if __name__ == "__main__":
    unittest.main()
