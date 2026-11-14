"""Small, independently testable boundaries for the local telemetry service."""

import math
import json
import threading
import time


def validate_hours(value, retention_days):
    """Reject unbounded, nonfinite and negative database lookback windows."""
    try:
        hours = float(value)
    except (TypeError, ValueError):
        raise ValueError("hours must be a number") from None
    if not math.isfinite(hours) or not 0 < hours <= retention_days * 24:
        raise ValueError(f"hours must be greater than 0 and at most {retention_days * 24}")
    return hours


def read_json_bounded(response, max_bytes=4 * 1024 * 1024):
    """A long ActivityWatch history must not exhaust the collector's memory."""
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError("Activity history exceeds the response limit; request fewer hours")
    return json.loads(body.decode("utf-8"))


def sample_duration(elapsed, interval=5.0):
    """Do not turn suspend/clock jumps into a burst of fictitious sampled work."""
    if not math.isfinite(elapsed) or elapsed <= 0 or elapsed > interval * 3:
        return None
    return elapsed


def select_activity_buckets(buckets, hostname):
    """Prefer this host's histories; never merge imported or ambiguous ones."""
    if not isinstance(buckets, dict):
        raise ValueError("Invalid ActivityWatch bucket response")
    hostname = hostname.lower().rstrip(".")
    candidates = {}
    for bucket_id, metadata in buckets.items():
        if not isinstance(bucket_id, str) or not isinstance(metadata, dict):
            continue
        bid = bucket_id.lower()
        if bid.startswith("aw-watcher-afk"):
            group = "afk"
        elif bid.startswith("aw-watcher-window"):
            group = "window"
        elif bid.startswith("aw-watcher-web"):
            if "firefox" in bid:
                group = "firefox"
            elif any(name in bid for name in ("chrome", "chromium", "brave", "edge", "opera", "vivaldi")):
                group = "chrome"
            else:
                continue
        else:
            continue
        reported_host = str(metadata.get("hostname") or "").lower().rstrip(".")
        unknown_host = reported_host in ("", "unknown")
        if reported_host == hostname or (unknown_host and bid.endswith("_" + hostname)):
            priority = 1
        elif unknown_host and "_" not in bid:
            # Older browser extensions use one hostless bucket per browser.
            priority = 0
        else:
            continue
        candidates.setdefault(group, {}).setdefault(priority, []).append(bucket_id)
    selected = {}
    for group, ranked in candidates.items():
        matches = ranked[max(ranked)]
        if len(matches) != 1:
            raise ValueError("Ambiguous ActivityWatch " + group + " histories for this host")
        selected[group] = matches[0]
    return selected


def local_request_allowed(headers, port):
    """Refuse DNS rebinding and cross-origin browser access to private reports.

    Loopback HTTP remains accessible to other local OS users. This is a browser
    boundary, not authentication, and the database itself remains owner-only.
    """
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if headers.get("Host", "").lower() not in hosts:
        return False
    origin = headers.get("Origin")
    if origin and origin.lower() not in {"http://" + host for host in hosts}:
        return False
    return headers.get("Sec-Fetch-Site", "none") in ("same-origin", "none")


class SummaryCache:
    """Coalesce simultaneous dashboard queries and bound retained results."""

    def __init__(self, ttl=15, clock=time.monotonic):
        self.ttl = ttl
        self.clock = clock
        self.lock = threading.Lock()
        self.entries = {}

    def get(self, key, compute):
        with self.lock:
            now = self.clock()
            cached = self.entries.get(key)
            if cached and now - cached[0] < self.ttl:
                return cached[1]
            result = compute()
            self.entries = {
                k: v for k, v in self.entries.items()
                if now - v[0] < self.ttl
            }
            if len(self.entries) >= 8:
                self.entries.clear()
            # A slow calculation already used part of the freshness budget.
            self.entries[key] = (now, result)
            return result
