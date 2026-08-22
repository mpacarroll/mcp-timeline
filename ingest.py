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

import schema

SOURCE = "google_timeline"


def parse_ts(s):
    """Return (aware datetime, local_date, local_hms). Local values come from
    the string's own offset, so a 11pm subway ride stays on the rider's day.
    Z-suffixed strings carry no local offset; their UTC values are the best
    available."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt, s[:10], s[11:19]


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
    # places/visits have exactly one writer (this importer), so a full
    # rebuild is safe. activities/path_points are shared with other
    # importers (e.g. ingest_healthkit.py): only this source's own rows
    # get cleared, never the whole table.
    db.executescript("DROP TABLE IF EXISTS places; DROP TABLE IF EXISTS visits;")
    schema.ensure_schema(db)
    db.execute("DELETE FROM activities WHERE source = ?", (SOURCE,))
    db.execute("DELETE FROM path_points WHERE source = ?", (SOURCE,))

    skipped = 0
    places = {}  # place_id -> aggregate, built from visits only

    for rec in records:
        try:
            start, local_date, start_hms = parse_ts(rec["startTime"])
            end, _, end_hms = parse_ts(rec["endTime"])
            duration_min = (end - start).total_seconds() / 60

            if "visit" in rec:
                top = rec["visit"]["topCandidate"]
                pid = top["placeID"]
                lat, lng = parse_geo(top["placeLocation"])
                db.execute(
                    "INSERT INTO visits (place_id, start_utc, end_utc, start_local,"
                    " end_local, local_date, duration_min, probability)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (pid, to_utc_iso(start), to_utc_iso(end), start_hms, end_hms,
                     local_date, duration_min,
                     opt_float(rec["visit"].get("probability"))))
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
                    "INSERT INTO activities (source, mode, start_utc, end_utc,"
                    " start_local, end_local, local_date, duration_min, distance_m,"
                    " start_lat, start_lng, end_lat, end_lng, probability)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (SOURCE, act.get("topCandidate", {}).get("type", "unknown"),
                     to_utc_iso(start), to_utc_iso(end), start_hms, end_hms,
                     local_date, duration_min,
                     opt_float(act.get("distanceMeters")), s_lat, s_lng, e_lat, e_lng,
                     opt_float(act.get("probability"))))

            elif "timelinePath" in rec:
                for pt in rec["timelinePath"]:
                    lat, lng = parse_geo(pt["point"])
                    offset_min = float(pt.get("durationMinutesOffsetFromStartTime", 0))
                    ts = start.timestamp() + offset_min * 60
                    ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")
                    db.execute(
                        "INSERT INTO path_points (source, ts_utc, lat, lng) VALUES (?,?,?,?)",
                        (SOURCE, ts_iso, lat, lng))
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
    for table in ("places", "visits"):
        n = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table:12s} {n:6d}")
    for table in ("activities", "path_points"):
        n = db.execute(f"SELECT COUNT(*) FROM {table} WHERE source = ?",
                        (SOURCE,)).fetchone()[0]
        print(f"{table:12s} {n:6d}  (source={SOURCE})")
    print(f"{'skipped':12s} {skipped:6d}")
    db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("export_json", help="Timeline export file (iOS on-device format)")
    ap.add_argument("db", help="SQLite file to (re)build")
    args = ap.parse_args()
    ingest(args.export_json, args.db)
