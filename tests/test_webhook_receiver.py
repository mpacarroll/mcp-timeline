#!/usr/bin/env python3
"""Tests for the capture receiver's payload summarizer.

This receiver exists to learn an undocumented payload format, so the
summary it prints is the actual product. These check that it stays
readable and never raises on the shapes it will realistically meet,
including malformed ones: a crash here loses the very capture it was
supposed to explain.

Run: python3 -m unittest discover -s tests
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("WEBHOOK_TOKEN", "token-for-import-only")
import webhook_receiver


class DescribePayloadTests(unittest.TestCase):
    def describe(self, obj, content_type="application/json", **kw):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        return webhook_receiver.describe_payload(body, content_type, **kw)

    def test_reports_object_keys(self):
        lines = self.describe({"data": {"workouts": [], "metrics": []}})
        self.assertIn("<root>: object with 1 keys (data)", lines[0])
        self.assertTrue(any("data: object with 2 keys" in line for line in lines))

    def test_reports_array_length_and_element_shape(self):
        lines = self.describe({"workouts": [{"name": "Running", "duration": 30},
                                            {"name": "Cycling", "duration": 45}]})
        self.assertTrue(any("workouts: array of 2" in line for line in lines))
        self.assertTrue(any("workouts[0]: object with 2 keys" in line for line in lines))
        self.assertTrue(any("workouts[0].name: str = 'Running'" in line
                            for line in lines))

    def test_only_first_array_element_is_walked(self):
        # A day of health samples can be thousands of entries; walking them
        # all would bury the structure and dump real data into the log.
        lines = self.describe({"samples": [{"v": i} for i in range(500)]})
        self.assertTrue(any("samples: array of 500" in line for line in lines))
        self.assertFalse(any("samples[1]" in line for line in lines))

    def test_long_values_are_truncated(self):
        lines = self.describe({"note": "x" * 500})
        note = next(line for line in lines if line.startswith("note:"))
        self.assertLess(len(note), 100)
        self.assertTrue(note.endswith("..."))

    def test_output_is_capped(self):
        deep = {f"key{i}": {f"inner{j}": j for j in range(10)} for i in range(20)}
        lines = self.describe(deep, max_lines=15)
        self.assertLessEqual(len(lines), 16)  # cap plus the truncation notice
        self.assertIn("truncated", lines[-1])

    def test_empty_containers_do_not_crash(self):
        self.assertTrue(self.describe({}))
        self.assertTrue(self.describe([]))
        self.assertTrue(self.describe({"a": [], "b": {}}))

    def test_null_and_mixed_types(self):
        lines = self.describe({"a": None, "b": True, "c": 1.5, "d": 7})
        joined = "\n".join(lines)
        for expected in ("a: NoneType", "b: bool", "c: float", "d: int"):
            self.assertIn(expected, joined)

    def test_top_level_array(self):
        lines = self.describe([{"lat": 1.0, "lon": 2.0}])
        self.assertTrue(any("array of 1" in line for line in lines))

    def test_invalid_json_is_reported_not_raised(self):
        lines = webhook_receiver.describe_payload(b"{not json at all",
                                                  "application/json")
        self.assertTrue(any("not JSON" in line for line in lines))

    def test_non_utf8_bytes_do_not_raise(self):
        lines = webhook_receiver.describe_payload(b"\xff\xfe\x00binary",
                                                  "application/json")
        self.assertTrue(any("not JSON" in line for line in lines))

    def test_csv_reports_line_count_and_header(self):
        body = b"date,steps,distance\n2026-02-01,1200,0.8\n2026-02-02,900,0.6\n"
        lines = webhook_receiver.describe_payload(body, "text/csv")
        self.assertIn("CSV, 3 lines", lines[0])
        self.assertIn("date,steps,distance", lines[1])

    def test_empty_body_does_not_raise(self):
        self.assertTrue(webhook_receiver.describe_payload(b"", "application/json"))
        self.assertTrue(webhook_receiver.describe_payload(b"", "text/csv"))

    def test_realistic_health_shape_is_legible(self):
        # Approximating what a Shortcuts or Health Auto Export payload
        # looks like, to confirm the summary is actually readable.
        payload = {"data": {"workouts": [
            {"name": "Outdoor Run",
             "start": "2026-02-01 06:00:00 -0500",
             "end": "2026-02-01 06:30:00 -0500",
             "distance": {"qty": 5.0, "units": "km"},
             "route": [{"lat": 1.0, "lon": 2.0, "timestamp": "2026-02-01"}]}]}}
        joined = "\n".join(self.describe(payload))
        self.assertIn("data.workouts: array of 1", joined)
        self.assertIn("data.workouts[0].distance: object", joined)
        self.assertIn("data.workouts[0].route: array of 1", joined)
        self.assertIn("data.workouts[0].route[0].lat: float", joined)


if __name__ == "__main__":
    unittest.main()
