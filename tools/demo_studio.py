#!/usr/bin/env python3
"""Build a demo database for the history studio from synthetic data only.

    python3 tools/demo_studio.py demo.db
    TIMELINE_DB=demo.db python3 studio_web.py

The database holds the invented Lisbon journey from
tests/fixtures/synthetic_journey.json, captured as a trip with the edits a
person would plausibly make: place names, two captions, a note, a story in
their own words, three redactions (home, a clinic visit, a friend's
apartment), a family share page and a read-only agent grant.

The agent grant's token is written to <db>.grant (owner-only) instead of
being printed, so it never lands in a terminal log.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import ingest  # noqa: E402
import studio  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "synthetic_journey.json")
NAMES = os.path.join(ROOT, "tests", "fixtures", "synthetic_journey_place_names.json")


def build(db_path, quiet=False):
    if os.path.exists(db_path):
        os.remove(db_path)
    ingest.ingest(FIXTURE, db_path)
    db = studio.connect(db_path)
    with open(NAMES) as f:
        for place_id, label in json.load(f).items():
            studio.name_place(db, place_id, label)
    trip = studio.create_trip(db, "Lisbon long weekend", "2026-05-14", "2026-05-17",
                              summary="Four days in Lisbon in May.")
    tid = trip["trip_id"]
    items = studio.items_for_trip(db, tid)

    def find(label, date=None):
        return next(i for i in items if i["source_label"] == label
                    and (date is None or i["local_date"] == date))

    studio.edit_item(db, find("Belem Tower")["item_id"],
                     caption="Waited out a rain shower under the arches.")
    studio.edit_item(db, find("Cafe Andorinha")["item_id"],
                     caption="Best pastel de nata of the trip.")
    studio.edit_item(db, find("Lumen Dermatology Clinic")["item_id"],
                     caption="Follow-up on the rash, prescription filled.")
    studio.add_note(db, tid, "2026-05-16", "Bought tiles for the kitchen backsplash.",
                    "13:40")
    studio.set_story(db, tid, "2026-05-15",
                     "Landed before sunrise and slept off the flight at Hotel Rio Azul. "
                     "Quick stop at Lumen Dermatology Clinic, then lunch at Riverside "
                     "Market and the tram out to Belem Tower and the monastery.")
    studio.set_story(db, tid, "2026-05-16",
                     "Climbed to Sao Jorge Castle in the morning, coffee at Cafe "
                     "Andorinha, and a long dinner at Ines's apartment.")

    home = db.execute("SELECT lat, lng FROM places WHERE place_id = 'SYNTH_HOME_0001'"
                      ).fetchone()
    studio.add_redaction(db, "area", "mask", lat=home["lat"], lng=home["lng"],
                         radius_m=400, replacement="Home",
                         note="never share where we live")
    studio.add_redaction(db, "place", "mask", trip_id=tid, target="SYNTH_CLINIC_0006",
                         replacement="Appointment", note="medical")
    friend = db.execute("SELECT lat, lng FROM places WHERE place_id = 'SYNTH_FRIEND_0013'"
                        ).fetchone()
    studio.add_redaction(db, "area", "mask", trip_id=tid, lat=friend["lat"],
                         lng=friend["lng"], radius_m=200, replacement="a friend's place",
                         note="her address is hers to share")

    share = studio.create_share(db, tid, "Mom and Dad", "family")
    grant = studio.create_grant(db, tid, "Claude Desktop", ["read", "suggest"], "agent", 24)
    token_path = db_path + ".grant"
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(grant["token"] + "\n")
    db.close()
    if not quiet:
        print(f"demo database: {db_path}")
        print(f"trip: {tid}")
        print(f"family share page: /share/{share}")
        print(f"agent grant {grant['grant_id']}; token in {token_path} (owner-only)")
        print(f"\nopen the studio:\n  TIMELINE_DB={db_path} python3 studio_web.py")
    return {"trip_id": tid, "share_id": share, "grant": grant, "token_path": token_path}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: demo_studio.py <demo.db>")
    build(sys.argv[1])
