#!/usr/bin/env python3
"""
test_report.py - Synthetic unit test suite for laptop-report invariants.

Verifies:
1. tracked <= window
2. sum(sites per browser) <= that browser's focus time
3. sum(apps) <= active time <= tracked
"""

import os
import sys
import unittest

# Import activity_intersector from modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "modules"))
import activity_intersector as ai


class TestLaptopReportInvariants(unittest.TestCase):

    def test_window_boundary_clipping(self):
        """Events straddling the window boundary must be clipped to [start, end]."""
        w_start = 1000.0
        w_end = 2000.0  # window duration = 1000s

        # Event 1: starts before window (800), ends inside window (1200) -> 200s inside
        # Event 2: starts inside window (1800), ends after window (2200) -> 200s inside
        # Event 3: completely before window (500 to 700) -> 0s
        # Event 4: completely after window (2500 to 2700) -> 0s
        afk_events = [
            {"timestamp": 800.0, "duration": 400.0, "data": {"status": "not-afk"}},
            {"timestamp": 1800.0, "duration": 400.0, "data": {"status": "not-afk"}},
            {"timestamp": 500.0, "duration": 200.0, "data": {"status": "not-afk"}},
            {"timestamp": 2500.0, "duration": 200.0, "data": {"status": "not-afk"}},
        ]
        window_events = [
            {"timestamp": 800.0, "duration": 400.0, "data": {"app": "kitty", "title": "term"}},
            {"timestamp": 1800.0, "duration": 400.0, "data": {"app": "kitty", "title": "term"}},
        ]

        summary = ai.compute_screen_time_summary(
            afk_events=afk_events,
            window_events=window_events,
            web_events_by_browser={},
            window_start=w_start,
            window_end=w_end,
        )

        self.assertTrue(summary["invariants_passed"])
        self.assertAlmostEqual(summary["total_tracked_seconds"], 400.0, places=1)
        self.assertAlmostEqual(summary["active_screen_seconds"], 400.0, places=1)
        self.assertLessEqual(summary["total_tracked_seconds"], 1000.0)

    def test_spanning_event_clipped_to_window(self):
        """A single event spanning across the entire window cannot exceed window duration."""
        w_start = 5000.0
        w_end = 10000.0  # 5000s window

        # Event from 0 to 20000 (20000s duration)
        afk_events = [
            {"timestamp": 0.0, "duration": 20000.0, "data": {"status": "not-afk"}},
        ]
        window_events = [
            {"timestamp": 0.0, "duration": 20000.0, "data": {"app": "antigravity", "title": "code"}},
        ]

        summary = ai.compute_screen_time_summary(
            afk_events=afk_events,
            window_events=window_events,
            web_events_by_browser={},
            window_start=w_start,
            window_end=w_end,
        )

        self.assertTrue(summary["invariants_passed"])
        self.assertAlmostEqual(summary["total_tracked_seconds"], 5000.0, places=1)
        self.assertAlmostEqual(summary["active_screen_seconds"], 5000.0, places=1)
        self.assertEqual(len(summary["apps"]), 1)
        self.assertAlmostEqual(summary["apps"][0]["seconds"], 5000.0, places=1)

    def test_afk_time_subtraction(self):
        """AFK time must be strictly subtracted: sum(apps) <= active time <= tracked."""
        w_start = 1000.0
        w_end = 2000.0  # 1000s

        # User is active for 400s (1000 to 1400), then AFK for 600s (1400 to 2000)
        afk_events = [
            {"timestamp": 1000.0, "duration": 400.0, "data": {"status": "not-afk"}},
            {"timestamp": 1400.0, "duration": 600.0, "data": {"status": "afk"}},
        ]
        # Window was left open with firefox for the entire 1000s
        window_events = [
            {"timestamp": 1000.0, "duration": 1000.0, "data": {"app": "firefox", "title": "browser"}},
        ]

        summary = ai.compute_screen_time_summary(
            afk_events=afk_events,
            window_events=window_events,
            web_events_by_browser={},
            window_start=w_start,
            window_end=w_end,
        )

        self.assertTrue(summary["invariants_passed"])
        self.assertAlmostEqual(summary["total_tracked_seconds"], 1000.0, places=1)
        self.assertAlmostEqual(summary["active_screen_seconds"], 400.0, places=1)

        # Firefox active focus MUST be only 400s, not 1000s!
        self.assertEqual(len(summary["apps"]), 1)
        self.assertAlmostEqual(summary["apps"][0]["seconds"], 400.0, places=1)
        self.assertLessEqual(summary["apps"][0]["seconds"], summary["active_screen_seconds"])

    def test_browser_site_focus_intersection(self):
        """
        Site time must ONLY accumulate when that browser window is focused and user is not AFK.
        Evidence from user report: claude.ai showed 11h+ while Firefox was only 3h58m!
        Here: claude.ai open for 1000s, but Firefox only focused for 300s.
        """
        w_start = 1000.0
        w_end = 2000.0  # 1000s

        afk_events = [
            {"timestamp": 1000.0, "duration": 1000.0, "data": {"status": "not-afk"}},
        ]
        # User in antigravity for 700s, switches to firefox for 300s
        window_events = [
            {"timestamp": 1000.0, "duration": 700.0, "data": {"app": "antigravity", "title": "editor"}},
            {"timestamp": 1700.0, "duration": 300.0, "data": {"app": "firefox", "title": "web"}},
        ]
        # Web watcher recorded claude.ai active for the whole 1000s!
        web_events_firefox = [
            {"timestamp": 1000.0, "duration": 1000.0, "data": {"url": "https://claude.ai/chat", "title": "Claude"}},
        ]

        summary = ai.compute_screen_time_summary(
            afk_events=afk_events,
            window_events=window_events,
            web_events_by_browser={"firefox": web_events_firefox},
            window_start=w_start,
            window_end=w_end,
        )

        self.assertTrue(summary["invariants_passed"])
        # Firefox focus is 300s
        self.assertAlmostEqual(summary["browser_focus"]["firefox"], 300.0, places=1)

        # claude.ai site time MUST be <= 300.0s (NOT 1000.0s!)
        tabs = summary["browser_tabs"]
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["site"], "claude.ai")
        self.assertAlmostEqual(tabs[0]["seconds"], 300.0, places=1)

        # Invariant 2: sum(sites) <= browser focus time
        sum_sites = sum(t["seconds"] for t in tabs if t["browser"] == "firefox")
        self.assertLessEqual(sum_sites, summary["browser_focus"]["firefox"] + 0.1)

    def test_background_browser_yields_zero_site_time(self):
        """If browser runs in background while another app is active, site time must be 0."""
        w_start = 1000.0
        w_end = 2000.0

        afk_events = [
            {"timestamp": 1000.0, "duration": 1000.0, "data": {"status": "not-afk"}},
        ]
        # Only kitty was focused
        window_events = [
            {"timestamp": 1000.0, "duration": 1000.0, "data": {"app": "kitty", "title": "terminal"}},
        ]
        # Browser watcher has active tabs
        web_events_firefox = [
            {"timestamp": 1000.0, "duration": 1000.0, "data": {"url": "https://claude.ai/chat"}},
        ]

        summary = ai.compute_screen_time_summary(
            afk_events=afk_events,
            window_events=window_events,
            web_events_by_browser={"firefox": web_events_firefox},
            window_start=w_start,
            window_end=w_end,
        )

        self.assertTrue(summary["invariants_passed"])
        self.assertEqual(summary["browser_focus"]["firefox"], 0.0)
        self.assertEqual(len(summary["browser_tabs"]), 0)

    def test_multi_tab_sum_under_browser_focus(self):
        """Sum of multiple sites within a browser must be <= that browser's focus time."""
        w_start = 1000.0
        w_end = 2000.0

        afk_events = [
            {"timestamp": 1000.0, "duration": 1000.0, "data": {"status": "not-afk"}},
        ]
        # Firefox focused from 1200 to 1800 (600s total)
        window_events = [
            {"timestamp": 1000.0, "duration": 200.0, "data": {"app": "antigravity"}},
            {"timestamp": 1200.0, "duration": 600.0, "data": {"app": "firefox"}},
            {"timestamp": 1800.0, "duration": 200.0, "data": {"app": "antigravity"}},
        ]
        # Tab 1: github.com active 1100 to 1500 (400s)
        # Tab 2: youtube.com active 1500 to 1900 (400s)
        web_events_firefox = [
            {"timestamp": 1100.0, "duration": 400.0, "data": {"url": "https://github.com/mteyra"}},
            {"timestamp": 1500.0, "duration": 400.0, "data": {"url": "https://www.youtube.com/watch"}},
        ]

        summary = ai.compute_screen_time_summary(
            afk_events=afk_events,
            window_events=window_events,
            web_events_by_browser={"firefox": web_events_firefox},
            window_start=w_start,
            window_end=w_end,
        )

        self.assertTrue(summary["invariants_passed"])
        ff_focus = summary["browser_focus"]["firefox"]
        self.assertAlmostEqual(ff_focus, 600.0, places=1)

        sum_sites = sum(t["seconds"] for t in summary["browser_tabs"] if t["browser"] == "firefox")
        self.assertLessEqual(sum_sites, ff_focus + 0.1)
        self.assertAlmostEqual(sum_sites, 600.0, places=1)

    def test_multi_browser_isolation(self):
        """Chrome sites must intersect Chrome windows, Firefox sites intersect Firefox windows."""
        w_start = 1000.0
        w_end = 2000.0

        afk_events = [
            {"timestamp": 1000.0, "duration": 1000.0, "data": {"status": "not-afk"}},
        ]
        window_events = [
            {"timestamp": 1000.0, "duration": 300.0, "data": {"app": "google-chrome"}},
            {"timestamp": 1300.0, "duration": 400.0, "data": {"app": "firefox"}},
            {"timestamp": 1700.0, "duration": 300.0, "data": {"app": "kitty"}},
        ]
        web_chrome = [
            {"timestamp": 1000.0, "duration": 1000.0, "data": {"url": "https://google.com"}},
        ]
        web_firefox = [
            {"timestamp": 1000.0, "duration": 1000.0, "data": {"url": "https://github.com"}},
        ]

        summary = ai.compute_screen_time_summary(
            afk_events=afk_events,
            window_events=window_events,
            web_events_by_browser={"chrome": web_chrome, "firefox": web_firefox},
            window_start=w_start,
            window_end=w_end,
        )

        self.assertTrue(summary["invariants_passed"])
        self.assertAlmostEqual(summary["browser_focus"]["chrome"], 300.0, places=1)
        self.assertAlmostEqual(summary["browser_focus"]["firefox"], 400.0, places=1)

        chrome_site = next(t for t in summary["browser_tabs"] if t["site"] == "google.com")
        ff_site = next(t for t in summary["browser_tabs"] if t["site"] == "github.com")

        self.assertAlmostEqual(chrome_site["seconds"], 300.0, places=1)
        self.assertAlmostEqual(ff_site["seconds"], 400.0, places=1)

    def test_suspend_sleep_gap(self):
        """System sleep/suspend creates a time gap: tracked <= window must hold."""
        w_start = 1000.0
        w_end = 10000.0  # 9000s window

        # Machine active 1000 to 2000, then slept from 2000 to 8000, then active 8000 to 9000
        afk_events = [
            {"timestamp": 1000.0, "duration": 1000.0, "data": {"status": "not-afk"}},
            {"timestamp": 8000.0, "duration": 1000.0, "data": {"status": "not-afk"}},
        ]
        window_events = [
            {"timestamp": 1000.0, "duration": 1000.0, "data": {"app": "firefox"}},
            {"timestamp": 8000.0, "duration": 1000.0, "data": {"app": "antigravity"}},
        ]

        summary = ai.compute_screen_time_summary(
            afk_events=afk_events,
            window_events=window_events,
            web_events_by_browser={},
            window_start=w_start,
            window_end=w_end,
        )

        self.assertTrue(summary["invariants_passed"])
        # Tracked and active should be 2000s, far below 9000s
        self.assertAlmostEqual(summary["total_tracked_seconds"], 2000.0, places=1)
        self.assertAlmostEqual(summary["active_screen_seconds"], 2000.0, places=1)
        self.assertLessEqual(summary["total_tracked_seconds"], 9000.0)


    def test_synthetic_fuzz_invariants(self):
        """Fuzz test with 50 randomized overlapping/jittered streams of events."""
        import random

        for seed in range(50):
            random.seed(seed)
            w_start = 1000.0
            w_end = 5000.0  # 4000s window

            afk_events = []
            t = w_start - 500.0
            while t < w_end + 500.0:
                dur = random.uniform(5.0, 300.0)
                status = random.choice(["afk", "not-afk"])
                afk_events.append({"timestamp": t, "duration": dur, "data": {"status": status}})
                t += dur + random.uniform(-10.0, 50.0)

            win_events = []
            t = w_start - 300.0
            apps = ["antigravity", "firefox", "kitty", "google-chrome"]
            while t < w_end + 300.0:
                dur = random.uniform(5.0, 200.0)
                app = random.choice(apps)
                win_events.append({"timestamp": t, "duration": dur, "data": {"app": app}})
                t += dur + random.uniform(-5.0, 20.0)

            web_ff = []
            t = w_start - 200.0
            sites = ["https://claude.ai", "https://github.com", "https://amazon.de"]
            while t < w_end + 200.0:
                dur = random.uniform(10.0, 400.0)
                site = random.choice(sites)
                web_ff.append({"timestamp": t, "duration": dur, "data": {"url": site, "audible": random.choice([True, False])}})
                t += dur + random.uniform(-10.0, 30.0)

            summary = ai.compute_screen_time_summary(
                afk_events=afk_events,
                window_events=win_events,
                web_events_by_browser={"firefox": web_ff},
                window_start=w_start,
                window_end=w_end,
            )
            self.assertTrue(summary["invariants_passed"], f"Seed {seed} failed invariants: {summary}")


if __name__ == "__main__":
    unittest.main()
