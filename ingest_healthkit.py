#!/usr/bin/env python3
"""Ingest Apple Health workouts into the same SQLite schema ingest.py writes.

Source: the Health app's "Export All Health Data" archive, unzipped. Reads
export.xml (specifically <Workout> elements; the much larger volume of
<Record> biometric samples is skipped without being held in memory) and,
where a workout references one, its GPS route from workout-routes/*.gpx.

Usage:
    python3 ingest_healthkit.py <apple_health_export_dir> <timeline.db>

<apple_health_export_dir> is the folder containing export.xml directly
(the "apple_health_export" folder inside Health's export.zip). Rebuilds
this importer's own rows from scratch on every run, same as ingest.py;
rows written by other importers (e.g. Google Timeline) are untouched.
"""

import argparse
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import schema

SOURCE = "healthkit"

GPX_NS = "{http://www.topografix.com/GPX/1/1}"

# Explicit overrides where a shared vocabulary with Google Timeline's
# inferred activity modes is worth having (so activity_stats("walking", ...)
# reports on both sources together). Anything not listed here falls back to
# a generic HKWorkoutActivityType -> snake_case conversion.
MODE_OVERRIDES = {
    "HKWorkoutActivityTypeWalking": "walking",
    "HKWorkoutActivityTypeCycling": "cycling",
    "HKWorkoutActivityTypeRunning": "running",
    "HKWorkoutActivityTypeSwimming": "swimming",
    "HKWorkoutActivityTypeHiking": "hiking",
    "HKWorkoutActivityTypeElliptical": "elliptical",
    "HKWorkoutActivityTypeRowing": "rowing",
    "HKWorkoutActivityTypeYoga": "yoga",
    "HKWorkoutActivityTypeFunctionalStrengthTraining": "strength_training",
    "HKWorkoutActivityTypeTraditionalStrengthTraining": "strength_training",
}

DISTANCE_UNIT_TO_M = {"km": 1000.0, "mi": 1609.34, "m": 1.0, "ft": 0.3048}
DURATION_UNIT_TO_MIN = {"min": 1.0, "sec": 1 / 60, "hr": 60.0}


def mode_for(workout_type):
    if workout_type in MODE_OVERRIDES:
        return MODE_OVERRIDES[workout_type]
    name = workout_type.removeprefix("HKWorkoutActivityType") or "unknown"
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def parse_hk_ts(s):
    """Health export timestamps look like '2026-01-15 07:00:00 -0500':
    space-separated, always carrying an explicit UTC offset."""
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")
    local_date = dt.strftime("%Y-%m-%d")
    local_hms = dt.strftime("%H:%M:%S")
    return dt, local_date, local_hms


def to_utc_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_route(gpx_path):
    """Return [(ts_iso_utc, lat, lng), ...] from a workout-routes GPX file,
    or [] if the file is missing or unparseable (a route is optional)."""
    try:
        root = ET.parse(gpx_path).getroot()
    except (ET.ParseError, OSError):
        return []
    points = []
    for trkpt in root.iter(f"{GPX_NS}trkpt"):
        try:
            lat, lng = float(trkpt.get("lat")), float(trkpt.get("lon"))
            time_el = trkpt.find(f"{GPX_NS}time")
            ts = datetime.fromisoformat(time_el.text.replace("Z", "+00:00"))
            points.append((to_utc_iso(ts), lat, lng))
        except (TypeError, ValueError, AttributeError):
            continue
    return points


def ingest_healthkit(export_dir, db_path):
    xml_path = os.path.join(export_dir, "export.xml")
    if not os.path.exists(xml_path):
        sys.exit(f"No export.xml in {export_dir}. Point this at the "
                  "apple_health_export folder from Health's export.zip.")

    db = sqlite3.connect(db_path)
    schema.ensure_schema(db)
    db.execute("DELETE FROM activities WHERE source = ?", (SOURCE,))
    db.execute("DELETE FROM path_points WHERE source = ?", (SOURCE,))

    workouts = 0
    routes = 0
    skipped = 0

    # iterparse + clear(): export.xml is dominated by <Record> biometric
    # samples that vastly outnumber <Workout> elements and are irrelevant
    # here. Clearing every element after it's handled keeps memory bounded
    # regardless of export size, rather than holding the whole tree.
    for event, elem in ET.iterparse(xml_path, events=("end",)):
        if elem.tag != "Workout":
            if elem.tag in ("Record", "Correlation", "ActivitySummary"):
                elem.clear()
            continue
        try:
            start, local_date, start_hms = parse_hk_ts(elem.attrib["startDate"])
            end, _, end_hms = parse_hk_ts(elem.attrib["endDate"])
            duration_min = (end - start).total_seconds() / 60

            dist_unit = elem.attrib.get("totalDistanceUnitName") \
                or elem.attrib.get("totalDistanceUnit")
            distance_m = None
            if "totalDistance" in elem.attrib and dist_unit in DISTANCE_UNIT_TO_M:
                distance_m = float(elem.attrib["totalDistance"]) * DISTANCE_UNIT_TO_M[dist_unit]

            route_ref = elem.find("./WorkoutRoute/FileReference")
            points = []
            if route_ref is not None and route_ref.get("file"):
                gpx_path = os.path.join(export_dir, route_ref.get("file").lstrip("/"))
                points = parse_route(gpx_path)
                if points:
                    routes += 1

            s_lat, s_lng = (points[0][1], points[0][2]) if points else (None, None)
            e_lat, e_lng = (points[-1][1], points[-1][2]) if points else (None, None)

            db.execute(
                "INSERT INTO activities (source, mode, start_utc, end_utc,"
                " start_local, end_local, local_date, duration_min, distance_m,"
                " start_lat, start_lng, end_lat, end_lng, probability)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (SOURCE, mode_for(elem.attrib.get("workoutActivityType", "unknown")),
                 to_utc_iso(start), to_utc_iso(end), start_hms, end_hms,
                 local_date, duration_min, distance_m, s_lat, s_lng, e_lat, e_lng,
                 None))
            db.executemany(
                "INSERT INTO path_points (source, ts_utc, lat, lng) VALUES (?,?,?,?)",
                [(SOURCE, ts, lat, lng) for ts, lat, lng in points])
            workouts += 1
        except (KeyError, ValueError, TypeError):
            skipped += 1
        finally:
            elem.clear()

    db.commit()
    print(f"{'workouts':12s} {workouts:6d}")
    print(f"{'routes':12s} {routes:6d}")
    print(f"{'skipped':12s} {skipped:6d}")
    db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("export_dir", help="apple_health_export folder (contains export.xml)")
    ap.add_argument("db", help="SQLite file to update")
    args = ap.parse_args()
    ingest_healthkit(args.export_dir, args.db)
