#!/usr/bin/env python3
"""Proof that redaction and deletion reach every way out of the studio.

Builds the synthetic Lisbon journey (tools/demo_studio.py: invented
people, invented home, invented clinic) and renders every export format
under every disclosure preset plus custom profiles, then asserts that
the redacted and deleted facts appear in none of them: not as names, not
in free text the person wrote, not as coordinates at any precision that
could locate them.

New export formats are covered automatically: the tests iterate over
studio_export.EXPORTERS rather than a hand-kept list.

Run: python3 -m unittest discover -s tests
"""

import json
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import demo_studio  # noqa: E402
import studio  # noqa: E402
import studio_export  # noqa: E402

CLINIC = (38.72265, -9.14791)
FRIEND = (38.73613, -9.15427)
HOME = (42.39012, -71.10234)
MARKET = (38.70700, -9.14590)
CASTLE = (38.71390, -9.13340)

CUSTOM_PROFILES = [
    {"base": "owner", "name": "everything but provenance", "sources": False},
    {"base": "family", "name": "family with routes", "routes": True,
     "coordinates": "exact"},
    {"base": "public", "name": "one day", "days": ["2026-05-15"], "captions": True},
]


def coord_needles(lat, lng):
    """Strings that would locate a point to within roughly 100m or better.

    Two-decimal values (about 1km) are not included: a neighborhood-level
    profile legitimately rounds other stops in the same district to the
    same value, and that coarseness is the point of the profile.
    """
    out = set()
    for v in (lat, lng):
        for places in (3, 4, 5):
            out.add(f"{round(v, places):.{places}f}".rstrip("0").rstrip("."))
            out.add(f"{v:.{places}f}")
    return {n for n in out if len(n.split(".")[-1]) >= 3}


def number_in(needle, text):
    """True when needle appears as a whole number, so 38.714 does not
    match inside 38.7148 (a different, unredacted place)."""
    return re.search(r"(?<![\d.])" + re.escape(needle) + r"(?![\d])", text) is not None


def all_renders(db, trip_id):
    profiles = list(studio.PROFILES) + CUSTOM_PROFILES
    for profile in profiles:
        for fmt in studio_export.EXPORTERS:
            name = profile if isinstance(profile, str) else profile["name"]
            yield f"{fmt}/{name}", studio_export.render(db, trip_id, fmt, profile)


class StudioCase(unittest.TestCase):
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

    def assert_absent_everywhere(self, words=(), points=()):
        renders = list(all_renders(self.db, self.trip_id))
        self.assertGreaterEqual(len(renders), len(studio_export.EXPORTERS) * 7)
        for where, text in renders:
            for w in words:
                m = re.search(r"\b" + re.escape(w), text, re.IGNORECASE)
                self.assertIsNone(m, f"{w!r} leaked into {where}: "
                                     f"...{text[max(0, m.start() - 60):m.end() + 60]}..."
                                  if m else "")
            for lat, lng in points:
                for needle in coord_needles(lat, lng):
                    self.assertFalse(number_in(needle, text),
                                     f"{needle} ({lat},{lng}) leaked into {where}")
        return renders


class RedactionReachesEveryExport(StudioCase):
    def test_redacted_places_never_appear(self):
        self.assert_absent_everywhere(
            words=["Lumen", "Dermatology", "rash", "prescription", "Ines"],
            points=[CLINIC, FRIEND, HOME])

    def test_masked_stops_still_say_something_happened(self):
        view = studio_export.build_view(self.db, self.trip_id, "owner")
        labels = [e["label"] for d in view["days"] for e in d["items"]]
        self.assertIn("Appointment", labels)
        self.assertIn("a friend's place", labels)
        self.assertIn("Home", labels)
        masked = [e for d in view["days"] for e in d["items"] if e.get("redacted")]
        for e in masked:
            self.assertNotIn("lat", e)
            self.assertNotIn("route", e)
            self.assertNotIn("caption", e)

    def test_story_text_is_scrubbed_not_just_labels(self):
        view = studio_export.build_view(self.db, self.trip_id, "owner")
        day = next(d for d in view["days"] if d["date"] == "2026-05-15")
        self.assertIn("Quick stop at Appointment", day["story"])
        self.assertEqual(day["story_source"], "written by you")

    def test_routes_into_a_redacted_stop_are_dropped(self):
        view = studio_export.build_view(self.db, self.trip_id, "owner")
        d15 = next(d for d in view["days"] if d["date"] == "2026-05-15")
        walk_in = next(e for e in d15["items"]
                       if e["kind"] == "movement" and e.get("start") == "11:00")
        self.assertNotIn("route", walk_in)
        self.assertNotIn("end_lat", walk_in)
        # A route that does not touch a redacted stop is kept.
        d16 = next(d for d in view["days"] if d["date"] == "2026-05-16")
        castle_walk = next(e for e in d16["items"]
                           if e["kind"] == "movement" and e.get("start") == "09:00")
        self.assertGreater(len(castle_walk["route"]), 2)

    def test_the_view_reports_a_count_not_what_was_redacted(self):
        view = studio_export.build_view(self.db, self.trip_id, "owner")
        # Clinic, friend's apartment, and the arrival home on the 17th.
        self.assertEqual(view["redacted_items"], 3)
        self.assertNotIn("note", json.dumps(view["disclosure"]))

    def test_removing_a_redaction_restores_the_item(self):
        rid = next(r["redaction_id"] for r in studio.list_redactions(self.db)
                   if r["kind"] == "place")
        studio.remove_redaction(self.db, rid)
        md = studio_export.render(self.db, self.trip_id, "md", "owner")
        self.assertIn("Lumen Dermatology Clinic", md)

    def test_remove_action_leaves_no_trace(self):
        studio.add_redaction(self.db, "place", "remove", trip_id=self.trip_id,
                             target="SYNTH_CAFE_0012")
        for where, text in all_renders(self.db, self.trip_id):
            self.assertFalse("Andorinha" in text, where)
            # A removed stop is not replaced by a placeholder entry, and
            # your own sentence about it reads "[removed]", not a label
            # that implies a hidden place.
            self.assertFalse("Private place" in text, where)
        view = studio_export.build_view(self.db, self.trip_id, "owner")
        d16 = next(d for d in view["days"] if d["date"] == "2026-05-16")
        self.assertIn("coffee at [removed]", d16["story"])

    def test_date_redaction_removes_the_whole_day(self):
        studio.add_redaction(self.db, "date", "remove", trip_id=self.trip_id,
                             target="2026-05-16")
        view = studio_export.build_view(self.db, self.trip_id, "owner")
        d16 = [d for d in view["days"] if d["date"] == "2026-05-16"]
        self.assertTrue(all(not d["items"] for d in d16))
        self.assert_absent_everywhere(words=["Sao Jorge", "Andorinha"],
                                      points=[CASTLE])

    def test_global_redaction_applies_to_new_trips(self):
        other = studio.create_trip(self.db, "Week before", "2026-05-13", "2026-05-13")
        md = studio_export.render(self.db, other["trip_id"], "md", "owner")
        self.assertIn("Home", md)
        for needle in coord_needles(*HOME):
            for fmt in studio_export.EXPORTERS:
                self.assertFalse(number_in(needle, studio_export.render(
                    self.db, other["trip_id"], fmt, "owner")), needle)

    def test_hidden_items_appear_in_no_export(self):
        item = next(i for i in studio.items_for_trip(self.db, self.trip_id)
                    if i["source_label"] == "Riverside Market")
        studio.edit_item(self.db, item["item_id"], hidden=True)
        for where, text in all_renders(self.db, self.trip_id):
            self.assertFalse(item["item_id"] in text, where)

    def test_share_page_follows_later_redactions(self):
        share = studio.get_share(self.db, self.demo["share_id"])
        before = studio_export.render(self.db, share["trip_id"], "html", share["profile"])
        self.assertIn("Riverside Market", before)
        studio.add_redaction(self.db, "place", "mask", target="SYNTH_MARKET_0007",
                             replacement="Lunch spot")
        after = studio_export.render(self.db, share["trip_id"], "html", share["profile"])
        self.assertNotIn("Riverside Market", after)
        self.assertIn("Lunch spot", after)


class DeletionReachesEveryExport(StudioCase):
    def test_deleted_item_and_its_name_in_the_story_are_gone(self):
        item = next(i for i in studio.items_for_trip(self.db, self.trip_id)
                    if i["source_label"] == "Riverside Market")
        studio.delete_item(self.db, item["item_id"])
        self.assert_absent_everywhere(words=["Riverside Market"], points=[MARKET])
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM studio_edits WHERE item_id = ?",
            (item["item_id"],)).fetchone()[0], 0)

    def test_deleted_note_text_is_gone(self):
        note = next(i for i in studio.items_for_trip(self.db, self.trip_id)
                    if i["kind"] == "note")
        studio.delete_item(self.db, note["item_id"])
        self.assert_absent_everywhere(words=["backsplash"])

    def test_purge_removes_history_trip_items_story_and_names(self):
        counts = studio.purge_history(self.db, "2026-05-16", "2026-05-16")
        self.assertGreater(counts["visits"], 0)
        self.assertGreater(counts["raw_fixes"], 0)
        self.assertGreater(counts["trip_items"], 0)
        self.assertEqual(counts["story_days"], 1)
        self.assert_absent_everywhere(
            words=["Sao Jorge", "Andorinha", "Graca", "backsplash", "pastel"],
            points=[CASTLE])
        # Gone from the source history itself, not only from the trip.
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM visits WHERE local_date = '2026-05-16'").fetchone()[0], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM activities WHERE local_date = '2026-05-16'"
        ).fetchone()[0], 0)
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM places WHERE place_id = 'SYNTH_CASTLE_0010'").fetchone())
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM studio_place_labels WHERE place_id = 'SYNTH_CASTLE_0010'"
        ).fetchone())
        # Places that still have visits on other days survive with fixed counts.
        hotel = self.db.execute("SELECT visit_count FROM places WHERE place_id ="
                                " 'SYNTH_HOTEL_0005'").fetchone()
        self.assertEqual(hotel["visit_count"], 2)
        md = studio_export.render(self.db, self.trip_id, "md", "owner")
        self.assertIn("Hotel Rio Azul", md)

    def test_purge_reaches_the_geocode_cache(self):
        import geocode
        key = geocode.cache_key(*CASTLE)
        self.db.execute("INSERT INTO geocode_cache VALUES (?,?,?,?)",
                        (key[0], key[1], "Castle Hill", studio.now_utc()))
        self.db.commit()
        studio.purge_history(self.db, "2026-05-16", "2026-05-16")
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM geocode_cache WHERE lat_key = ? AND lng_key = ?", key).fetchone())

    def test_deleted_trip_takes_shares_and_grants_with_it(self):
        studio.delete_trip(self.db, self.trip_id)
        with self.assertRaises(studio.StudioError):
            studio.get_share(self.db, self.demo["share_id"])
        with self.assertRaises(studio.StudioError):
            studio_export.build_view(self.db, self.trip_id, "owner")
        with self.assertRaises(studio.StudioError):
            studio.resolve_grant(self.db, self.demo["grant"]["token"], "studio_trip")
        for table in ("studio_items", "studio_story", "studio_edits", "studio_shares",
                      "studio_grants", "studio_proposals"):
            self.assertEqual(self.db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE trip_id = ?",
                (self.trip_id,)).fetchone()[0], 0, table)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM studio_item_points"
                                         ).fetchone()[0], 0)


class StaleExports(StudioCase):
    def export_file(self, fmt="md"):
        fd, path = tempfile.mkstemp(suffix="." + fmt)
        content = studio_export.render(self.db, self.trip_id, fmt, "owner")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        studio.log_export(self.db, self.trip_id, fmt, "owner", "file", content, path)
        return path

    def test_redaction_marks_earlier_exports_stale_and_removes_files(self):
        path = self.export_file()
        self.assertIn("Riverside Market", open(path).read())
        studio.log_export(self.db, self.trip_id, "csv", "owner", "download", "x")
        studio.add_redaction(self.db, "place", target="SYNTH_MARKET_0007")
        stale = [x for x in studio.list_exports(self.db) if x["stale_reason"]]
        self.assertEqual(len(stale), 2)
        out = studio.remove_stale_exports(self.db)
        self.assertEqual(out["removed"], [path])
        self.assertFalse(os.path.exists(path))
        self.assertEqual(len(out["outside_studio"]), 1)

    def test_hand_edited_export_is_reported_not_destroyed(self):
        path = self.export_file()
        with open(path, "a") as f:
            f.write("my own notes\n")
        item = next(i for i in studio.items_for_trip(self.db, self.trip_id)
                    if i["source_label"] == "Belem Tower")
        studio.delete_item(self.db, item["item_id"])
        out = studio.remove_stale_exports(self.db)
        self.assertEqual(out["removed"], [])
        self.assertEqual(out["kept"][0]["path"], path)
        self.assertTrue(os.path.exists(path))
        os.remove(path)

    def test_purge_marks_exports_stale(self):
        path = self.export_file("geojson")
        studio.purge_history(self.db, "2026-05-15", "2026-05-15")
        self.assertTrue(all(x["stale_reason"] for x in studio.list_exports(self.db)))
        studio.remove_stale_exports(self.db)
        self.assertFalse(os.path.exists(path))


class Disclosure(StudioCase):
    def test_family_profile_has_no_exact_times_or_fine_coordinates(self):
        view = studio_export.build_view(self.db, self.trip_id, "family")
        items = [e for d in view["days"] for e in d["items"]]
        self.assertTrue(items)
        for e in items:
            self.assertNotIn("start", e)
            self.assertNotIn("route", e)
            self.assertNotIn("source", e)
            if "lat" in e:
                self.assertEqual(e["lat"], round(e["lat"], 2))

    def test_public_profile_has_no_movement_captions_or_times(self):
        view = studio_export.build_view(self.db, self.trip_id, "public")
        for e in (e for d in view["days"] for e in d["items"]):
            self.assertNotEqual(e["kind"], "movement")
            self.assertNotIn("caption", e)
            self.assertNotIn("part_of_day", e)
            self.assertNotIn("duration_min", e)
        text = studio_export.render(self.db, self.trip_id, "md", "public")
        self.assertNotIn("rain shower", text)

    def test_days_filter_shares_only_chosen_days(self):
        view = studio_export.build_view(self.db, self.trip_id,
                                        {"base": "family", "days": ["2026-05-16"]})
        self.assertEqual([d["date"] for d in view["days"]], ["2026-05-16"])
        text = studio_export.render(self.db, self.trip_id, "md",
                                    {"base": "family", "days": ["2026-05-16"]})
        self.assertNotIn("Belem", text)

    def test_overnight_flight_and_midnight_stay_carry_next_day(self):
        view = studio_export.build_view(self.db, self.trip_id, "owner")
        d14 = next(d for d in view["days"] if d["date"] == "2026-05-14")
        flight = next(e for e in d14["items"] if e.get("mode") == "flying")
        self.assertEqual((flight["start"], flight["end"], flight["end_day_offset"]),
                         ("18:30", "06:15", 1))
        self.assertEqual(flight["duration_min"], 405)
        d15 = next(d for d in view["days"] if d["date"] == "2026-05-15")
        hotel = d15["items"][-1]
        self.assertEqual((hotel["label"], hotel["end"], hotel["end_day_offset"]),
                         ("Hotel Rio Azul", "09:00", 1))
        d17 = next(d for d in view["days"] if d["date"] == "2026-05-17")
        home_flight = next(e for e in d17["items"] if e.get("mode") == "flying")
        self.assertNotIn("end_day_offset", home_flight)
        self.assertEqual(home_flight["duration_min"], 450)

    def test_unknown_profile_fields_are_refused(self):
        with self.assertRaises(studio.StudioError):
            studio_export.build_view(self.db, self.trip_id, {"base": "owner",
                                                             "secret_mode": True})
        with self.assertRaises(studio.StudioError):
            studio_export.build_view(self.db, self.trip_id, {"times": "to the second"})
        with self.assertRaises(studio.StudioError):
            studio_export.build_view(self.db, self.trip_id, "everyone")

    def test_gpx_and_geojson_are_well_formed(self):
        import xml.dom.minidom
        xml.dom.minidom.parseString(studio_export.render(self.db, self.trip_id, "gpx",
                                                         "owner"))
        gj = json.loads(studio_export.render(self.db, self.trip_id, "geojson", "owner"))
        self.assertEqual(gj["type"], "FeatureCollection")
        self.assertTrue(any(f["geometry"]["type"] == "LineString" for f in gj["features"]))

    def test_html_export_escapes_user_text_and_has_no_scripts(self):
        item = next(i for i in studio.items_for_trip(self.db, self.trip_id)
                    if i["source_label"] == "Belem Tower")
        studio.edit_item(self.db, item["item_id"], caption="<script>alert(1)</script>")
        page = studio_export.render(self.db, self.trip_id, "html", "owner")
        self.assertNotIn("<script", page)
        self.assertIn("&lt;script&gt;", page)


if __name__ == "__main__":
    unittest.main()
