#!/usr/bin/env python3
"""Ingest a Google Maps Timeline export into SQLite.

Supports the iOS on-device export format (Google Maps app, 2024+): a JSON
array of visit, activity, and timelinePath records. Other formats (Android
on-device, legacy Takeout Semantic Location History) belong in future
importers that write the same schema; the schema is the contract, the
server never reads raw exports.

Usage:
    python3 ingest.py <export.json> <timeline.db>

Rebuilds the database from scratch on every run: full-history exports make
incremental sync pointless, and a rebuild can never drift.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE places (
    place_id      TEXT PRIMARY KEY,
    lat           REAL NOT NULL,
    lng           REAL NOT NULL,
    semantic_type TEXT,               -- Home / Work / Unknown / ...
    resolved_name TEXT,               -- filled lazily, never by ingest
    visit_count   INTEGER NOT NULL,
    first_seen    TEXT NOT NULL,      -- UTC ISO-8601
    last_seen     TEXT NOT NULL
);

CREATE TABLE visits (
    id            INTEGER PRIMARY KEY,
    place_id      TEXT REFERENCES places(place_id),
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL,
    local_date    TEXT NOT NULL,      -- date in the device's own offset; grouping key
    duration_min  REAL NOT NULL,
    probability   REAL
);

CREATE TABLE activities (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL,      -- walking / in subway / flying / ...
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL,
    local_date    TEXT NOT NULL,
    duration_min  REAL NOT NULL,
    distance_m    REAL,
    start_lat     REAL, start_lng    REAL,
    end_lat       REAL, end_lng      REAL,
    probability   REAL
);

CREATE TABLE path_points (
    id            INTEGER PRIMARY KEY,
    ts_utc        TEXT NOT NULL,      -- segment start + minute offset; UTC only,
    lat           REAL NOT NULL,      -- the export gives no local offset for paths
    lng           REAL NOT NULL
);

CREATE INDEX idx_visits_local_date     ON visits(local_date);
CREATE INDEX idx_visits_place          ON visits(place_id);
CREATE INDEX idx_activities_local_date ON activities(local_date);
CREATE INDEX idx_activities_mode       ON activities(mode);
CREATE INDEX idx_path_points_ts        ON path_points(ts_utc);
"""


def parse_ts(s):
    """Return (aware datetime, local_date). local_date comes from the string's
    own offset, so a 11pm subway ride stays on the rider's day. Z-suffixed
    strings carry no local offset; their UTC date is the best available."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt, s[:10]


def to_utc_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_geo(s):
    """'geo:40.7,-73.9' -> (40.7, -73.9)"""
    lat, lng = s.removeprefix("geo:").split(",")
    return float(lat), float(lng)


def opt_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ingest(export_path, db_path):
    with open(export_path) as f:
        records = json.load(f)
    if not isinstance(records, list):
        sys.exit("Unrecognized format: expected a top-level JSON array. "
                 "Is this the iOS on-device Timeline export?")

    db = sqlite3.connect(db_path)
    db.executescript("DROP TABLE IF EXISTS places; DROP TABLE IF EXISTS visits;"
                     "DROP TABLE IF EXISTS activities; DROP TABLE IF EXISTS path_points;")
    db.executescript(SCHEMA)

    skipped = 0
    places = {}  # place_id -> aggregate, built from visits only

    for rec in records:
        try:
            start, local_date = parse_ts(rec["startTime"])
            end, _ = parse_ts(rec["endTime"])
            duration_min = (end - start).total_seconds() / 60

            if "visit" in rec:
                top = rec["visit"]["topCandidate"]
                pid = top["placeID"]
                lat, lng = parse_geo(top["placeLocation"])
                db.execute(
                    "INSERT INTO visits (place_id, start_utc, end_utc, local_date,"
                    " duration_min, probability) VALUES (?,?,?,?,?,?)",
                    (pid, to_utc_iso(start), to_utc_iso(end), local_date,
                     duration_min, opt_float(rec["visit"].get("probability"))))
                p = places.setdefault(pid, {
                    "lat": lat, "lng": lng, "semantic_type": top.get("semanticType"),
                    "visit_count": 0, "first_seen": start, "last_seen": end})
                p["visit_count"] += 1
                p["first_seen"] = min(p["first_seen"], start)
                p["last_seen"] = max(p["last_seen"], end)
                # Prefer a real label over Unknown if any visit carries one
                if p["semantic_type"] in (None, "Unknown"):
                    p["semantic_type"] = top.get("semanticType") or p["semantic_type"]

            elif "activity" in rec:
                act = rec["activity"]
                s_lat, s_lng = parse_geo(act["start"]) if "start" in act else (None, None)
                e_lat, e_lng = parse_geo(act["end"]) if "end" in act else (None, None)
                db.execute(
                    "INSERT INTO activities (mode, start_utc, end_utc, local_date,"
                    " duration_min, distance_m, start_lat, start_lng, end_lat, end_lng,"
                    " probability) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (act.get("topCandidate", {}).get("type", "unknown"),
                     to_utc_iso(start), to_utc_iso(end), local_date, duration_min,
                     opt_float(act.get("distanceMeters")), s_lat, s_lng, e_lat, e_lng,
                     opt_float(act.get("probability"))))

            elif "timelinePath" in rec:
                for pt in rec["timelinePath"]:
                    lat, lng = parse_geo(pt["point"])
                    offset_min = float(pt.get("durationMinutesOffsetFromStartTime", 0))
                    ts = start.timestamp() + offset_min * 60
                    ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")
                    db.execute("INSERT INTO path_points (ts_utc, lat, lng) VALUES (?,?,?)",
                               (ts_iso, lat, lng))
            else:
                skipped += 1
        except (KeyError, ValueError, TypeError):
            skipped += 1

    for pid, p in places.items():
        db.execute(
            "INSERT INTO places (place_id, lat, lng, semantic_type, visit_count,"
            " first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
            (pid, p["lat"], p["lng"], p["semantic_type"], p["visit_count"],
             to_utc_iso(p["first_seen"]), to_utc_iso(p["last_seen"])))

    db.commit()
    for table in ("places", "visits", "activities", "path_points"):
        n = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table:12s} {n:6d}")
    print(f"{'skipped':12s} {skipped:6d}")
    db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("export_json", help="Timeline export file (iOS on-device format)")
    ap.add_argument("db", help="SQLite file to (re)build")
    args = ap.parse_args()
    ingest(args.export_json, args.db)
