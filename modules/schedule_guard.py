#!/usr/bin/env python3
"""Defer scheduled work before it competes with a busy Linux system."""

import math
import os
from pathlib import Path
import sys

DEFERRED = 75


def read_full_pressure(path):
    """PSI is optional on kernels without pressure accounting."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return 0.0
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] == "full":
            values = dict(item.split("=", 1) for item in fields[1:])
            value = float(values["avg60"])
            if math.isfinite(value) and 0 <= value <= 100:
                return value
            raise ValueError("invalid pressure average")
    raise ValueError(f"missing full pressure average in {path}")


def busy_reasons(proc=Path("/proc"), cpu_count=None):
    memory = {}
    for line in (proc / "meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in ("MemAvailable", "MemTotal"):
            memory[key] = float(value.split()[0])
    total, available = memory["MemTotal"], memory["MemAvailable"]
    if not (math.isfinite(total) and math.isfinite(available) and 0 <= available <= total and total > 0):
        raise ValueError("invalid available-memory readings")
    load = float((proc / "loadavg").read_text().split()[0])
    if not math.isfinite(load) or load < 0:
        raise ValueError("invalid load average")
    cpus = max(1, cpu_count if cpu_count is not None else (os.cpu_count() or 1))
    reasons = []
    if available / total < 0.15:
        reasons.append(f"only {available / total:.0%} of memory is available (needs 15%)")
    if load >= cpus:
        reasons.append(f"load average {load:.2f} is at least the {cpus} logical CPUs")
    memory_pressure = read_full_pressure(proc / "pressure" / "memory")
    io_pressure = read_full_pressure(proc / "pressure" / "io")
    if memory_pressure >= 2:
        reasons.append(f"memory stalls average {memory_pressure:.1f}% over 60 seconds")
    if io_pressure >= 10:
        reasons.append(f"I/O stalls average {io_pressure:.1f}% over 60 seconds")
    return reasons


def main():
    try:
        reasons = busy_reasons()
    except (OSError, ValueError, KeyError, IndexError) as exc:
        print(f"nixcare: Cannot check system load: {exc}", file=sys.stderr)
        return 1
    if reasons:
        print("nixcare: Waiting for system resources: " + "; ".join(reasons)
              + ". Scheduled work will retry in 15 minutes.", file=sys.stderr)
        return DEFERRED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
