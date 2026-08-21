#!/usr/bin/env python3
"""HealthKit importer tests against a synthetic fixture. No real health
data anywhere here or in tests/fixtures/sample_healthkit_export/ -- dates,
coordinates, and source names are all invented.

Run: python3 -m unittest discover -s tests
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ingest
import ingest_healthkit

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "sample_healthkit_export")
TIMELINE_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_export.json")


class IngestHealthKitTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        ingest_healthkit.ingest_healthkit(FIXTURE_DIR, self.db_path)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row

    def tearDown(self):
        self.db.close()
        os.remove(self.db_path)

    def q(self, sql, params=()):
        return self.db.execute(sql, params).fetchall()

    def test_workout_count(self):
        self.assertEqual(
            self.q("SELECT COUNT(*) n FROM activities WHERE source = 'healthkit'")[0]["n"], 5)

    def test_mode_override_used_for_shared_vocabulary(self):
        row = self.q("SELECT * FROM activities WHERE source = 'healthkit'"
                      " AND local_date = '2026-02-04'")[0]
        self.assertEqual(row["mode"], "walking")

    def test_mode_fallback_camel_case_conversion(self):
        row = self.q("SELECT * FROM activities WHERE source = 'healthkit'"
                      " AND local_date = '2026-02-05'")[0]
        self.assertEqual(row["mode"], "stair_climbing")

    def test_distance_converted_to_meters(self):
        row = self.q("SELECT * FROM activities WHERE mode = 'running'")[0]
        self.assertAlmostEqual(row["distance_m"], 5000.0)

    def test_workout_without_distance_attribute_is_null(self):
        row = self.q("SELECT * FROM activities WHERE mode = 'strength_training'")[0]
        self.assertIsNone(row["distance_m"])

    def test_local_date_uses_own_offset_not_utc(self):
        # 23:15 -05:00 on Feb 4 is already Feb 5 in UTC; local_date must stay Feb 4.
        row = self.q("SELECT * FROM activities WHERE mode = 'walking'")[0]
        self.assertEqual(row["local_date"], "2026-02-04")

    def test_route_points_ingested_and_tagged(self):
        rows = self.q("SELECT * FROM path_points WHERE source = 'healthkit' ORDER BY ts_utc")
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["lat"], 2.0)
        self.assertAlmostEqual(rows[1]["lat"], 2.001)

    def test_start_end_latlng_derived_from_route(self):
        row = self.q("SELECT * FROM activities WHERE mode = 'running'")[0]
        self.assertAlmostEqual(row["start_lat"], 2.0)
        self.assertAlmostEqual(row["end_lat"], 2.001)

    def test_workout_without_route_has_no_latlng(self):
        row = self.q("SELECT * FROM activities WHERE mode = 'cycling'")[0]
        self.assertIsNone(row["start_lat"])
        self.assertIsNone(row["end_lat"])

    def test_rebuild_is_idempotent(self):
        ingest_healthkit.ingest_healthkit(FIXTURE_DIR, self.db_path)
        self.assertEqual(
            self.q("SELECT COUNT(*) n FROM activities WHERE source = 'healthkit'")[0]["n"], 5)


class CoexistenceTests(unittest.TestCase):
    """The core guarantee added alongside this importer: rebuilding one
    source's rows must never touch another source's rows in the same db."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = None

    def tearDown(self):
        if self.db is not None:
            self.db.close()
        os.remove(self.db_path)

    def q(self, sql, params=()):
        return self.db.execute(sql, params).fetchall()

    def test_two_sources_coexist_and_rebuilds_stay_scoped(self):
        ingest.ingest(TIMELINE_FIXTURE, self.db_path)
        ingest_healthkit.ingest_healthkit(FIXTURE_DIR, self.db_path)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row

        self.assertEqual(
            self.q("SELECT COUNT(*) n FROM activities WHERE source = 'google_timeline'")[0]["n"], 3)
        self.assertEqual(
            self.q("SELECT COUNT(*) n FROM activities WHERE source = 'healthkit'")[0]["n"], 5)

        # Re-running the Timeline importer must not touch HealthKit's rows,
        # and vice versa -- this is the whole point of source-scoped rebuild.
        ingest.ingest(TIMELINE_FIXTURE, self.db_path)
        self.assertEqual(
            self.q("SELECT COUNT(*) n FROM activities WHERE source = 'healthkit'")[0]["n"], 5)
        ingest_healthkit.ingest_healthkit(FIXTURE_DIR, self.db_path)
        self.assertEqual(
            self.q("SELECT COUNT(*) n FROM activities WHERE source = 'google_timeline'")[0]["n"], 3)

    def test_order_independent(self):
        # HealthKit ingested into a brand-new db before Timeline ever runs.
        ingest_healthkit.ingest_healthkit(FIXTURE_DIR, self.db_path)
        ingest.ingest(TIMELINE_FIXTURE, self.db_path)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.assertEqual(
            self.q("SELECT COUNT(*) n FROM activities WHERE source = 'healthkit'")[0]["n"], 5)
        self.assertEqual(
            self.q("SELECT COUNT(*) n FROM activities WHERE source = 'google_timeline'")[0]["n"], 3)


if __name__ == "__main__":
    unittest.main()
