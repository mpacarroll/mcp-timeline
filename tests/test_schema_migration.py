#!/usr/bin/env python3
"""Tests that ensure_schema upgrades a database created before the
`source` column existed.

This is a regression test for a real failure: a database built by the
pre-multi-source ingest.py kept its old columns, because CREATE TABLE IF
NOT EXISTS is a no-op on an existing table, and every later statement
touching `source` failed with "no such column: source". It took down the
OwnTracks receiver on a machine where the data was real.

Run: python3 -m unittest discover -s tests
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import schema

# Verbatim shape of the tables as they were before `source` was added.
LEGACY_SCHEMA = """
CREATE TABLE places (
    place_id      TEXT PRIMARY KEY,
    lat           REAL NOT NULL,
    lng           REAL NOT NULL,
    semantic_type TEXT,
    resolved_name TEXT,
    visit_count   INTEGER NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE TABLE visits (
    id            INTEGER PRIMARY KEY,
    place_id      TEXT REFERENCES places(place_id),
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL,
    start_local   TEXT NOT NULL,
    end_local     TEXT NOT NULL,
    local_date    TEXT NOT NULL,
    duration_min  REAL NOT NULL,
    probability   REAL
);
CREATE TABLE activities (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL,
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL,
    start_local   TEXT NOT NULL,
    end_local     TEXT NOT NULL,
    local_date    TEXT NOT NULL,
    duration_min  REAL NOT NULL,
    distance_m    REAL,
    start_lat     REAL, start_lng    REAL,
    end_lat       REAL, end_lng      REAL,
    probability   REAL
);
CREATE TABLE path_points (
    id            INTEGER PRIMARY KEY,
    ts_utc        TEXT NOT NULL,
    lat           REAL NOT NULL,
    lng           REAL NOT NULL
);
CREATE INDEX idx_visits_local_date     ON visits(local_date);
CREATE INDEX idx_activities_local_date ON activities(local_date);
CREATE INDEX idx_path_points_ts        ON path_points(ts_utc);
"""


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.remove(self.db_path)

    def legacy_db(self, activities=2, points=3):
        db = sqlite3.connect(self.db_path)
        db.executescript(LEGACY_SCHEMA)
        for i in range(activities):
            db.execute(
                "INSERT INTO activities (mode, start_utc, end_utc, start_local,"
                " end_local, local_date, duration_min) VALUES (?,?,?,?,?,?,?)",
                ("walking", f"2026-01-0{i+1}T10:00:00Z", f"2026-01-0{i+1}T10:30:00Z",
                 "10:00:00", "10:30:00", f"2026-01-0{i+1}", 30.0))
        for i in range(points):
            db.execute("INSERT INTO path_points (ts_utc, lat, lng) VALUES (?,?,?)",
                       (f"2026-01-01T1{i}:00:00Z", 1.0 + i, 2.0 + i))
        db.commit()
        return db

    def columns(self, db, table):
        return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}

    def test_legacy_database_reproduces_the_original_failure_without_migration(self):
        # Guards the test itself: if this stops failing, the fixture no
        # longer represents the broken state and the test below proves
        # nothing.
        db = self.legacy_db()
        with self.assertRaises(sqlite3.OperationalError) as ctx:
            db.executescript(schema.SHARED_INDEXES)
        self.assertIn("source", str(ctx.exception))
        db.close()

    def test_migration_adds_source_to_both_tables(self):
        db = self.legacy_db()
        migrated = schema.ensure_schema(db)
        self.assertEqual(sorted(migrated), ["activities", "path_points"])
        self.assertIn("source", self.columns(db, "activities"))
        self.assertIn("source", self.columns(db, "path_points"))
        db.close()

    def test_existing_rows_are_backfilled_not_lost(self):
        db = self.legacy_db(activities=2, points=3)
        schema.ensure_schema(db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM activities").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM path_points").fetchone()[0], 3)
        sources = {r[0] for r in db.execute("SELECT DISTINCT source FROM activities")}
        self.assertEqual(sources, {schema.LEGACY_SOURCE})
        sources = {r[0] for r in db.execute("SELECT DISTINCT source FROM path_points")}
        self.assertEqual(sources, {schema.LEGACY_SOURCE})
        db.close()

    def test_indexes_build_after_migration(self):
        db = self.legacy_db()
        schema.ensure_schema(db)
        names = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_activities_source", names)
        self.assertIn("idx_path_points_source", names)
        db.close()

    def test_new_rows_can_be_written_after_migration(self):
        db = self.legacy_db()
        schema.ensure_schema(db)
        db.execute("INSERT INTO path_points (source, ts_utc, lat, lng)"
                   " VALUES (?,?,?,?)", ("owntracks", "2026-02-01T00:00:00Z", 5.0, 6.0))
        db.commit()
        row = db.execute("SELECT lat FROM path_points WHERE source='owntracks'").fetchone()
        self.assertAlmostEqual(row[0], 5.0)
        db.close()

    def test_migration_is_idempotent(self):
        db = self.legacy_db()
        self.assertTrue(schema.ensure_schema(db))
        # A second call has nothing left to migrate and must not error.
        self.assertEqual(schema.ensure_schema(db), [])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM activities").fetchone()[0], 2)
        db.close()

    def test_fresh_database_needs_no_migration(self):
        db = sqlite3.connect(self.db_path)
        self.assertEqual(schema.ensure_schema(db), [])
        self.assertIn("source", self.columns(db, "activities"))
        db.close()

    def test_current_database_needs_no_migration(self):
        db = sqlite3.connect(self.db_path)
        schema.ensure_schema(db)
        db.close()
        db = sqlite3.connect(self.db_path)
        self.assertEqual(schema.ensure_schema(db), [])
        db.close()

    def test_real_importer_runs_against_a_migrated_legacy_database(self):
        # The end-to-end version of the bug: ingest into a legacy database
        # and confirm both sources coexist afterwards.
        import ingest
        db = self.legacy_db()
        schema.ensure_schema(db)
        db.close()
        fixture = os.path.join(os.path.dirname(__file__), "fixtures",
                               "sample_export.json")
        ingest.ingest(fixture, self.db_path)
        db = sqlite3.connect(self.db_path)
        # The legacy path_points rows keep their backfilled source; the
        # importer only clears and rewrites its own.
        legacy = db.execute("SELECT COUNT(*) FROM path_points WHERE source=?",
                            (schema.LEGACY_SOURCE,)).fetchone()[0]
        self.assertGreater(legacy, 0)
        db.close()


if __name__ == "__main__":
    unittest.main()
