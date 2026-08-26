"""Turn coordinates into human-readable place labels, on demand.

A continuous location feed knows where you were but not what was there.
Google Timeline shipped place identity with its exports; raw fixes carry
none, so a stay in an unfamiliar city reads as a coordinate pair unless
something resolves it.

Three deliberate constraints, because this is the only part of the
project that sends anything off the machine:

- **Opt in.** Disabled unless GEOCODE_ENABLED is set. A tool that
  silently forwards someone's location history to a third party by
  default would be indefensible, however useful the result.
- **Stays only, never raw fixes.** Callers resolve the handful of places
  someone actually stopped, not the thousands of points in between. That
  is a small fraction of the trail, and the rest never leaves.
- **Cached permanently.** A coordinate is looked up once, ever. Repeated
  questions about the same day cost nothing and send nothing.

Uses OpenStreetMap's Nominatim, which needs no API key. Their usage
policy requires an identifying User-Agent and at most one request per
second, both enforced below. Failures are never fatal: an outage leaves
the answer showing coordinates instead of a name, and is deliberately
not cached, so a momentary network problem cannot permanently mark a
real place as unidentifiable.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ENABLED = bool(os.environ.get("GEOCODE_ENABLED"))

ENDPOINT = os.environ.get("GEOCODE_ENDPOINT",
                          "https://nominatim.openstreetmap.org/reverse")

# Nominatim asks that clients identify themselves with something a human
# could contact. Overridable so a fork does not impersonate this project.
USER_AGENT = os.environ.get(
    "GEOCODE_USER_AGENT",
    "mcp-timeline/1.0 (personal location tool; github.com/mpacarroll/mcp-timeline)")

TIMEOUT_S = float(os.environ.get("GEOCODE_TIMEOUT_S", "6"))

# Their policy is one request per second. Enforced here rather than
# trusted to callers, since exceeding it gets the whole project blocked.
MIN_INTERVAL_S = float(os.environ.get("GEOCODE_MIN_INTERVAL_S", "1.1"))

# ~11 meters. Fine enough that neighboring buildings differ, coarse
# enough that two stays at the same cafe share one cached lookup.
PRECISION = 4


class GeocodeUnavailable(Exception):
    """The lookup could not be performed: offline, timed out, rate limited,
    or the service errored.

    Deliberately distinct from a lookup that succeeded and found no useful
    name. The first is temporary and must not be cached; the second is a
    fact about the coordinate and should be. Collapsing them means one
    network blip permanently marks a real place as unidentifiable.
    """


_rate_lock = threading.Lock()
_last_request_at = 0.0


def cache_key(lat, lng):
    return (round(float(lat), PRECISION), round(float(lng), PRECISION))


def _short_label(payload):
    """Build a compact label from a Nominatim response.

    display_name is the full postal chain ("123, Main Street, Some
    Neighborhood, City, County, State, 12345, Country"), which is far too
    long to put in every entry of a day summary. Prefer the name of the
    thing at that coordinate, and fall back to street plus locality.
    """
    if not isinstance(payload, dict):
        return None
    address = payload.get("address") or {}
    locality = (address.get("city") or address.get("town")
                or address.get("village") or address.get("suburb")
                or address.get("neighbourhood"))

    # A named POI (restaurant, hotel, station) is the most useful answer.
    name = payload.get("name")
    if name:
        return f"{name}, {locality}" if locality else name

    street = address.get("road")
    if street:
        number = address.get("house_number")
        street = f"{number} {street}" if number else street
        return f"{street}, {locality}" if locality else street

    if locality:
        return locality
    display = payload.get("display_name")
    if display:
        # Last resort: first two components of the postal chain.
        return ", ".join(display.split(", ")[:2])
    return None


def _throttle():
    global _last_request_at
    with _rate_lock:
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _fetch(lat, lng):
    """Query Nominatim.

    Returns a label, or None when the lookup succeeded but nothing useful
    is at that coordinate. Raises GeocodeUnavailable when the lookup could
    not be performed at all.
    """
    query = urllib.parse.urlencode({
        "format": "jsonv2", "lat": f"{lat}", "lon": f"{lng}", "zoom": "18"})
    request = urllib.request.Request(f"{ENDPOINT}?{query}",
                                     headers={"User-Agent": USER_AGENT})
    _throttle()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return _short_label(json.loads(response.read()))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
            ValueError, OSError) as exc:
        # Offline, rate limited, or the service is down. Signalled rather
        # than returned so the caller can decline to cache it.
        raise GeocodeUnavailable(str(exc)) from exc


def label_for(db, lat, lng, fetch=None):
    """Best-effort label for a coordinate, cached in the given database.

    Returns None when geocoding is disabled, the lookup fails, or nothing
    is there.

    A successful lookup is cached either way, including one that found no
    name, so a genuinely nameless spot is not re-requested on every query.
    A failed lookup is NOT cached, so an outage does not permanently mark
    a real place as unidentifiable.
    """
    # Resolved here rather than as a default argument: a default binds
    # the function object at definition time, so monkeypatching
    # geocode._fetch would silently have no effect and a test believing
    # itself stubbed would quietly hit the live service.
    fetch = fetch or _fetch

    lat_key, lng_key = cache_key(lat, lng)
    row = db.execute(
        "SELECT label FROM geocode_cache WHERE lat_key = ? AND lng_key = ?",
        (lat_key, lng_key)).fetchone()
    if row is not None:
        return row[0]

    if not ENABLED:
        return None

    try:
        label = fetch(lat, lng)
    except GeocodeUnavailable:
        return None
    db.execute(
        "INSERT OR REPLACE INTO geocode_cache (lat_key, lng_key, label,"
        " fetched_utc) VALUES (?,?,?,?)",
        (lat_key, lng_key, label,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    db.commit()
    return label
