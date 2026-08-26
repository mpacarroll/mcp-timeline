#!/usr/bin/env python3
"""Tests for reverse geocoding.

No test here touches the network. The fetch function is injected, so the
caching, opt-in, and failure behavior are all exercised without sending a
single coordinate anywhere. That matters more than usual: this is the one
module that can talk to a third party, and a test suite that quietly made
live requests would be both slow and a privacy leak in CI.

Run: python3 -m unittest discover -s tests
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import geocode
import schema


class ShortLabelTests(unittest.TestCase):
    """Nominatim's display_name is the full postal chain, far too long for
    every entry in a day summary. These check the compaction."""

    def test_named_poi_preferred(self):
        payload = {"name": "Katz's Delicatessen",
                   "address": {"road": "East Houston Street", "city": "New York"},
                   "display_name": "Katz's, 205, East Houston Street, ..."}
        self.assertEqual(geocode._short_label(payload),
                         "Katz's Delicatessen, New York")

    def test_street_address_when_no_poi(self):
        payload = {"address": {"house_number": "205", "road": "East Houston Street",
                               "city": "New York"}}
        self.assertEqual(geocode._short_label(payload),
                         "205 East Houston Street, New York")

    def test_road_without_number(self):
        payload = {"address": {"road": "Grand Concourse", "town": "Yonkers"}}
        self.assertEqual(geocode._short_label(payload), "Grand Concourse, Yonkers")

    def test_locality_fallbacks_in_order(self):
        for key in ("city", "town", "village", "suburb", "neighbourhood"):
            label = geocode._short_label({"name": "Spot", "address": {key: "Placeville"}})
            self.assertEqual(label, "Spot, Placeville", key)

    def test_locality_only(self):
        self.assertEqual(geocode._short_label({"address": {"city": "Boston"}}), "Boston")

    def test_display_name_last_resort(self):
        payload = {"display_name": "Some Building, Some Street, City, Country"}
        self.assertEqual(geocode._short_label(payload), "Some Building, Some Street")

    def test_empty_and_malformed_payloads(self):
        for payload in ({}, {"address": {}}, None, [], "not a dict", 42):
            self.assertIsNone(geocode._short_label(payload), repr(payload))


class CacheKeyTests(unittest.TestCase):
    def test_rounds_to_about_eleven_meters(self):
        self.assertEqual(geocode.cache_key(40.758876999, -73.979866999),
                         (40.7589, -73.9799))

    def test_nearby_points_share_a_key(self):
        # Two fixes a couple of meters apart at the same cafe.
        self.assertEqual(geocode.cache_key(40.75887, -73.97986),
                         geocode.cache_key(40.75888, -73.97987))

    def test_distinct_places_do_not_collide(self):
        self.assertNotEqual(geocode.cache_key(40.7589, -73.9799),
                            geocode.cache_key(40.7600, -73.9799))


class LabelForTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = sqlite3.connect(self.db_path)
        schema.ensure_schema(self.db)
        self.calls = []
        self._enabled = geocode.ENABLED
        geocode.ENABLED = True

    def tearDown(self):
        geocode.ENABLED = self._enabled
        self.db.close()
        os.remove(self.db_path)

    def fetch(self, label):
        def _fetch(lat, lng):
            self.calls.append((lat, lng))
            return label
        return _fetch

    def test_returns_label_and_caches_it(self):
        result = geocode.label_for(self.db, 40.7589, -73.9799,
                                   fetch=self.fetch("Radio City"))
        self.assertEqual(result, "Radio City")
        row = self.db.execute("SELECT label FROM geocode_cache").fetchone()
        self.assertEqual(row[0], "Radio City")

    def test_second_lookup_does_not_refetch(self):
        fetch = self.fetch("Radio City")
        geocode.label_for(self.db, 40.7589, -73.9799, fetch=fetch)
        geocode.label_for(self.db, 40.7589, -73.9799, fetch=fetch)
        self.assertEqual(len(self.calls), 1, "a cached coordinate must not be re-sent")

    def test_nearby_coordinate_uses_the_same_cache_entry(self):
        fetch = self.fetch("Radio City")
        geocode.label_for(self.db, 40.75887, -73.97986, fetch=fetch)
        geocode.label_for(self.db, 40.75888, -73.97987, fetch=fetch)
        self.assertEqual(len(self.calls), 1)

    def test_disabled_never_fetches(self):
        geocode.ENABLED = False
        result = geocode.label_for(self.db, 40.7589, -73.9799,
                                   fetch=self.fetch("Radio City"))
        self.assertIsNone(result)
        self.assertEqual(self.calls, [], "must not send anything when opted out")
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM geocode_cache").fetchone()[0], 0)

    def test_disabled_still_serves_an_existing_cache_entry(self):
        geocode.label_for(self.db, 40.7589, -73.9799, fetch=self.fetch("Radio City"))
        geocode.ENABLED = False
        self.assertEqual(
            geocode.label_for(self.db, 40.7589, -73.9799, fetch=self.fetch("other")),
            "Radio City")

    def test_label_for_does_not_swallow_programming_errors(self):
        # Catching network failures is _fetch's job, verified below against
        # a real unreachable endpoint. label_for deliberately does not add a
        # second blanket except, which would hide genuine bugs in a fetcher.
        def boom(lat, lng):
            raise ValueError("a bug, not a network failure")
        with self.assertRaises(ValueError):
            geocode.label_for(self.db, 5.0, 6.0, fetch=boom)

    def test_a_lookup_that_finds_nothing_is_cached(self):
        # A coordinate with genuinely no name should not be re-requested
        # on every query about that day.
        fetch = self.fetch(None)
        self.assertIsNone(geocode.label_for(self.db, 1.5, 2.5, fetch=fetch))
        self.assertIsNone(geocode.label_for(self.db, 1.5, 2.5, fetch=fetch))
        self.assertEqual(len(self.calls), 1)

    def test_a_failed_lookup_is_not_cached_and_is_retried(self):
        # A transient outage must not permanently mark a real place as
        # unidentifiable. This is not hypothetical: one blip while
        # resolving a hotel would otherwise lose that name forever.
        calls = []

        def flaky(lat, lng):
            calls.append(1)
            if len(calls) == 1:
                raise geocode.GeocodeUnavailable("network down")
            return "Resolved Later"

        self.assertIsNone(geocode.label_for(self.db, 7.0, 8.0, fetch=flaky))
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM geocode_cache").fetchone()[0], 0,
            "a failed lookup must leave no cache entry")
        self.assertEqual(geocode.label_for(self.db, 7.0, 8.0, fetch=flaky),
                         "Resolved Later")
        self.assertEqual(len(calls), 2)

class StubbingTests(unittest.TestCase):
    """Guards a real trap: with fetch=_fetch as a default argument, the
    function object binds at definition time, so replacing geocode._fetch
    has no effect and a test that believes it is stubbed silently makes
    live network requests. Found exactly that way."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = sqlite3.connect(self.db_path)
        schema.ensure_schema(self.db)
        self._enabled, self._fetch = geocode.ENABLED, geocode._fetch
        geocode.ENABLED = True

    def tearDown(self):
        geocode.ENABLED, geocode._fetch = self._enabled, self._fetch
        self.db.close()
        os.remove(self.db_path)

    def test_replacing_module_level_fetch_is_honored(self):
        calls = []

        def stub(lat, lng):
            calls.append((lat, lng))
            return "Stubbed Place"

        geocode._fetch = stub
        self.assertEqual(geocode.label_for(self.db, 51.5, -0.12), "Stubbed Place")
        self.assertEqual(len(calls), 1,
                         "the module-level stub must be used, not the original")


class RealFetchFailureTests(unittest.TestCase):
    """_fetch must swallow every network failure, since a geocoding outage
    should degrade an answer rather than break the tool."""

    def test_unreachable_endpoint_raises_unavailable_not_a_miss(self):
        original = geocode.ENDPOINT
        original_interval = geocode.MIN_INTERVAL_S
        # Reserved TEST-NET-1 address, guaranteed not to route anywhere.
        geocode.ENDPOINT = "http://192.0.2.1:9/reverse"
        geocode.TIMEOUT_S = 0.4
        geocode.MIN_INTERVAL_S = 0.0
        try:
            with self.assertRaises(geocode.GeocodeUnavailable):
                geocode._fetch(40.0, -73.0)
        finally:
            geocode.ENDPOINT = original
            geocode.MIN_INTERVAL_S = original_interval


if __name__ == "__main__":
    unittest.main()
