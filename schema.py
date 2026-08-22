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

SHARED_SCHEMA = """
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

CREATE INDEX IF NOT EXISTS idx_activities_local_date ON activities(local_date);
CREATE INDEX IF NOT EXISTS idx_activities_mode       ON activities(mode);
CREATE INDEX IF NOT EXISTS idx_activities_source     ON activities(source);
CREATE INDEX IF NOT EXISTS idx_path_points_ts        ON path_points(ts_utc);
CREATE INDEX IF NOT EXISTS idx_path_points_source    ON path_points(source);
"""


def ensure_schema(db):
    """Create every table/index that doesn't already exist. Safe to call on
    a brand-new database, one built by a different importer, or one already
    fully populated -- idempotent either way."""
    db.executescript(PLACES_VISITS_SCHEMA)
    db.executescript(SHARED_SCHEMA)
