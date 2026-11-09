#!/usr/bin/env python3
"""Generate the synthetic journey fixture used by the history studio.

Every person, home, clinic and friend in this file is invented. The
coordinates for the home and the friend's apartment are made up and do not
point at anyone's residence; the public landmarks (airports, a tower, a
castle) use their public coordinates only so the route sketch looks like a
real trip. No real location history is read or written by this script.

The output mirrors the Google Maps iOS on-device Timeline export format,
so it goes through ingest.py exactly like a real export would.

Run:
    python3 tools/make_synthetic_journey.py tests/fixtures/synthetic_journey.json

The journey (four days, two time zones):

- 2026-05-13 (EDT): an ordinary day at home and work, outside the trip.
- 2026-05-14 (EDT): home, drive to the airport, overnight flight that
  departs on the 14th local time and lands on the 15th in Lisbon.
- 2026-05-15 (WEST): taxi to the hotel, a clinic appointment (the
  sensitive stop the demo redacts), a market, the riverside tower and
  monastery by tram, back to the hotel for a stay that crosses midnight.
- 2026-05-16 (WEST): castle walk with a recorded route, viewpoint, cafe,
  an evening at a friend's apartment (the private address the demo hides
  with an area redaction), taxi back.
- 2026-05-17 (WEST then EDT): fly home, drive home.
"""

import json
import sys
from datetime import datetime, timedelta, timezone

EDT = timezone(timedelta(hours=-4))
WEST = timezone(timedelta(hours=1))

PLACES = {
    # place_id: (lat, lng, semanticType)
    "SYNTH_HOME_0001": (42.39012, -71.10234, "Home"),
    "SYNTH_WORK_0002": (42.36345, -71.08811, "Work"),
    "SYNTH_BOS_0003": (42.36560, -71.00960, "Unknown"),
    "SYNTH_LIS_0004": (38.77420, -9.13420, "Unknown"),
    "SYNTH_HOTEL_0005": (38.71480, -9.14070, "Inferred Accommodation"),
    "SYNTH_CLINIC_0006": (38.72265, -9.14791, "Unknown"),
    "SYNTH_MARKET_0007": (38.70700, -9.14590, "Unknown"),
    "SYNTH_TOWER_0008": (38.69160, -9.21600, "Unknown"),
    "SYNTH_MONASTERY_0009": (38.69790, -9.20640, "Unknown"),
    "SYNTH_CASTLE_0010": (38.71390, -9.13340, "Unknown"),
    "SYNTH_VIEWPOINT_0011": (38.71710, -9.13290, "Unknown"),
    "SYNTH_CAFE_0012": (38.71100, -9.13700, "Unknown"),
    "SYNTH_FRIEND_0013": (38.73613, -9.15427, "Unknown"),
}


def iso(dt):
    return dt.isoformat(timespec="milliseconds")


def geo(lat, lng):
    return f"geo:{lat:.6f},{lng:.6f}"


def visit(place_id, start, end):
    lat, lng, semantic = PLACES[place_id]
    return {"startTime": iso(start), "endTime": iso(end),
            "visit": {"hierarchyLevel": "0", "probability": "0.900000",
                      "topCandidate": {"probability": "0.880000",
                                       "semanticType": semantic,
                                       "placeID": place_id,
                                       "placeLocation": geo(lat, lng)}}}


def activity(mode, start, end, from_id, to_id, meters):
    a, b = PLACES[from_id], PLACES[to_id]
    return {"startTime": iso(start), "endTime": iso(end),
            "activity": {"probability": "0.850000",
                         "start": geo(a[0], a[1]), "end": geo(b[0], b[1]),
                         "topCandidate": {"type": mode, "probability": "0.800000"},
                         "distanceMeters": f"{meters:.6f}"}}


def path(start, end, from_id, to_id, steps):
    """A straight-ish recorded route between two places, one fix per step."""
    a, b = PLACES[from_id], PLACES[to_id]
    total = (end - start).total_seconds() / 60
    points = []
    for i in range(steps + 1):
        f = i / steps
        # A small sideways wobble so the sketch is not a ruler line.
        wobble = 0.0004 * (1 if i % 2 else -1) if 0 < i < steps else 0.0
        points.append({"point": geo(a[0] + (b[0] - a[0]) * f + wobble,
                                    a[1] + (b[1] - a[1]) * f),
                       "durationMinutesOffsetFromStartTime": f"{total * f:.0f}"})
    return {"startTime": iso(start), "endTime": iso(end), "timelinePath": points}


def d(tz, day, hh, mm=0):
    return datetime(2026, 5, day, hh, mm, tzinfo=tz)


def build():
    r = []
    # 2026-05-13: outside the trip.
    r.append(visit("SYNTH_HOME_0001", d(EDT, 13, 0), d(EDT, 13, 8, 10)))
    r.append(activity("in subway", d(EDT, 13, 8, 10), d(EDT, 13, 8, 40),
                      "SYNTH_HOME_0001", "SYNTH_WORK_0002", 4100))
    r.append(visit("SYNTH_WORK_0002", d(EDT, 13, 8, 40), d(EDT, 13, 17, 30)))
    r.append(activity("in subway", d(EDT, 13, 17, 30), d(EDT, 13, 18, 0),
                      "SYNTH_WORK_0002", "SYNTH_HOME_0001", 4100))
    r.append(visit("SYNTH_HOME_0001", d(EDT, 13, 18, 0), d(EDT, 14, 15, 30)))

    # 2026-05-14: leave home, overnight flight.
    r.append(activity("in passenger vehicle", d(EDT, 14, 15, 30), d(EDT, 14, 16, 10),
                      "SYNTH_HOME_0001", "SYNTH_BOS_0003", 17800))
    r.append(visit("SYNTH_BOS_0003", d(EDT, 14, 16, 10), d(EDT, 14, 18, 30)))
    # Departs 18:30 EDT (22:30Z), lands 06:15 WEST on the 15th (05:15Z).
    r.append(activity("flying", d(EDT, 14, 18, 30), d(WEST, 15, 6, 15),
                      "SYNTH_BOS_0003", "SYNTH_LIS_0004", 5180000))

    # 2026-05-15: Lisbon, day one.
    r.append(visit("SYNTH_LIS_0004", d(WEST, 15, 6, 15), d(WEST, 15, 7, 0)))
    r.append(activity("in passenger vehicle", d(WEST, 15, 7, 0), d(WEST, 15, 7, 35),
                      "SYNTH_LIS_0004", "SYNTH_HOTEL_0005", 7300))
    r.append(visit("SYNTH_HOTEL_0005", d(WEST, 15, 7, 35), d(WEST, 15, 11, 0)))
    r.append(activity("walking", d(WEST, 15, 11, 0), d(WEST, 15, 11, 15),
                      "SYNTH_HOTEL_0005", "SYNTH_CLINIC_0006", 1050))
    r.append(path(d(WEST, 15, 11, 0), d(WEST, 15, 11, 15),
                  "SYNTH_HOTEL_0005", "SYNTH_CLINIC_0006", 5))
    r.append(visit("SYNTH_CLINIC_0006", d(WEST, 15, 11, 15), d(WEST, 15, 12, 5)))
    r.append(activity("walking", d(WEST, 15, 12, 5), d(WEST, 15, 12, 40),
                      "SYNTH_CLINIC_0006", "SYNTH_MARKET_0007", 1900))
    r.append(path(d(WEST, 15, 12, 5), d(WEST, 15, 12, 40),
                  "SYNTH_CLINIC_0006", "SYNTH_MARKET_0007", 7))
    r.append(visit("SYNTH_MARKET_0007", d(WEST, 15, 12, 40), d(WEST, 15, 14, 0)))
    r.append(activity("in tram", d(WEST, 15, 14, 0), d(WEST, 15, 14, 30),
                      "SYNTH_MARKET_0007", "SYNTH_TOWER_0008", 6400))
    r.append(visit("SYNTH_TOWER_0008", d(WEST, 15, 14, 30), d(WEST, 15, 16, 30)))
    r.append(activity("walking", d(WEST, 15, 16, 30), d(WEST, 15, 16, 40),
                      "SYNTH_TOWER_0008", "SYNTH_MONASTERY_0009", 950))
    r.append(visit("SYNTH_MONASTERY_0009", d(WEST, 15, 16, 40), d(WEST, 15, 17, 50)))
    r.append(activity("in tram", d(WEST, 15, 18, 0), d(WEST, 15, 18, 35),
                      "SYNTH_MONASTERY_0009", "SYNTH_HOTEL_0005", 6900))
    # Crosses midnight: belongs to the 15th, ends on the 16th.
    r.append(visit("SYNTH_HOTEL_0005", d(WEST, 15, 18, 45), d(WEST, 16, 9, 0)))

    # 2026-05-16: Lisbon, day two.
    r.append(activity("walking", d(WEST, 16, 9, 0), d(WEST, 16, 9, 40),
                      "SYNTH_HOTEL_0005", "SYNTH_CASTLE_0010", 2200))
    r.append(path(d(WEST, 16, 9, 0), d(WEST, 16, 9, 40),
                  "SYNTH_HOTEL_0005", "SYNTH_CASTLE_0010", 8))
    r.append(visit("SYNTH_CASTLE_0010", d(WEST, 16, 9, 40), d(WEST, 16, 11, 30)))
    r.append(activity("walking", d(WEST, 16, 11, 30), d(WEST, 16, 11, 50),
                      "SYNTH_CASTLE_0010", "SYNTH_VIEWPOINT_0011", 600))
    r.append(visit("SYNTH_VIEWPOINT_0011", d(WEST, 16, 11, 50), d(WEST, 16, 12, 30)))
    r.append(activity("walking", d(WEST, 16, 12, 30), d(WEST, 16, 13, 0),
                      "SYNTH_VIEWPOINT_0011", "SYNTH_CAFE_0012", 900))
    r.append(visit("SYNTH_CAFE_0012", d(WEST, 16, 13, 0), d(WEST, 16, 14, 0)))
    r.append(activity("walking", d(WEST, 16, 14, 0), d(WEST, 16, 14, 25),
                      "SYNTH_CAFE_0012", "SYNTH_HOTEL_0005", 1100))
    r.append(visit("SYNTH_HOTEL_0005", d(WEST, 16, 14, 25), d(WEST, 16, 18, 30)))
    r.append(activity("in passenger vehicle", d(WEST, 16, 18, 30), d(WEST, 16, 18, 55),
                      "SYNTH_HOTEL_0005", "SYNTH_FRIEND_0013", 3600))
    r.append(path(d(WEST, 16, 18, 30), d(WEST, 16, 18, 55),
                  "SYNTH_HOTEL_0005", "SYNTH_FRIEND_0013", 6))
    r.append(visit("SYNTH_FRIEND_0013", d(WEST, 16, 18, 55), d(WEST, 16, 23, 0)))
    r.append(activity("in passenger vehicle", d(WEST, 16, 23, 0), d(WEST, 16, 23, 25),
                      "SYNTH_FRIEND_0013", "SYNTH_HOTEL_0005", 3700))
    r.append(visit("SYNTH_HOTEL_0005", d(WEST, 16, 23, 25), d(WEST, 17, 8, 0)))

    # 2026-05-17: home.
    r.append(activity("in passenger vehicle", d(WEST, 17, 8, 0), d(WEST, 17, 8, 30),
                      "SYNTH_HOTEL_0005", "SYNTH_LIS_0004", 7400))
    r.append(visit("SYNTH_LIS_0004", d(WEST, 17, 8, 30), d(WEST, 17, 11, 0)))
    # Departs 11:00 WEST (10:00Z), lands 13:30 EDT (17:30Z).
    r.append(activity("flying", d(WEST, 17, 11, 0), d(EDT, 17, 13, 30),
                      "SYNTH_LIS_0004", "SYNTH_BOS_0003", 5180000))
    r.append(visit("SYNTH_BOS_0003", d(EDT, 17, 13, 30), d(EDT, 17, 14, 15)))
    r.append(activity("in passenger vehicle", d(EDT, 17, 14, 15), d(EDT, 17, 14, 55),
                      "SYNTH_BOS_0003", "SYNTH_HOME_0001", 17900))
    r.append(visit("SYNTH_HOME_0001", d(EDT, 17, 14, 55), d(EDT, 17, 23, 59)))
    return r


# Names a person would type into the studio for places Timeline left
# unnamed. Applied by demo_studio.py as user-provided labels, never by
# ingest, so provenance stays honest ("named by you").
PLACE_NAMES = {
    "SYNTH_BOS_0003": "Boston Logan Airport",
    "SYNTH_LIS_0004": "Lisbon Airport",
    "SYNTH_HOTEL_0005": "Hotel Rio Azul",
    "SYNTH_CLINIC_0006": "Lumen Dermatology Clinic",
    "SYNTH_MARKET_0007": "Riverside Market",
    "SYNTH_TOWER_0008": "Belem Tower",
    "SYNTH_MONASTERY_0009": "Jeronimos Monastery",
    "SYNTH_CASTLE_0010": "Sao Jorge Castle",
    "SYNTH_VIEWPOINT_0011": "Graca Viewpoint",
    "SYNTH_CAFE_0012": "Cafe Andorinha",
    "SYNTH_FRIEND_0013": "Ines's apartment",
}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: make_synthetic_journey.py <out.json>")
    with open(sys.argv[1], "w") as f:
        json.dump(build(), f, indent=2)
        f.write("\n")
    with open(sys.argv[1].replace(".json", "_place_names.json"), "w") as f:
        json.dump(PLACE_NAMES, f, indent=2)
        f.write("\n")
    print(f"wrote {sys.argv[1]}")
