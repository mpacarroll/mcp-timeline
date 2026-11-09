"""One way out of the studio: build_view(), then a serializer.

Every export format, every share page and every agent tool call is built
from the dict build_view() returns, and serializers never touch the
database. That single choke point is what makes the redaction promise
checkable: tests/test_studio_export.py renders every format under every
preset and asserts a redacted or deleted fact appears in none of them.

Order of operations inside build_view(), each step narrowing the last:

1. Hidden items drop out (you hid them in the editor).
2. Redactions: remove drops an item; mask keeps a neutral entry with no
   name, caption, coordinates or route. Movements that start or end at a
   redacted stop lose that endpoint and their whole route, because a
   route that ends at the clinic is the clinic. Route points inside a
   redacted area are dropped wherever they occur.
3. Names of anything redacted are scrubbed from free text: your story,
   captions, notes and the trip title. Writing "dermatology appointment
   at Lumen" in a story and then redacting Lumen must not leave Lumen
   in the story.
4. The disclosure profile trims what remains: which days, time
   precision, coordinate precision, routes, captions, story, movement,
   provenance.
"""

import csv
import html
import io
import json
import re
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

import studio
from geo import haversine_m

COORD_DECIMALS = {"exact": 5, "neighborhood": 2, "city": 1}
ENDPOINT_MATCH_M = 200
# A stop with no place record (rebuilt from raw fixes) this close to a
# redacted place is that place: "redact the clinic" must cover the day
# OwnTracks saw you there as well as the day Timeline named it.
PLACE_MATCH_M = 150


# ---------------------------------------------------------------- redactions

def load_redactions(db, trip_id):
    return db.execute(
        "SELECT * FROM studio_redactions WHERE trip_id IS NULL OR trip_id = ?",
        (trip_id,)).fetchall()


def _inside(r, lat, lng):
    return (lat is not None and lng is not None
            and haversine_m(lat, lng, r["lat"], r["lng"]) <= r["radius_m"])


def place_points(db, redactions):
    """{place_id: (lat, lng)} for every place redaction whose location is
    known, from the places table or, after a purge removed that row, from
    any captured item that still carries it."""
    out = {}
    for r in redactions:
        if r["kind"] != "place" or r["target"] in out:
            continue
        row = db.execute("SELECT lat, lng FROM places WHERE place_id = ?",
                         (r["target"],)).fetchone()
        if row is None or row["lat"] is None:
            row = db.execute("SELECT lat, lng FROM studio_items WHERE place_id = ?"
                             " AND lat IS NOT NULL LIMIT 1", (r["target"],)).fetchone()
        if row is not None and row["lat"] is not None:
            out[r["target"]] = (row["lat"], row["lng"])
    return out


def matching_redactions(redactions, item, place_pts=None):
    """Redactions that apply to an item as a whole (not just its route)."""
    out = []
    place_pts = place_pts or {}
    for r in redactions:
        k = r["kind"]
        if k == "item" and r["target"] == item["item_id"]:
            out.append(r)
        elif k == "date" and r["target"] == item["local_date"]:
            out.append(r)
        elif k == "place" and item.get("place_id") and r["target"] == item["place_id"]:
            out.append(r)
        elif (k == "place" and item["kind"] == "visit" and not item.get("place_id")
              and r["target"] in place_pts and item["lat"] is not None
              and haversine_m(item["lat"], item["lng"], *place_pts[r["target"]])
              <= PLACE_MATCH_M):
            out.append(r)
        elif k == "area" and item["kind"] == "visit" and _inside(r, item["lat"], item["lng"]):
            out.append(r)
    return out


def _strongest(matches):
    """remove beats mask; among equals the earliest wins, so the label a
    person sees is stable as redactions are added."""
    removes = [m for m in matches if m["action"] == "remove"]
    return removes[0] if removes else matches[0]


def _place_terms(db, place_id):
    terms = []
    r = db.execute("SELECT label FROM studio_place_labels WHERE place_id = ?",
                   (place_id,)).fetchone()
    if r:
        terms.append(r["label"])
    p = db.execute("SELECT resolved_name FROM places WHERE place_id = ?",
                   (place_id,)).fetchone()
    if p and p["resolved_name"]:
        terms.append(p["resolved_name"])
    return terms


def _scrubber(pairs):
    """Build a function replacing each secret term with its replacement.
    Terms shorter than three characters are skipped: scrubbing "Al" out of
    every sentence would wreck the story without protecting anything."""
    pairs = sorted({(t.strip(), rep) for t, rep in pairs if t and len(t.strip()) >= 3},
                   key=lambda p: len(p[0]), reverse=True)
    if not pairs:
        return lambda s: s
    lookup = {t.lower(): rep for t, rep in pairs}
    pattern = re.compile("|".join(re.escape(t) for t, _ in pairs), re.IGNORECASE)

    def scrub(s):
        if not s:
            return s
        return pattern.sub(lambda m: lookup[m.group(0).lower()], s)
    return scrub


# ---------------------------------------------------------------- disclosure

def _part_of_day(local_iso):
    h = int(local_iso[11:13])
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 21:
        return "evening"
    return "night"


def route_ends(it):
    """A movement's two ends: its recorded endpoints, or, for a movement
    rebuilt from raw fixes (which has none), the first and last route
    point. Without the fallback a raw route into a redacted stop would
    survive simply because nobody stored where it ended."""
    pts = it.get("points") or []
    start = ((it["lat"], it["lng"]) if it["lat"] is not None
             else (pts[0]["lat"], pts[0]["lng"]) if pts else None)
    end = ((it["end_lat"], it["end_lng"]) if it["end_lat"] is not None
           else (pts[-1]["lat"], pts[-1]["lng"]) if pts else None)
    return start, end


def _round(v, level):
    if v is None or level == "none":
        return None
    return round(v, COORD_DECIMALS[level])


def _day_offset(start_local, end_local):
    if not start_local or not end_local:
        return 0
    return (datetime.strptime(end_local[:10], "%Y-%m-%d")
            - datetime.strptime(start_local[:10], "%Y-%m-%d")).days


# ---------------------------------------------------------------- the view

def build_view(db, trip_id, profile="owner"):
    """The only representation of a trip that ever leaves the studio."""
    p = studio.normalize_profile(profile)
    trip = studio.get_trip_row(db, trip_id)
    items = studio.items_for_trip(db, trip_id)
    redactions = load_redactions(db, trip_id)
    areas = [r for r in redactions if r["kind"] == "area"]
    place_pts = place_points(db, redactions)

    secret_pairs = []
    for r in redactions:
        if r["kind"] == "place":
            said = r["replacement"] if r["action"] == "mask" else "[removed]"
            secret_pairs += [(t, said) for t in _place_terms(db, r["target"])]

    kept, redacted_count = [], 0
    # (lat, lng) of stops whose location must not leak. Redacted places
    # count even when this trip never visits them: a trip that starts with
    # the drive home from the clinic still ends a route at the clinic.
    redacted_stops = list(place_pts.values())
    for it in items:
        if it["hidden"]:
            continue
        matches = matching_redactions(redactions, it, place_pts)
        if matches:
            r = _strongest(matches)
            redacted_count += 1
            # A removed stop leaves no placeholder entry, so a sentence you
            # wrote about it gets a neutral marker rather than a label that
            # implies a hidden place.
            said = r["replacement"] if r["action"] == "mask" else "[removed]"
            for t in (it["source_label"], it["label"]):
                if it["kind"] == "visit" and t not in ("unnamed place",
                                                        "unidentified stop"):
                    secret_pairs.append((t, said))
            if it["kind"] == "note" and it["caption"]:
                secret_pairs.append((it["caption"], said))
            if it["kind"] == "visit" and it["lat"] is not None:
                redacted_stops.append((it["lat"], it["lng"]))
            if r["action"] == "remove":
                continue
            it = dict(it, label=r["replacement"], source_label=r["replacement"],
                      caption=None, place_id=None, lat=None, lng=None, end_lat=None,
                      end_lng=None, points=[], masked=True, label_source="redacted")
        kept.append(dict(it))

    # Movements touching a redacted stop or area lose that endpoint and
    # their route; stray route points inside an area are dropped anywhere.
    for it in kept:
        if it["kind"] != "movement" or it.get("masked"):
            continue
        def hidden_at(lat, lng):
            return (any(haversine_m(lat, lng, s[0], s[1]) <= ENDPOINT_MATCH_M
                        for s in redacted_stops)
                    or any(_inside(a, lat, lng) for a in areas))

        touched = False
        for end, cols in zip(route_ends(it), (("lat", "lng"), ("end_lat", "end_lng"))):
            if end is not None and hidden_at(*end):
                it[cols[0]] = it[cols[1]] = None
                touched = True
        if touched:
            it["points"] = []
        else:
            it["points"] = [pt for pt in it["points"]
                            if not hidden_at(pt["lat"], pt["lng"])]

    scrub = _scrubber(secret_pairs)
    for it in kept:
        it["label"] = scrub(it["label"])
        it["source_label"] = scrub(it["source_label"])
        it["caption"] = scrub(it["caption"])

    # A redacted day takes the story you wrote for it along: the story is
    # the most revealing thing about that day. A masked day falls back to
    # the auto-draft of its masked entries; a removed day disappears.
    date_redactions = {}
    for r in redactions:
        if r["kind"] == "date":
            prev = date_redactions.get(r["target"])
            date_redactions[r["target"]] = _strongest([x for x in (prev, r) if x])
    stories = {r["local_date"]: scrub(r["body"]) for r in db.execute(
        "SELECT local_date, body FROM studio_story WHERE trip_id = ?", (trip_id,))
        if r["local_date"] not in date_redactions}

    # Disclosure.
    allowed_days = set(p["days"]) if p["days"] is not None else None
    days = {}
    for it in kept:
        if allowed_days is not None and it["local_date"] not in allowed_days:
            continue
        if it["kind"] == "movement" and not p["movement"]:
            continue
        days.setdefault(it["local_date"], []).append(_disclose_item(it, p))

    all_dates = sorted(set(days) | ({d for d in stories
                                      if allowed_days is None or d in allowed_days}
                                     if p["story"] else set()))
    out_days = []
    for date in all_dates:
        if not trip["start_date"] <= date <= trip["end_date"]:
            continue
        if date_redactions.get(date, {"action": None})["action"] == "remove":
            continue
        entries = days.get(date, [])
        day = {"date": date, "items": entries}
        if p["story"]:
            if date in stories:
                day["story"], day["story_source"] = stories[date], "written by you"
            else:
                day["story"], day["story_source"] = auto_story(entries), "auto-drafted"
        out_days.append(day)

    return {
        "trip": {"title": scrub(trip["title"]),
                 "summary": scrub(trip["summary"]) if p["story"] else None,
                 "start_date": trip["start_date"], "end_date": trip["end_date"]},
        "days": out_days,
        "disclosure": {k: p[k] for k in ("name", "times", "coordinates", "routes",
                                         "captions", "story", "movement", "sources",
                                         "days")},
        # A count only. Listing what was redacted would itself disclose it.
        "redacted_items": redacted_count,
        "generated_utc": studio.now_utc(),
    }


def _disclose_item(it, p):
    out = {"ref": it["item_id"], "kind": it["kind"],
           "label": it["label"] or it["source_label"]}
    if it.get("masked"):
        out["redacted"] = True
    if it["kind"] == "movement":
        out["mode"] = it["mode"]
        if it["km"] is not None:
            out["km"] = it["km"]
    if p["captions"] and it["caption"]:
        out["caption"] = it["caption"]
    if p["times"] == "exact" and it["start_local"]:
        out["start"] = it["start_local"][11:16]
        if it["end_local"]:
            out["end"] = it["end_local"][11:16]
            offset = _day_offset(it["start_local"], it["end_local"])
            if offset:
                out["end_day_offset"] = offset
    elif p["times"] == "part_of_day" and it["start_local"]:
        out["part_of_day"] = _part_of_day(it["start_local"])
    if p["times"] != "none" and it["start_utc"] and it["end_utc"]:
        out["duration_min"] = round((studio._parse_utc(it["end_utc"])
                                     - studio._parse_utc(it["start_utc"])
                                     ).total_seconds() / 60)
    level = p["coordinates"]
    if level != "none":
        if it["lat"] is not None:
            out["lat"], out["lng"] = _round(it["lat"], level), _round(it["lng"], level)
        if it["kind"] == "movement" and it["end_lat"] is not None:
            out["end_lat"] = _round(it["end_lat"], level)
            out["end_lng"] = _round(it["end_lng"], level)
        if p["routes"] and it["points"]:
            out["route"] = [[_round(pt["lat"], level), _round(pt["lng"], level)]
                            for pt in it["points"]]
    if p["sources"]:
        out["source"] = it["source"]
        out["label_source"] = it["label_source"]
        if it["label"] and it["label"] != it["source_label"]:
            out["source_label"] = it["source_label"]
    return out


def _fmt_duration(minutes):
    if minutes is None:
        return ""
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m} min"


MODE_WORDS = {"walking": "Walked", "in passenger vehicle": "Drove",
              "in subway": "Took the subway", "in bus": "Took the bus",
              "in train": "Took the train", "in tram": "Took the tram",
              "flying": "Flew", "cycling": "Cycled", "running": "Ran",
              "unknown": "Moved"}


def auto_story(entries):
    """A plain, deterministic draft from the day's disclosed items only.

    Built from the already-redacted, already-disclosed entries, so the
    draft can never mention something the view itself does not show.
    """
    if not entries:
        return "Nothing recorded for this day."
    parts = []
    for e in entries:
        when = e.get("start") or e.get("part_of_day") or ""
        when = f"{when}: " if when else ""
        if e["kind"] == "visit":
            dur = _fmt_duration(e.get("duration_min"))
            parts.append(f"{when}{e['label']}" + (f" ({dur})" if dur else "") + ".")
        elif e["kind"] == "movement":
            verb = MODE_WORDS.get(e.get("mode"), "Traveled by " + str(e.get("mode")))
            km = f" {e['km']:g} km" if e.get("km") else ""
            arrive = (" arriving the next day" if e.get("end_day_offset") == 1 else
                      f" arriving {e['end_day_offset']} days later"
                      if e.get("end_day_offset") else "")
            parts.append(f"{when}{verb}{km}{arrive}.")
        elif e["kind"] == "note" and e.get("caption"):
            parts.append(f"{when}Note: {e['caption']}")
    return " ".join(parts)


# ---------------------------------------------------------------- serializers

def to_json(view):
    return json.dumps(view, indent=2, ensure_ascii=False) + "\n"


def to_geojson(view):
    features = []
    for day in view["days"]:
        for e in day["items"]:
            props = {k: v for k, v in e.items()
                     if k in ("ref", "kind", "label", "mode", "km", "start", "end",
                              "part_of_day", "caption")}
            props["date"] = day["date"]
            if e["kind"] == "visit" and e.get("lat") is not None:
                features.append({"type": "Feature", "properties": props,
                                 "geometry": {"type": "Point",
                                              "coordinates": [e["lng"], e["lat"]]}})
            elif e["kind"] == "movement" and e.get("route"):
                features.append({"type": "Feature", "properties": props,
                                 "geometry": {"type": "LineString",
                                              "coordinates": [[p[1], p[0]]
                                                              for p in e["route"]]}})
    return json.dumps({"type": "FeatureCollection",
                       "name": view["trip"]["title"],
                       "disclosure": view["disclosure"]["name"],
                       "features": features}, indent=2, ensure_ascii=False) + "\n"


def to_gpx(view):
    x = xml_escape
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="mcp-timeline studio"'
             ' xmlns="http://www.topografix.com/GPX/1/1">',
             f"  <metadata><name>{x(view['trip']['title'])}</name></metadata>"]
    for day in view["days"]:
        for e in day["items"]:
            if e["kind"] == "visit" and e.get("lat") is not None:
                lines.append(f'  <wpt lat="{e["lat"]}" lon="{e["lng"]}">'
                             f"<name>{x(e['label'])}</name>"
                             f"<desc>{x(day['date'])}</desc></wpt>")
    for day in view["days"]:
        for e in day["items"]:
            if e["kind"] == "movement" and e.get("route"):
                lines.append(f"  <trk><name>{x(day['date'])} {x(str(e.get('mode')))}"
                             "</name><trkseg>")
                lines += [f'    <trkpt lat="{p[0]}" lon="{p[1]}"/>' for p in e["route"]]
                lines.append("  </trkseg></trk>")
    lines.append("</gpx>")
    return "\n".join(lines) + "\n"


def _entry_line(e):
    when = ""
    if e.get("start"):
        when = e["start"] + (f"-{e['end']}" if e.get("end") else "")
        if e.get("end_day_offset"):
            when += f" (+{e['end_day_offset']}d)"
    elif e.get("part_of_day"):
        when = e["part_of_day"]
    if e["kind"] == "movement":
        what = f"{e.get('mode')}" + (f", {e['km']:g} km" if e.get("km") else "")
    else:
        what = e["label"]
    line = f"{when}  {what}".strip()
    if e.get("caption") and e["kind"] != "note":
        line += f" ({e['caption']})"
    if e["kind"] == "note":
        line = f"{when}  Note: {e.get('caption', '')}".strip()
    return line


def to_markdown(view):
    t = view["trip"]
    out = [f"# {t['title']}", "",
           f"{t['start_date']} to {t['end_date']}  ",
           f"Shared view: {view['disclosure']['name']}", ""]
    if t.get("summary"):
        out += [t["summary"], ""]
    for day in view["days"]:
        out += [f"## {day['date']}", ""]
        if day.get("story"):
            out += [day["story"], ""]
        for e in day["items"]:
            out.append(f"- {_entry_line(e)}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def to_csv(view):
    buf = io.StringIO()
    fields = ["date", "kind", "label", "mode", "km", "start", "end", "end_day_offset",
              "part_of_day", "duration_min", "lat", "lng", "end_lat", "end_lng", "caption"]
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for day in view["days"]:
        for e in day["items"]:
            w.writerow({"date": day["date"], **e})
    return buf.getvalue()


def to_html(view):
    """A standalone page with no scripts and no external requests."""
    h = html.escape
    t = view["trip"]
    body = [f"<h1>{h(t['title'])}</h1>",
            f"<p class='meta'>{h(t['start_date'])} to {h(t['end_date'])} &middot; "
            f"shared view: {h(view['disclosure']['name'])}</p>"]
    if t.get("summary"):
        body.append(f"<p>{h(t['summary'])}</p>")
    for day in view["days"]:
        body.append(f"<section><h2>{h(day['date'])}</h2>")
        if day.get("story"):
            body.append(f"<p class='story'>{h(day['story'])}</p>")
        body.append("<ul>")
        for e in day["items"]:
            cls = "redacted" if e.get("redacted") else e["kind"]
            body.append(f"<li class='{cls}'>{h(_entry_line(e))}</li>")
        body.append("</ul></section>")
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{h(t['title'])}</title><style>"
            ":root{--bg:#fbfaf7;--fg:#1d2433;--muted:#5d6577;--line:#e3e0d8;"
            "--accent:#2f6f62}"
            "@media (prefers-color-scheme:dark){:root{--bg:#14171c;--fg:#e8e6e1;"
            "--muted:#9aa1ad;--line:#2a2f37;--accent:#7cc4b2}}"
            "body{background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,"
            "sans-serif;max-width:720px;margin:0 auto;padding:24px 16px}"
            "h1{margin:0 0 4px}.meta{color:var(--muted)}section{border-top:1px solid "
            "var(--line);margin-top:20px}h2{font-size:1.05rem;color:var(--accent)}"
            "li{margin:4px 0}li.movement{color:var(--muted)}"
            "li.redacted{font-style:italic;color:var(--muted)}"
            "</style></head><body>" + "".join(body) + "</body></html>\n")


EXPORTERS = {
    "json": (to_json, "application/json", "json"),
    "geojson": (to_geojson, "application/geo+json", "geojson"),
    "gpx": (to_gpx, "application/gpx+xml", "gpx"),
    "md": (to_markdown, "text/markdown; charset=utf-8", "md"),
    "html": (to_html, "text/html; charset=utf-8", "html"),
    "csv": (to_csv, "text/csv; charset=utf-8", "csv"),
}


def render(db, trip_id, fmt, profile="owner"):
    if fmt not in EXPORTERS:
        raise studio.StudioError(f"format must be one of {', '.join(sorted(EXPORTERS))}")
    return EXPORTERS[fmt][0](build_view(db, trip_id, profile))
