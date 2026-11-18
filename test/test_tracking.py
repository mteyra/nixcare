"""Regression tests for telemetry's browser boundary and interval accounting."""

import http.client
import importlib.util
import builtins
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

MODULES = Path(__file__).resolve().parents[1] / "modules"
sys.path.insert(0, str(MODULES))
import activity_intersector as ai
from tracking_support import SummaryCache, local_request_allowed, validate_hours, read_json_bounded, sample_duration, select_activity_buckets


def load_fresh(filename, name):
    """Load a module from modules/ as a new, independent copy for each test."""
    spec = importlib.util.spec_from_file_location(name, MODULES / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collector_namespace():
    """The collector's globals, with the default settings the NixOS module uses."""
    module = load_fresh("laptop_telemetry.py", "telemetry_test")
    module.apply_settings(module.parse_args(["--user", "testuser", "--retention-days", "90"]))
    return vars(module)


def report_namespace():
    return vars(load_fresh("laptop_report.py", "report_test"))


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

    def test_slow_summary_does_not_get_an_extra_freshness_window(self):
        clock = [0]
        cache = SummaryCache(ttl=15, clock=lambda: clock[0])
        calls = []
        def compute():
            calls.append(True)
            clock[0] += 20
            return len(calls)
        self.assertEqual(cache.get(24, compute), 1)
        self.assertEqual(cache.get(24, compute), 2)

    def test_activity_response_memory_limit_is_enforced(self):
        self.assertEqual(read_json_bounded(io.BytesIO(b'{"ok":true}')), {"ok": True})
        with self.assertRaisesRegex(ValueError, "response limit"):
            read_json_bounded(io.BytesIO(b"x" * 17), max_bytes=16)

    def test_suspend_or_clock_jump_is_not_a_short_burst_of_work(self):
        self.assertEqual(sample_duration(5.25), 5.25)
        for elapsed in (-1, 0, 16, 3600, float("inf"), float("nan")):
            self.assertIsNone(sample_duration(elapsed))

    def test_buckets_prefer_current_host_without_merging_legacy_or_imported_data(self):
        buckets = {
            "aw-watcher-afk_other": {"hostname": "other"},
            "aw-watcher-afk_here": {"hostname": "here"},
            "aw-watcher-window_here": {},
            "aw-watcher-web-firefox": {"hostname": "unknown"},
            "aw-watcher-web-firefox_here": {"hostname": "here"},
            "aw-watcher-web-chrome_other": {"hostname": "other"},
        }
        self.assertEqual(select_activity_buckets(buckets, "HERE"), {
            "afk": "aw-watcher-afk_here", "window": "aw-watcher-window_here",
            "firefox": "aw-watcher-web-firefox_here",
        })

    def test_unique_hostless_browser_is_supported_but_ambiguous_histories_are_rejected(self):
        self.assertEqual(select_activity_buckets({"aw-watcher-web-firefox": {"hostname": "unknown"}}, "here"),
                         {"firefox": "aw-watcher-web-firefox"})
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            select_activity_buckets({
                "aw-watcher-afk_here": {"hostname": "here"},
                "aw-watcher-afk_duplicate": {"hostname": "here"},
            }, "here")

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

    def test_gpu_scan_reads_only_drm_descriptors_and_keeps_devices_separate(self):
        ns = collector_namespace()
        with tempfile.TemporaryDirectory() as temporary:
            process = Path(temporary) / "123"
            (process / "fd").mkdir(parents=True)
            (process / "fdinfo").mkdir()
            rest = ["0"] * 22
            rest[0], rest[11], rest[19], rest[21] = "S", "100", "42", "32"
            (process / "stat").write_text("123 (browser) " + " ".join(rest))
            (process / "io").write_text("write_bytes: 4096\n")
            for descriptor, target, pci in (
                ("0", "/tmp/ordinary-file", None),
                ("1", "/dev/dri/renderD128", "0000:00:02.0"),
                ("2", "/dev/dri/renderD128", "0000:00:02.0"),
                ("3", "/dev/dri/renderD129", "0000:01:00.0"),
            ):
                (process / "fd" / descriptor).symlink_to(target)
                if pci:
                    (process / "fdinfo" / descriptor).write_text(
                        "pos: 0\n" + "extra: " + "x" * 600 + "\n"
                        + "drm-client-id: 7\ndrm-pdev: " + pci + "\ndrm-engine-render: 1234 ns\n")
                else:
                    (process / "fdinfo" / descriptor).write_text("Ordinary descriptor")
            reads = []
            def tracked_open(path, *args, **kwargs):
                reads.append(str(path))
                return builtins.open(path, *args, **kwargs)
            ns["open"] = tracked_open
            processes, clients = ns["scan_processes"](temporary)
        self.assertEqual(processes[123]["wr_bytes"], 4096)
        self.assertNotIn(str(process / "fdinfo" / "0"), reads)
        self.assertEqual(len(clients), 2)
        self.assertEqual({client["render_ns"] for client in clients.values()}, {1234})


class TrackingWindowTests(unittest.TestCase):
    def setUp(self):
        self.ns = collector_namespace()
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.executescript(self.ns["SCHEMA"])
        self.columns = self.conn.execute("PRAGMA table_info(samples)").fetchall()
        patcher = mock.patch.object(self.ns["time"], "time", return_value=1000.75)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sample(self, ts, **updates):
        values = {col[1]: "" if col[2] == "TEXT" else 0 for col in self.columns}
        values.update(ts=ts, dt=5)
        values.update(updates)
        self.conn.execute("INSERT INTO samples VALUES (" + ",".join("?" for _ in values) + ")", list(values.values()))

    def summary(self, seconds=60):
        return self.ns["build_summary"](self.conn, seconds / 3600)

    def test_fractional_window_excludes_old_and_future_samples_everywhere(self):
        self.sample(998, cpu_busy_pct=80, top_cpu_comm="old")
        self.sample(999, cpu_busy_pct=20, top_cpu_comm="included")
        self.sample(1001, cpu_busy_pct=99, top_cpu_comm="future")
        result = self.summary(2)
        self.assertEqual(result["window_start"], 998.75)
        self.assertEqual(result["window_end"], 1000.75)
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["latest"]["ts"], 999)
        self.assertEqual(result["potential"]["cpu_busy"], {"avg": 20, "max": 20, "p95": 20})
        self.assertEqual(result["total_tracked_seconds"], .25)
        self.assertEqual(result["hardware_tracked_seconds"], .25)
        self.assertEqual(result["hardware_coverage_pct"], 12.5)
        self.assertEqual([p["comm"] for p in result["top_cpu_processes"]], ["included"])

    def test_empty_window_has_no_stale_latest_or_fake_zero_measurements(self):
        self.sample(900, cpu_busy_pct=65)
        result = self.summary(10)
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(result["latest"], {})
        self.assertIsNone(result["potential"]["cpu_busy"]["avg"])
        self.assertEqual(result["hardware_coverage_pct"], 0)

    def test_nearest_rank_p95_uses_ceiling_and_ignores_missing_readings(self):
        for ts, busy in ((995, None), (996, 10), (997, 20), (998, 90)):
            self.sample(ts, cpu_busy_pct=busy)
        result = self.summary()
        self.assertEqual(result["potential"]["cpu_busy"]["p95"], 90)

    def test_query_snapshot_does_not_leave_read_transaction_open(self):
        self.conn.commit()
        self.summary()
        self.assertFalse(self.conn.in_transaction)

    def test_missing_idle_evidence_falls_back_to_sampled_activity(self):
        self.ns["ACTIVITY_ENABLED"] = True
        self.sample(999, afk=0, active_app="editor")
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        def response(request, **kwargs):
            url = request.full_url if hasattr(request, "full_url") else request
            data = {"aw-watcher-window_test": {"hostname": self.ns["socket"].gethostname()}} if url.endswith("/buckets/") else []
            return Response(json.dumps(data).encode())
        self.ns["compute_screen_time_summary"] = lambda **kwargs: {"active_measurement_available": False}
        with mock.patch.object(self.ns["urllib"].request, "urlopen", side_effect=response):
            result = self.summary()
        self.assertEqual(result["activity_source"], "telemetry-samples")
        self.assertIn("idle/active evidence", result["activity_warning"])
        self.assertEqual(result["active_screen_seconds"], 5)

    def test_oversized_history_is_explicit_fallback_not_silent_truncation(self):
        self.ns["ACTIVITY_ENABLED"] = True
        self.sample(999, afk=0, active_app="editor")
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        def response(request, **kwargs):
            url = request.full_url if hasattr(request, "full_url") else request
            data = {"aw-watcher-afk_test": {"hostname": self.ns["socket"].gethostname()}} if url.endswith("/buckets/") else [{}] * 20001
            return Response(json.dumps(data).encode())
        with mock.patch.object(self.ns["urllib"].request, "urlopen", side_effect=response):
            result = self.summary()
        self.assertIn("20000 events", result["activity_warning"])
        self.assertEqual(result["activity_source"], "telemetry-samples")
        self.assertEqual(result["active_screen_seconds"], 5)

    def test_spanning_activity_boundary_is_requested_with_fractional_times(self):
        self.ns["ACTIVITY_ENABLED"] = True
        requested = []
        def response(request, **kwargs):
            url = request.full_url if hasattr(request, "full_url") else request
            requested.append(url)
            if url.endswith("/buckets/"):
                data = {name: {"hostname": self.ns["socket"].gethostname()} for name in (
                    "aw-watcher-afk_test", "aw-watcher-window_test")}
            elif "start=" in url:
                data = []
            else:
                data = [{"timestamp": 995, "duration": 5, "data": {"status": "not-afk", "app": "editor"}}]
            return io.BytesIO(json.dumps(data).encode())
        with mock.patch.object(self.ns["urllib"].request, "urlopen", side_effect=response):
            result = self.summary(2)
        self.assertEqual(result["activity_source"], "activitywatch")
        self.assertLessEqual(result["active_screen_seconds"], 1.25)
        self.assertGreater(result["active_screen_seconds"], 1)
        query = self.ns["urllib"].parse.parse_qs(self.ns["urllib"].parse.urlsplit(requested[1]).query)
        self.assertTrue(query["start"][0].endswith(".750000+00:00"))
        self.assertEqual(query["limit"], ["20001"])
        self.assertFalse(result["apps"][0]["hardware_available"])

    def test_live_sampler_rejects_future_activity_and_normalizes_domains(self):
        self.ns["ACTIVITY_ENABLED"] = True
        event_start = [1001]
        def response(request, **kwargs):
            url = request.full_url if hasattr(request, "full_url") else request
            if url.endswith("/buckets/"):
                data = {name: {"hostname": self.ns["socket"].gethostname()} for name in (
                    "aw-watcher-afk_test", "aw-watcher-web-firefox_test")}
            else:
                data = [{"timestamp": event_start[0], "duration": 0,
                         "data": {"status": "not-afk", "url": "https://user:secret@example.org:443/a"}}]
            return io.BytesIO(json.dumps(data).encode())
        with mock.patch.object(self.ns["urllib"].request, "urlopen", side_effect=response):
            result = self.ns["query_activitywatch"]("firefox")
            self.assertEqual(result[:2], (1, ""))
            event_start[0] = 999
            result = self.ns["query_activitywatch"]("firefox")
            self.assertEqual(result[:2], (0, "example.org"))

    def test_report_cli_rejects_invalid_hours_before_network_request(self):
        ns = report_namespace()
        for value in ("nan", "inf", "0", "-1", "2161"):
            with self.subTest(value=value), mock.patch.object(sys, "argv", ["laptop-report", "--hours", value]):
                with mock.patch.object(ns["urllib"].request, "urlopen") as network, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as exited:
                        ns["main"]()
                    self.assertEqual(exited.exception.code, 2)
                    network.assert_not_called()

    def test_report_cli_empty_window_explains_unavailable_hardware(self):
        result = self.summary()
        ns = report_namespace()
        response = io.BytesIO(json.dumps(result).encode())
        with mock.patch.object(sys, "argv", ["laptop-report", "--hours", "0.5"]):
            with mock.patch.object(ns["urllib"].request, "urlopen", return_value=response):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    ns["main"]()
        self.assertIn("No hardware samples in this window", output.getvalue())
        self.assertNotIn("CPU Thread Busy", output.getvalue())


class CommandLineSettingsTests(unittest.TestCase):
    """The NixOS module configures the collector and report only through these options."""

    def test_settings_reach_the_collector(self):
        module = load_fresh("laptop_telemetry.py", "telemetry_settings_test")
        module.apply_settings(module.parse_args(
            ["--user", "alice", "--retention-days", "30", "--activity", "--nvme", "/x/bin/nvme"]))
        self.assertEqual(
            (module.OWNER_USER, module.RETENTION_DAYS, module.ACTIVITY_ENABLED, module.NVME_BIN),
            ("alice", 30, True, "/x/bin/nvme"))

    def test_activity_stays_off_unless_requested(self):
        module = load_fresh("laptop_telemetry.py", "telemetry_settings_test")
        module.apply_settings(module.parse_args(["--user", "alice"]))
        self.assertFalse(module.ACTIVITY_ENABLED)
        self.assertEqual(module.RETENTION_DAYS, 90)

    def test_collector_requires_user_and_positive_retention(self):
        module = load_fresh("laptop_telemetry.py", "telemetry_settings_test")
        for argv in ([], ["--user", "alice", "--retention-days", "0"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    module.parse_args(argv)

    def test_report_hours_limit_follows_retention(self):
        ns = report_namespace()
        argv = ["laptop-report", "--retention-days", "1", "--hours", "25"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(ns["urllib"].request, "urlopen") as network:
                with self.assertRaises(SystemExit) as exited:
                    ns["main"]()
                network.assert_not_called()
        self.assertEqual(exited.exception.code, 2)


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
