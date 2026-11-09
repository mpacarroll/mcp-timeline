#!/usr/bin/env python3
"""Generate a synthetic month of history for the studio's trip finder.

Every person and private place here is invented. It extends the Lisbon
journey (tools/make_synthetic_journey.py) with an ordinary May around an
invented home, so the finder has something to tell apart from trips:

- Workdays: home, subway to work, subway home. Weekends: home and a
  grocery store nearby.
- 2026-05-09: a day trip to an invented beach about 57 km away.
- 2026-05-14 to 2026-05-17: the Lisbon journey, unchanged.
- 2026-05-20: an outing to an invented farm stand about 30 km away,
  under the finder's default 50 km threshold on purpose.
- 2026-05-23 to 2026-05-24: a weekend at an invented lakeside cabin
  about 150 km away, overnight.
- 2026-05-28: a day the Timeline export knows nothing about. The demo
  adds it as OwnTracks raw fixes (see RAW_DAY below), so the finder has
  to reconstruct it from points.

The coordinates of the home, the cabin, the beach and the farm stand are
made up and do not point at anyone's residence. No real location history
is read or written by this script.

Run:
    python3 tools/make_synthetic_season.py tests/fixtures/synthetic_season.json
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_synthetic_journey as journey  # noqa: E402
from make_synthetic_journey import EDT, activity, visit  # noqa: E402

journey.PLACES.update({
    "SYNTH_GROCER_0014": (42.38720, -71.09650, "Unknown"),
    "SYNTH_BEACH_0015": (42.70210, -70.55020, "Unknown"),
    "SYNTH_CHOWDER_0016": (42.69720, -70.54480, "Unknown"),
    "SYNTH_FARM_0017": (42.47230, -71.43010, "Unknown"),
    "SYNTH_CABIN_0018": (43.62510, -71.38240, "Inferred Accommodation"),
    "SYNTH_TRAIL_0019": (43.66080, -71.33570, "Unknown"),
    "SYNTH_LAKEDINER_0020": (43.61190, -71.37020, "Unknown"),
})

PLACE_NAMES = dict(journey.PLACE_NAMES, **{
    "SYNTH_GROCER_0014": "Corner Grocery",
    "SYNTH_BEACH_0015": "Gull Point Beach",
    "SYNTH_CHOWDER_0016": "Harbor Chowder House",
    "SYNTH_FARM_0017": "Orchard Hill Farm Stand",
    "SYNTH_CABIN_0018": "Pine Hollow Cabin",
    "SYNTH_TRAIL_0019": "Ridge Loop Trail",
    "SYNTH_LAKEDINER_0020": "Lakeside Diner",
})

# OwnTracks-only day trip on 2026-05-28 (UTC times), to an invented spot
# about 70 km south-east. (minutes after 13:00Z, lat, lng)
RAW_DAY = "2026-05-28"
RAW_DEST = (41.90200, -70.55100)


def raw_day_fixes():
    """[(ts_utc, lat, lng)]: leave home, drive, two hours at the spot,
    drive back. Spaced so dwell.derive_entries sees stays and moves."""
    home = journey.PLACES["SYNTH_HOME_0001"][:2]
    base = datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc)
    out = []
    for i in range(10):  # 20 min at home
        out.append((base + timedelta(minutes=2 * i), home[0], home[1]))
    for i in range(1, 31):  # 60 min drive out
        f = i / 30
        out.append((base + timedelta(minutes=20 + 2 * i),
                    home[0] + (RAW_DEST[0] - home[0]) * f,
                    home[1] + (RAW_DEST[1] - home[1]) * f))
    for i in range(1, 61):  # 120 min at the destination
        out.append((base + timedelta(minutes=80 + 2 * i),
                    RAW_DEST[0] + 0.00002 * (i % 3), RAW_DEST[1]))
    for i in range(1, 31):  # 60 min drive back
        f = i / 30
        out.append((base + timedelta(minutes=200 + 2 * i),
                    RAW_DEST[0] + (home[0] - RAW_DEST[0]) * f,
                    RAW_DEST[1] + (home[1] - RAW_DEST[1]) * f))
    for i in range(1, 11):
        out.append((base + timedelta(minutes=260 + 2 * i), home[0], home[1]))
    return [(t.strftime("%Y-%m-%dT%H:%M:%SZ"), lat, lng) for t, lat, lng in out]


def d(day, hh, mm=0):
    return datetime(2026, 5, day, hh, mm, tzinfo=EDT)


def at_home(day, h0, m0, h1, m1):
    return visit("SYNTH_HOME_0001", d(day, h0, m0), d(day, h1, m1))


def weekday(day):
    return datetime(2026, 5, day).weekday()


def ordinary(day):
    """A whole day that starts and ends at home, so days never depend on
    their neighbors."""
    if weekday(day) < 5:
        return [
            at_home(day, 0, 0, 8, 10),
            activity("in subway", d(day, 8, 10), d(day, 8, 40),
                     "SYNTH_HOME_0001", "SYNTH_WORK_0002", 4100),
            visit("SYNTH_WORK_0002", d(day, 8, 40), d(day, 17, 30)),
            activity("in subway", d(day, 17, 30), d(day, 18, 0),
                     "SYNTH_WORK_0002", "SYNTH_HOME_0001", 4100),
            at_home(day, 18, 0, 23, 59),
        ]
    return [
        at_home(day, 0, 0, 10, 0),
        activity("walking", d(day, 10, 0), d(day, 10, 15),
                 "SYNTH_HOME_0001", "SYNTH_GROCER_0014", 700),
        visit("SYNTH_GROCER_0014", d(day, 10, 15), d(day, 10, 50)),
        activity("walking", d(day, 10, 50), d(day, 11, 5),
                 "SYNTH_GROCER_0014", "SYNTH_HOME_0001", 700),
        at_home(day, 11, 5, 23, 59),
    ]


SPECIAL = {
    9: [
        at_home(9, 0, 0, 9, 0),
        activity("in passenger vehicle", d(9, 9, 0), d(9, 10, 5),
                 "SYNTH_HOME_0001", "SYNTH_BEACH_0015", 61000),
        visit("SYNTH_BEACH_0015", d(9, 10, 5), d(9, 13, 30)),
        activity("walking", d(9, 13, 30), d(9, 13, 45),
                 "SYNTH_BEACH_0015", "SYNTH_CHOWDER_0016", 800),
        visit("SYNTH_CHOWDER_0016", d(9, 13, 45), d(9, 15, 0)),
        activity("in passenger vehicle", d(9, 15, 0), d(9, 16, 10),
                 "SYNTH_CHOWDER_0016", "SYNTH_HOME_0001", 61500),
        at_home(9, 16, 10, 23, 59),
    ],
    20: [
        at_home(20, 0, 0, 17, 50),
        activity("in passenger vehicle", d(20, 18, 0), d(20, 18, 40),
                 "SYNTH_HOME_0001", "SYNTH_FARM_0017", 31000),
        visit("SYNTH_FARM_0017", d(20, 18, 40), d(20, 19, 20)),
        activity("in passenger vehicle", d(20, 19, 20), d(20, 20, 0),
                 "SYNTH_FARM_0017", "SYNTH_HOME_0001", 31000),
        at_home(20, 20, 0, 23, 59),
    ],
    23: [
        at_home(23, 0, 0, 9, 0),
        activity("in passenger vehicle", d(23, 9, 0), d(23, 11, 0),
                 "SYNTH_HOME_0001", "SYNTH_CABIN_0018", 152000),
        visit("SYNTH_CABIN_0018", d(23, 11, 0), d(23, 13, 0)),
        activity("walking", d(23, 13, 0), d(23, 13, 40),
                 "SYNTH_CABIN_0018", "SYNTH_TRAIL_0019", 4800),
        visit("SYNTH_TRAIL_0019", d(23, 13, 40), d(23, 16, 30)),
        activity("walking", d(23, 16, 30), d(23, 17, 10),
                 "SYNTH_TRAIL_0019", "SYNTH_CABIN_0018", 4800),
        # Crosses midnight: an overnight away from home.
        visit("SYNTH_CABIN_0018", d(23, 17, 10), d(24, 9, 30)),
    ],
    24: [
        activity("in passenger vehicle", d(24, 9, 30), d(24, 9, 45),
                 "SYNTH_CABIN_0018", "SYNTH_LAKEDINER_0020", 2100),
        visit("SYNTH_LAKEDINER_0020", d(24, 9, 45), d(24, 10, 45)),
        activity("in passenger vehicle", d(24, 10, 45), d(24, 12, 50),
                 "SYNTH_LAKEDINER_0020", "SYNTH_HOME_0001", 153000),
        at_home(24, 12, 50, 23, 59),
    ],
    28: [],  # only raw fixes know about this day (see RAW_DAY)
}


def build():
    r = []
    for day in range(1, 32):
        if 13 <= day <= 17:
            continue  # the Lisbon journey covers these (13th is its home day)
        r += SPECIAL.get(day, ordinary(day)) if day in SPECIAL else ordinary(day)
    r += journey.build()
    r.sort(key=lambda e: e["startTime"])
    return r


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: make_synthetic_season.py <out.json>")
    with open(sys.argv[1], "w") as f:
        json.dump(build(), f, indent=2)
        f.write("\n")
    with open(sys.argv[1].replace(".json", "_place_names.json"), "w") as f:
        json.dump(PLACE_NAMES, f, indent=2)
        f.write("\n")
    print(f"wrote {sys.argv[1]}")
