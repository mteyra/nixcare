"""Regression tests for telemetry's browser boundary and interval accounting."""

import http.client
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import textwrap
import threading
import time
import unittest

MODULES = Path(__file__).resolve().parents[1] / "modules"
sys.path.insert(0, str(MODULES))
import activity_intersector as ai
from tracking_support import SummaryCache, local_request_allowed, validate_hours


def collector_namespace():
    """Test the actual embedded collector, substituting only Nix constants."""
    source = (MODULES / "track.nix").read_text()
    source = source.split('collectorPy = pkgs.writeText "laptop-telemetry-daemon.py" \'\'\n', 1)[1]
    source = textwrap.dedent(source.split("\n  '';", 1)[0])
    source = source.replace("${intersectorCode}", (MODULES / "activity_intersector.py").read_text())
    source = source.replace("${trackingSupportCode}", (MODULES / "tracking_support.py").read_text())
    source = re.sub(r"\$\{[^}]+\}", "0", source)
    namespace = {"__name__": "telemetry_test"}
    exec(compile(source, "track.nix:collector", "exec"), namespace)
    namespace["RETENTION_DAYS"] = 90
    return namespace


class TrackingBoundaryTests(unittest.TestCase):
    def test_hours_bounded_and_finite(self):
        self.assertEqual(validate_hours("24", 90), 24)
        for value in ("nan", "inf", "-inf", 0, -1, 2161, "oops", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_hours(value, 90)

    def test_browser_request_boundary(self):
        good = {"Host": "127.0.0.1:5601"}
        self.assertTrue(local_request_allowed(good, 5601))
        self.assertTrue(local_request_allowed({"Host": "localhost:5601", "Origin": "http://localhost:5601"}, 5601))
        for headers in (
            {},
            {"Host": "attacker.example:5601"},
            {**good, "Origin": "https://attacker.example"},
            {**good, "Origin": "null"},
            {**good, "Sec-Fetch-Site": "cross-site"},
        ):
            self.assertFalse(local_request_allowed(headers, 5601))

    def test_summary_cache_coalesces_and_expires(self):
        clock = [1]
        cache = SummaryCache(ttl=15, clock=lambda: clock[0])
        calls = []

        def compute():
            calls.append(True)
            return len(calls)

        self.assertEqual(cache.get(24, compute), 1)
        self.assertEqual(cache.get(24, compute), 1)
        clock[0] += 16
        self.assertEqual(cache.get(24, compute), 2)
        for hours in range(30):
            cache.get(hours, compute)
        self.assertLessEqual(len(cache.entries), 8)

    def test_malformed_events_do_not_break_report(self):
        events = [
            {"timestamp": "invalid", "duration": 10},
            {"timestamp": float("nan"), "duration": 10},
            {"timestamp": 1, "duration": float("inf")},
            {"timestamp": 1, "duration": -4},
            {"start": 1, "end": 4, "data": None},
            None,
            [],
        ]
        self.assertEqual(ai.clip_events(events, 0, 10), [(1, 4, {})])

    def test_latest_afk_event_overrides_overlapping_active_event(self):
        summary = ai.compute_screen_time_summary(
            [{"timestamp": 0, "duration": 100, "data": {"status": "not-afk"}},
             {"timestamp": 40, "duration": 60, "data": {"status": "afk"}}],
            [{"timestamp": 0, "duration": 100, "data": {"app": "firefox"}}],
            {}, 0, 100,
        )
        self.assertEqual(summary["active_screen_seconds"], 40)
        self.assertTrue(summary["invariants_passed"])

    def test_domain_extraction_drops_credentials_and_keeps_ipv6(self):
        self.assertEqual(ai.extract_site_domain("https://user:secret@example.org:443/a"), "example.org")
        self.assertEqual(ai.extract_site_domain("http://[::1]:8000/a"), "::1")

    def test_rounding_many_small_app_rows_cannot_invent_active_time(self):
        summary = ai.compute_screen_time_summary(
            [{"timestamp": 0, "duration": 6, "data": {"status": "not-afk"}}],
            [{"timestamp": i * 0.06, "duration": 0.06, "data": {"app": str(i)}} for i in range(100)],
            {}, 0, 6,
        )
        self.assertTrue(summary["invariants_passed"])
        self.assertLessEqual(sum(a["seconds"] for a in summary["apps"]), 6)

    def test_real_summary_deduplicates_sample_intervals_and_hides_disabled_activity(self):
        ns = collector_namespace()
        conn = sqlite3.connect(":memory:")
        conn.executescript(ns["SCHEMA"])
        columns = conn.execute("PRAGMA table_info(samples)").fetchall()
        now = int(time.time())
        for ts in (now - 10, now - 5):
            values = {col[1]: "" if col[2] == "TEXT" else 0 for col in columns}
            values.update(ts=ts, dt=15, afk=0, active_app="private-app", browser_url="https://private.example")
            conn.execute("INSERT INTO samples VALUES (" + ",".join("?" for _ in values) + ")", list(values.values()))
        result = ns["build_summary"](conn, 1)
        conn.close()
        self.assertEqual(result["total_tracked_seconds"], 20)
        self.assertEqual(result["active_screen_seconds"], 0)
        self.assertEqual(result["apps"], [])
        self.assertEqual(result["latest"]["browser_url"], "")
        self.assertFalse(result["activity_enabled"])


class TelemetryHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = collector_namespace()
        cls.temp = tempfile.TemporaryDirectory()
        cls.ns["DB_PATH"] = cls.temp.name + "/telemetry.db"
        sqlite3.connect(cls.ns["DB_PATH"]).close()
        cls.ns["build_summary"] = lambda conn, hours: {"window_hours": hours, "safe": True}
        cls.server = cls.ns["TelemetryServer"](("127.0.0.1", 0), cls.ns["Handler"])
        cls.port = cls.server.server_address[1]
        cls.ns["PORT"] = cls.port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_valid_json_and_private_response_headers(self):
        status, headers, body = self.request("/api/summary?hours=1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["window_hours"], 1)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_invalid_requests_get_errors_without_crashing(self):
        for value in ("nan", "inf", "-1", "99999", "invalid"):
            self.assertEqual(self.request("/api/summary?hours=" + value)[0], 400)
        self.assertEqual(self.request("/missing")[0], 404)
        self.assertEqual(self.request("/api/summary")[0], 200)

    def test_rebinding_and_cross_origin_are_denied(self):
        self.assertEqual(self.request("/", {"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("/api/summary", {"Origin": "https://attacker.example"})[0], 403)

    def test_activity_disabled_does_not_contact_watchers(self):
        self.assertEqual(self.ns["query_hyprland"](), ("", "", "", ""))
        self.assertEqual(self.ns["query_activitywatch"](), (1, "", "", "", 0, 0))


if __name__ == "__main__":
    unittest.main()
