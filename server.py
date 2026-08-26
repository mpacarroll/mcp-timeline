#!/usr/bin/env python3
"""MCP server for a Google Maps Timeline SQLite index.

Build the database with ingest.py first, then register with any MCP client.
Claude Code:

    claude mcp add timeline -e TIMELINE_DB=/path/to/timeline.db -- \
        /path/to/.venv/bin/python /path/to/server.py

All tools are read-only. Tool docstrings are the API surface an LLM sees;
edit them with the same care as user-facing copy.
"""

import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import dwell
from geo import haversine_m
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

DB_PATH = os.environ.get("TIMELINE_DB", "timeline.db")

MODES = ["walking", "in subway", "in passenger vehicle", "in bus", "in train",
         "in tram", "cycling", "flying", "running", "swimming", "hiking",
         "strength_training", "yoga", "elliptical", "rowing"]

server = MCPServer(
    name="timeline",
    instructions="Personal location history: place visits and movement "
                 "segments ingested from a Google Maps Timeline export. "
                 "Dates are the user's local days (YYYY-MM-DD).")


def _db():
    if not os.path.exists(DB_PATH):
        raise RuntimeError(
            f"No database at {DB_PATH}. Run ingest.py first or set TIMELINE_DB.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _label(row):
    return row["resolved_name"] or row["semantic_type"] or "unnamed place"


def _local_day_bounds_utc(date):
    """UTC range covering one local calendar day.

    path_points stores UTC only, because not every source reports a local
    offset. The server runs on the machine the data belongs to, so its
    system timezone is the right one to interpret "2026-03-01" with.
    """
    start_local = datetime.strptime(date, "%Y-%m-%d").astimezone()
    end_local = start_local + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (start_local.astimezone(timezone.utc).strftime(fmt),
            end_local.astimezone(timezone.utc).strftime(fmt))


def _nearest_place(db, lat, lng, radius_m=150):
    """Label a derived stay with a known place, when one is close enough.

    Raw fixes carry no place identity, but the places table already knows
    where Home and Work are. Reusing it turns "you were at 40.7128,
    -74.0060" into "you were at Home" at no extra cost.
    """
    dlat = radius_m / 111320.0
    dlng = radius_m / (111320.0 * max(math.cos(math.radians(lat)), 0.01))
    best, best_d = None, None
    for r in db.execute(
            "SELECT * FROM places WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
            (lat - dlat, lat + dlat, lng - dlng, lng + dlng)):
        d = haversine_m(lat, lng, r["lat"], r["lng"])
        if d <= radius_m and (best_d is None or d < best_d):
            best, best_d = r, d
    return (_label(best), best["place_id"]) if best is not None else (None, None)


def _derive_day_from_fixes(db, date):
    """Rebuild a day from raw location fixes when no source recorded it.

    Google Timeline wrote visit and activity rows because it did its own
    inference. A continuous feed like OwnTracks only leaves breadcrumbs, so
    days covered by it alone would otherwise read as "no data" even with
    thousands of fixes on file.
    """
    start_utc, end_utc = _local_day_bounds_utc(date)
    points = [(datetime.strptime(r["ts_utc"], "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=timezone.utc), r["lat"], r["lng"])
              for r in db.execute(
                  "SELECT ts_utc, lat, lng FROM path_points"
                  " WHERE ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc",
                  (start_utc, end_utc))]
    if not points:
        return [], 0

    entries = []
    for e in dwell.derive_entries(points, gap_min=60):
        local_start = e["start"].astimezone()
        local_end = e["end"].astimezone()
        entry = {"start": local_start.strftime("%H:%M:%S"),
                 "end": local_end.strftime("%H:%M:%S"),
                 "duration_min": round(e["duration_min"])}
        if e["type"] == "stay":
            label, place_id = _nearest_place(db, e["lat"], e["lng"])
            entry.update({"type": "visit", "place": label or "unrecognized place",
                          "lat": e["lat"], "lng": e["lng"]})
            if place_id:
                entry["place_id"] = place_id
        else:
            entry.update({"type": "movement", "mode": "unknown", "km": e["km"]})
        entries.append(entry)
    return entries, len(points)


@server.tool()
def activity_stats(mode: str, start_date: str, end_date: str,
                   group_by: str = "month") -> dict[str, Any]:
    """Summarize movement for one transport mode or workout type over a
    local-date range.

    Use for questions like "how much did I walk in March" or "average subway
    time per week". mode is commonly one of: walking, in subway, in
    passenger vehicle, in bus, in train, in tram, cycling, flying (Google
    Timeline's inferred segments), or running, swimming, hiking,
    strength_training, yoga, elliptical, rowing (Apple Health workouts).
    Other values are accepted too, since new data sources add new modes over
    time; an unrecognized or empty-for-this-range mode returns a valid
    result with zero totals and a note listing modes that do have data,
    rather than an error. start_date and end_date are inclusive YYYY-MM-DD
    local dates; group_by is day, week, or month. Returns totals (km, hours,
    active days) plus one row per period. Not for "where was I" questions;
    use day_summary for those.
    """
    group_expr = {"day": "local_date",
                  "week": "strftime('%Y-W%W', local_date)",
                  "month": "substr(local_date, 1, 7)"}.get(group_by)
    if group_expr is None:
        raise ValueError("group_by must be day, week, or month")

    db = _db()
    totals = db.execute(
        "SELECT COUNT(*) segments, COUNT(DISTINCT local_date) active_days,"
        " COALESCE(SUM(distance_m), 0) m, COALESCE(SUM(duration_min), 0) min"
        " FROM activities WHERE mode = ? AND local_date BETWEEN ? AND ?",
        (mode, start_date, end_date)).fetchone()
    periods = [
        {"period": r["p"], "km": round(r["m"] / 1000, 1),
         "hours": round(r["min"] / 60, 1), "segments": r["n"]}
        for r in db.execute(
            f"SELECT {group_expr} p, SUM(distance_m) m, SUM(duration_min) min,"
            f" COUNT(*) n FROM activities"
            " WHERE mode = ? AND local_date BETWEEN ? AND ?"
            f" GROUP BY {group_expr} ORDER BY p",
            (mode, start_date, end_date))]
    result = {"mode": mode, "start_date": start_date, "end_date": end_date,
              "total_km": round(totals["m"] / 1000, 1),
              "total_hours": round(totals["min"] / 60, 1),
              "active_days": totals["active_days"],
              "avg_min_per_active_day":
                  round(totals["min"] / totals["active_days"], 1)
                  if totals["active_days"] else 0,
              "by_period": periods}
    if not totals["segments"]:
        known = [r["mode"] for r in
                 db.execute("SELECT DISTINCT mode FROM activities ORDER BY mode")]
        result["note"] = (f"no data for mode {mode!r} in this range; modes "
                           f"with data in the database: {', '.join(known) or 'none'}")
    db.close()
    return result


@server.tool()
def day_summary(date: str) -> dict[str, Any]:
    """Chronological story of one local day: place visits and movement.

    Use for "where was I on 2026-03-14" or for daily-recap context. date is
    YYYY-MM-DD. Returns an ordered timeline; visits carry a place label where
    one is known (Home, Work, a resolved name) and movement segments carry
    mode and distance. A date with no data returns a valid empty timeline
    with a note, never an error.

    Always check the derived_from field before describing the result, and
    say which kind of day it was if the distinction matters:

    - "recorded visits and activities" means a source did its own inference
      (a Google Timeline export), so place names and transport modes are
      that source's judgment.
    - a raw-fix description means the day was reconstructed here from
      continuous location fixes, by grouping them into stays and movement
      at query time. Those entries carry coordinates, and a place name only
      when the stay is within 150m of an already-known place. Transport
      mode is "unknown" for these, because raw coordinates do not say
      whether you walked or drove. Do not invent one.
    """
    db = _db()
    entries = []
    for r in db.execute(
            "SELECT v.start_local, v.end_local, v.start_utc, v.duration_min,"
            " v.place_id, p.resolved_name, p.semantic_type, p.visit_count"
            " FROM visits v JOIN places p USING (place_id)"
            " WHERE v.local_date = ?", (date,)):
        entries.append({
            "type": "visit", "place": _label(r), "place_id": r["place_id"],
            "start": r["start_local"], "end": r["end_local"],
            "duration_min": round(r["duration_min"]),
            "sort": r["start_utc"]})
    for r in db.execute(
            "SELECT start_local, end_local, start_utc, mode, duration_min,"
            " distance_m FROM activities WHERE local_date = ?", (date,)):
        entries.append({
            "type": "movement", "mode": r["mode"],
            "start": r["start_local"], "end": r["end_local"],
            "duration_min": round(r["duration_min"]),
            "km": round((r["distance_m"] or 0) / 1000, 2),
            "sort": r["start_utc"]})
    entries.sort(key=lambda e: e.pop("sort"))
    result = {"date": date, "entries": entries}
    if entries:
        result["derived_from"] = "recorded visits and activities"
    else:
        # No source wrote visit or activity rows for this day. Raw fixes
        # may still cover it, which is the normal case for any day after
        # the last Timeline export.
        derived, fix_count = _derive_day_from_fixes(db, date)
        if derived:
            result["entries"] = derived
            result["derived_from"] = (
                f"{fix_count} raw location fixes, grouped into stays and "
                "movement at query time; place names come from matching "
                "known places within 150m")
        else:
            result["note"] = "no data recorded for this date"
    db.close()
    return result


@server.tool()
def visits_near(lat: float, lng: float, radius_m: int = 100) -> list[dict]:
    """Places the user has actually visited within radius_m meters of a point.

    Use for "have I been here before". A visit means dwell time at a place;
    walking or driving past does not count. Returns matches sorted nearest
    first, each with visit_count and first/last visit dates. An empty list
    means no known visits that close; try a larger radius before concluding
    the user has never been there.
    """
    dlat = radius_m / 111320.0
    dlng = radius_m / (111320.0 * max(math.cos(math.radians(lat)), 0.01))
    db = _db()
    out = []
    for r in db.execute(
            "SELECT * FROM places WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
            (lat - dlat, lat + dlat, lng - dlng, lng + dlng)):
        d = haversine_m(lat, lng, r["lat"], r["lng"])
        if d <= radius_m:
            out.append({"place_id": r["place_id"], "label": _label(r),
                        "distance_m": round(d), "visit_count": r["visit_count"],
                        "first_visit": r["first_seen"][:10],
                        "last_visit": r["last_seen"][:10]})
    db.close()
    return sorted(out, key=lambda p: p["distance_m"])


@server.tool()
def place_history(place: str, limit: int = 20) -> dict[str, Any]:
    """Every recorded visit to one place, looked up by place_id or by name.

    place may be an exact place_id (starts with ChIJ) or a name matched
    case-insensitively against resolved names and labels such as Home or
    Work. If a name matches several places, returns the candidates instead
    of guessing; call again with a place_id to disambiguate. Returns total
    visit count, first and last visit, and the most recent visits up to
    limit (default 20).
    """
    db = _db()
    rows = db.execute("SELECT * FROM places WHERE place_id = ?", (place,)).fetchall()
    if not rows:
        rows = db.execute(
            "SELECT * FROM places WHERE resolved_name LIKE ? OR semantic_type LIKE ?"
            " ORDER BY visit_count DESC", (f"%{place}%", f"%{place}%")).fetchall()
    if not rows:
        db.close()
        return {"query": place, "matches": 0,
                "note": "no place matched; try visits_near with coordinates"}
    if len(rows) > 1:
        cands = [{"place_id": r["place_id"], "label": _label(r),
                  "visit_count": r["visit_count"]} for r in rows]
        db.close()
        return {"query": place, "matches": len(rows), "candidates": cands,
                "note": "several places matched; call again with a place_id"}
    p = rows[0]
    visits = [{"date": r["local_date"], "start": r["start_local"],
               "end": r["end_local"], "duration_min": round(r["duration_min"])}
              for r in db.execute(
                  "SELECT * FROM visits WHERE place_id = ?"
                  " ORDER BY start_utc DESC LIMIT ?", (p["place_id"], limit))]
    db.close()
    return {"place_id": p["place_id"], "label": _label(p),
            "visit_count": p["visit_count"],
            "first_visit": p["first_seen"][:10], "last_visit": p["last_seen"][:10],
            "recent_visits": visits}


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        # No application-level auth here on purpose: the SDK's token_verifier
        # cannot be used standalone (it requires full AuthSettings, i.e. a real
        # OAuth issuer with discovery/resource metadata endpoints), which is
        # more infrastructure than a single-user personal tool needs. Access
        # control belongs at the network layer instead (e.g. Cloudflare Access
        # in front of the tunnel exposing this port) rather than fought into
        # this file. Never expose this transport without something in front
        # of it gating access; there is nothing in this process doing that.
        host = os.environ.get("MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("MCP_PORT", "8000"))

        # The SDK's own DNS-rebinding protection rejects any Host header
        # other than localhost variants when bound to 127.0.0.1 -- correct
        # by default, but it also blocks the public hostname a tunnel
        # forwards once one is actually in front of this process. Widen the
        # allowlist to include it rather than disabling the protection.
        public_host = os.environ.get("MCP_PUBLIC_HOST")
        transport_security = None
        if public_host:
            transport_security = TransportSecuritySettings(
                allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", public_host],
                allowed_origins=["http://127.0.0.1:*", "http://localhost:*",
                                  "http://[::1]:*", f"https://{public_host}"])

        server.run("streamable-http", host=host, port=port,
                   transport_security=transport_security)
    else:
        server.run("stdio")
