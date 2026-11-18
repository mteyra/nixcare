#!/usr/bin/env python3
"""laptop-report: print the laptop telemetry summary from the local dashboard.

The NixOS module (modules/track.nix) installs it as `laptop-report` and passes
--retention-days so --hours can't ask for more history than is kept.
"""
import argparse
from datetime import datetime
import json
import math
import sys
import urllib.request

def fmt_dur(s):
    s = int(round(s))
    if s < 60:
        return str(s) + "s"
    if s < 3600:
        return str(s // 60) + "m " + ("%02ds" % (s % 60))
    return str(s // 3600) + "h " + ("%02dm" % ((s % 3600) // 60))

def print_breakdown(data, hours):
    w_sec = max(1.0, float(hours) * 3600.0)
    tracked_sec = float(data.get("total_tracked_seconds") or 0.0)
    active_sec = float(data.get("active_screen_seconds") or 0.0)
    b = data.get("breakdown") or {}
    input_sec = float(b.get("input_active_seconds", active_sec))
    media_sec = float(b.get("media_active_seconds", max(0.0, active_sec - input_sec)))
    pause_sec = float(b.get("daytime_idle_seconds", max(0.0, tracked_sec - active_sec)))
    sleep_sec = float(b.get("overnight_sleep_seconds", 0.0))
    susp_sec = float(b.get("suspend_seconds", max(0.0, w_sec - tracked_sec)))
    waking_sec = float(b.get("waking_seconds", max(active_sec, w_sec - sleep_sec - susp_sec)))
    waking_pct = float(b.get("waking_active_pct", (active_sec / waking_sec * 100.0) if waking_sec > 0 else 0.0))
    blocks = b.get("sleep_blocks") or []

    bar_w = 68
    parts = [input_sec, media_sec, pause_sec, sleep_sec, susp_sec]
    chars = ["█", "▓", "▒", "░", "·"]
    counts = [int(round(p / w_sec * bar_w)) for p in parts]
    for idx, p in enumerate(parts):
        if p > 0 and counts[idx] == 0:
            counts[idx] = 1
    diff = bar_w - sum(counts)
    if diff != 0:
        largest = max(range(len(parts)), key=lambda i: parts[i])
        counts[largest] = max(0, counts[largest] + diff)
    bar = "".join(chars[i] * counts[i] for i in range(len(parts)))

    sleep_detail = "Continuous idle blocks >= 3h"
    if blocks:
        formatted = []
        for blk in sorted(blocks, key=lambda x: x.get("start", 0))[:2]:
            try:
                st = datetime.fromtimestamp(float(blk["start"])).strftime("%a %H:%M")
                bsec = int(round(float(blk.get("seconds", 0))))
                bdur = "%dh%02dm" % (bsec // 3600, (bsec % 3600) // 60)
                formatted.append("%s (%s)" % (st, bdur))
            except Exception:
                pass
        if formatted:
            sleep_detail = ", ".join(formatted)
            if len(blocks) > 2:
                sleep_detail += " (+" + str(len(blocks) - 2) + ")"

    print("-" * 78)
    print(" WHERE THE %gh ACTUALLY WENT (Timeline & Presence Breakdown)" % hours)
    print("  [%s]" % bar)
    print("   █ Input  ▓ Video  ▒ Reading/Breaks (<3h)  ░ Sleep (>=3h)  · Off/Sleep")
    print("")
    print("  Category                          Time     Share   What It Was")
    print("  " + "-" * 74)
    print("  █ Active Screen Time (Total)  %8s    %5.1f%%   %.1f%% of ~%s waking time" % (
        fmt_dur(active_sec), active_sec / w_sec * 100.0, waking_pct, fmt_dur(waking_sec)))
    print("    ├─ Active Typing & Trackpad %8s    %5.1f%%   Keyboard/mouse input (<3m idle)" % (
        fmt_dur(input_sec), input_sec / w_sec * 100.0))
    print("    └─ Hands-Off Video Playback %8s    %5.1f%%   Audible video playback (>3m idle)" % (
        fmt_dur(media_sec), media_sec / w_sec * 100.0))
    print("  ▒ Daytime Pauses & Reading    %8s    %5.1f%%   Reading, thinking, meals (3m-3h)" % (
        fmt_dur(pause_sec), pause_sec / w_sec * 100.0))
    print("  ░ Overnight / Long Idle (>=3h)%8s    %5.1f%%   %s" % (
        fmt_dur(sleep_sec), sleep_sec / w_sec * 100.0, sleep_detail[:36]))
    print("  · Deep Suspend / Powered Off  %8s    %5.1f%%   Lid closed / system suspended" % (
        fmt_dur(susp_sec), susp_sec / w_sec * 100.0))

def main():
    ap = argparse.ArgumentParser(description="Laptop Hardware Potential & Screen Time Report")
    ap.add_argument("--hours", type=float, default=24.0, help="Lookback window in hours (default: 24)")
    ap.add_argument("--json", action="store_true", help="Print raw JSON summary")
    ap.add_argument("--retention-days", type=int, default=90, help="Days of history the collector keeps (set by the NixOS module; default: 90)")
    args = ap.parse_args()
    max_hours = args.retention_days * 24
    if not math.isfinite(args.hours) or not 0 < args.hours <= max_hours:
        ap.error("--hours must be greater than 0 and at most " + str(max_hours))

    url = "http://127.0.0.1:5601/api/summary?hours=" + str(args.hours)
    try:
        with urllib.request.urlopen(url, timeout=30.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print("Error querying laptop-telemetry daemon at " + url + ": " + str(e), file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(data, indent=2))
        return

    P = data["potential"]
    L = data.get("latest") or {}
    print("=" * 78)
    print(" NIXCARE — HARDWARE POTENTIAL & SCREEN TIME (last %gh)" % args.hours)
    print("=" * 78)
    print(" Samples: %d  |  Tracked: %s  |  Active screen time: %s" % (
        data["sample_count"], fmt_dur(data["total_tracked_seconds"]), fmt_dur(data["active_screen_seconds"])))
    print(" Hardware coverage: %.1f%% | Activity source: %s" % (
        data.get("hardware_coverage_pct", 0), data.get("activity_source", "unknown")))
    if data.get("activity_warning"):
        print(" Note: " + data["activity_warning"])
    if data["sample_count"] == 0:
        print(" No hardware samples in this window. Hardware values are unavailable.")
    else:
        print(" Power profile: %s (EPP=%s, AC=%s)  |  Battery health: %s%%" % (
            L.get("power_profile", "?"), L.get("epp", "?"), L.get("ac_online", 0), P["battery_health_pct"]))
        print_breakdown(data, args.hours)
        print("-" * 78)
        print(" HARDWARE POTENTIAL UTILIZATION (System capacity & workload ceilings)")
        print("  CPU Frequency-Weighted Busy (estimate): cur %5.1f%% | avg %5.1f%% | p95 %5.1f%% | peak %5.1f%%" % (
            L.get("cpu_potential_pct", 0), P["cpu_true_potential"]["avg"], P["cpu_true_potential"]["p95"], P["cpu_true_potential"]["max"]))
        print("  CPU Thread Busy (all active threads)   : cur %5.1f%% | avg %5.1f%% | p95 %5.1f%% | peak %5.1f%%" % (
            L.get("cpu_busy_pct", 0), P["cpu_busy"]["avg"], P["cpu_busy"]["p95"], P["cpu_busy"]["max"]))
        print("  CPU Busiest Single Thread (1T max)     : cur %5.1f%% | avg %5.1f%% | p95 %5.1f%% | peak %5.1f%%" % (
            L.get("cpu_max_thread_pct", 0), P["cpu_single_thread"]["avg"], P["cpu_single_thread"]["p95"], P["cpu_single_thread"]["max"]))
        print("  CPU Clocks (peak recorded / ceiling)   : cur %5.0f MHz | avg %5.0f MHz | peak %5.0f MHz" % (
            L.get("cpu_avg_mhz", 0), P["cpu_freq_mhz"]["avg"], P["cpu_freq_mhz"]["max"]))
        print("  GPU 3D/Render (Compositor & 3D engine) : cur %5.1f%% | avg %5.1f%% | p95 %5.1f%% | peak %5.1f%%" % (
            L.get("gpu_render_pct", 0), P["gpu_render"]["avg"], P["gpu_render"]["p95"], P["gpu_render"]["max"]))
        print("  GPU Video Decode (Hardware Acceleration): cur %5.1f%% | avg %5.1f%% | p95 %5.1f%% | peak %5.1f%%" % (
            L.get("gpu_video_pct", 0), P["gpu_video_decode"]["avg"], P["gpu_video_decode"]["p95"], P["gpu_video_decode"]["max"]))
        print("  RAM Used (excl. reclaimable cache)     : cur %5.1f%% | avg %5.1f%% | p95 %5.1f%% | peak %5.1f%% (min avail %.0f MiB)" % (
            L.get("mem_used_pct", 0), P["ram_pct"]["avg"], P["ram_pct"]["p95"], P["ram_pct"]["max"], P["ram_avail_min_mib"]))
        print("  Compressed Swap (zram / zswap)         : zram %.0f->%.0f MiB | zswap %.0f MiB" % (
            L.get("zram_orig_mib", 0), L.get("zram_compr_mib", 0), L.get("zswap_mib", 0)))
        print("  Disk Busy & Write Rate (Primary Drive) : busy avg %4.1f%% (peak %4.1f%%) | write avg %6.1f KiB/s (peak %6.1f KiB/s)" % (
            P["nvme_busy_pct"]["avg"], P["nvme_busy_pct"]["max"], P["nvme_write_kibs"]["avg"], P["nvme_write_kibs"]["max"]))
        print("  Package Power & Temperature       : %4.1f W (avg %4.1f W, peak %4.1f W) | %4.0f C (peak %4.0f C)" % (
            L.get("pkg_watts", 0), P["pkg_watts"]["avg"], P["pkg_watts"]["max"], L.get("cpu_temp_c", 0), P["cpu_temp_c"]["max"]))
        print("  Kernel PSI Stalls (CPU / Mem / IO) : CPU avg %.2f%% | Mem avg %.2f%% | IO avg %.2f%%" % (
            P["psi_cpu_some"]["avg"], P["psi_mem_some"]["avg"], P["psi_io_some"]["avg"]))
        print("-" * 78)
    print(" SCREEN TIME & WHOLE-SYSTEM LOAD WHILE APPLICATION IS FOCUSED")
    for a in data["apps"][:12]:
        if not a.get("hardware_available", True):
            print("  %-28s  %8s  | Hardware unavailable" % (a["app"][:28], fmt_dur(a["seconds"])))
            continue
        print("  %-28s  %8s  | CPU %5.1f%% (pot %4.1f%%) | GPU %4.1f%% | %4.1f W" % (
            a["app"][:28], fmt_dur(a["seconds"]), a["avg_cpu_pct"], a["avg_cpu_potential_pct"], a["avg_gpu_pct"], a["avg_watts"]))
    print("-" * 78)
    print(" SCREEN TIME BY BROWSER TAB / SITE (Firefox & Chrome)")
    if not data["browser_tabs"]:
        print("  (No active browser focus recorded in window yet)")
    for t in data["browser_tabs"][:12]:
        site_label = t["site"]
        aud_s = float(t.get("audible_seconds") or 0.0)
        if aud_s >= 30.0:
            site_label = "%s [audio %s]" % (site_label[:26], fmt_dur(aud_s))
        if not t.get("hardware_available", True):
            print("  %-42s  %8s  | Hardware unavailable" % (site_label[:42], fmt_dur(t["seconds"])))
            continue
        print("  %-42s  %8s  | CPU %4.1f%% | GPU %4.1f%% | %4.1f W" % (
            site_label[:42], fmt_dur(t["seconds"]), t["avg_cpu_pct"], t["avg_gpu_pct"], t["avg_watts"]))
    print("-" * 78)
    print(" Dashboards: http://127.0.0.1:5601 (Hardware Potential + Screen Time) | http://127.0.0.1:5600 (ActivityWatch)")
    print("=" * 78)

if __name__ == "__main__":
    main()
