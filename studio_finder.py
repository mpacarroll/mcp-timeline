"""Trip finder: suggest trips from history instead of typing date ranges.

A trip is a run of days spent away from home. The finder works out where
home is, marks each day "away" when anything recorded that day (a visit,
either end of a journey, or a raw fix) is at least min_km from home, joins
consecutive away days, and bridges a day with no data at all between two
away days (a phone that died on day two is still one trip). Each run comes
back as a suggestion the person can capture as an editable trip, rename on
the way in, or dismiss.

What this deliberately does not do:

- It is not an agent tool. Scanning the whole history is the broad access
  the studio exists to avoid handing out; suggestions are for the person,
  in the studio page and the CLI only.
- It never puts home's coordinates in a suggestion, and never writes the
  distance from home into a trip, because "5,180 km from home" in an export
  narrows down home.
- It never titles a suggestion after, or lists, a place the person has
  redacted anywhere. A suggestion list is exactly the kind of screen that
  gets shown over someone's shoulder.

Home, in order of preference: the one you set; the place your source
marked Home; the place you were most often at overnight; for raw-fix-only
history, the spot with the most fixes between midnight and 5am.
"""

import hashlib
from datetime import datetime, timedelta

import studio
import studio_export
from geo import haversine_m

DEFAULT_MIN_KM = 50
MIN_KM_RANGE = (5, 5000)
# Places this close to home are home, never a trip's highlight.
HOME_RADIUS_M = 1000
# Where a flight starts or ends is an airport: never a trip's title.
AIRPORT_RADIUS_M = 3000
HOME_PROTECT_RADIUS_M = 300
TOP_PLACES = 5


class NoHome(studio.StudioError):
    pass


# ---------------------------------------------------------------- home

def _setting(db, key):
    r = db.execute("SELECT value FROM studio_settings WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def set_home(db, lat, lng):
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        raise studio.StudioError("home needs lat and lng")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise studio.StudioError("home is not a valid coordinate")
    for k, v in (("home_lat", lat), ("home_lng", lng), ("home_set_utc", studio.now_utc())):
        db.execute("INSERT INTO studio_settings VALUES (?, ?) ON CONFLICT(key)"
                   " DO UPDATE SET value = excluded.value", (k, str(v)))
    db.commit()


def set_home_place(db, place_id):
    r = db.execute("SELECT lat, lng FROM places WHERE place_id = ?", (place_id,)).fetchone()
    if r is None:
        raise studio.StudioError("no such place")
    set_home(db, r["lat"], r["lng"])


def clear_home(db):
    """Forget a home you set, and go back to detecting it."""
    db.execute("DELETE FROM studio_settings WHERE key LIKE 'home_%'")
    db.commit()


def get_home(db):
    """{lat, lng, how, place_id?} or None. Owner-only: never exported."""
    lat, lng = _setting(db, "home_lat"), _setting(db, "home_lng")
    if lat is not None and lng is not None:
        return {"lat": float(lat), "lng": float(lng), "how": "set by you"}
    r = db.execute("SELECT place_id, lat, lng FROM places WHERE semantic_type = 'Home'"
                   " ORDER BY visit_count DESC LIMIT 1").fetchone()
    if r:
        return {"lat": r["lat"], "lng": r["lng"], "place_id": r["place_id"],
                "how": "marked Home by your history"}
    # Overnight: a visit that starts in the small hours, or crosses midnight.
    r = db.execute(
        "SELECT v.place_id, p.lat, p.lng, COUNT(DISTINCT v.local_date) nights"
        " FROM visits v JOIN places p ON p.place_id = v.place_id"
        " WHERE v.start_local < '05:00:00' OR v.end_local < v.start_local"
        " GROUP BY v.place_id ORDER BY nights DESC, p.visit_count DESC LIMIT 1").fetchone()
    if r and r["nights"] >= 2:
        return {"lat": r["lat"], "lng": r["lng"], "place_id": r["place_id"],
                "how": "where you most often spent the night"}
    counts = {}
    for p in db.execute("SELECT ts_utc, lat, lng FROM path_points"):
        local = studio._parse_utc(p["ts_utc"]).astimezone()
        if local.hour < 5:
            key = (round(p["lat"], 3), round(p["lng"], 3))
            counts[key] = counts.get(key, 0) + 1
    if counts:
        (lat, lng), n = max(counts.items(), key=lambda kv: kv[1])
        if n >= 3:
            return {"lat": lat, "lng": lng,
                    "how": "where your raw fixes most often were overnight"}
    return None


def home_protected(db, home=None):
    """True when a redaction covering every trip already hides home."""
    home = home or get_home(db)
    if home is None:
        return False
    for r in db.execute("SELECT * FROM studio_redactions WHERE trip_id IS NULL"
                        " AND kind = 'area'"):
        if haversine_m(home["lat"], home["lng"], r["lat"], r["lng"]) <= r["radius_m"]:
            return True
    if home.get("place_id") and db.execute(
            "SELECT 1 FROM studio_redactions WHERE trip_id IS NULL AND kind = 'place'"
            " AND target = ?", (home["place_id"],)).fetchone():
        return True
    return False


def protect_home(db, radius_m=HOME_PROTECT_RADIUS_M, replacement="Home"):
    """Mask home in every trip and every export, past and future. A trip
    begins and ends at home, so this is the redaction almost everyone
    wants before sharing anything."""
    home = get_home(db)
    if home is None:
        raise NoHome("set where home is first")
    if home_protected(db, home):
        return None
    return studio.add_redaction(db, "area", "mask", lat=home["lat"], lng=home["lng"],
                                radius_m=radius_m, replacement=replacement,
                                note="added by the trip finder")


# ---------------------------------------------------------------- observations

def _observations(db):
    """{local_date: [obs]}, obs = dict(lat, lng, minutes, place_id, kind)."""
    days = {}

    def add(date, **o):
        if o["lat"] is not None and o["lng"] is not None:
            days.setdefault(date, []).append(o)

    for r in db.execute("SELECT v.local_date, v.place_id, v.duration_min, p.lat, p.lng"
                        " FROM visits v LEFT JOIN places p ON p.place_id = v.place_id"):
        add(r["local_date"], lat=r["lat"], lng=r["lng"], minutes=r["duration_min"],
            place_id=r["place_id"], kind="visit")
    for r in db.execute("SELECT local_date, mode, start_lat, start_lng, end_lat, end_lng"
                        " FROM activities"):
        for lat, lng in ((r["start_lat"], r["start_lng"]), (r["end_lat"], r["end_lng"])):
            add(r["local_date"], lat=lat, lng=lng, minutes=0, place_id=None,
                kind="movement", mode=r["mode"])
    # Raw fixes only count on days no source summarized: a summarized day
    # already says where you were, and its fixes would just add noise.
    summarized = set(days)
    for p in db.execute("SELECT ts_utc, lat, lng FROM path_points"):
        date = studio._parse_utc(p["ts_utc"]).astimezone().strftime("%Y-%m-%d")
        if date not in summarized:
            add(date, lat=p["lat"], lng=p["lng"], minutes=0, place_id=None, kind="raw")
    return days


def _sensitive(db):
    """(place_ids, areas) the person redacted anywhere, in any trip."""
    place_ids, areas = set(), []
    for r in db.execute("SELECT * FROM studio_redactions"):
        if r["kind"] == "place":
            place_ids.add(r["target"])
        elif r["kind"] == "area":
            areas.append(r)
    return place_ids, areas


def _next_day(date):
    return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _runs(away, observed):
    """Join consecutive away dates; bridge dates with no data at all."""
    runs, cur = [], None
    for date in sorted(away):
        if cur:
            gap, nxt = [], _next_day(cur[-1])
            while nxt < date and nxt not in observed:
                gap.append(nxt)
                nxt = _next_day(nxt)
            if nxt == date:
                cur += gap + [date]
                continue
            runs.append(cur)
        cur = [date]
    if cur:
        runs.append(cur)
    return runs


# ---------------------------------------------------------------- suggestions

def suggestion_id(start_date, end_date):
    return "sg_" + hashlib.sha256(f"{start_date}/{end_date}".encode()).hexdigest()[:12]


def suggest_trips(db, min_km=DEFAULT_MIN_KM, include_dismissed=False):
    """Every stretch away from home in the history, newest first.

    Returns {"home": {how, protected} | None, "min_km", "suggestions": [...]}.
    Home's coordinates are not in the result.
    """
    try:
        min_km = float(min_km)
    except (TypeError, ValueError):
        raise studio.StudioError("min_km must be a number")
    if not MIN_KM_RANGE[0] <= min_km <= MIN_KM_RANGE[1]:
        raise studio.StudioError(f"min_km must be between {MIN_KM_RANGE[0]} and"
                                 f" {MIN_KM_RANGE[1]}")
    home = get_home(db)
    if home is None:
        return {"home": None, "min_km": min_km, "suggestions": [],
                "needs_home": "no home could be worked out from this history;"
                              " set one to find trips"}
    obs = _observations(db)
    hl, hg = home["lat"], home["lng"]
    away = {date for date, os in obs.items()
            if max(haversine_m(hl, hg, o["lat"], o["lng"]) for o in os) >= min_km * 1000}

    sensitive_ids, sensitive_areas = _sensitive(db)
    airports = [(o["lat"], o["lng"]) for os in obs.values() for o in os
                if o["kind"] == "movement" and o.get("mode") == "flying"]
    dismissed = {(r["start_date"], r["end_date"]) for r in db.execute(
        "SELECT start_date, end_date FROM studio_dismissed_trips")}
    trips = studio.list_trips(db)

    out = []
    for run in _runs(away, set(obs)):
        start, end = run[0], run[-1]
        status = "dismissed" if (start, end) in dismissed else "new"
        if status == "dismissed" and not include_dismissed:
            continue
        day_obs = [o for d in run for o in obs.get(d, [])]
        farthest = max(haversine_m(hl, hg, o["lat"], o["lng"]) for o in day_obs)
        dwell, private = {}, set()
        for o in day_obs:
            if o["kind"] != "visit" or not o["place_id"]:
                continue
            if haversine_m(hl, hg, o["lat"], o["lng"]) <= HOME_RADIUS_M:
                continue
            if (o["place_id"] in sensitive_ids
                    or any(studio_export._inside(a, o["lat"], o["lng"])
                           for a in sensitive_areas)):
                private.add(o["place_id"])
                continue
            dwell[o["place_id"]] = dwell.get(o["place_id"], 0) + (o["minutes"] or 0)
        ranked = []
        for pid, minutes in sorted(dwell.items(), key=lambda kv: -kv[1]):
            label, how = studio.place_label(db, pid)
            if label in ("unnamed place", "unidentified stop") or not label:
                continue
            row = db.execute("SELECT lat, lng, semantic_type FROM places WHERE"
                             " place_id = ?", (pid,)).fetchone()
            ranked.append({"place_id": pid, "label": label, "minutes": round(minutes),
                           "lodging": bool(row and row["semantic_type"]
                                           and "Accommodation" in row["semantic_type"]),
                           "airport": bool(row and any(
                               haversine_m(row["lat"], row["lng"], a[0], a[1])
                               <= AIRPORT_RADIUS_M for a in airports))})
        anchor = next((p for p in ranked if not p["lodging"] and not p["airport"]),
                      ranked[0] if ranked else None)
        if anchor:
            title, title_how = f"Trip near {anchor['label']}", (
                "named after the place you spent longest at, not counting where you"
                " slept or flew from")
        elif len(run) == 1:
            title, title_how = f"Day out on {start}", "no named places on this day"
        else:
            title, title_how = f"Away {start} to {end}", "no named places on these days"
        kinds = {o["kind"] for o in day_obs}
        overlaps = [{"trip_id": t["trip_id"], "title": t["title"]} for t in trips
                    if t["start_date"] <= end and t["end_date"] >= start]
        if overlaps and status == "new":
            status = "captured"
        modes = sorted({o["mode"] for o in day_obs if o["kind"] == "movement"
                        and o.get("mode") not in (None, "unknown", "walking")})
        out.append({
            "id": suggestion_id(start, end),
            "start_date": start, "end_date": end, "days": len(run),
            "nights": len(run) - 1,
            "farthest_km": round(farthest / 1000),
            "title": title, "title_source": title_how,
            # Highlights first; where you slept and flew from after them.
            "places": [{"label": p["label"], "minutes": p["minutes"]}
                       for p in sorted(ranked, key=lambda p: p["lodging"] or p["airport"])
                       [:TOP_PLACES]],
            "private_stops": len(private),
            "modes": modes,
            "bridged_days": [d for d in run if d not in obs],
            "reconstructed": kinds == {"raw"},
            "evidence": {k: sum(1 for o in day_obs if o["kind"] == k)
                         for k in ("visit", "movement", "raw")},
            "status": status,
            "overlaps": overlaps,
        })
    out.sort(key=lambda s: s["start_date"], reverse=True)
    return {"home": {"how": home["how"], "protected": home_protected(db, home)},
            "min_km": min_km, "suggestions": out}


def _find(db, start_date, end_date, min_km):
    for s in suggest_trips(db, min_km, include_dismissed=True)["suggestions"]:
        if s["start_date"] == start_date and s["end_date"] == end_date:
            return s
    return None


def accept(db, start_date, end_date, title=None, min_km=DEFAULT_MIN_KM):
    """Capture a suggestion as a trip, under your title or the suggested one."""
    studio._validate_date(start_date, "start_date")
    studio._validate_date(end_date, "end_date")
    s = _find(db, start_date, end_date, min_km)
    if s is None:
        raise studio.StudioError("no suggested trip for those dates; capture it as a"
                                 " new trip instead")
    db.execute("DELETE FROM studio_dismissed_trips WHERE start_date = ? AND end_date = ?",
               (start_date, end_date))
    return studio.create_trip(db, (title or "").strip() or s["title"], start_date,
                              end_date)


def dismiss(db, start_date, end_date):
    studio._validate_date(start_date, "start_date")
    studio._validate_date(end_date, "end_date")
    db.execute("INSERT OR REPLACE INTO studio_dismissed_trips VALUES (?, ?, ?)",
               (start_date, end_date, studio.now_utc()))
    db.commit()


def restore(db, start_date, end_date):
    n = db.execute("DELETE FROM studio_dismissed_trips WHERE start_date = ?"
                   " AND end_date = ?", (start_date, end_date)).rowcount
    db.commit()
    if not n:
        raise studio.StudioError("that trip was not dismissed")


def forget_dismissals_in(db, start_date, end_date):
    """Purging history for a range also forgets dismissals that touch it:
    even "this was not a trip" is a fact about those days."""
    db.execute("DELETE FROM studio_dismissed_trips WHERE start_date <= ?"
               " AND end_date >= ?", (end_date, start_date))

