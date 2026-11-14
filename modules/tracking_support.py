"""Small, independently testable boundaries for the local telemetry service."""

import math
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
            self.entries[key] = (self.clock(), result)
            return result
