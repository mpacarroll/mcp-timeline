#!/usr/bin/env python3
"""Tests for turning raw location fixes into stays and movement.

All coordinates and times here are invented. Distances use a degree of
latitude as roughly 111km, so 0.001 degrees is about 111 meters, which is
how the fixtures below stay outside a 100m radius on purpose.

Run: python3 -m unittest discover -s tests
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dwell
from geo import haversine_m

T0 = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)


def pt(minutes, lat, lng):
    return (T0 + timedelta(minutes=minutes), lat, lng)


class HaversineTests(unittest.TestCase):
    def test_zero_distance(self):
        self.assertAlmostEqual(haversine_m(10.0, 20.0, 10.0, 20.0), 0.0)

    def test_known_distance_one_degree_latitude(self):
        # One degree of latitude is about 111.2 km anywhere on the globe.
        d = haversine_m(0.0, 0.0, 1.0, 0.0)
        self.assertAlmostEqual(d / 1000, 111.2, delta=0.5)

    def test_symmetric(self):
        a = haversine_m(40.0, -74.0, 41.0, -73.0)
        b = haversine_m(41.0, -73.0, 40.0, -74.0)
        self.assertAlmostEqual(a, b, places=6)


class DeriveEntriesTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(dwell.derive_entries([]), [])

    def test_single_fix_is_not_a_journey(self):
        self.assertEqual(dwell.derive_entries([pt(0, 10.0, 20.0)]), [])

    def test_sitting_still_is_one_stay(self):
        points = [pt(m, 10.0, 20.0) for m in range(0, 61, 5)]
        entries = dwell.derive_entries(points)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["type"], "stay")
        self.assertAlmostEqual(entries[0]["duration_min"], 60.0)
        self.assertEqual(entries[0]["fixes"], 13)

    def test_brief_pause_is_not_a_stay(self):
        # Three minutes in one spot, below the 10 minute threshold, then on.
        points = [pt(0, 10.0, 20.0), pt(1, 10.0, 20.0), pt(3, 10.0, 20.0),
                  pt(6, 10.01, 20.0), pt(9, 10.02, 20.0)]
        entries = dwell.derive_entries(points)
        self.assertTrue(all(e["type"] == "movement" for e in entries), entries)

    def test_stay_move_stay(self):
        points = []
        points += [pt(m, 10.0, 20.0) for m in range(0, 31, 5)]      # 30 min here
        points += [pt(35, 10.005, 20.0), pt(40, 10.010, 20.0)]      # moving
        points += [pt(m, 10.02, 20.0) for m in range(45, 76, 5)]    # 30 min there
        entries = dwell.derive_entries(points)
        kinds = [e["type"] for e in entries]
        self.assertEqual(kinds, ["stay", "movement", "stay"])
        self.assertAlmostEqual(entries[0]["lat"], 10.0, places=4)
        self.assertAlmostEqual(entries[2]["lat"], 10.02, places=4)

    def test_movement_is_merged_not_fragmented(self):
        # A steady walk: every fix is beyond the radius from the last, so
        # each is its own cluster. It must still come back as one segment.
        points = [pt(m, 10.0 + 0.002 * m, 20.0) for m in range(0, 21, 2)]
        entries = dwell.derive_entries(points)
        self.assertEqual(len(entries), 1, entries)
        self.assertEqual(entries[0]["type"], "movement")
        self.assertEqual(entries[0]["fixes"], 11)

    def test_movement_distance_is_path_length(self):
        # Two hops of about 111m each along a line.
        points = [pt(0, 10.0, 20.0), pt(5, 10.001, 20.0), pt(10, 10.002, 20.0)]
        entries = dwell.derive_entries(points, radius_m=50)
        self.assertEqual(len(entries), 1)
        self.assertAlmostEqual(entries[0]["km"], 0.22, delta=0.02)

    def test_radius_controls_what_counts_as_still(self):
        # GPS jitter while sitting still: fixes bounce between two points
        # about 55m apart for an hour, never drifting anywhere. A wide
        # radius reads that as one stay. A radius tighter than the jitter
        # reads every bounce as a hop, and the accumulated back-and-forth
        # is far enough to register as movement. Same data, different call.
        points = [pt(m, 10.0 if (m // 10) % 2 == 0 else 10.0005, 20.0)
                  for m in range(0, 61, 10)]
        wide = dwell.derive_entries(points, radius_m=200)
        narrow = dwell.derive_entries(points, radius_m=20)
        self.assertEqual([e["type"] for e in wide], ["stay"])
        self.assertTrue(all(e["type"] == "movement" for e in narrow), narrow)

    def test_stationary_jitter_below_radius_is_not_reported_as_movement(self):
        # The inverse of the case above, and a real bug once: a run of
        # fixes that never travels as far as the radius must not come back
        # as movement, or the reader is told they went somewhere.
        points = [pt(m, 10.0, 20.0) for m in range(0, 6)]
        entries = dwell.derive_entries(points, min_dwell_min=30)
        self.assertEqual(entries, [],
                         "a run covering no ground is neither a stay nor a trip")

    def test_min_dwell_controls_what_counts_as_a_stay(self):
        points = [pt(m, 10.0, 20.0) for m in range(0, 6)]  # 5 minutes
        self.assertEqual([e["type"] for e in
                          dwell.derive_entries(points, min_dwell_min=3)], ["stay"])
        self.assertEqual(dwell.derive_entries(points, min_dwell_min=30), [])

    def test_entries_are_chronological(self):
        points = []
        points += [pt(m, 10.0, 20.0) for m in range(0, 31, 5)]
        points += [pt(35, 10.005, 20.0)]
        points += [pt(m, 10.02, 20.0) for m in range(40, 71, 5)]
        entries = dwell.derive_entries(points)
        starts = [e["start"] for e in entries]
        self.assertEqual(starts, sorted(starts))

    def test_unsorted_input_is_sorted(self):
        points = [pt(30, 10.0, 20.0), pt(0, 10.0, 20.0), pt(15, 10.0, 20.0)]
        entries = dwell.derive_entries(points)
        self.assertEqual(len(entries), 1)
        self.assertAlmostEqual(entries[0]["duration_min"], 30.0)

    def test_gap_splitting_prevents_absurd_overnight_movement(self):
        # Phone reports, goes quiet for 8 hours, resumes far away. Without
        # gap splitting that is one "movement" spanning the whole night.
        points = [pt(0, 10.0, 20.0), pt(5, 10.01, 20.0),
                  pt(480, 11.0, 21.0), pt(485, 11.01, 21.0)]
        joined = dwell.derive_entries(points, gap_min=None)
        self.assertEqual(len(joined), 1)
        split = dwell.derive_entries(points, gap_min=60)
        self.assertEqual(len(split), 2)
        self.assertLess(split[0]["duration_min"], 60)
        self.assertLess(split[1]["duration_min"], 60)

    def test_realistic_commute_shape(self):
        points = []
        points += [pt(m, 40.700, -74.000) for m in range(0, 31, 10)]     # home
        for i, m in enumerate(range(35, 60, 5)):                          # transit
            points.append(pt(m, 40.700 + 0.004 * (i + 1), -74.000))
        points += [pt(m, 40.720, -74.000) for m in range(65, 186, 15)]   # work
        entries = dwell.derive_entries(points, gap_min=60)
        self.assertEqual([e["type"] for e in entries],
                         ["stay", "movement", "stay"])
        self.assertGreater(entries[2]["duration_min"], 100)
        self.assertGreater(entries[1]["km"], 1.0)


if __name__ == "__main__":
    unittest.main()
