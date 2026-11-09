#!/usr/bin/env python3
"""Regressions for defects found reviewing the first studio slice.

Each test reproduces a leak or scope escape that the original suite
missed, against the same synthetic Lisbon journey:

- A removed day kept the story you wrote for it.
- "Redact this place" missed a stop rebuilt from raw fixes at the same
  spot, and the raw route into it (raw movements store no endpoints).
- Deleting a raw-fix stop left the raw route that ends there.
- studio_scope returned the trip title without redaction.
- An agent could propose story text for a day its grant does not show.
- Revoking a grant left its proposals one click from being accepted.

Run: python3 -m unittest discover -s tests
"""

import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("TIMELINE_TOOLS", "all")
import demo_studio  # noqa: E402
import server  # noqa: E402
import studio  # noqa: E402
import studio_export  # noqa: E402

CLINIC = (38.72265, -9.14791)
ERRAND_DAY = "2026-05-20"


def add_raw_day_at_clinic(db):
    """An OwnTracks-only day: 40 minutes at the clinic, a walk north, a
    stay somewhere else. No visit or activity rows, so the studio rebuilds
    it from raw fixes with no place ids and no movement endpoints."""
    base = datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
    rows = [(base + timedelta(minutes=2 * i), CLINIC[0] + 0.00001 * (i % 2), CLINIC[1])
            for i in range(20)]
    rows += [(base + timedelta(minutes=40 + 2 * i), CLINIC[0] + 0.001 * i, CLINIC[1])
             for i in range(1, 15)]
    rows += [(base + timedelta(minutes=70 + 2 * i), CLINIC[0] + 0.015, CLINIC[1])
             for i in range(20)]
    for ts, lat, lng in rows:
        db.execute("INSERT INTO path_points (source, ts_utc, lat, lng) VALUES (?,?,?,?)",
                   ("owntracks", ts.strftime("%Y-%m-%dT%H:%M:%SZ"), lat, lng))
    db.commit()


def near_clinic(lat, lng, m=200):
    from geo import haversine_m
    return haversine_m(lat, lng, *CLINIC) <= m


class Base(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.demo = demo_studio.build(self.db_path, quiet=True)
        self.trip_id = self.demo["trip_id"]
        self.db = studio.connect(self.db_path)

    def tearDown(self):
        self.db.close()
        for p in (self.db_path, self.db_path + ".grant"):
            if os.path.exists(p):
                os.remove(p)


class RedactedDayTakesItsStory(Base):
    SECRET = "Vet visit for the dog, paid the bill"

    def test_removed_day_leaves_no_story_in_any_export(self):
        studio.set_story(self.db, self.trip_id, "2026-05-16", self.SECRET)
        studio.add_redaction(self.db, "date", "remove", trip_id=self.trip_id,
                             target="2026-05-16")
        for profile in studio.PROFILES:
            view = studio_export.build_view(self.db, self.trip_id, profile)
            self.assertNotIn("2026-05-16", [d["date"] for d in view["days"]])
            for fmt in studio_export.EXPORTERS:
                out = studio_export.render(self.db, self.trip_id, fmt, profile)
                self.assertNotIn("Vet visit", out, f"{fmt}/{profile}")

    def test_masked_day_shows_only_the_auto_draft(self):
        studio.set_story(self.db, self.trip_id, "2026-05-16", self.SECRET)
        studio.add_redaction(self.db, "date", "mask", trip_id=self.trip_id,
                             target="2026-05-16", replacement="Private day")
        view = studio_export.build_view(self.db, self.trip_id, "owner")
        day = next(d for d in view["days"] if d["date"] == "2026-05-16")
        self.assertEqual(day["story_source"], "auto-drafted")
        self.assertNotIn("Vet", day["story"])
        self.assertTrue(all(e.get("redacted") for e in day["items"]))


class PlaceRedactionCoversRawFixStops(Base):
    def setUp(self):
        super().setUp()
        add_raw_day_at_clinic(self.db)
        self.errand = studio.create_trip(self.db, "Errand day", ERRAND_DAY,
                                         ERRAND_DAY)["trip_id"]

    def test_fixture_really_has_an_unplaced_stop_at_the_clinic(self):
        items = studio.items_for_trip(self.db, self.errand)
        stop = [i for i in items if i["kind"] == "visit" and i["lat"] is not None
                and near_clinic(i["lat"], i["lng"])]
        self.assertEqual(len(stop), 1)
        self.assertIsNone(stop[0]["place_id"])
        raw_moves = [i for i in items if i["kind"] == "movement"]
        self.assertTrue(raw_moves and raw_moves[0]["lat"] is None and raw_moves[0]["points"])

    def test_global_place_redaction_hides_the_raw_stop_and_route(self):
        studio.add_redaction(self.db, "place", "remove", target="SYNTH_CLINIC_0006")
        view = studio_export.build_view(self.db, self.errand, "owner")
        for d in view["days"]:
            for e in d["items"]:
                if "lat" in e:
                    self.assertFalse(near_clinic(e["lat"], e["lng"]), e)
                for lat, lng in e.get("route", []):
                    self.assertFalse(near_clinic(lat, lng), e)
        for fmt in studio_export.EXPORTERS:
            out = studio_export.render(self.db, self.errand, fmt, "owner")
            self.assertIsNone(re.search(r"38\.7226[0-9]?(?!\d)", out), fmt)

    def test_masked_raw_stop_keeps_that_something_happened(self):
        studio.add_redaction(self.db, "place", "mask", target="SYNTH_CLINIC_0006",
                             replacement="Appointment")
        view = studio_export.build_view(self.db, self.errand, "owner")
        labels = [e["label"] for d in view["days"] for e in d["items"]]
        self.assertIn("Appointment", labels)

    def test_editor_flags_the_raw_stop_as_redacted(self):
        import studio_web
        studio.add_redaction(self.db, "place", "mask", target="SYNTH_CLINIC_0006")
        stops = [i for d in studio_web.editor_view(self.db, self.errand)["days"]
                 for i in d["items"] if i["kind"] == "visit" and near_clinic(i["lat"], i["lng"])]
        self.assertEqual(stops[0]["redaction"]["action"], "mask")

    def test_unrelated_raw_stop_is_untouched(self):
        studio.add_redaction(self.db, "place", "remove", target="SYNTH_CLINIC_0006")
        view = studio_export.build_view(self.db, self.errand, "owner")
        far = [e for d in view["days"] for e in d["items"]
               if e["kind"] == "visit" and e.get("lat") is not None]
        self.assertEqual(len(far), 1)
        self.assertFalse(near_clinic(far[0]["lat"], far[0]["lng"]))


class DeletingARawStopTrimsTheRawRoute(Base):
    def test_delete_item_drops_route_points_that_end_at_the_stop(self):
        add_raw_day_at_clinic(self.db)
        errand = studio.create_trip(self.db, "Errand day", ERRAND_DAY,
                                    ERRAND_DAY)["trip_id"]
        stop = next(i for i in studio.items_for_trip(self.db, errand)
                    if i["kind"] == "visit" and near_clinic(i["lat"], i["lng"]))
        studio.delete_item(self.db, stop["item_id"])
        for i in studio.items_for_trip(self.db, errand):
            for p in i["points"]:
                self.assertFalse(near_clinic(p["lat"], p["lng"]), i["item_id"])


class AgentScope(Base):
    """Calls the MCP tool functions in process with a real grant token."""

    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.get(k) for k in ("TIMELINE_GRANT",)}
        self._db_path = server.DB_PATH
        server.DB_PATH = self.db_path

    def tearDown(self):
        server.DB_PATH = self._db_path
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def use(self, token):
        os.environ["TIMELINE_GRANT"] = token

    def test_scope_title_is_redacted(self):
        studio.update_trip(self.db, self.trip_id,
                           title="Lisbon and Lumen Dermatology Clinic")
        self.use(self.demo["grant"]["token"])
        scope = server.studio_scope()
        self.assertTrue(scope["allowed"])
        self.assertNotIn("Lumen", scope["trip"]["title"])
        self.assertEqual(scope["trip"]["title"], server.studio_trip()["trip"]["title"])

    def test_story_proposal_outside_shared_days_is_refused(self):
        g = studio.create_grant(self.db, self.trip_id, "narrow agent",
                                ["read", "suggest"],
                                {"base": "agent", "name": "one day",
                                 "days": ["2026-05-15"]}, 1)
        self.use(g["token"])
        self.assertFalse(server.studio_day("2026-05-16")["allowed"])
        self.assertFalse(server.studio_propose("story", "made up", date="2026-05-16")
                         ["allowed"])
        self.assertTrue(server.studio_propose("story", "fine", date="2026-05-15")
                        ["allowed"])

    def test_story_and_caption_proposals_need_those_fields_in_the_view(self):
        g = studio.create_grant(self.db, self.trip_id, "no-words agent",
                                ["read", "suggest"],
                                {"base": "agent", "name": "no words", "story": False,
                                 "captions": False}, 1)
        self.use(g["token"])
        ref = next(e["ref"] for d in server.studio_trip()["days"] for e in d["items"]
                   if not e.get("redacted"))
        self.assertFalse(server.studio_propose("story", "x", date="2026-05-15")["allowed"])
        self.assertFalse(server.studio_propose("caption", "x", ref=ref)["allowed"])

    def test_revoking_a_grant_withdraws_its_proposals(self):
        self.use(self.demo["grant"]["token"])
        pid = server.studio_propose("story", "agent words", date="2026-05-15")["proposal_id"]
        studio.revoke_grant(self.db, self.demo["grant"]["grant_id"])
        self.assertEqual(studio.list_proposals(self.db, self.trip_id, "pending"), [])
        with self.assertRaises(studio.StudioError):
            studio.decide_proposal(self.db, pid, True)

    def test_expired_grant_proposal_cannot_be_accepted(self):
        self.use(self.demo["grant"]["token"])
        pid = server.studio_propose("story", "agent words", date="2026-05-15")["proposal_id"]
        self.db.execute("UPDATE studio_grants SET expires_utc = '2000-01-01T00:00:00Z'")
        self.db.commit()
        with self.assertRaises(studio.StudioError):
            studio.decide_proposal(self.db, pid, True)
        studio.decide_proposal(self.db, pid, False)  # rejecting still works


if __name__ == "__main__":
    unittest.main()
