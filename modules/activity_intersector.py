#!/usr/bin/env python3
"""
activity_intersector.py - Mathematical interval intersection engine for ActivityWatch and telemetry.

Guarantees 3 core invariants:
1. tracked <= window
2. sum(sites per browser) <= that browser's focus time
3. sum(apps) <= active time <= tracked
"""

import urllib.parse
from datetime import datetime, timezone

BROWSER_FAMILIES = {
    "firefox": {"firefox", "navigator"},
    "chrome": {
        "google-chrome",
        "chromium",
        "chrome",
        "brave",
        "brave-browser",
        "microsoft-edge",
        "opera",
        "vivaldi",
    },
}

ALL_BROWSER_APPS = set.union(*BROWSER_FAMILIES.values())


def is_browser_app(app_name):
    if not app_name:
        return False
    return app_name.lower() in ALL_BROWSER_APPS


def parse_iso(ts):
    """Parse ISO 8601 timestamp string into unix epoch timestamp (float)."""
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).timestamp()


def clip_events(events, t_start, t_end):
    """
    Clip events to [t_start, t_end].
    Each event must have 'timestamp' (or 'start') and 'duration' (or 'end').
    Returns sorted list of (start, end, data_dict).
    """
    clipped = []
    for ev in events:
        if isinstance(ev, (list, tuple)):
            s = float(ev[0])
            e = float(ev[1])
            data = ev[2] if len(ev) > 2 else {}
        else:
            ts = ev.get("timestamp") if "timestamp" in ev else ev.get("start")
            if ts is None:
                continue
            s = parse_iso(ts)
            dur = float(ev.get("duration", 0.0) or 0.0)
            e = s + dur
            data = ev.get("data", {})

        s_c = max(s, t_start)
        e_c = min(e, t_end)
        if s_c < e_c:
            clipped.append((s_c, e_c, data))

    clipped.sort(key=lambda x: x[0])
    return clipped


def make_disjoint(events):
    """
    Given a list of (start, end, data), ensure intervals are strictly non-overlapping.
    If event B starts before event A ends, event A is truncated to event B's start.
    """
    if not events:
        return []
    sorted_evs = sorted(events, key=lambda x: x[0])
    res = []
    for s, e, data in sorted_evs:
        if s >= e:
            continue
        if res:
            prev_s, prev_e, prev_data = res[-1]
            if s < prev_e:
                if prev_s >= s:
                    # Previous interval completely superseded
                    res.pop()
                else:
                    res[-1] = (prev_s, s, prev_data)
        if not res or s < e:
            res.append((s, e, data))
    return [r for r in res if r[0] < r[1]]


def merge_intervals(intervals):
    """Merge overlapping or contiguous intervals [(start, end, ...)]."""
    if not intervals:
        return []
    sorted_ivs = sorted(intervals, key=lambda x: x[0])
    merged = [sorted_ivs[0]]
    for item in sorted_ivs[1:]:
        s = item[0]
        e = item[1]
        data = item[2] if len(item) > 2 else None
        prev_s, prev_e = merged[-1][0], merged[-1][1]
        if s <= prev_e:
            merged[-1] = (prev_s, max(prev_e, e), data)
        else:
            merged.append((s, e, data))
    return [(m[0], m[1]) for m in merged]


def intersect_intervals(ivs_a, ivs_b):
    """
    Intersect two lists of disjoint intervals sorted by start time.
    Returns list of (start, end, data_a, data_b).
    """
    res = []
    i, j = 0, 0
    while i < len(ivs_a) and j < len(ivs_b):
        sa = ivs_a[i][0]
        ea = ivs_a[i][1]
        da = ivs_a[i][2] if len(ivs_a[i]) > 2 else {}

        sb = ivs_b[j][0]
        eb = ivs_b[j][1]
        db = ivs_b[j][2] if len(ivs_b[j]) > 2 else {}

        s = max(sa, sb)
        e = min(ea, eb)
        if s < e:
            res.append((s, e, da, db))
        if ea < eb:
            i += 1
        else:
            j += 1
    return res


def extract_site_domain(url, title=""):
    """Extract clean domain/site name from URL or title."""
    if not url:
        return title or "(browser)"
    try:
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc
        if netloc:
            # Strip port if present
            if ":" in netloc:
                netloc = netloc.split(":", 1)[0]
            return netloc
        if parsed.scheme in ("about", "chrome", "moz-extension"):
            return parsed.scheme + ":" + (parsed.path or "")
    except Exception:
        pass
    return title or "(browser)"


def compute_screen_time_summary(
    afk_events,
    window_events,
    web_events_by_browser,
    window_start,
    window_end,
):
    """
    Computes exact screen time summary guaranteed to satisfy all 3 invariants.

    Parameters:
    - afk_events: raw events from aw-watcher-afk
    - window_events: raw events from aw-watcher-window
    - web_events_by_browser: dict mapping browser_key ('firefox', 'chrome', etc.) -> list of web events
    - window_start: unix timestamp (float)
    - window_end: unix timestamp (float)

    Returns:
    dict with:
    - window_seconds
    - total_tracked_seconds
    - active_screen_seconds
    - apps: list of {"app": str, "seconds": float}
    - browser_focus: dict of {browser_key: seconds}
    - browser_tabs: list of {"site": str, "browser": str, "seconds": float, "audible_seconds": float}
    - invariants_passed: bool
    """
    window_duration = max(0.0, float(window_end - window_start))

    # 1. Clip and merge AFK events
    clipped_afk = clip_events(afk_events, window_start, window_end)

    # Tracked time: union of all AFK events (afk + not-afk)
    tracked_ivs = merge_intervals([(s, e, d) for s, e, d in clipped_afk])
    total_tracked_seconds = min(
        window_duration,
        sum(e - s for s, e in tracked_ivs),
    )

    # Active time: union of 'not-afk' events
    not_afk_raw = [(s, e, d) for s, e, d in clipped_afk if d.get("status") == "not-afk"]
    active_ivs = merge_intervals(not_afk_raw)
    active_screen_seconds = min(
        total_tracked_seconds,
        sum(e - s for s, e in active_ivs),
    )

    # If no AFK bucket exists or returned 0 events, but window events exist,
    # fallback active time to window duration / events.
    if not clipped_afk and window_events:
        clipped_win_fallback = clip_events(window_events, window_start, window_end)
        disjoint_win_fallback = make_disjoint(clipped_win_fallback)
        total_tracked_seconds = min(
            window_duration,
            sum(e - s for s, e, _ in disjoint_win_fallback),
        )
        active_screen_seconds = total_tracked_seconds
        active_ivs = [(s, e) for s, e, _ in disjoint_win_fallback]

    # 2. Window events clipped and made disjoint
    clipped_window = clip_events(window_events, window_start, window_end)
    disjoint_window = make_disjoint(clipped_window)

    # Intersect window events with active (not-afk) intervals
    active_window_ivs = intersect_intervals(disjoint_window, [(s, e, {}) for s, e in active_ivs])

    # Aggregate by app
    app_durations = {}
    for s, e, da, _ in active_window_ivs:
        app = str(da.get("app") or "(unfocused/desktop)").strip()
        if not app:
            app = "(unfocused/desktop)"
        dur = e - s
        app_durations[app] = app_durations.get(app, 0.0) + dur

    # Sort apps descending
    apps_list = [
        {"app": app, "seconds": round(sec, 1)}
        for app, sec in sorted(app_durations.items(), key=lambda x: x[1], reverse=True)
    ]

    # 3. Browser site focus intersection
    browser_focus = {}
    browser_tabs_list = []

    # Map each browser family to its active intervals
    for b_key, b_apps in BROWSER_FAMILIES.items():
        b_active_ivs = [
            (s, e, da)
            for s, e, da, _ in active_window_ivs
            if (da.get("app") or "").lower() in b_apps
        ]
        b_focus_sec = sum(e - s for s, e, _ in b_active_ivs)
        browser_focus[b_key] = b_focus_sec

        # Get web events for this browser
        raw_web = web_events_by_browser.get(b_key, [])
        if not raw_web:
            # Check generic key 'web' or 'all'
            raw_web = web_events_by_browser.get("web", [])

        if raw_web and b_active_ivs:
            clipped_web = clip_events(raw_web, window_start, window_end)
            disjoint_web = make_disjoint(clipped_web)

            # Intersect web events with focused active browser window intervals
            site_active = intersect_intervals(disjoint_web, b_active_ivs)

            site_dur = {}
            site_audible = {}
            site_title = {}

            for s, e, dw, _ in site_active:
                dur = e - s
                url = dw.get("url") or ""
                title = dw.get("title") or ""
                site = extract_site_domain(url, title)

                site_dur[site] = site_dur.get(site, 0.0) + dur
                site_title[site] = title
                if dw.get("audible"):
                    site_audible[site] = site_audible.get(site, 0.0) + dur

            for site, sec in sorted(site_dur.items(), key=lambda x: x[1], reverse=True):
                browser_tabs_list.append({
                    "site": site,
                    "title": site_title.get(site, ""),
                    "browser": b_key,
                    "seconds": round(sec, 1),
                    "audible_seconds": round(site_audible.get(site, 0.0), 1),
                })

    # Sort tabs overall by seconds descending
    browser_tabs_list.sort(key=lambda x: x["seconds"], reverse=True)

    # 4. Verify Invariants
    invariants_passed = verify_invariants({
        "window_seconds": window_duration,
        "total_tracked_seconds": total_tracked_seconds,
        "active_screen_seconds": active_screen_seconds,
        "apps": apps_list,
        "browser_focus": browser_focus,
        "browser_tabs": browser_tabs_list,
    })

    return {
        "window_seconds": round(window_duration, 1),
        "total_tracked_seconds": round(total_tracked_seconds, 1),
        "active_screen_seconds": round(active_screen_seconds, 1),
        "apps": apps_list,
        "browser_focus": {k: round(v, 1) for k, v in browser_focus.items()},
        "browser_tabs": browser_tabs_list,
        "invariants_passed": invariants_passed,
    }


def verify_invariants(summary, tolerance=0.15):
    """
    Check the 3 invariants:
    1. tracked <= window
    2. sum(sites per browser) <= that browser's focus time
    3. sum(apps) <= active time <= tracked
    """
    window = summary["window_seconds"]
    tracked = summary["total_tracked_seconds"]
    active = summary["active_screen_seconds"]
    apps = summary["apps"]
    browser_focus = summary.get("browser_focus", {})
    browser_tabs = summary.get("browser_tabs", [])

    # Invariant 1: tracked <= window
    if tracked > window + tolerance:
        return False

    # Invariant 3: sum(apps) <= active time <= tracked
    if active > tracked + tolerance:
        return False

    sum_apps = sum(a["seconds"] for a in apps)
    if sum_apps > active + tolerance:
        return False

    # Invariant 2: sum(sites per browser) <= that browser's focus time
    for b_key, b_focus in browser_focus.items():
        sum_sites = sum(t["seconds"] for t in browser_tabs if t.get("browser") == b_key)
        if sum_sites > b_focus + tolerance:
            return False

    return True
