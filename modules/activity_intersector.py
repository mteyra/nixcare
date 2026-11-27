#!/usr/bin/env python3
"""
activity_intersector.py - Mathematical interval intersection engine for ActivityWatch and telemetry.

Guarantees 3 core invariants:
1. tracked <= window
2. sum(sites per browser) <= that browser's focus time
3. sum(apps) <= active time <= tracked
"""

import math
import re
import time
import urllib.parse
from datetime import datetime

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


def duration_seconds(value):
    """Round attributed durations down so many tiny rows cannot invent time."""
    return math.floor(max(0.0, value) * 10 + 1e-9) / 10


def is_browser_app(app_name):
    if not isinstance(app_name, str):
        return False
    return app_name.strip().lower() in ALL_BROWSER_APPS


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
        try:
            if isinstance(ev, (list, tuple)):
                s = float(ev[0])
                e = float(ev[1])
                data = ev[2] if len(ev) > 2 else {}
            else:
                ts = ev.get("timestamp") if "timestamp" in ev else ev.get("start")
                if ts is None:
                    continue
                s = parse_iso(ts)
                e = float(ev["end"]) if "end" in ev else s + float(ev.get("duration", 0.0) or 0.0)
                data = ev.get("data", {})
            if not math.isfinite(s) or not math.isfinite(e):
                continue
            if not isinstance(data, dict):
                data = {}
        except (TypeError, ValueError, IndexError, AttributeError, OverflowError):
            continue

        s_c = max(s, t_start)
        e_c = min(e, t_end)
        if s_c < e_c:
            # Keep original chronology when multiple events began before the
            # window. Sorting only clipped starts makes API response order
            # decide whether an older active event overrides a newer AFK one.
            clipped.append((s, s_c, e_c, data))

    clipped.sort(key=lambda x: x[0])
    return [(s_c, e_c, data) for _, s_c, e_c, data in clipped]


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


def subtract_intervals(base_ivs, sub_ivs):
    """
    Subtract sorted disjoint intervals `sub_ivs` from sorted disjoint intervals `base_ivs`.
    Returns a list of (start, end, data) intervals remaining in `base_ivs`.
    """
    if not base_ivs:
        return []
    if not sub_ivs:
        return [(b[0], b[1], b[2] if len(b) > 2 else {}) for b in base_ivs if b[0] < b[1]]

    res = []
    j = 0
    n_sub = len(sub_ivs)
    for b in base_ivs:
        bs, be = b[0], b[1]
        bdata = b[2] if len(b) > 2 else {}
        if bs >= be:
            continue
        cur = bs
        while j < n_sub and sub_ivs[j][1] <= cur:
            j += 1
        k = j
        while k < n_sub and sub_ivs[k][0] < be:
            ss, se = sub_ivs[k][0], sub_ivs[k][1]
            if ss > cur:
                res.append((cur, min(be, ss), bdata))
            cur = max(cur, se)
            if cur >= be:
                break
            k += 1
        if cur < be:
            res.append((cur, be, bdata))
    return res


BROWSER_TITLE_SUFFIXES = (
    " — Mozilla Firefox",
    " - Mozilla Firefox",
    " — Firefox",
    " - Firefox",
    " - Google Chrome",
    " — Google Chrome",
    " - Chromium",
    " — Chromium",
    " - Brave",
    " — Brave",
    " - Vivaldi",
    " — Vivaldi",
    " - Microsoft Edge",
    " — Microsoft Edge",
)


def strip_browser_suffix(title):
    """Strip standard browser window title suffixes to recover the active tab title."""
    if not isinstance(title, str):
        return ""
    t = title.strip()
    for suffix in BROWSER_TITLE_SUFFIXES:
        if t.endswith(suffix):
            return t[:-len(suffix)].strip()
    return t


def reconstruct_browser_web_intervals(raw_web, b_win_ivs, window_start, window_end, pulse_lease=65.0):
    """
    Reconstruct active browser tab intervals aligned to focused browser windows.
    Handles both standard duration>0 web events AND multi-window duration=0 heartbeats
    (where 2+ open browser windows interleave 60s heartbeats in the same bucket).
    """
    if not raw_web or not b_win_ivs:
        return []

    standard_disjoint = make_disjoint(clip_events(raw_web, window_start, window_end))

    # Parse all valid raw web heartbeats (including duration=0) to recover multi-window streams
    parsed_raw = []
    for ev in raw_web:
        try:
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
                if dur < 0:
                    continue
                e = float(ev["end"]) if "end" in ev else s + dur
                data = ev.get("data", {})
            if not math.isfinite(s) or not math.isfinite(e) or e < s:
                continue
            if not isinstance(data, dict):
                data = {}
            parsed_raw.append((s, e, data))
        except (TypeError, ValueError, IndexError, AttributeError, OverflowError):
            continue

    parsed_raw.sort(key=lambda x: x[0])

    by_title = {}
    for s, e, d in parsed_raw:
        t = str(d.get("title") or "").strip()
        if t:
            by_title.setdefault(t, []).append((s, e, d))

    extended_by_title = {}
    for t, evs in by_title.items():
        leased = []
        for idx, (s, e, d) in enumerate(evs):
            next_s = evs[idx + 1][0] if idx + 1 < len(evs) else (s + pulse_lease)
            lease_e = max(e, min(next_s, s + pulse_lease))
            s_c = max(s, window_start)
            e_c = min(lease_e, window_end)
            if s_c < e_c:
                leased.append((s, s_c, e_c, d))
        leased.sort(key=lambda x: x[0])
        extended_by_title[t] = make_disjoint([(s_c, e_c, d) for _, s_c, e_c, d in leased])

    resolved = []
    for ws, we, wd in b_win_ivs:
        wtitle = strip_browser_suffix(wd.get("title", "")) if isinstance(wd, dict) else ""
        if wtitle and wtitle in extended_by_title and extended_by_title[wtitle]:
            matched = [
                (s, e, web_d)
                for s, e, _, web_d in intersect_intervals([(ws, we, wd)], extended_by_title[wtitle])
            ]
            resolved.extend(matched)
            remaining = subtract_intervals([(ws, we, wd)], [(s, e, {}) for s, e, _ in matched])
            if remaining and standard_disjoint:
                for s, e, web_d, _ in intersect_intervals(standard_disjoint, remaining):
                    resolved.append((s, e, web_d))
        elif standard_disjoint:
            for s, e, web_d, _ in intersect_intervals(standard_disjoint, [(ws, we, wd)]):
                resolved.append((s, e, web_d))

    return make_disjoint(resolved)


def extract_site_domain(url, title=""):
    """Extract clean domain/site name from URL or title."""
    fallback = title if isinstance(title, str) and title else "(browser)"
    if not isinstance(url, str) or not url:
        return fallback
    try:
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.hostname
        if netloc:
            return netloc
        if parsed.scheme in ("about", "chrome", "moz-extension"):
            return parsed.scheme + ":" + (parsed.path or "")
    except Exception:
        pass
    return fallback


AUDIO_ONLY_DOMAINS = {
    "open.spotify.com",
    "music.youtube.com",
    "soundcloud.com",
    "www.soundcloud.com",
    "tidal.com",
    "listen.tidal.com",
    "deezer.com",
    "www.deezer.com",
    "music.apple.com",
}


def is_video_site(site):
    """
    Return True if an audible focused browser tab on `site` represents video playback
    (i.e. any site other than dedicated audio-only music players).
    """
    if not isinstance(site, str):
        return False
    s = site.strip().lower()
    if not s:
        return False
    return s not in AUDIO_ONLY_DOMAINS


# Screen-time labels: one flat list where every second of active time has
# exactly one label. Only the sites listed here get their own label (the
# fixed list); every other site stays inside the browser's app name.
DEFAULT_SITE_LABELS = {
    "claude.ai": "claude.ai",
    "youtube.com": "YouTube",
    "github.com": "GitHub",
    "studon.fau.de": "StudOn",
}

# Terminals split into "Claude Code" and "<app>-shell". Shell titles (fish)
# end in the working directory ("~", "/e/nixos", "user@host: ~"); Claude Code
# replaces the title with "Claude Code" or its session name.
TERMINAL_APPS = {"kitty"}
_SHELL_TITLE = re.compile(r"(^|\s)[~/]\S*$|^\S+@\S+:")


def site_label(site, site_labels):
    """Label for a listed site or any of its subdomains, else None."""
    host = site.strip().lower().rstrip(".") if isinstance(site, str) else ""
    while host:
        if host in site_labels:
            return site_labels[host]
        if "." not in host:
            return None
        host = host.split(".", 1)[1]
    return None


def window_label(app, title):
    """Label for a focused window: the app name, or Claude Code / <terminal>-shell."""
    if app.strip().lower() in TERMINAL_APPS:
        t = title.strip() if isinstance(title, str) else ""
        if t == "Claude Code" or t.startswith("claude ") or (t and not _SHELL_TITLE.search(t)):
            return "Claude Code"
        return app + "-shell"
    return app


def hourly_seconds(intervals):
    """Split (start, end) intervals at local clock hours: [{"start", "seconds"}]."""
    buckets = {}
    for s, e in intervals:
        t = s
        while t < e:
            offset = time.localtime(t).tm_gmtoff
            hour_start = t - ((t + offset) % 3600)
            seg_end = min(e, hour_start + 3600)
            buckets[hour_start] = buckets.get(hour_start, 0.0) + (seg_end - t)
            t = seg_end
    return [
        {"start": int(round(h)), "seconds": duration_seconds(sec)}
        for h, sec in sorted(buckets.items())
    ]


def compute_screen_time_summary(
    afk_events,
    window_events,
    web_events_by_browser,
    window_start,
    window_end,
    site_labels=None,
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
    - input_active_seconds
    - media_active_seconds
    - daytime_idle_seconds
    - overnight_sleep_seconds
    - suspend_seconds
    - waking_seconds
    - waking_active_pct
    - sleep_blocks: list of {"start": float, "end": float, "seconds": float}
    - apps: list of {"app": str, "seconds": float}
    - browser_focus: dict of {browser_key: seconds}
    - browser_tabs: list of {"site": str, "browser": str, "seconds": float, "audible_seconds": float}
    - labels: list of {"label": str, "seconds": float}, one label per second of
      active window time (site_labels, Claude Code / <terminal>-shell, app name)
    - hourly: list of {"start": local hour start (unix), "seconds": active seconds}
    - invariants_passed: bool
    """
    if site_labels is None:
        site_labels = DEFAULT_SITE_LABELS
    window_start, window_end = float(window_start), float(window_end)
    if not math.isfinite(window_start) or not math.isfinite(window_end) or window_end < window_start:
        raise ValueError("Activity window must have finite, ordered boundaries")
    window_duration = window_end - window_start

    # 1. Clip and merge AFK events
    clipped_afk = make_disjoint(clip_events(afk_events, window_start, window_end))
    active_measurement_available = any(
        d.get("status") in ("afk", "not-afk") for _, _, d in clipped_afk
    )

    # Tracked time: union of all AFK events (afk + not-afk)
    tracked_ivs = merge_intervals([(s, e, d) for s, e, d in clipped_afk])
    total_tracked_seconds = min(
        window_duration,
        sum(e - s for s, e in tracked_ivs),
    )

    # Input-active time: union of 'not-afk' events
    not_afk_raw = [(s, e, d) for s, e, d in clipped_afk if d.get("status") == "not-afk"]
    input_active_ivs = merge_intervals(not_afk_raw)

    # 2. Window events clipped and made disjoint
    clipped_window = clip_events(window_events, window_start, window_end)
    disjoint_window = make_disjoint(clipped_window)

    # Pre-resolve browser web intervals against focused browser windows so we can:
    # (a) recover multi-window duration=0 heartbeats via window title matching, and
    # (b) exempt focused audible media playback (e.g. >3m YouTube videos) from the AFK cutoff.
    resolved_web_by_browser = {}
    audible_focused_ivs = []
    for b_key, b_apps in BROWSER_FAMILIES.items():
        b_win_ivs = [
            (s, e, da)
            for s, e, da in disjoint_window
            if isinstance(da.get("app"), str) and da["app"].strip().lower() in b_apps
        ]
        raw_web = web_events_by_browser.get(b_key, [])
        if not raw_web:
            raw_web = web_events_by_browser.get("web", [])
        if raw_web and b_win_ivs:
            resolved_web = reconstruct_browser_web_intervals(
                raw_web, b_win_ivs, window_start, window_end
            )
            resolved_web_by_browser[b_key] = resolved_web
            for s, e, dw in resolved_web:
                site = extract_site_domain(
                    dw.get("url") or "",
                    dw.get("title") if isinstance(dw.get("title"), str) else "",
                )
                if dw.get("audible") and is_video_site(site):
                    audible_focused_ivs.append((s, e, {}))
        else:
            resolved_web_by_browser[b_key] = []

    if active_measurement_available:
        audible_in_tracked = [
            (s, e, {})
            for s, e, _, _ in intersect_intervals(
                make_disjoint(audible_focused_ivs),
                [(ts, te, {}) for ts, te in tracked_ivs],
            )
        ]
        active_ivs = merge_intervals(
            [(s, e, {}) for s, e in input_active_ivs] + audible_in_tracked
        )
        input_active_seconds = min(
            total_tracked_seconds,
            sum(e - s for s, e in input_active_ivs),
        )
        active_screen_seconds = min(
            total_tracked_seconds,
            sum(e - s for s, e in active_ivs),
        )
        media_active_seconds = max(0.0, active_screen_seconds - input_active_seconds)
    else:
        # An open window proves coverage, not that its user was present. Let the
        # caller fall back to sampled AFK evidence instead of inventing active time.
        if window_events:
            total_tracked_seconds = min(
                window_duration,
                sum(e - s for s, e, _ in disjoint_window),
            )
        input_active_seconds = 0.0
        media_active_seconds = 0.0
        active_screen_seconds = 0.0
        active_ivs = []

    # Compute idle/sleep/suspend presence breakdown across the lookback window
    effective_idle_ivs = subtract_intervals(
        [(s, e, {}) for s, e in tracked_ivs],
        [(s, e, {}) for s, e in active_ivs],
    )
    sleep_threshold = 3.0 * 3600.0
    sleep_blocks = []
    overnight_sleep_raw = 0.0
    daytime_idle_raw = 0.0
    for s, e, _ in effective_idle_ivs:
        dur = e - s
        if dur >= sleep_threshold:
            overnight_sleep_raw += dur
            sleep_blocks.append({
                "start": s,
                "end": e,
                "seconds": duration_seconds(dur),
            })
        else:
            daytime_idle_raw += dur

    suspend_raw = max(0.0, window_duration - total_tracked_seconds)
    waking_raw = max(active_screen_seconds, window_duration - overnight_sleep_raw - suspend_raw)
    waking_active_pct = round((active_screen_seconds / waking_raw) * 100.0, 1) if waking_raw > 0 else 0.0

    # Intersect window events with active (input + audible media) intervals
    active_window_ivs = intersect_intervals(disjoint_window, [(s, e, {}) for s, e in active_ivs])

    # Aggregate by app
    app_durations = {}
    label_durations = {}
    for s, e, da, _ in active_window_ivs:
        app = str(da.get("app") or "(unfocused/desktop)").strip()
        if not app:
            app = "(unfocused/desktop)"
        dur = e - s
        app_durations[app] = app_durations.get(app, 0.0) + dur
        label = window_label(app, da.get("title"))
        label_durations[label] = label_durations.get(label, 0.0) + dur

    # Sort apps descending
    apps_list = [
        {"app": app, "seconds": duration_seconds(sec)}
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
            if isinstance(da.get("app"), str) and da["app"].strip().lower() in b_apps
        ]
        b_focus_sec = sum(e - s for s, e, _ in b_active_ivs)
        browser_focus[b_key] = b_focus_sec

        resolved_web = resolved_web_by_browser.get(b_key, [])
        if resolved_web and b_active_ivs:
            # Intersect resolved web events with focused active browser window intervals
            site_active = intersect_intervals(resolved_web, b_active_ivs)

            site_dur = {}
            site_audible = {}
            site_title = {}

            for s, e, dw, dwin in site_active:
                dur = e - s
                url = dw.get("url") or ""
                title = dw.get("title") if isinstance(dw.get("title"), str) else ""
                site = extract_site_domain(url, title)

                listed = site_label(site, site_labels)
                if listed:
                    # Move this time from the browser's own label to the site's.
                    browser_label = window_label(
                        str(dwin.get("app") or "").strip(), dwin.get("title"))
                    label_durations[browser_label] = label_durations.get(browser_label, 0.0) - dur
                    label_durations[listed] = label_durations.get(listed, 0.0) + dur

                site_dur[site] = site_dur.get(site, 0.0) + dur
                if title and (site not in site_title or not site_title[site]):
                    site_title[site] = title
                if dw.get("audible"):
                    site_audible[site] = site_audible.get(site, 0.0) + dur

            for site, sec in sorted(site_dur.items(), key=lambda x: x[1], reverse=True):
                browser_tabs_list.append({
                    "site": site,
                    "title": site_title.get(site, ""),
                    "browser": b_key,
                    "seconds": duration_seconds(sec),
                    "audible_seconds": duration_seconds(site_audible.get(site, 0.0)),
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
        "window_seconds": window_duration,
        "total_tracked_seconds": duration_seconds(total_tracked_seconds),
        "active_screen_seconds": duration_seconds(active_screen_seconds),
        "input_active_seconds": duration_seconds(input_active_seconds),
        "media_active_seconds": duration_seconds(media_active_seconds),
        "daytime_idle_seconds": duration_seconds(daytime_idle_raw),
        "overnight_sleep_seconds": duration_seconds(overnight_sleep_raw),
        "suspend_seconds": duration_seconds(suspend_raw),
        "waking_seconds": duration_seconds(waking_raw),
        "waking_active_pct": waking_active_pct,
        "sleep_blocks": sleep_blocks,
        "active_measurement_available": active_measurement_available,
        "apps": apps_list,
        "browser_focus": {k: duration_seconds(v) for k, v in browser_focus.items()},
        "browser_tabs": browser_tabs_list,
        "labels": [
            {"label": label, "seconds": duration_seconds(max(0.0, sec))}
            for label, sec in sorted(label_durations.items(), key=lambda x: x[1], reverse=True)
            if sec > 0.5
        ],
        "hourly": hourly_seconds(active_ivs),
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
    durations = (
        [window, tracked, active]
        + [a["seconds"] for a in apps]
        + list(browser_focus.values())
        + [t["seconds"] for t in browser_tabs]
        + [t.get("audible_seconds", 0) for t in browser_tabs]
    )
    if any(not math.isfinite(value) or value < 0 for value in durations):
        return False
    if any(t.get("audible_seconds", 0) > t["seconds"] + tolerance for t in browser_tabs):
        return False
    if any(t.get("browser") not in browser_focus for t in browser_tabs):
        return False

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
