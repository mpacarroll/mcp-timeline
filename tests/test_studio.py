#!/usr/bin/env python3
"""Tests for the history studio's data model: capture, editing, source
inspection, grants and proposals. Synthetic data only.

Run: python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import demo_studio  # noqa: E402
import ingest  # noqa: E402
import studio  # noqa: E402
import studio_export  # noqa: E402

JOURNEY = os.path.join(ROOT, "tests", "fixtures", "synthetic_journey.json")


class FreshDb(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        ingest.ingest(JOURNEY, self.db_path)
        self.db = studio.connect(self.db_path)

    def tearDown(self):
        self.db.close()
        os.remove(self.db_path)

    def item(self, trip_id, label, date=None):
        return next(i for i in studio.items_for_trip(self.db, trip_id)
                    if (i["label"] or i["source_label"]) == label
                    and (date is None or i["local_date"] == date))


class Capture(FreshDb):
    def test_trip_captures_only_its_days(self):
        out = studio.create_trip(self.db, "Lisbon", "2026-05-14", "2026-05-17")
        items = studio.items_for_trip(self.db, out["trip_id"])
        self.assertEqual(out["items"], len(items))
        self.assertEqual({i["local_date"] for i in items},
                         {"2026-05-14", "2026-05-15", "2026-05-16", "2026-05-17"})
        self.assertFalse(any(i["local_date"] == "2026-05-13" for i in items))

    def test_routes_are_captured_with_movements(self):
        tid = studio.create_trip(self.db, "Lisbon", "2026-05-16", "2026-05-16")["trip_id"]
        walk = next(i for i in studio.items_for_trip(self.db, tid)
                    if i["kind"] == "movement" and i["start_utc"] == "2026-05-16T08:00:00Z")
        self.assertEqual(len(walk["points"]), 9)

    def test_overnight_end_gets_the_right_local_date(self):
        tid = studio.create_trip(self.db, "Lisbon", "2026-05-14", "2026-05-14")["trip_id"]
        flight = next(i for i in studio.items_for_trip(self.db, tid) if i["mode"] == "flying")
        self.assertEqual(flight["start_local"], "2026-05-14T18:30:00")
        self.assertEqual(flight["end_local"], "2026-05-15T06:15:00")

    def test_items_are_in_time_order_across_a_time_zone_change(self):
        tid = studio.create_trip(self.db, "Home", "2026-05-17", "2026-05-17")["trip_id"]
        utc = [i["start_utc"] for i in studio.items_for_trip(self.db, tid)]
        self.assertEqual(utc, sorted(utc))

    def test_validation(self):
        bad = [("", "2026-05-14", "2026-05-17"), ("x", "May 14", "2026-05-17"),
               ("x", "2026-05-17", "2026-05-14"), ("x", "2026-01-01", "2026-12-31")]
        for args in bad:
            with self.assertRaises(studio.StudioError, msg=args):
                studio.create_trip(self.db, *args)

    def test_empty_range_is_a_valid_empty_trip(self):
        out = studio.create_trip(self.db, "Quiet", "2025-01-01", "2025-01-02")
        self.assertEqual(out["items"], 0)
        view = studio_export.build_view(self.db, out["trip_id"], "owner")
        self.assertEqual(view["days"], [])

    def test_raw_fix_days_are_reconstructed_and_labeled(self):
        # An OwnTracks-style day: fixes only, no visit or activity rows.
        os.environ["TZ"] = "UTC"
        time.tzset()
        try:
            rows = []
            for m in range(0, 40, 2):  # 40 minutes at one spot
                rows.append(("owntracks", f"2026-06-01T09:{m:02d}:00Z", 10.0, 10.0))
            for k, m in enumerate(range(40, 60, 2)):  # then walk away
                rows.append(("owntracks", f"2026-06-01T09:{m:02d}:00Z",
                             10.0 + 0.001 * (k + 1), 10.0))
            self.db.executemany("INSERT INTO path_points (source, ts_utc, lat, lng)"
                                " VALUES (?,?,?,?)", rows)
            self.db.commit()
            tid = studio.create_trip(self.db, "Raw", "2026-06-01", "2026-06-01")["trip_id"]
            items = studio.items_for_trip(self.db, tid)
            self.assertEqual([i["kind"] for i in items], ["visit", "movement"])
            self.assertEqual(items[0]["label_source"], "reconstructed from raw fixes")
            self.assertEqual(items[1]["mode"], "unknown")
            insp = studio.inspect_item(self.db, items[0]["item_id"])
            self.assertEqual(insp["source_status"], "present")
        finally:
            del os.environ["TZ"]
            time.tzset()

    def test_recapture_adds_new_rows_and_keeps_edits(self):
        tid = studio.create_trip(self.db, "Lisbon", "2026-05-15", "2026-05-15")["trip_id"]
        hotel = self.item(tid, "Inferred Accommodation", "2026-05-15")
        studio.edit_item(self.db, hotel["item_id"], label="Our hotel")
        self.db.execute("INSERT INTO visits (place_id, start_utc, end_utc, start_local,"
                        " end_local, local_date, duration_min) VALUES (?,?,?,?,?,?,?)",
                        ("SYNTH_CAFE_0012", "2026-05-15T22:00:00Z", "2026-05-15T22:30:00Z",
                         "23:00:00", "23:30:00", "2026-05-15", 30))
        self.db.commit()
        self.assertEqual(studio.recapture(self.db, tid), {"added": 1})
        self.assertEqual(studio.recapture(self.db, tid), {"added": 0})
        self.assertEqual(studio.get_item_row(self.db, hotel["item_id"])["label"], "Our hotel")


class Editing(FreshDb):
    def setUp(self):
        super().setUp()
        self.tid = studio.create_trip(self.db, "Lisbon", "2026-05-14",
                                      "2026-05-17")["trip_id"]

    def test_edit_keeps_the_source_snapshot_and_logs_history(self):
        it = self.item(self.tid, "Inferred Accommodation", "2026-05-15")
        studio.edit_item(self.db, it["item_id"], label="Hotel Rio Azul",
                         caption="Room 4")
        studio.edit_item(self.db, it["item_id"], label="The hotel")
        insp = studio.inspect_item(self.db, it["item_id"])
        self.assertEqual(insp["as_captured"]["source_label"], "Inferred Accommodation")
        self.assertEqual(insp["your_changes"]["label"], "The hotel")
        self.assertEqual([(e["field"], e["old"], e["new"]) for e in insp["edit_history"]],
                         [("label", None, "Hotel Rio Azul"), ("caption", None, "Room 4"),
                          ("label", "Hotel Rio Azul", "The hotel")])
        self.assertEqual(insp["source_status"], "present")
        # The history itself is untouched.
        self.assertEqual(self.db.execute(
            "SELECT semantic_type FROM places WHERE place_id = 'SYNTH_HOTEL_0005'"
        ).fetchone()[0], "Inferred Accommodation")

    def test_inspect_reports_a_vanished_source_row(self):
        it = self.item(self.tid, "Inferred Accommodation", "2026-05-15")
        self.db.execute("DELETE FROM visits WHERE start_utc = ?", (it["start_utc"],))
        self.db.commit()
        insp = studio.inspect_item(self.db, it["item_id"])
        self.assertTrue(insp["source_status"].startswith("source row no longer present"))
        self.assertEqual(insp["as_captured"]["start_utc"], it["start_utc"])

    def test_inspect_lists_redactions_that_apply(self):
        it = self.item(self.tid, "unnamed place", "2026-05-15")
        studio.add_redaction(self.db, "item", "mask", target=it["item_id"])
        insp = studio.inspect_item(self.db, it["item_id"])
        self.assertEqual(insp["redactions"][0]["kind"], "item")

    def test_naming_a_place_relabels_existing_items(self):
        studio.name_place(self.db, "SYNTH_CASTLE_0010", "Sao Jorge Castle")
        it = self.item(self.tid, "Sao Jorge Castle")
        self.assertEqual(it["label_source"], "named by you")
        with self.assertRaises(studio.StudioError):
            studio.name_place(self.db, "NOPE", "x")
        with self.assertRaises(studio.StudioError):
            studio.name_place(self.db, "SYNTH_CASTLE_0010", "  ")

    def test_notes_sort_into_the_day_by_local_time(self):
        studio.add_note(self.db, self.tid, "2026-05-16", "tiles", "13:40")
        studio.add_note(self.db, self.tid, "2026-05-16", "untimed")
        day = [i for i in studio.items_for_trip(self.db, self.tid)
               if i["local_date"] == "2026-05-16"]
        names = [i["caption"] if i["kind"] == "note" else i["start_local"][11:16]
                 for i in day]
        self.assertEqual(names[names.index("tiles") - 1], "13:00")
        self.assertEqual(names[-1], "untimed")
        with self.assertRaises(studio.StudioError):
            studio.add_note(self.db, self.tid, "2026-06-01", "outside the trip")

    def test_story_override_and_revert(self):
        studio.set_story(self.db, self.tid, "2026-05-15", "My words.")
        view = studio_export.build_view(self.db, self.tid, "owner")
        d = next(d for d in view["days"] if d["date"] == "2026-05-15")
        self.assertEqual((d["story"], d["story_source"]), ("My words.", "written by you"))
        studio.set_story(self.db, self.tid, "2026-05-15", "")
        view = studio_export.build_view(self.db, self.tid, "owner")
        d = next(d for d in view["days"] if d["date"] == "2026-05-15")
        self.assertEqual(d["story_source"], "auto-drafted")

    def test_redaction_validation(self):
        bad = [dict(kind="planet"), dict(kind="place", target="NOPE"),
               dict(kind="area", lat=1, lng=1, radius_m=5),
               dict(kind="area", lat=100, lng=1, radius_m=100),
               dict(kind="date", target="soon"), dict(kind="item", target="i_nope"),
               dict(kind="place", target="SYNTH_CAFE_0012", action="blur")]
        for kw in bad:
            with self.assertRaises(studio.StudioError, msg=kw):
                studio.add_redaction(self.db, **kw)


class Grants(FreshDb):
    def setUp(self):
        super().setUp()
        self.tid = studio.create_trip(self.db, "Lisbon", "2026-05-14",
                                      "2026-05-17")["trip_id"]

    def test_only_the_token_hash_is_stored(self):
        g = studio.create_grant(self.db, self.tid, "Claude")
        dump = "\n".join(self.db.iterdump())
        self.assertNotIn(g["token"], dump)
        self.assertEqual(studio.resolve_grant(self.db, g["token"], "t")["trip_id"], self.tid)

    def test_refusals_are_logged(self):
        g = studio.create_grant(self.db, self.tid, "Claude")
        with self.assertRaises(studio.StudioError):
            studio.resolve_grant(self.db, "tlg_wrong", "studio_trip")
        with self.assertRaises(studio.StudioError):
            studio.resolve_grant(self.db, g["token"], "studio_propose", need="suggest")
        studio.revoke_grant(self.db, g["grant_id"])
        with self.assertRaises(studio.StudioError):
            studio.resolve_grant(self.db, g["token"], "studio_trip")
        outcomes = [r["outcome"] for r in studio.access_log(self.db)]
        self.assertEqual(outcomes, ["refused: revoked", "refused: lacks suggest",
                                    "refused: unknown grant"])

    def test_expired_grant_is_refused(self):
        g = studio.create_grant(self.db, self.tid, "Claude", hours=1)
        with mock.patch.object(studio, "now_utc", return_value="2999-01-01T00:00:00Z"):
            with self.assertRaises(studio.StudioError) as cm:
                studio.resolve_grant(self.db, g["token"], "studio_trip")
            self.assertFalse(studio.list_grants(self.db)[0]["active"])
        self.assertIn("expired", str(cm.exception))

    def test_grant_validation(self):
        for kw in (dict(agent=""), dict(agent="a", capabilities=["write"]),
                   dict(agent="a", capabilities=["suggest"]), dict(agent="a", hours=0),
                   dict(agent="a", hours=24 * 31), dict(agent="a", profile="everyone")):
            with self.assertRaises(studio.StudioError, msg=kw):
                studio.create_grant(self.db, self.tid, **kw)

    def test_proposals_wait_for_a_decision(self):
        g = studio.create_grant(self.db, self.tid, "Claude", ["read", "suggest"])
        grant = studio.resolve_grant(self.db, g["token"], "studio_propose", "suggest")
        it = self.item(self.tid, "Inferred Accommodation", "2026-05-15")
        pid = studio.propose(self.db, grant, "caption", "Rooftop breakfast",
                             item_id=it["item_id"])
        self.assertIsNone(studio.get_item_row(self.db, it["item_id"])["caption"])
        studio.decide_proposal(self.db, pid, accept=True)
        self.assertEqual(studio.get_item_row(self.db, it["item_id"])["caption"],
                         "Rooftop breakfast")
        sid = studio.propose(self.db, grant, "story", "A day of trams.",
                             local_date="2026-05-15")
        studio.decide_proposal(self.db, sid, accept=False)
        self.assertIsNone(self.db.execute("SELECT 1 FROM studio_story").fetchone())
        with self.assertRaises(studio.StudioError):
            studio.decide_proposal(self.db, sid, accept=True)
        with self.assertRaises(studio.StudioError):
            studio.propose(self.db, grant, "story", "x", local_date="2026-07-01")

    def test_proposal_cannot_reach_another_trip(self):
        other = studio.create_trip(self.db, "Before", "2026-05-13", "2026-05-13")["trip_id"]
        g = studio.create_grant(self.db, self.tid, "Claude", ["read", "suggest"])
        grant = studio.resolve_grant(self.db, g["token"], "t", "suggest")
        foreign = studio.items_for_trip(self.db, other)[0]
        with self.assertRaises(studio.StudioError):
            studio.propose(self.db, grant, "caption", "x", item_id=foreign["item_id"])


class DemoBuild(unittest.TestCase):
    def test_demo_builds_and_token_file_is_owner_only(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            out = demo_studio.build(path, quiet=True)
            self.assertEqual(os.stat(out["token_path"]).st_mode & 0o777, 0o600)
            db = studio.connect(path)
            self.assertEqual(len(studio.list_trips(db)), 1)
            self.assertEqual(len(studio.list_redactions(db)), 3)
            db.close()
        finally:
            for p in (path, path + ".grant"):
                if os.path.exists(p):
                    os.remove(p)


if __name__ == "__main__":
    unittest.main()
