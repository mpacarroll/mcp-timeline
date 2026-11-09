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

# The history studio's own tables (see studio.py). Everything here is
# authored by the person, or derived from source rows at capture time, and
# lives beside the source tables rather than inside them: an importer
# rebuild (ingest.py drops and recreates places/visits) must never erase a
# trip someone spent an evening editing, and a studio edit must never
# rewrite what a source actually recorded.
STUDIO_SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_trips (
    trip_id      TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    start_date   TEXT NOT NULL,     -- inclusive local dates
    end_date     TEXT NOT NULL,
    summary      TEXT,
    created_utc  TEXT NOT NULL,
    updated_utc  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS studio_items (
    item_id      TEXT PRIMARY KEY,  -- stable hash of trip + source identity
    trip_id      TEXT NOT NULL REFERENCES studio_trips(trip_id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,     -- visit / movement / note
    local_date   TEXT NOT NULL,
    start_utc    TEXT,
    end_utc      TEXT,
    start_local  TEXT,              -- full local ISO datetime when known
    end_local    TEXT,
    place_id     TEXT,
    source_label TEXT,              -- label as captured, before any edit
    label_source TEXT,              -- how source_label was obtained
    label        TEXT,              -- your override, NULL if unedited
    caption      TEXT,
    mode         TEXT,
    km           REAL,
    lat REAL, lng REAL, end_lat REAL, end_lng REAL,
    source       TEXT NOT NULL,     -- google_timeline / healthkit / raw fixes / you
    source_table TEXT,
    hidden       INTEGER NOT NULL DEFAULT 0,
    created_utc  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_studio_items_trip ON studio_items(trip_id, start_utc);

CREATE TABLE IF NOT EXISTS studio_item_points (
    item_id TEXT NOT NULL REFERENCES studio_items(item_id) ON DELETE CASCADE,
    seq     INTEGER NOT NULL,
    ts_utc  TEXT NOT NULL,
    lat     REAL NOT NULL,
    lng     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_studio_points_item ON studio_item_points(item_id);

CREATE TABLE IF NOT EXISTS studio_edits (
    id       INTEGER PRIMARY KEY,
    trip_id  TEXT NOT NULL,
    item_id  TEXT,
    field    TEXT NOT NULL,
    old      TEXT,
    new      TEXT,
    at_utc   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS studio_place_labels (
    place_id TEXT PRIMARY KEY,
    label    TEXT NOT NULL,
    at_utc   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS studio_story (
    trip_id     TEXT NOT NULL REFERENCES studio_trips(trip_id) ON DELETE CASCADE,
    local_date  TEXT NOT NULL,
    body        TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    PRIMARY KEY (trip_id, local_date)
);

CREATE TABLE IF NOT EXISTS studio_redactions (
    redaction_id TEXT PRIMARY KEY,
    trip_id      TEXT,              -- NULL applies to every trip
    kind         TEXT NOT NULL,     -- place / item / area / date
    target       TEXT,              -- place_id, item_id or date
    lat REAL, lng REAL, radius_m REAL,
    action       TEXT NOT NULL,     -- mask / remove
    replacement  TEXT NOT NULL,
    note         TEXT,              -- why, for you only; never exported
    created_utc  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS studio_shares (
    share_id     TEXT PRIMARY KEY,
    trip_id      TEXT NOT NULL REFERENCES studio_trips(trip_id) ON DELETE CASCADE,
    audience     TEXT NOT NULL,
    profile      TEXT NOT NULL,     -- JSON disclosure profile
    created_utc  TEXT NOT NULL,
    revoked_utc  TEXT
);

CREATE TABLE IF NOT EXISTS studio_grants (
    grant_id     TEXT PRIMARY KEY,  -- public id, safe to show
    token_sha256 TEXT NOT NULL UNIQUE,
    trip_id      TEXT NOT NULL REFERENCES studio_trips(trip_id) ON DELETE CASCADE,
    agent        TEXT NOT NULL,
    capabilities TEXT NOT NULL,     -- JSON list: read, suggest
    profile      TEXT NOT NULL,     -- JSON disclosure profile
    expires_utc  TEXT NOT NULL,
    created_utc  TEXT NOT NULL,
    revoked_utc  TEXT
);

CREATE TABLE IF NOT EXISTS studio_access_log (
    id        INTEGER PRIMARY KEY,
    grant_id  TEXT,
    trip_id   TEXT,
    tool      TEXT NOT NULL,
    outcome   TEXT NOT NULL,
    at_utc    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS studio_proposals (
    proposal_id TEXT PRIMARY KEY,
    grant_id    TEXT NOT NULL,
    trip_id     TEXT NOT NULL REFERENCES studio_trips(trip_id) ON DELETE CASCADE,
    item_id     TEXT,
    field       TEXT NOT NULL,      -- caption / story
    local_date  TEXT,
    value       TEXT NOT NULL,
    status      TEXT NOT NULL,      -- pending / accepted / rejected / withdrawn
    created_utc TEXT NOT NULL,
    decided_utc TEXT
);

CREATE TABLE IF NOT EXISTS studio_exports (
    export_id    TEXT PRIMARY KEY,
    trip_id      TEXT NOT NULL,
    format       TEXT NOT NULL,
    profile_name TEXT NOT NULL,
    destination  TEXT NOT NULL,     -- file / download / share
    path         TEXT,              -- set only for files the studio wrote
    sha256       TEXT NOT NULL,
    created_utc  TEXT NOT NULL,
    stale_reason TEXT,              -- set when a later redaction or deletion outdates it
    removed_utc  TEXT
);

-- Trip finder (studio_finder.py). Home is set by the person or detected
-- on demand; it is never exported and never offered to an agent.
CREATE TABLE IF NOT EXISTS studio_settings (
    key   TEXT PRIMARY KEY,         -- home_lat / home_lng / home_set_utc
    value TEXT NOT NULL
);

-- A suggested trip the person said was not a trip. Keyed by its dates,
-- so the same stretch is not suggested again after a re-ingest.
CREATE TABLE IF NOT EXISTS studio_dismissed_trips (
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    dismissed_utc TEXT NOT NULL,
    PRIMARY KEY (start_date, end_date)
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
    db.executescript(STUDIO_SCHEMA)
    migrated = _migrate_add_source(db)
    db.executescript(SHARED_INDEXES)
    db.commit()
    return migrated
