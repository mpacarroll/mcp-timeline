#!/usr/bin/env python3
"""History studio: turn location history into editable trips and stories.

The four MCP tools in server.py answer questions about the whole history.
The studio is the other half: a person picks a stretch of days, captures it
as a trip, edits what the sources got wrong, writes the story, decides what
must never leave the machine, and only then shares a view of it, with a
human or with an agent.

Design rules, each enforced in code and covered by tests:

- Source rows are never rewritten by an edit. A trip item snapshots what a
  source recorded at capture time; your edits sit beside that snapshot, so
  inspecting an item always shows both "what the source said" and "what
  you changed".
- Every way out goes through one function, studio_export.build_view().
  Exports, share pages and agent tool calls all read its output and
  nothing else, so a redaction or deletion is applied once and reaches
  all of them. tests/test_studio_export.py proves that per format.
- Redactions apply to every export, including your own archive. To export
  something you redacted, remove the redaction first; there is no
  "owner bypass" for a test to miss.
- Agents get nothing by default. An agent reads a trip only through a
  grant you created: one trip, an expiry, a disclosure profile, and an
  access log you can read. Agent writes are proposals you accept.

Usage (CLI; the browser studio is studio_web.py):

    python3 studio.py --db timeline.db trips
    python3 studio.py --db timeline.db create "Lisbon" 2026-05-14 2026-05-17
    python3 studio.py --db timeline.db show <trip_id>
    python3 studio.py --db timeline.db export <trip_id> --format md --profile family -o out.md
    python3 studio.py --db timeline.db purge 2026-05-15 2026-05-15 --yes
"""

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import dwell
import schema
from geo import haversine_m

UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Disclosure presets. A profile decides how much of a trip a given audience
# sees; redactions decide what nobody sees. Both apply in build_view().
PROFILES = {
    "owner": {
        "name": "owner", "times": "exact", "coordinates": "exact",
        "routes": True, "captions": True, "story": True, "movement": True,
        "sources": True, "days": None},
    "family": {
        "name": "family", "times": "part_of_day", "coordinates": "neighborhood",
        "routes": False, "captions": True, "story": True, "movement": True,
        "sources": False, "days": None},
    "public": {
        "name": "public", "times": "none", "coordinates": "city",
        "routes": False, "captions": False, "story": True, "movement": False,
        "sources": False, "days": None},
    "agent": {
        "name": "agent", "times": "exact", "coordinates": "neighborhood",
        "routes": False, "captions": True, "story": True, "movement": True,
        "sources": False, "days": None},
}

TIME_LEVELS = ("exact", "part_of_day", "none")
COORD_LEVELS = ("exact", "neighborhood", "city", "none")
CAPABILITIES = ("read", "suggest")
REDACTION_KINDS = ("place", "item", "area", "date")
REDACTION_ACTIONS = ("mask", "remove")


class StudioError(ValueError):
    """A request the studio refuses, with a message fit to show a person."""


def now_utc():
    return datetime.now(timezone.utc).strftime(UTC_FMT)


def connect(db_path):
    """Open a database and bring it to the current schema.

    Unlike server.py, the studio creates the file if missing: it has its
    own tables, and an empty studio over an empty history is valid.
    """
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    schema.ensure_schema(db)
    return db


def _new_id(prefix):
    return f"{prefix}_{secrets.token_hex(6)}"


def _stable_id(*parts):
    return "i_" + hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _parse_utc(s):
    return datetime.strptime(s, UTC_FMT).replace(tzinfo=timezone.utc)


def _validate_date(s, field="date"):
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise StudioError(f"{field} must be YYYY-MM-DD, got {s!r}")
    return s


def local_iso(local_date, local_hms, utc_str, start_local_iso=None):
    """Rebuild a full local datetime from what the source tables store.

    Source rows keep the local date only for the start, and the local
    wall-clock time for both ends. For an end time the date is unknown,
    and it matters: an overnight flight that departs 18:30 on the 14th
    lands on the 15th. The device's offset at that moment is recoverable
    from the gap between local wall-clock and UTC, which also handles a
    flight that changes time zone mid-item.
    """
    if not local_hms or not utc_str:
        return None
    utc = _parse_utc(utc_str)
    if len(local_hms) > 8:  # already a full datetime
        return local_hms
    h, m, s = (int(x) for x in local_hms.split(":"))
    if local_date and start_local_iso is None:
        return f"{local_date}T{local_hms}"
    wall = timedelta(hours=h, minutes=m, seconds=s)
    utc_tod = timedelta(hours=utc.hour, minutes=utc.minute, seconds=utc.second)
    offset = wall - utc_tod
    # Real offsets lie between -12h and +14h; fold the day wrap into that.
    while offset > timedelta(hours=14):
        offset -= timedelta(days=1)
    while offset < timedelta(hours=-12):
        offset += timedelta(days=1)
    return (utc + offset).strftime("%Y-%m-%dT%H:%M:%S")


def place_label(db, place_id, fallback_row=None):
    """Return (label, how it was obtained) for a place."""
    if place_id:
        r = db.execute("SELECT label FROM studio_place_labels WHERE place_id = ?",
                       (place_id,)).fetchone()
        if r:
            return r["label"], "named by you"
        p = db.execute("SELECT resolved_name, semantic_type FROM places"
                       " WHERE place_id = ?", (place_id,)).fetchone()
        if p and p["resolved_name"]:
            return p["resolved_name"], "resolved name"
        if p and p["semantic_type"] and p["semantic_type"] != "Unknown":
            return p["semantic_type"], "source category"
    return "unnamed place", "none"


def name_place(db, place_id, label):
    """Give a place a name of your own. It wins over any source label in
    future captures and in existing trips that have not overridden it."""
    label = (label or "").strip()
    if not label:
        raise StudioError("a place name cannot be empty")
    if not db.execute("SELECT 1 FROM places WHERE place_id = ?", (place_id,)).fetchone():
        raise StudioError(f"no place {place_id!r} in the history")
    db.execute("INSERT INTO studio_place_labels (place_id, label, at_utc) VALUES (?,?,?)"
               " ON CONFLICT(place_id) DO UPDATE SET label = excluded.label,"
               " at_utc = excluded.at_utc", (place_id, label, now_utc()))
    db.execute("UPDATE studio_items SET source_label = ?, label_source = 'named by you'"
               " WHERE place_id = ?", (label, place_id))
    db.commit()


# ---------------------------------------------------------------- trips

def create_trip(db, title, start_date, end_date, summary=None):
    """Capture every recorded or reconstructable item in a date range."""
    title = (title or "").strip()
    if not title:
        raise StudioError("a trip needs a title")
    _validate_date(start_date, "start_date")
    _validate_date(end_date, "end_date")
    if end_date < start_date:
        raise StudioError("end_date is before start_date")
    days = (datetime.strptime(end_date, "%Y-%m-%d")
            - datetime.strptime(start_date, "%Y-%m-%d")).days + 1
    if days > 92:
        raise StudioError("a trip can cover at most 92 days")
    trip_id = _new_id("t")
    ts = now_utc()
    db.execute("INSERT INTO studio_trips VALUES (?,?,?,?,?,?,?)",
               (trip_id, title, start_date, end_date, summary, ts, ts))
    captured = _capture(db, trip_id, start_date, end_date)
    db.commit()
    return {"trip_id": trip_id, "items": captured}


def _capture(db, trip_id, start_date, end_date):
    ts = now_utc()
    count = 0
    covered_dates = set()
    for r in db.execute(
            "SELECT * FROM visits WHERE local_date BETWEEN ? AND ? ORDER BY start_utc",
            (start_date, end_date)).fetchall():
        place = db.execute("SELECT lat, lng FROM places WHERE place_id = ?",
                           (r["place_id"],)).fetchone()
        label, how = place_label(db, r["place_id"])
        start_local = local_iso(r["local_date"], r["start_local"], r["start_utc"])
        item_id = _stable_id(trip_id, "visit", r["start_utc"], r["place_id"])
        db.execute(
            "INSERT OR IGNORE INTO studio_items (item_id, trip_id, kind, local_date,"
            " start_utc, end_utc, start_local, end_local, place_id, source_label,"
            " label_source, lat, lng, source, source_table, created_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, trip_id, "visit", r["local_date"], r["start_utc"], r["end_utc"],
             start_local, local_iso(None, r["end_local"], r["end_utc"], start_local),
             r["place_id"], label, how,
             place["lat"] if place else None, place["lng"] if place else None,
             "google_timeline", "visits", ts))
        covered_dates.add(r["local_date"])
        count += 1
    for r in db.execute(
            "SELECT * FROM activities WHERE local_date BETWEEN ? AND ? ORDER BY start_utc",
            (start_date, end_date)).fetchall():
        start_local = local_iso(r["local_date"], r["start_local"], r["start_utc"])
        item_id = _stable_id(trip_id, "movement", r["source"], r["start_utc"], r["mode"])
        db.execute(
            "INSERT OR IGNORE INTO studio_items (item_id, trip_id, kind, local_date,"
            " start_utc, end_utc, start_local, end_local, source_label, label_source,"
            " mode, km, lat, lng, end_lat, end_lng, source, source_table, created_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, trip_id, "movement", r["local_date"], r["start_utc"], r["end_utc"],
             start_local, local_iso(None, r["end_local"], r["end_utc"], start_local),
             r["mode"], "transport mode from source", r["mode"],
             round((r["distance_m"] or 0) / 1000, 2),
             r["start_lat"], r["start_lng"], r["end_lat"], r["end_lng"],
             r["source"], "activities", ts))
        for i, p in enumerate(db.execute(
                "SELECT ts_utc, lat, lng FROM path_points WHERE ts_utc >= ? AND ts_utc <= ?"
                " ORDER BY ts_utc", (r["start_utc"], r["end_utc"])).fetchall()):
            db.execute("INSERT INTO studio_item_points VALUES (?,?,?,?,?)",
                       (item_id, i, p["ts_utc"], p["lat"], p["lng"]))
        covered_dates.add(r["local_date"])
        count += 1
    count += _capture_raw_fixes(db, trip_id, start_date, end_date, covered_dates, ts)
    return count


def _capture_raw_fixes(db, trip_id, start_date, end_date, covered, ts):
    """Days no source summarized (an OwnTracks-only day, say) are rebuilt
    from raw fixes, the same way server.day_summary does, and marked as
    reconstructed so nobody mistakes them for recorded visits."""
    count = 0
    day = datetime.strptime(start_date, "%Y-%m-%d")
    last = datetime.strptime(end_date, "%Y-%m-%d")
    while day <= last:
        date = day.strftime("%Y-%m-%d")
        day += timedelta(days=1)
        if date in covered:
            continue
        start_local = datetime.strptime(date, "%Y-%m-%d").astimezone()
        lo = start_local.astimezone(timezone.utc).strftime(UTC_FMT)
        hi = (start_local + timedelta(days=1)).astimezone(timezone.utc).strftime(UTC_FMT)
        points = [(_parse_utc(r["ts_utc"]), r["lat"], r["lng"]) for r in db.execute(
            "SELECT ts_utc, lat, lng FROM path_points WHERE ts_utc >= ? AND ts_utc < ?"
            " ORDER BY ts_utc", (lo, hi))]
        for e in dwell.derive_entries(points, gap_min=60):
            s_utc, e_utc = e["start"].strftime(UTC_FMT), e["end"].strftime(UTC_FMT)
            s_loc = e["start"].astimezone().strftime("%Y-%m-%dT%H:%M:%S")
            e_loc = e["end"].astimezone().strftime("%Y-%m-%dT%H:%M:%S")
            if e["type"] == "stay":
                item_id = _stable_id(trip_id, "visit", "raw", s_utc)
                db.execute(
                    "INSERT OR IGNORE INTO studio_items (item_id, trip_id, kind,"
                    " local_date, start_utc, end_utc, start_local, end_local,"
                    " source_label, label_source, lat, lng, source, source_table,"
                    " created_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item_id, trip_id, "visit", date, s_utc, e_utc, s_loc, e_loc,
                     "unidentified stop", "reconstructed from raw fixes",
                     e["lat"], e["lng"], "raw fixes", "path_points", ts))
            else:
                item_id = _stable_id(trip_id, "movement", "raw", s_utc)
                db.execute(
                    "INSERT OR IGNORE INTO studio_items (item_id, trip_id, kind,"
                    " local_date, start_utc, end_utc, start_local, end_local,"
                    " source_label, label_source, mode, km, source, source_table,"
                    " created_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item_id, trip_id, "movement", date, s_utc, e_utc, s_loc, e_loc,
                     "movement", "reconstructed from raw fixes", "unknown", e["km"],
                     "raw fixes", "path_points", ts))
                for i, p in enumerate(db.execute(
                        "SELECT ts_utc, lat, lng FROM path_points WHERE ts_utc >= ?"
                        " AND ts_utc <= ? ORDER BY ts_utc", (s_utc, e_utc)).fetchall()):
                    db.execute("INSERT INTO studio_item_points VALUES (?,?,?,?,?)",
                               (item_id, i, p["ts_utc"], p["lat"], p["lng"]))
            count += 1
    return count


def recapture(db, trip_id):
    """Pull in source rows added since the trip was captured. Existing
    items keep every edit, because item ids are stable hashes of the
    source identity."""
    trip = get_trip_row(db, trip_id)
    before = db.execute("SELECT COUNT(*) FROM studio_items WHERE trip_id = ?",
                        (trip_id,)).fetchone()[0]
    _capture(db, trip_id, trip["start_date"], trip["end_date"])
    after = db.execute("SELECT COUNT(*) FROM studio_items WHERE trip_id = ?",
                       (trip_id,)).fetchone()[0]
    _touch(db, trip_id)
    db.commit()
    return {"added": after - before}


def get_trip_row(db, trip_id):
    r = db.execute("SELECT * FROM studio_trips WHERE trip_id = ?", (trip_id,)).fetchone()
    if r is None:
        raise StudioError(f"no trip {trip_id!r}")
    return r


def list_trips(db):
    return [dict(r) | {"items": r["n"]} for r in db.execute(
        "SELECT t.*, (SELECT COUNT(*) FROM studio_items i WHERE i.trip_id = t.trip_id) n"
        " FROM studio_trips t ORDER BY start_date DESC")]


def _touch(db, trip_id):
    db.execute("UPDATE studio_trips SET updated_utc = ? WHERE trip_id = ?",
               (now_utc(), trip_id))


def update_trip(db, trip_id, title=None, summary=None):
    trip = get_trip_row(db, trip_id)
    for field, value in (("title", title), ("summary", summary)):
        if value is None:
            continue
        if field == "title" and not value.strip():
            raise StudioError("a trip needs a title")
        db.execute(f"UPDATE studio_trips SET {field} = ? WHERE trip_id = ?",
                   (value, trip_id))
        _log_edit(db, trip_id, None, field, trip[field], value)
    _touch(db, trip_id)
    db.commit()


def _log_edit(db, trip_id, item_id, field, old, new):
    db.execute("INSERT INTO studio_edits (trip_id, item_id, field, old, new, at_utc)"
               " VALUES (?,?,?,?,?,?)", (trip_id, item_id, field,
                                         None if old is None else str(old),
                                         None if new is None else str(new), now_utc()))


def get_item_row(db, item_id):
    r = db.execute("SELECT * FROM studio_items WHERE item_id = ?", (item_id,)).fetchone()
    if r is None:
        raise StudioError(f"no item {item_id!r}")
    return r


def edit_item(db, item_id, label=None, caption=None, hidden=None):
    """Edit what a trip shows for an item. The source snapshot is kept."""
    item = get_item_row(db, item_id)
    changes = {}
    if label is not None:
        changes["label"] = label.strip() or None
    if caption is not None:
        changes["caption"] = caption.strip() or None
    if hidden is not None:
        changes["hidden"] = 1 if hidden else 0
    for field, value in changes.items():
        if item[field] == value:
            continue
        db.execute(f"UPDATE studio_items SET {field} = ? WHERE item_id = ?",
                   (value, item_id))
        _log_edit(db, item["trip_id"], item_id, field, item[field], value)
    _touch(db, item["trip_id"])
    db.commit()
    return dict(get_item_row(db, item_id))


def add_note(db, trip_id, local_date, text, time_local=None):
    """A note you write into a day: something no sensor recorded."""
    trip = get_trip_row(db, trip_id)
    _validate_date(local_date)
    if not trip["start_date"] <= local_date <= trip["end_date"]:
        raise StudioError("that date is outside the trip")
    text = (text or "").strip()
    if not text:
        raise StudioError("a note cannot be empty")
    start_local = f"{local_date}T{time_local}:00" if time_local else None
    item_id = _new_id("n")
    db.execute(
        "INSERT INTO studio_items (item_id, trip_id, kind, local_date, start_local,"
        " source_label, label_source, caption, source, created_utc)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (item_id, trip_id, "note", local_date, start_local, "note", "written by you",
         text, "you", now_utc()))
    _log_edit(db, trip_id, item_id, "note", None, "added")
    _touch(db, trip_id)
    db.commit()
    return item_id


def set_story(db, trip_id, local_date, body):
    """Replace the auto-drafted story for one day with your own words.
    An empty body reverts to the auto-draft."""
    get_trip_row(db, trip_id)
    _validate_date(local_date)
    body = (body or "").strip()
    old = db.execute("SELECT body FROM studio_story WHERE trip_id = ? AND local_date = ?",
                     (trip_id, local_date)).fetchone()
    if body:
        db.execute("INSERT INTO studio_story VALUES (?,?,?,?) ON CONFLICT(trip_id,"
                   " local_date) DO UPDATE SET body = excluded.body,"
                   " updated_utc = excluded.updated_utc",
                   (trip_id, local_date, body, now_utc()))
    else:
        db.execute("DELETE FROM studio_story WHERE trip_id = ? AND local_date = ?",
                   (trip_id, local_date))
    _log_edit(db, trip_id, None, f"story {local_date}", old["body"] if old else None,
              body or None)
    _touch(db, trip_id)
    db.commit()


def _chronological(rows):
    """Order a trip's items within each day.

    Timed items sort by UTC, which stays correct across a time-zone change
    mid-day. A note only has the local time you typed, so it goes after
    the last timed item that started at or before that local time; an
    untimed note goes at the end of its day.
    """
    by_day = {}
    for r in rows:
        by_day.setdefault(r["local_date"], []).append(r)
    out = []
    for date in sorted(by_day):
        timed = sorted((r for r in by_day[date] if r["start_utc"]),
                       key=lambda r: r["start_utc"])
        notes = sorted((r for r in by_day[date] if not r["start_utc"]),
                       key=lambda r: (r["start_local"] is None, r["start_local"] or "",
                                      r["created_utc"]))
        merged = list(timed)
        for n in notes:
            pos = len(merged)
            if n["start_local"]:
                pos = 0
                for i, t in enumerate(merged):
                    if (t["start_local"] or "") <= n["start_local"]:
                        pos = i + 1
            merged.insert(pos, n)
        out += merged
    return out


def items_for_trip(db, trip_id):
    rows = _chronological(db.execute(
        "SELECT * FROM studio_items WHERE trip_id = ?", (trip_id,)).fetchall())
    items = []
    for r in rows:
        d = dict(r)
        d["points"] = [dict(p) for p in db.execute(
            "SELECT ts_utc, lat, lng FROM studio_item_points WHERE item_id = ?"
            " ORDER BY seq", (r["item_id"],))]
        items.append(d)
    return items


# ---------------------------------------------------------------- inspection

def inspect_item(db, item_id):
    """Everything known about where an item came from and what will show.

    Answers four questions: what did the source record, what did you
    change, is the source row still there, and which redactions touch it.
    """
    import studio_export
    item = dict(get_item_row(db, item_id))
    snapshot = {k: item[k] for k in (
        "kind", "local_date", "start_utc", "end_utc", "start_local", "end_local",
        "place_id", "source_label", "label_source", "mode", "km", "lat", "lng",
        "end_lat", "end_lng")}
    edits = [dict(r) for r in db.execute(
        "SELECT field, old, new, at_utc FROM studio_edits WHERE item_id = ? ORDER BY id",
        (item_id,))]
    status, rows = _source_status(db, item)
    redactions = [dict(r) for r in studio_export.matching_redactions(
        studio_export.load_redactions(db, item["trip_id"]), item)]
    points = db.execute("SELECT COUNT(*) FROM studio_item_points WHERE item_id = ?",
                        (item_id,)).fetchone()[0]
    place = None
    if item["place_id"]:
        r = db.execute("SELECT place_id, semantic_type, resolved_name, visit_count,"
                       " first_seen, last_seen FROM places WHERE place_id = ?",
                       (item["place_id"],)).fetchone()
        place = dict(r) if r else None
    return {"item_id": item_id, "trip_id": item["trip_id"],
            "source": item["source"], "source_table": item["source_table"],
            "captured_utc": item["created_utc"], "as_captured": snapshot,
            "route_points": points, "place_record": place,
            "your_changes": {"label": item["label"], "caption": item["caption"],
                             "hidden": bool(item["hidden"])},
            "edit_history": edits, "source_status": status, "source_rows": rows,
            "redactions": redactions}


def _source_status(db, item):
    if item["source"] == "you":
        return "written by you; no source row", []
    if item["source_table"] == "visits":
        rows = db.execute("SELECT * FROM visits WHERE start_utc = ? AND place_id = ?",
                          (item["start_utc"], item["place_id"])).fetchall()
    elif item["source_table"] == "activities":
        rows = db.execute("SELECT * FROM activities WHERE start_utc = ? AND mode = ?"
                          " AND source = ?", (item["start_utc"], item["mode"],
                                              item["source"])).fetchall()
    else:
        rows = db.execute("SELECT * FROM path_points WHERE ts_utc >= ? AND ts_utc <= ?",
                          (item["start_utc"], item["end_utc"])).fetchall()
    if not rows:
        return ("source row no longer present (purged or re-imported without it);"
                " the trip keeps its captured snapshot"), []
    return "present", [dict(r) for r in rows[:5]]


# ---------------------------------------------------------------- redaction

def add_redaction(db, kind, action="mask", trip_id=None, target=None, lat=None,
                  lng=None, radius_m=None, replacement=None, note=None):
    """Declare something that must not leave the studio.

    kind: place (target=place_id), item (target=item_id), area (lat, lng,
    radius_m), date (target=YYYY-MM-DD). action: mask keeps that something
    happened under a neutral label without coordinates or route; remove
    drops it entirely.
    """
    if kind not in REDACTION_KINDS:
        raise StudioError(f"kind must be one of {', '.join(REDACTION_KINDS)}")
    if action not in REDACTION_ACTIONS:
        raise StudioError("action must be mask or remove")
    if trip_id is not None:
        get_trip_row(db, trip_id)
    if kind == "place":
        if not target or not (db.execute("SELECT 1 FROM places WHERE place_id = ?",
                                         (target,)).fetchone()
                              or db.execute("SELECT 1 FROM studio_items WHERE place_id = ?",
                                            (target,)).fetchone()):
            raise StudioError("place redaction needs a known place_id")
    elif kind == "item":
        get_item_row(db, target)
    elif kind == "date":
        _validate_date(target)
    elif kind == "area":
        try:
            lat, lng, radius_m = float(lat), float(lng), float(radius_m)
        except (TypeError, ValueError):
            raise StudioError("area redaction needs lat, lng and radius_m")
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise StudioError("area center is not a valid coordinate")
        if not 25 <= radius_m <= 50000:
            raise StudioError("radius_m must be between 25 and 50000")
    replacement = (replacement or "").strip() or (
        "Private area" if kind == "area" else "Private place")
    rid = _new_id("r")
    db.execute("INSERT INTO studio_redactions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (rid, trip_id, kind, target, lat, lng, radius_m, action, replacement,
                note, now_utc()))
    _mark_exports_stale(db, trip_id, "a redaction was added after this export")
    db.commit()
    return rid


def remove_redaction(db, redaction_id):
    r = db.execute("SELECT * FROM studio_redactions WHERE redaction_id = ?",
                   (redaction_id,)).fetchone()
    if r is None:
        raise StudioError(f"no redaction {redaction_id!r}")
    db.execute("DELETE FROM studio_redactions WHERE redaction_id = ?", (redaction_id,))
    db.commit()


def list_redactions(db, trip_id=None):
    if trip_id is None:
        return [dict(r) for r in db.execute(
            "SELECT * FROM studio_redactions ORDER BY created_utc")]
    return [dict(r) for r in db.execute(
        "SELECT * FROM studio_redactions WHERE trip_id IS NULL OR trip_id = ?"
        " ORDER BY created_utc", (trip_id,))]


# ---------------------------------------------------------------- deletion

def _scrub_terms_from_text(db, trip_id, terms, replacement="[removed]"):
    """Take a deleted thing's names out of everything you wrote about it.

    Deleting an item while your story still says "lunch at Riverside
    Market" would leave the deleted fact in every export. Names are
    replaced in story bodies, captions, trip titles and summaries.
    """
    terms = sorted({t for t in terms if t and len(t) >= 3}, key=len, reverse=True)
    if not terms:
        return
    pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    where, args = ("", ()) if trip_id is None else (" WHERE trip_id = ?", (trip_id,))
    for r in db.execute("SELECT trip_id, local_date, body FROM studio_story" + where,
                        args).fetchall():
        new = pattern.sub(replacement, r["body"])
        if new != r["body"]:
            db.execute("UPDATE studio_story SET body = ? WHERE trip_id = ?"
                       " AND local_date = ?", (new, r["trip_id"], r["local_date"]))
    for r in db.execute("SELECT item_id, caption, label FROM studio_items" + where
                        + ("" if trip_id is None else ""), args).fetchall():
        for field in ("caption", "label"):
            if r[field] and pattern.search(r[field]):
                db.execute(f"UPDATE studio_items SET {field} = ? WHERE item_id = ?",
                           (pattern.sub(replacement, r[field]), r["item_id"]))
    for r in db.execute("SELECT trip_id, title, summary FROM studio_trips" + where,
                        args).fetchall():
        for field in ("title", "summary"):
            if r[field] and pattern.search(r[field]):
                db.execute(f"UPDATE studio_trips SET {field} = ? WHERE trip_id = ?",
                           (pattern.sub(replacement, r[field]), r["trip_id"]))


def delete_item(db, item_id):
    """Delete an item from its trip, and its names from your writing.

    The edit log for the item is deleted too: it holds old labels and
    captions, and a deletion that leaves a history of the deleted text
    behind is not a deletion.
    """
    item = get_item_row(db, item_id)
    terms = [item["label"], item["caption"] if item["kind"] == "note" else None]
    if item["kind"] == "visit" and item["source_label"] not in (
            "unnamed place", "unidentified stop"):
        terms.append(item["source_label"])
    _scrub_terms_from_text(db, item["trip_id"], terms)
    if item["kind"] == "visit" and item["lat"] is not None:
        _trim_routes_near(db, item["trip_id"], item["lat"], item["lng"])
    db.execute("DELETE FROM studio_item_points WHERE item_id = ?", (item_id,))
    db.execute("DELETE FROM studio_edits WHERE item_id = ?", (item_id,))
    db.execute("DELETE FROM studio_proposals WHERE item_id = ?", (item_id,))
    db.execute("DELETE FROM studio_redactions WHERE kind = 'item' AND target = ?",
               (item_id,))
    db.execute("DELETE FROM studio_items WHERE item_id = ?", (item_id,))
    _mark_exports_stale(db, item["trip_id"], "an item was deleted after this export")
    _touch(db, item["trip_id"])
    db.commit()


def _trim_routes_near(db, trip_id, lat, lng, radius_m=200):
    """A journey that ends at a deleted stop still points at it. Drop the
    route and the matching endpoint of any movement in the trip that
    starts or ends there."""
    for m in db.execute("SELECT * FROM studio_items WHERE trip_id = ? AND kind = 'movement'",
                        (trip_id,)).fetchall():
        pts = db.execute("SELECT seq, lat, lng FROM studio_item_points WHERE item_id = ?"
                         " ORDER BY seq", (m["item_id"],)).fetchall()
        touched = False
        for a, b in (("lat", "lng"), ("end_lat", "end_lng")):
            if m[a] is not None and haversine_m(lat, lng, m[a], m[b]) <= radius_m:
                db.execute(f"UPDATE studio_items SET {a} = NULL, {b} = NULL"
                           " WHERE item_id = ?", (m["item_id"],))
                touched = True
        # A movement rebuilt from raw fixes has no stored endpoints; its
        # first and last route points are where it starts and ends.
        if pts and m["lat"] is None and m["end_lat"] is None:
            touched = touched or any(haversine_m(lat, lng, p["lat"], p["lng"]) <= radius_m
                                     for p in (pts[0], pts[-1]))
        if touched:
            db.execute("DELETE FROM studio_item_points WHERE item_id = ?", (m["item_id"],))
        else:
            for p in pts:
                if haversine_m(lat, lng, p["lat"], p["lng"]) <= radius_m:
                    db.execute("DELETE FROM studio_item_points WHERE item_id = ?"
                               " AND seq = ?", (m["item_id"], p["seq"]))


def delete_trip(db, trip_id):
    """Delete a trip and everything hanging off it: items, routes, story,
    edits, shares, grants and proposals. The access log keeps rows that
    say a grant was used, with the trip id only, so you can still see
    that an agent read something before you deleted it."""
    get_trip_row(db, trip_id)
    item_ids = [r[0] for r in db.execute(
        "SELECT item_id FROM studio_items WHERE trip_id = ?", (trip_id,))]
    for iid in item_ids:
        db.execute("DELETE FROM studio_item_points WHERE item_id = ?", (iid,))
    for table in ("studio_items", "studio_story", "studio_edits", "studio_shares",
                  "studio_grants", "studio_proposals"):
        db.execute(f"DELETE FROM {table} WHERE trip_id = ?", (trip_id,))
    db.execute("DELETE FROM studio_redactions WHERE trip_id = ?", (trip_id,))
    _mark_exports_stale(db, trip_id, "the trip was deleted after this export")
    db.execute("DELETE FROM studio_trips WHERE trip_id = ?", (trip_id,))
    db.commit()


def purge_history(db, start_date, end_date):
    """Delete source history for a local-date range, everywhere.

    Removes visit, activity and raw-fix rows from the history itself, every
    trip item captured from them in every trip, their names from your
    writing, and the geocode cache entries for coordinates that no longer
    appear anywhere. Places with no remaining visits are deleted too.
    Returns counts so the caller can show exactly what went.
    """
    _validate_date(start_date, "start_date")
    _validate_date(end_date, "end_date")
    if end_date < start_date:
        raise StudioError("end_date is before start_date")
    counts = {}
    # Raw fixes carry only UTC; widen to the full span any time zone could
    # put on those local dates, then trim with the recorded rows' own UTC
    # ranges so neighboring days are not caught.
    spans = [(r["start_utc"], r["end_utc"]) for r in db.execute(
        "SELECT start_utc, end_utc FROM visits WHERE local_date BETWEEN ? AND ?"
        " UNION ALL SELECT start_utc, end_utc FROM activities"
        " WHERE local_date BETWEEN ? AND ?",
        (start_date, end_date, start_date, end_date))]
    lo_local = datetime.strptime(start_date, "%Y-%m-%d").astimezone()
    hi_local = datetime.strptime(end_date, "%Y-%m-%d").astimezone() + timedelta(days=1)
    spans.append((lo_local.astimezone(timezone.utc).strftime(UTC_FMT),
                  hi_local.astimezone(timezone.utc).strftime(UTC_FMT)))
    pts = 0
    for lo, hi in spans:
        pts += db.execute("DELETE FROM path_points WHERE ts_utc >= ? AND ts_utc <= ?",
                          (lo, hi)).rowcount
    counts["raw_fixes"] = pts
    touched_places = [r[0] for r in db.execute(
        "SELECT DISTINCT place_id FROM visits WHERE local_date BETWEEN ? AND ?",
        (start_date, end_date))]
    counts["visits"] = db.execute("DELETE FROM visits WHERE local_date BETWEEN ? AND ?",
                                  (start_date, end_date)).rowcount
    counts["activities"] = db.execute(
        "DELETE FROM activities WHERE local_date BETWEEN ? AND ?",
        (start_date, end_date)).rowcount
    removed_places = 0
    for pid in touched_places:
        left = db.execute("SELECT COUNT(*), MIN(start_utc), MAX(end_utc) FROM visits"
                          " WHERE place_id = ?", (pid,)).fetchone()
        if left[0] == 0:
            p = db.execute("SELECT lat, lng FROM places WHERE place_id = ?",
                           (pid,)).fetchone()
            db.execute("DELETE FROM places WHERE place_id = ?", (pid,))
            db.execute("DELETE FROM studio_place_labels WHERE place_id = ?", (pid,))
            if p:
                _drop_geocode_near(db, p["lat"], p["lng"])
            removed_places += 1
        else:
            db.execute("UPDATE places SET visit_count = ?, first_seen = ?, last_seen = ?"
                       " WHERE place_id = ?", (left[0], left[1], left[2], pid))
    counts["places_removed"] = removed_places

    # Notes you wrote about those days go too: a purge means nothing about
    # these days remains, not only nothing a sensor recorded.
    items = db.execute("SELECT * FROM studio_items WHERE local_date BETWEEN ? AND ?",
                       (start_date, end_date)).fetchall()
    trips = {}
    for it in items:
        trips.setdefault(it["trip_id"], []).append(it)
    for trip_id, its in trips.items():
        terms = []
        for it in its:
            terms.append(it["label"])
            if it["kind"] == "note":
                terms.append(it["caption"])
            still = None
            if it["kind"] == "visit":
                # Only scrub a place's name, and the routes into it, if no
                # surviving item in this trip still visits it.
                still = db.execute(
                    "SELECT 1 FROM studio_items WHERE trip_id = ? AND place_id = ?"
                    " AND NOT (local_date BETWEEN ? AND ?) LIMIT 1",
                    (trip_id, it["place_id"], start_date, end_date)).fetchone()
                if not still and it["source_label"] not in ("unnamed place",
                                                            "unidentified stop"):
                    terms.append(it["source_label"])
            if it["lat"] is not None and not still:
                _drop_geocode_near(db, it["lat"], it["lng"])
                if it["kind"] == "visit":
                    _trim_routes_near(db, trip_id, it["lat"], it["lng"])
        _scrub_terms_from_text(db, trip_id, terms)
        for it in its:
            db.execute("DELETE FROM studio_item_points WHERE item_id = ?", (it["item_id"],))
            db.execute("DELETE FROM studio_edits WHERE item_id = ?", (it["item_id"],))
            db.execute("DELETE FROM studio_proposals WHERE item_id = ?", (it["item_id"],))
            db.execute("DELETE FROM studio_items WHERE item_id = ?", (it["item_id"],))
        _mark_exports_stale(db, trip_id, "history in this trip was purged after this export")
    counts["trip_items"] = len(items)
    # Story text you wrote for purged dates is part of the history too.
    counts["story_days"] = db.execute(
        "DELETE FROM studio_story WHERE local_date BETWEEN ? AND ?",
        (start_date, end_date)).rowcount
    import studio_finder
    studio_finder.forget_dismissals_in(db, start_date, end_date)
    db.commit()
    return counts


def _drop_geocode_near(db, lat, lng):
    import geocode
    key_lat, key_lng = geocode.cache_key(lat, lng)
    db.execute("DELETE FROM geocode_cache WHERE lat_key = ? AND lng_key = ?",
               (key_lat, key_lng))


# ---------------------------------------------------------------- exports log

def _mark_exports_stale(db, trip_id, reason):
    if trip_id is None:
        db.execute("UPDATE studio_exports SET stale_reason = ? WHERE stale_reason IS NULL"
                   " AND removed_utc IS NULL", (reason,))
    else:
        db.execute("UPDATE studio_exports SET stale_reason = ? WHERE trip_id = ?"
                   " AND stale_reason IS NULL AND removed_utc IS NULL", (reason, trip_id))


def log_export(db, trip_id, fmt, profile_name, destination, content, path=None):
    eid = _new_id("x")
    db.execute("INSERT INTO studio_exports (export_id, trip_id, format, profile_name,"
               " destination, path, sha256, created_utc) VALUES (?,?,?,?,?,?,?,?)",
               (eid, trip_id, fmt, profile_name, destination, path,
                hashlib.sha256(content if isinstance(content, bytes)
                               else content.encode()).hexdigest(), now_utc()))
    db.commit()
    return eid


def list_exports(db, trip_id=None):
    q = "SELECT * FROM studio_exports"
    args = ()
    if trip_id:
        q += " WHERE trip_id = ?"
        args = (trip_id,)
    return [dict(r) for r in db.execute(q + " ORDER BY created_utc DESC", args)]


def remove_stale_exports(db):
    """Delete files the studio wrote that a later redaction or deletion
    outdated. Only files whose content still matches what the studio wrote
    are removed, so a file you edited by hand is reported, not destroyed.
    Downloads went to your browser and cannot be reached; they are listed
    so you know to delete them yourself."""
    removed, kept, unreachable = [], [], []
    for r in db.execute("SELECT * FROM studio_exports WHERE stale_reason IS NOT NULL"
                        " AND removed_utc IS NULL").fetchall():
        if r["destination"] != "file" or not r["path"]:
            unreachable.append(dict(r))
            continue
        if not os.path.exists(r["path"]):
            db.execute("UPDATE studio_exports SET removed_utc = ? WHERE export_id = ?",
                       (now_utc(), r["export_id"]))
            removed.append(r["path"])
            continue
        with open(r["path"], "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        if digest != r["sha256"]:
            kept.append({"path": r["path"],
                         "reason": "file changed since export; delete it yourself"})
            continue
        os.remove(r["path"])
        db.execute("UPDATE studio_exports SET removed_utc = ? WHERE export_id = ?",
                   (now_utc(), r["export_id"]))
        removed.append(r["path"])
    db.commit()
    return {"removed": removed, "kept": kept,
            "outside_studio": [{"export_id": u["export_id"], "format": u["format"],
                                "created_utc": u["created_utc"],
                                "destination": u["destination"]} for u in unreachable]}


# ---------------------------------------------------------------- profiles

def normalize_profile(profile):
    """Validate a disclosure profile, filling gaps from the safest preset."""
    if isinstance(profile, str):
        if profile not in PROFILES:
            raise StudioError(f"unknown profile {profile!r}; presets are "
                              f"{', '.join(PROFILES)}")
        return dict(PROFILES[profile])
    if not isinstance(profile, dict):
        raise StudioError("a profile is a preset name or an object")
    base = dict(PROFILES.get(profile.get("base", "public"), PROFILES["public"]))
    for k, v in profile.items():
        if k == "base":
            continue
        if k not in base:
            raise StudioError(f"unknown profile field {k!r}")
        base[k] = v
    if base["times"] not in TIME_LEVELS:
        raise StudioError(f"times must be one of {', '.join(TIME_LEVELS)}")
    if base["coordinates"] not in COORD_LEVELS:
        raise StudioError(f"coordinates must be one of {', '.join(COORD_LEVELS)}")
    for flag in ("routes", "captions", "story", "movement", "sources"):
        base[flag] = bool(base[flag])
    if base["days"] is not None:
        if not isinstance(base["days"], list):
            raise StudioError("days must be a list of YYYY-MM-DD dates or null")
        base["days"] = [_validate_date(x) for x in base["days"]]
    base["name"] = str(base.get("name") or "custom")[:40]
    return base


# ---------------------------------------------------------------- shares

def create_share(db, trip_id, audience, profile):
    get_trip_row(db, trip_id)
    audience = (audience or "").strip()
    if not audience:
        raise StudioError("say who the share is for")
    p = normalize_profile(profile)
    sid = "s_" + secrets.token_urlsafe(12)
    db.execute("INSERT INTO studio_shares VALUES (?,?,?,?,?,?)",
               (sid, trip_id, audience[:80], json.dumps(p), now_utc(), None))
    db.commit()
    return sid


def list_shares(db, trip_id):
    return [dict(r) | {"profile": json.loads(r["profile"])} for r in db.execute(
        "SELECT * FROM studio_shares WHERE trip_id = ? ORDER BY created_utc", (trip_id,))]


def revoke_share(db, share_id):
    n = db.execute("UPDATE studio_shares SET revoked_utc = ? WHERE share_id = ?"
                   " AND revoked_utc IS NULL", (now_utc(), share_id)).rowcount
    db.commit()
    if not n:
        raise StudioError("no active share with that id")


def get_share(db, share_id):
    r = db.execute("SELECT * FROM studio_shares WHERE share_id = ?", (share_id,)).fetchone()
    if r is None:
        raise StudioError("no such share")
    if r["revoked_utc"]:
        raise StudioError("this share was revoked")
    return dict(r) | {"profile": json.loads(r["profile"])}


# ---------------------------------------------------------------- grants

def create_grant(db, trip_id, agent, capabilities=("read",), profile="agent",
                 hours=24):
    """Let one agent read one trip, through one disclosure profile, until
    an expiry. Returns the token once; only its hash is stored."""
    get_trip_row(db, trip_id)
    agent = (agent or "").strip()
    if not agent:
        raise StudioError("name the agent this grant is for")
    caps = sorted(set(capabilities))
    if not caps or any(c not in CAPABILITIES for c in caps):
        raise StudioError(f"capabilities must be from {', '.join(CAPABILITIES)}")
    if "read" not in caps:
        raise StudioError("every grant includes read")
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        raise StudioError("hours must be a number")
    if not 0 < hours <= 24 * 30:
        raise StudioError("a grant lasts between a few minutes and 30 days")
    p = normalize_profile(profile)
    token = "tlg_" + secrets.token_urlsafe(24)
    gid = _new_id("g")
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime(UTC_FMT)
    db.execute("INSERT INTO studio_grants VALUES (?,?,?,?,?,?,?,?,?)",
               (gid, hashlib.sha256(token.encode()).hexdigest(), trip_id, agent[:80],
                json.dumps(caps), json.dumps(p), expires, now_utc(), None))
    db.commit()
    return {"grant_id": gid, "token": token, "expires_utc": expires}


def list_grants(db, trip_id=None):
    q, args = "SELECT * FROM studio_grants", ()
    if trip_id:
        q, args = q + " WHERE trip_id = ?", (trip_id,)
    out = []
    for r in db.execute(q + " ORDER BY created_utc DESC", args):
        d = dict(r)
        d.pop("token_sha256")
        d["capabilities"] = json.loads(d["capabilities"])
        d["profile"] = json.loads(d["profile"])
        d["active"] = d["revoked_utc"] is None and d["expires_utc"] > now_utc()
        out.append(d)
    return out


def revoke_grant(db, grant_id):
    """End a grant. Its pending proposals are withdrawn with it: revoking
    an agent should not leave its words one click from your story."""
    ts = now_utc()
    n = db.execute("UPDATE studio_grants SET revoked_utc = ? WHERE grant_id = ?"
                   " AND revoked_utc IS NULL", (ts, grant_id)).rowcount
    if n:
        db.execute("UPDATE studio_proposals SET status = 'withdrawn', decided_utc = ?"
                   " WHERE grant_id = ? AND status = 'pending'", (ts, grant_id))
    db.commit()
    if not n:
        raise StudioError("no active grant with that id")


def resolve_grant(db, token, tool, need="read"):
    """Return the active grant for a token, or raise, logging either way."""
    digest = hashlib.sha256((token or "").encode()).hexdigest()
    r = db.execute("SELECT * FROM studio_grants WHERE token_sha256 = ?",
                   (digest,)).fetchone()
    if r is None:
        _log_access(db, None, None, tool, "refused: unknown grant")
        raise StudioError("no grant matches this token; ask the person to create one"
                          " in the history studio")
    caps = json.loads(r["capabilities"])
    if r["revoked_utc"]:
        outcome, msg = "refused: revoked", "this grant was revoked"
    elif r["expires_utc"] <= now_utc():
        outcome, msg = "refused: expired", "this grant expired"
    elif need not in caps:
        outcome, msg = f"refused: lacks {need}", f"this grant does not allow {need}"
    else:
        _log_access(db, r["grant_id"], r["trip_id"], tool, "allowed")
        return dict(r) | {"capabilities": caps, "profile": json.loads(r["profile"])}
    _log_access(db, r["grant_id"], r["trip_id"], tool, outcome)
    raise StudioError(msg)


def _log_access(db, grant_id, trip_id, tool, outcome):
    db.execute("INSERT INTO studio_access_log (grant_id, trip_id, tool, outcome, at_utc)"
               " VALUES (?,?,?,?,?)", (grant_id, trip_id, tool, outcome, now_utc()))
    db.commit()


def access_log(db, trip_id=None, limit=200):
    q, args = "SELECT * FROM studio_access_log", ()
    if trip_id:
        q, args = q + " WHERE trip_id = ?", (trip_id,)
    return [dict(r) for r in db.execute(q + " ORDER BY id DESC LIMIT ?", args + (limit,))]


# ---------------------------------------------------------------- proposals

def propose(db, grant, field, value, item_id=None, local_date=None):
    """An agent's suggested caption or story text, held for your decision."""
    value = (value or "").strip()
    if not value:
        raise StudioError("a proposal cannot be empty")
    if len(value) > 4000:
        raise StudioError("a proposal is limited to 4000 characters")
    if field == "caption":
        item = get_item_row(db, item_id)
        if item["trip_id"] != grant["trip_id"]:
            raise StudioError("that item is outside this grant")
        if item["hidden"]:
            raise StudioError("no such item in this trip")
    elif field == "story":
        trip = get_trip_row(db, grant["trip_id"])
        _validate_date(local_date)
        if not trip["start_date"] <= local_date <= trip["end_date"]:
            raise StudioError("that date is outside the trip")
    else:
        raise StudioError("field must be caption or story")
    pid = _new_id("p")
    db.execute("INSERT INTO studio_proposals VALUES (?,?,?,?,?,?,?,?,?,?)",
               (pid, grant["grant_id"], grant["trip_id"], item_id, field, local_date,
                value, "pending", now_utc(), None))
    db.commit()
    return pid


def list_proposals(db, trip_id, status=None):
    q = "SELECT * FROM studio_proposals WHERE trip_id = ?"
    args = (trip_id,)
    if status:
        q, args = q + " AND status = ?", args + (status,)
    return [dict(r) for r in db.execute(q + " ORDER BY created_utc", args)]


def decide_proposal(db, proposal_id, accept):
    p = db.execute("SELECT * FROM studio_proposals WHERE proposal_id = ?",
                   (proposal_id,)).fetchone()
    if p is None or p["status"] != "pending":
        raise StudioError("no pending proposal with that id")
    if accept:
        g = db.execute("SELECT revoked_utc, expires_utc FROM studio_grants"
                       " WHERE grant_id = ?", (p["grant_id"],)).fetchone()
        if g is None or g["revoked_utc"] or g["expires_utc"] <= now_utc():
            raise StudioError("the grant behind this proposal has ended; reject it"
                              " or write the text yourself")
    if accept:
        if p["field"] == "caption":
            edit_item(db, p["item_id"], caption=p["value"])
        else:
            set_story(db, p["trip_id"], p["local_date"], p["value"])
    db.execute("UPDATE studio_proposals SET status = ?, decided_utc = ?"
               " WHERE proposal_id = ?",
               ("accepted" if accept else "rejected", now_utc(), proposal_id))
    db.commit()


# ---------------------------------------------------------------- CLI

def _copy_or_save(token, grant_id):
    """Hand a grant token to the person without printing it to a log."""
    import shutil
    import subprocess
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=token.encode(), check=True)
            return f"token copied to the clipboard ({cmd[0]})"
    path = os.path.abspath(f"{grant_id}.token")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return f"token written to {path} (owner-only); delete it once configured"


def main(argv=None):
    import studio_export
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=os.environ.get("TIMELINE_DB", "timeline.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("trips")
    c = sub.add_parser("create")
    c.add_argument("title"), c.add_argument("start_date"), c.add_argument("end_date")
    s = sub.add_parser("show")
    s.add_argument("trip_id")
    s.add_argument("--profile", default="owner")
    e = sub.add_parser("export")
    e.add_argument("trip_id")
    e.add_argument("--format", default="md", choices=sorted(studio_export.EXPORTERS))
    e.add_argument("--profile", default="owner")
    e.add_argument("-o", "--out", required=True)
    n = sub.add_parser("name-place")
    n.add_argument("place_id"), n.add_argument("label")
    r = sub.add_parser("redact")
    r.add_argument("kind", choices=REDACTION_KINDS)
    r.add_argument("--target"), r.add_argument("--trip")
    r.add_argument("--lat", type=float), r.add_argument("--lng", type=float)
    r.add_argument("--radius-m", type=float)
    r.add_argument("--action", default="mask", choices=REDACTION_ACTIONS)
    r.add_argument("--replacement")
    g = sub.add_parser("grant")
    g.add_argument("trip_id"), g.add_argument("agent")
    g.add_argument("--suggest", action="store_true")
    g.add_argument("--profile", default="agent")
    g.add_argument("--hours", type=float, default=24)
    p = sub.add_parser("purge")
    p.add_argument("start_date"), p.add_argument("end_date")
    p.add_argument("--yes", action="store_true")
    sub.add_parser("stale-exports")
    sub.add_parser("remove-stale-exports")
    a = ap.parse_args(argv)
    db = connect(a.db)
    try:
        if a.cmd == "trips":
            for t in list_trips(db):
                print(f"{t['trip_id']}  {t['start_date']}..{t['end_date']}  "
                      f"{t['items']:>3} items  {t['title']}")
        elif a.cmd == "create":
            out = create_trip(db, a.title, a.start_date, a.end_date)
            print(f"created {out['trip_id']} with {out['items']} items")
        elif a.cmd == "show":
            print(studio_export.render(db, a.trip_id, "md", a.profile))
        elif a.cmd == "export":
            content = studio_export.render(db, a.trip_id, a.format, a.profile)
            mode = "wb" if isinstance(content, bytes) else "w"
            with open(a.out, mode) as f:
                f.write(content)
            log_export(db, a.trip_id, a.format, normalize_profile(a.profile)["name"],
                       "file", content, os.path.abspath(a.out))
            print(f"wrote {a.out}")
        elif a.cmd == "name-place":
            name_place(db, a.place_id, a.label)
            print("named")
        elif a.cmd == "redact":
            rid = add_redaction(db, a.kind, a.action, a.trip, a.target, a.lat, a.lng,
                                a.radius_m, a.replacement)
            print(f"added redaction {rid}")
        elif a.cmd == "grant":
            caps = ["read", "suggest"] if a.suggest else ["read"]
            out = create_grant(db, a.trip_id, a.agent, caps, a.profile, a.hours)
            print(f"grant {out['grant_id']} until {out['expires_utc']}; "
                  + _copy_or_save(out["token"], out["grant_id"]))
            print("configure the MCP server with TIMELINE_GRANT=<token>")
        elif a.cmd == "purge":
            if not a.yes:
                sys.exit("purge deletes history permanently; rerun with --yes")
            print(json.dumps(purge_history(db, a.start_date, a.end_date), indent=2))
        elif a.cmd == "stale-exports":
            for x in list_exports(db):
                if x["stale_reason"] and not x["removed_utc"]:
                    print(f"{x['export_id']}  {x['format']:8} {x['destination']:8} "
                          f"{x['path'] or '(outside the studio)'}  {x['stale_reason']}")
        elif a.cmd == "remove-stale-exports":
            print(json.dumps(remove_stale_exports(db), indent=2))
    except StudioError as err:
        sys.exit(f"error: {err}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
