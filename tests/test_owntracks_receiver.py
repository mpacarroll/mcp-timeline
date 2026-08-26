#!/usr/bin/env python3
"""Tests for the OwnTracks receiver, run against a real HTTP server process
and a real (temporary) database. No real location data: coordinates and
timestamps here are invented.

Run: python3 -m unittest discover -s tests
"""

import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "test-token-not-a-real-secret"
RECEIVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "owntracks_receiver.py")


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OwnTracksReceiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(cls.db_path)  # let the receiver create it from schema.py
        cls.port = free_port()

        env = os.environ.copy()
        env["OWNTRACKS_TOKEN"] = TOKEN
        env["TIMELINE_DB"] = cls.db_path
        env["OWNTRACKS_MAX_ACCURACY_M"] = "500"

        cls.proc = subprocess.Popen([sys.executable, RECEIVER, str(cls.port)],
                                    env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            cls.proc.terminate()
            raise RuntimeError("receiver did not start listening in time")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        if os.path.exists(cls.db_path):
            os.remove(cls.db_path)

    def post(self, payload, token=TOKEN):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        conn.request("POST", "/", body=json.dumps(payload), headers=headers)
        resp = conn.getresponse()
        status, body = resp.status, resp.read().decode()
        conn.close()
        return status, body

    def rows(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        out = [dict(r) for r in db.execute(
            "SELECT source, ts_utc, lat, lng FROM path_points ORDER BY ts_utc")]
        db.close()
        return out

    def location(self, tst, lat=10.0, lon=20.0, **extra):
        return {"_type": "location", "lat": lat, "lon": lon, "tst": tst, **extra}

    def test_unauthorized_is_rejected_and_stores_nothing(self):
        before = len(self.rows())
        status, _ = self.post(self.location(1700000100), token="wrong-token")
        self.assertEqual(status, 401)
        self.assertEqual(len(self.rows()), before)

    def test_missing_auth_header_is_rejected(self):
        status, _ = self.post(self.location(1700000110), token=None)
        self.assertEqual(status, 401)

    def test_location_is_stored_tagged_owntracks(self):
        status, body = self.post(
            self.location(1700000200, lat=11.5, lon=21.5, acc=8, t="t"))
        self.assertEqual(status, 200)
        self.assertEqual(body, "[]")
        stored = [r for r in self.rows() if r["ts_utc"] == "2023-11-14T22:16:40Z"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["source"], "owntracks")
        self.assertAlmostEqual(stored[0]["lat"], 11.5)
        self.assertAlmostEqual(stored[0]["lng"], 21.5)

    def test_duplicate_timestamp_is_not_stored_twice(self):
        payload = self.location(1700000300, acc=10)
        self.post(payload)
        count_after_first = len(self.rows())
        self.post(payload)
        self.assertEqual(len(self.rows()), count_after_first,
                         "resent fix should not create a second row")

    def test_low_accuracy_fix_is_dropped(self):
        before = len(self.rows())
        status, _ = self.post(self.location(1700000400, acc=3000))
        self.assertEqual(status, 200, "a dropped point still gets a clean 200")
        self.assertEqual(len(self.rows()), before,
                         "fix worse than the accuracy threshold must not be stored")

    def test_accuracy_at_threshold_is_kept(self):
        before = len(self.rows())
        self.post(self.location(1700000500, acc=500))
        self.assertEqual(len(self.rows()), before + 1)

    def test_missing_accuracy_is_kept(self):
        before = len(self.rows())
        self.post(self.location(1700000600))
        self.assertEqual(len(self.rows()), before + 1)

    def test_non_location_types_are_accepted_but_not_stored(self):
        before = len(self.rows())
        for payload in ({"_type": "transition", "event": "enter", "desc": "X"},
                        {"_type": "lwt", "tst": 1700000700},
                        {"_type": "waypoint", "desc": "Y", "lat": 1, "lon": 2},
                        {"_type": "status"}):
            status, _ = self.post(payload)
            self.assertEqual(status, 200, f"{payload['_type']} should get a 200")
        self.assertEqual(len(self.rows()), before)

    def test_malformed_payloads_do_not_crash_the_receiver(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/", body="{not json",
                     headers={"Authorization": f"Bearer {TOKEN}",
                              "Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()

        # Location messages missing required fields are skipped, not fatal.
        for bad in ({"_type": "location", "lat": 1.0},
                    {"_type": "location", "tst": 1700000800},
                    {"_type": "location", "lat": None, "lon": None, "tst": None}):
            status, _ = self.post(bad)
            self.assertEqual(status, 200)

        # Still alive and still storing after all of the above.
        before = len(self.rows())
        self.post(self.location(1700000900, acc=5))
        self.assertEqual(len(self.rows()), before + 1)

    def test_epoch_converted_to_utc_iso(self):
        self.post(self.location(1700001000, acc=5))
        self.assertTrue(any(r["ts_utc"] == "2023-11-14T22:30:00Z" for r in self.rows()))


if __name__ == "__main__":
    unittest.main()


class AuthSchemeTests(unittest.TestCase):
    """The OwnTracks iOS app authenticates with HTTP Basic and cannot send
    a custom Authorization header, so Basic must work. Bearer stays
    supported for curl and other clients."""

    def setUp(self):
        import importlib
        os.environ["OWNTRACKS_TOKEN"] = TOKEN
        import owntracks_receiver
        importlib.reload(owntracks_receiver)
        self.mod = owntracks_receiver

    def basic(self, user, password):
        import base64
        raw = f"{user}:{password}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def test_bearer_with_correct_token(self):
        self.assertTrue(self.mod._authorized(f"Bearer {TOKEN}"))

    def test_basic_with_correct_password(self):
        self.assertTrue(self.mod._authorized(self.basic("michael", TOKEN)))

    def test_basic_username_is_ignored(self):
        # OwnTracks' UserID identifies a device, it is not a second secret.
        for user in ("michael", "", "anything-at-all", "13"):
            self.assertTrue(self.mod._authorized(self.basic(user, TOKEN)),
                            f"username {user!r} should not affect the result")

    def test_basic_with_wrong_password_is_rejected(self):
        self.assertFalse(self.mod._authorized(self.basic("michael", "nope")))

    def test_bearer_with_wrong_token_is_rejected(self):
        self.assertFalse(self.mod._authorized("Bearer nope"))

    def test_token_as_basic_username_does_not_authorize(self):
        # Guards against a sloppier implementation that checked whether the
        # secret appeared anywhere in the decoded credentials.
        self.assertFalse(self.mod._authorized(self.basic(TOKEN, "nope")))

    def test_missing_and_empty_headers(self):
        for header in (None, "", "   "):
            self.assertFalse(self.mod._authorized(header))

    def test_unsupported_schemes_rejected(self):
        for header in (f"Digest {TOKEN}", TOKEN, f"Token {TOKEN}"):
            self.assertFalse(self.mod._authorized(header))

    def test_scheme_is_case_insensitive(self):
        self.assertTrue(self.mod._authorized(f"bearer {TOKEN}"))
        self.assertTrue(self.mod._authorized(self.basic("u", TOKEN).replace(
            "Basic", "basic", 1)))

    def test_malformed_basic_payloads_do_not_raise(self):
        import base64
        for header in ("Basic !!!not-base64!!!",
                       "Basic " + base64.b64encode(b"\xff\xfe").decode(),
                       "Basic " + base64.b64encode(b"no-colon-here").decode(),
                       "Basic "):
            self.assertFalse(self.mod._authorized(header), header)
