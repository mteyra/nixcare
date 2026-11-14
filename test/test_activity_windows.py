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


if __name__ == "__main__":
    unittest.main()
