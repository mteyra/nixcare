"""Window accounting regressions with explicit expected durations."""

import itertools
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "modules"))
import activity_intersector as ai


def event(start, end, **data):
    return {"timestamp": start, "duration": end - start, "data": data}


class ActivityWindowsTests(unittest.TestCase):
    def test_newer_afk_survives_window_clipping_in_any_response_order(self):
        events = [
            event(0, 200, status="not-afk"),
            event(50, 150, status="afk"),
            event(150, 200, status="not-afk"),
        ]
        for order in itertools.permutations(events):
            with self.subTest(order=order):
                summary = ai.compute_screen_time_summary(
                    order, [event(0, 200, app="firefox")], {}, 100, 200
                )
                self.assertEqual(summary["total_tracked_seconds"], 100)
                self.assertEqual(summary["active_screen_seconds"], 50)
                self.assertEqual(summary["apps"], [{"app": "firefox", "seconds": 50}])

    def test_latest_window_and_tab_crossing_boundary_keep_their_priority(self):
        summary = ai.compute_screen_time_summary(
            [event(0, 200, status="not-afk")],
            [event(50, 200, app="firefox"), event(0, 200, app="kitty")],
            {"firefox": [event(60, 200, url="https://new.example"),
                         event(0, 200, url="https://old.example")]},
            100, 200,
        )
        self.assertEqual(summary["apps"], [{"app": "firefox", "seconds": 100}])
        self.assertEqual(summary["browser_tabs"][0]["site"], "new.example")
        self.assertEqual(summary["browser_tabs"][0]["seconds"], 100)
        self.assertEqual(len(summary["browser_tabs"]), 1)

    def test_missing_idle_evidence_does_not_turn_open_window_into_active_time(self):
        summary = ai.compute_screen_time_summary(
            [], [event(0, 100, app="firefox")],
            {"firefox": [event(0, 100, url="https://example.org")]}, 0, 100
        )
        self.assertEqual(summary["total_tracked_seconds"], 100)
        self.assertFalse(summary["active_measurement_available"])
        self.assertEqual(summary["active_screen_seconds"], 0)
        self.assertEqual(summary["apps"], [])
        self.assertEqual(summary["browser_tabs"], [])

    def test_whitespace_in_app_names_has_consistent_browser_attribution(self):
        summary = ai.compute_screen_time_summary(
            [event(0, 10, status="not-afk")],
            [event(0, 10, app=" Firefox ")],
            {"firefox": [event(0, 10, url="https://example.org")]}, 0, 10
        )
        self.assertEqual(summary["browser_focus"]["firefox"], 10)
        self.assertEqual(summary["browser_tabs"][0]["seconds"], 10)
        self.assertTrue(ai.is_browser_app(" Firefox "))
        self.assertFalse(ai.is_browser_app({"app": "firefox"}))

    def test_malformed_labels_do_not_discard_valid_intervals(self):
        summary = ai.compute_screen_time_summary(
            [event(0, 10, status="not-afk")],
            [event(0, 5, app=123), event(5, 10, app="firefox")],
            {"firefox": [event(5, 10, url={"bad": True}, title=["bad"])]}, 0, 10
        )
        self.assertEqual(summary["active_screen_seconds"], 10)
        self.assertEqual(summary["browser_tabs"][0]["site"], "(browser)")
        self.assertTrue(summary["invariants_passed"])

    def test_invalid_windows_are_rejected(self):
        for start, end in ((0, float("nan")), (0, float("inf")), (10, 5)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                ai.compute_screen_time_summary([], [], {}, start, end)

    def test_rounding_cannot_enlarge_a_fractional_window(self):
        summary = ai.compute_screen_time_summary(
            [event(0, 1, status="not-afk")], [event(0, 1, app="firefox")], {}, 0, 0.06
        )
        self.assertEqual(summary["window_seconds"], 0.06)
        self.assertLessEqual(summary["active_screen_seconds"], 0.06)
        self.assertLessEqual(summary["total_tracked_seconds"], 0.06)
        self.assertTrue(ai.verify_invariants(summary, tolerance=0))

    def test_verifier_rejects_nonfinite_negative_and_unattributed_time(self):
        valid = dict(window_seconds=10, total_tracked_seconds=10,
                     active_screen_seconds=10, apps=[], browser_focus={}, browser_tabs=[])
        for value in (float("nan"), float("inf"), -1):
            self.assertFalse(ai.verify_invariants({**valid, "active_screen_seconds": value}))
        self.assertFalse(ai.verify_invariants({
            **valid, "browser_tabs": [{"browser": "firefox", "seconds": 2}]
        }))
        self.assertFalse(ai.verify_invariants({
            **valid, "browser_focus": {"firefox": 10},
            "browser_tabs": [{"browser": "firefox", "seconds": 2, "audible_seconds": 3}],
        }))

    def test_audible_video_exempts_focused_browser_from_3min_afk_cutoff(self):
        # 20-minute video (0..1200) followed by 20 minutes paused/idle (1200..2400).
        # User only touches mouse in the first 3 minutes (0..180).
        afk_events = [
            event(0, 180, status="not-afk"),
            event(180, 2400, status="afk"),
        ]
        win_events = [
            event(0, 2400, app="firefox", title="Lecture - YouTube — Mozilla Firefox"),
        ]
        web_events = {
            "firefox": [
                event(0, 1200, url="https://www.youtube.com/watch?v=abc", title="Lecture - YouTube", audible=True),
                event(1200, 2400, url="https://www.youtube.com/watch?v=abc", title="Lecture - YouTube", audible=False),
            ]
        }
        summary = ai.compute_screen_time_summary(afk_events, win_events, web_events, 0, 2400)
        self.assertTrue(summary["invariants_passed"])
        self.assertEqual(summary["total_tracked_seconds"], 2400)
        self.assertEqual(summary["input_active_seconds"], 180)
        self.assertEqual(summary["media_active_seconds"], 1020)
        self.assertEqual(summary["active_screen_seconds"], 1200)
        self.assertEqual(summary["daytime_idle_seconds"], 1200)
        self.assertEqual(summary["apps"], [{"app": "firefox", "seconds": 1200}])
        self.assertEqual(summary["browser_tabs"][0]["site"], "www.youtube.com")
        self.assertEqual(summary["browser_tabs"][0]["seconds"], 1200)
        self.assertEqual(summary["browser_tabs"][0]["audible_seconds"], 1200)

    def test_non_youtube_audible_site_does_not_bypass_afk_cutoff(self):
        afk_events = [
            event(0, 180, status="not-afk"),
            event(180, 1200, status="afk"),
        ]
        win_events = [
            event(0, 1200, app="firefox", title="Watch Show — Mozilla Firefox"),
        ]
        web_events = {
            "firefox": [
                event(0, 1200, url="https://hdtodayz.org/watch", title="Watch Show", audible=True),
            ]
        }
        summary = ai.compute_screen_time_summary(afk_events, win_events, web_events, 0, 1200)
        self.assertTrue(summary["invariants_passed"])
        self.assertEqual(summary["input_active_seconds"], 180)
        self.assertEqual(summary["media_active_seconds"], 0)
        self.assertEqual(summary["active_screen_seconds"], 180)
        self.assertEqual(summary["browser_tabs"][0]["site"], "hdtodayz.org")
        self.assertEqual(summary["browser_tabs"][0]["seconds"], 180)

    def test_multi_window_zero_duration_heartbeats_recover_focused_tab_and_ignore_background_window(self):
        # Foreground window watches YouTube (0..300) while a second unfocused Firefox window
        # pulses Rommelweb every 60s, causing aw-server to store duration=0 on all heartbeats.
        afk_events = [event(0, 180, status="not-afk"), event(180, 300, status="afk")]
        win_events = [
            event(0, 300, app="firefox", title=" Climbing Film - YouTube — Mozilla Firefox "),
        ]
        heartbeats = []
        for t in (0, 60, 120, 180, 240):
            heartbeats.append({
                "timestamp": t,
                "duration": 0.0,
                "data": {"url": "https://www.youtube.com/watch?v=climb", "title": "Climbing Film - YouTube", "audible": True},
            })
            heartbeats.append({
                "timestamp": t + 5,
                "duration": 0.0,
                "data": {"url": "http://127.0.0.1:4186/rommelweb/", "title": "Rommelweb — Find your people.", "audible": False},
            })
        summary = ai.compute_screen_time_summary(
            afk_events, win_events, {"firefox": heartbeats}, 0, 300
        )
        self.assertTrue(summary["invariants_passed"])
        self.assertEqual(summary["active_screen_seconds"], 300)
        self.assertEqual(summary["media_active_seconds"], 120)
        self.assertEqual(len(summary["browser_tabs"]), 1)
        self.assertEqual(summary["browser_tabs"][0]["site"], "www.youtube.com")
        self.assertEqual(summary["browser_tabs"][0]["seconds"], 300)
        self.assertEqual(summary["browser_tabs"][0]["audible_seconds"], 300)


if __name__ == "__main__":
    unittest.main()
