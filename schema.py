"""Shared SQLite schema for the timeline database.

Every importer (ingest.py for Google Timeline, ingest_healthkit.py for
Apple Health workouts, and any future one) writes into this same schema
instead of defining its own tables. activities and path_points are shared
across sources: each row carries a `source` column, and an importer only
ever deletes/rebuilds the rows carrying its own source value, so running
one importer can never wipe another's data. places and visits currently
have exactly one writer (ingest.py) and stay whole-table rebuilds.
"""

PLACES_VISITS_SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    place_id      TEXT PRIMARY KEY,
    lat           REAL NOT NULL,
    lng           REAL NOT NULL,
    semantic_type TEXT,               -- Home / Work / Unknown / ...
    resolved_name TEXT,               -- filled lazily, never by ingest
    visit_count   INTEGER NOT NULL,
    first_seen    TEXT NOT NULL,      -- UTC ISO-8601
    last_seen     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visits (
    id            INTEGER PRIMARY KEY,
    place_id      TEXT REFERENCES places(place_id),
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL,
    start_local   TEXT NOT NULL,      -- local wall-clock, for display
    end_local     TEXT NOT NULL,
    local_date    TEXT NOT NULL,      -- date in the device's own offset; grouping key
    duration_min  REAL NOT NULL,
    probability   REAL
);

CREATE INDEX IF NOT EXISTS idx_visits_local_date ON visits(local_date);
CREATE INDEX IF NOT EXISTS idx_visits_place      ON visits(place_id);
"""

SHARED_TABLES = """
CREATE TABLE IF NOT EXISTS activities (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,      -- google_timeline / healthkit / ...
    mode          TEXT NOT NULL,      -- walking / in subway / running / ...
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL,
    start_local   TEXT NOT NULL,      -- local wall-clock, for display
    end_local     TEXT NOT NULL,
    local_date    TEXT NOT NULL,
    duration_min  REAL NOT NULL,
    distance_m    REAL,
    start_lat     REAL, start_lng    REAL,
    end_lat       REAL, end_lng      REAL,
    probability   REAL
);

CREATE TABLE IF NOT EXISTS path_points (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    ts_utc        TEXT NOT NULL,      -- UTC only; not every source gives a local offset
    lat           REAL NOT NULL,
    lng           REAL NOT NULL
);
"""

GEOCODE_SCHEMA = """
-- Cache of reverse-geocoded coordinates. Separate from `places`, which
-- means "somewhere a source said I visited" and carries visit counts and
-- first/last-seen dates. A geocode is a fact about a coordinate, not a
-- record of having been there, and conflating them would corrupt the
-- visit statistics with places never actually visited.
CREATE TABLE IF NOT EXISTS geocode_cache (
    lat_key     REAL NOT NULL,       -- coordinates rounded, see geocode.py
    lng_key     REAL NOT NULL,
    label       TEXT,                -- short human label, NULL if none found
    fetched_utc TEXT NOT NULL,
    PRIMARY KEY (lat_key, lng_key)
);
"""

# Built separately from the tables above, because an index on `source`
# cannot be created until a database predating that column has been
# migrated to have one.
SHARED_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_activities_local_date ON activities(local_date);
CREATE INDEX IF NOT EXISTS idx_activities_mode       ON activities(mode);
CREATE INDEX IF NOT EXISTS idx_activities_source     ON activities(source);
CREATE INDEX IF NOT EXISTS idx_path_points_ts        ON path_points(ts_utc);
CREATE INDEX IF NOT EXISTS idx_path_points_source    ON path_points(source);
"""

# Rows written before the multi-source work all came from the Google
# Timeline importer, since it was the only writer that existed, so that is
# the honest value to backfill them with.
LEGACY_SOURCE = "google_timeline"

SOURCED_TABLES = ("activities", "path_points")


def _column_names(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _migrate_add_source(db):
    """Add the `source` column to tables created before it existed.

    CREATE TABLE IF NOT EXISTS silently does nothing when the table is
    already there, so a database built by an older version keeps its old
    shape and every later statement referencing `source` fails. Detect that
    and alter the table instead of assuming the definition above is what is
    actually on disk.

    Returns the tables it migrated, so callers can report the change rather
    than mutating a user's database silently.
    """
    migrated = []
    for table in SOURCED_TABLES:
        columns = _column_names(db, table)
        if not columns or "source" in columns:
            continue
        # SQLite cannot add a NOT NULL column without a default, and the
        # default doubles as the backfill for existing rows.
        db.execute(f"ALTER TABLE {table} ADD COLUMN source TEXT NOT NULL"
                   f" DEFAULT '{LEGACY_SOURCE}'")
        migrated.append(table)
    return migrated


def ensure_schema(db):
    """Bring a database up to the current schema, whatever state it is in.

    Handles three cases: a brand-new file, one already current, and one
    created before the `source` column existed. That third case is the
    reason this does more than run CREATE TABLE IF NOT EXISTS: that
    statement is a no-op on an existing table, so an older database keeps
    its old columns and any later reference to `source` fails with
    "no such column".

    Returns the list of tables migrated, empty when nothing changed.
    """
    db.executescript(PLACES_VISITS_SCHEMA)
    db.executescript(SHARED_TABLES)
    db.executescript(GEOCODE_SCHEMA)
    migrated = _migrate_add_source(db)
    db.executescript(SHARED_INDEXES)
    db.commit()
    return migrated
