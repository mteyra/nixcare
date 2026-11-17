{ config, pkgs, lib, ... }:

let
  cfg = config.services.nixcare;
  intersectorCode = builtins.readFile ./activity_intersector.py;
  trackingSupportCode = builtins.readFile ./tracking_support.py;

  awatcherStart = pkgs.writeShellScript "awatcher-hyprland-start" ''
    set -u
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    for _ in $(seq 1 30); do
      wd=$(ls "$XDG_RUNTIME_DIR"/wayland-* 2>/dev/null | grep -v '\.lock$' | head -n1 || true)
      hs=$(ls -d "$XDG_RUNTIME_DIR"/hypr/* 2>/dev/null | head -n1 || true)
      [ -n "$wd" ] && [ -n "$hs" ] && break
      sleep 1
    done
    [ -n "$wd" ] && export WAYLAND_DISPLAY="$(basename "$wd")"
    [ -n "$hs" ] && export HYPRLAND_INSTANCE_SIGNATURE="$(basename "$hs")"
    for _ in $(seq 1 20); do
      ${pkgs.curl}/bin/curl -fsS http://127.0.0.1:5600/api/0/info >/dev/null 2>&1 && break
      sleep 0.5
    done
    exec ${pkgs.awatcher}/bin/awatcher --idle-timeout 180
  '';

  collectorPy = pkgs.writeText "laptop-telemetry-daemon.py" ''
    #!/usr/bin/env python3
    import glob
    import http.server
    import json
    import os
    import pwd
    import socket
    import sqlite3
    import stat
    import struct
    import subprocess
    import threading
    import time
    import urllib.parse
    import urllib.request
    from datetime import datetime, timezone

    ${intersectorCode}
    ${trackingSupportCode}

    NVME_PERCENT_USED = None

    def detect_cpu_turbos():
        max_khz = 0
        for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/cpuinfo_max_freq"):
            try:
                with open(p) as f:
                    khz = int(f.read().strip())
                    if khz > max_khz:
                        max_khz = khz
            except Exception:
                pass
        if max_khz > 0:
            single = max_khz / 1000.0
            allcore = single
        else:
            single, allcore = 0.0, 0.0
        return single, allcore

    def find_primary_disk():
        for d in ("nvme0n1", "sda", "vda", "nvme1n1", "mmcblk0"):
            if os.path.exists(f"/sys/block/{d}/stat"):
                return d
        for p in glob.glob("/sys/block/*"):
            b = os.path.basename(p)
            if not b.startswith(("loop", "ram", "zram")):
                return b
        return "nvme0n1"

    PRIMARY_DISK = find_primary_disk()
    CPU_SINGLE_TURBO_MHZ, CPU_ALLCORE_TURBO_MHZ = detect_cpu_turbos()

    def refresh_nvme_smart():
        global NVME_PERCENT_USED
        nvme_ctrl = "/dev/nvme0"
        if not os.path.exists(nvme_ctrl):
            ctrls = glob.glob("/dev/nvme[0-9]*")
            if ctrls:
                nvme_ctrl = ctrls[0]
            else:
                return
        try:
            r = subprocess.run(
                ["${pkgs.nvme-cli}/bin/nvme", "smart-log", nvme_ctrl, "-o", "json"],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0 and r.stdout:
                NVME_PERCENT_USED = json.loads(r.stdout).get("percent_used")
        except Exception:
            pass

    DB_DIR = "/var/lib/laptop-telemetry"
    DB_PATH = "/var/lib/laptop-telemetry/telemetry.db"
    OWNER_USER = "${cfg.user}"
    RETENTION_DAYS = ${toString cfg.track.retentionDays}
    ACTIVITY_ENABLED = ${if cfg.track.activity.enable then "True" else "False"}
    PORT = 5601
    SAMPLE_INTERVAL = 5.0

    SCHEMA = """
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;
    CREATE TABLE IF NOT EXISTS samples (
      ts INTEGER PRIMARY KEY,
      dt REAL,
      cpu_busy_pct REAL,
      cpu_max_thread_pct REAL,
      cpu_avg_mhz REAL,
      cpu_max_mhz REAL,
      cpu_potential_pct REAL,
      load1 REAL,
      psi_cpu_some REAL,
      throttle_thermal INTEGER,
      throttle_prochot INTEGER,
      throttle_pwr INTEGER,
      top_cpu_comm TEXT,
      top_cpu_pct REAL,
      gpu_render_pct REAL,
      gpu_video_pct REAL,
      gpu_copy_pct REAL,
      gpu_freq_mhz REAL,
      top_gpu_comm TEXT,
      top_gpu_pct REAL,
      mem_used_pct REAL,
      mem_used_mib REAL,
      mem_avail_mib REAL,
      mem_cached_mib REAL,
      mem_shmem_mib REAL,
      zram_orig_mib REAL,
      zram_compr_mib REAL,
      zswap_mib REAL,
      psi_mem_some REAL,
      psi_mem_full REAL,
      top_mem_comm TEXT,
      top_mem_mib REAL,
      disk_read_kibs REAL,
      disk_write_kibs REAL,
      disk_busy_pct REAL,
      psi_io_some REAL,
      psi_io_full REAL,
      top_io_comm TEXT,
      top_io_write_kibs REAL,
      pkg_watts REAL,
      core_watts REAL,
      uncore_watts REAL,
      dram_watts REAL,
      rapl_pl1_watts REAL,
      cpu_temp_c REAL,
      nvme_temp_c REAL,
      ac_online INTEGER,
      power_profile TEXT,
      epp TEXT,
      bat_pct REAL,
      bat_watts REAL,
      bat_health_pct REAL,
      afk INTEGER,
      active_app TEXT,
      active_title TEXT,
      browser_domain TEXT,
      browser_url TEXT,
      browser_tab_title TEXT,
      browser_audible INTEGER,
      browser_tab_count INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
    CREATE INDEX IF NOT EXISTS idx_samples_app ON samples(active_app);
    CREATE INDEX IF NOT EXISTS idx_samples_domain ON samples(browser_domain);
    """

    def enforce_private_permissions():
        # The privileged collector must not write user-replaceable SQLite paths.
        if not stat.S_ISDIR(os.lstat(DB_DIR).st_mode):
            raise RuntimeError("Telemetry state directory must not be a symlink")
        os.chown(DB_DIR, 0, 0)
        os.chmod(DB_DIR, 0o700)
        for f in glob.glob(DB_PATH + "*"):
            st = os.lstat(f)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise RuntimeError("Telemetry files must be regular files without hardlinks")
            os.chown(f, 0, 0, follow_symlinks=False)
            os.chmod(f, 0o600)

    def read_text(path, default=""):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read().strip()
        except Exception:
            return default

    def read_int(path, default=0):
        s = read_text(path, "")
        try:
            return int(s)
        except Exception:
            return default

    def read_float(path, default=0.0):
        s = read_text(path, "")
        try:
            return float(s)
        except Exception:
            return default

    def read_psi(resource):
        txt = read_text("/proc/pressure/" + resource)
        some = 0.0
        full = 0.0
        for line in txt.splitlines():
            parts = line.split()
            if not parts:
                continue
            kind = parts[0]
            for kv in parts[1:]:
                if kv.startswith("avg10="):
                    try:
                        val = float(kv.split("=", 1)[1])
                        if kind == "some":
                            some = val
                        elif kind == "full":
                            full = val
                    except Exception:
                        pass
        return some, full

    def read_msr(reg):
        try:
            fd = os.open("/dev/cpu/0/msr", os.O_RDONLY)
            try:
                data = os.pread(fd, 8, reg)
                return struct.unpack("<Q", data)[0]
            finally:
                os.close(fd)
        except Exception:
            return None

    def read_cpu_jiffies():
        out = {}
        try:
            with open("/proc/stat", "r") as f:
                for line in f:
                    if not line.startswith("cpu"):
                        continue
                    parts = line.split()
                    name = parts[0]
                    vals = [int(x) for x in parts[1:]]
                    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                    # guest/guest_nice are already included in user/nice.
                    total = sum(vals[:8])
                    busy = total - idle
                    out["total" if name == "cpu" else name] = (busy, total)
        except Exception:
            pass
        return out

    def read_cpu_freqs():
        freqs = []
        for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
            khz = read_int(p, 0)
            if khz > 0:
                freqs.append(khz / 1000.0)
        if not freqs:
            return 0.0, 0.0
        return sum(freqs) / len(freqs), max(freqs)

    def read_rapl_uj():
        res = {}
        for d in glob.glob("/sys/class/powercap/*rapl*"):
            name = read_text(os.path.join(d, "name"))
            uj = read_int(os.path.join(d, "energy_uj"), -1)
            if name and uj >= 0:
                res[name] = uj
        return res

    def read_temps():
        cpu_t = 0.0
        disk_t = 0.0
        for tz in glob.glob("/sys/class/thermal/thermal_zone*"):
            ttype = read_text(os.path.join(tz, "type")).lower()
            temp = read_int(os.path.join(tz, "temp"), 0) / 1000.0
            if any(k in ttype for k in ("x86_pkg_temp", "k10temp", "coretemp", "cpu")):
                if temp > cpu_t:
                    cpu_t = temp
            elif any(k in ttype for k in ("ssd", "nvme", "drive")):
                disk_t = temp
        if cpu_t == 0.0:
            for hw in glob.glob("/sys/class/hwmon/hwmon*/temp*_input"):
                t = read_int(hw, 0) / 1000.0
                if 20.0 < t < 115.0 and t > cpu_t:
                    cpu_t = t
        return cpu_t, disk_t

    def read_mem():
        info = {}
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        parts = v.strip().split()
                        if parts:
                            info[k.strip()] = int(parts[0])
        except Exception:
            pass
        total_kib = info.get("MemTotal", 1)
        avail_kib = info.get("MemAvailable", total_kib)
        used_kib = max(0, total_kib - avail_kib)
        cached_kib = info.get("Cached", 0) + info.get("Buffers", 0) + info.get("SReclaimable", 0)
        shmem_kib = info.get("Shmem", 0)
        zswap_kib = info.get("Zswapped", 0)

        zram_orig_mib = 0.0
        zram_compr_mib = 0.0
        mm_stat = read_text("/sys/block/zram0/mm_stat")
        if mm_stat:
            parts = mm_stat.split()
            if len(parts) >= 2:
                try:
                    zram_orig_mib = int(parts[0]) / 1048576.0
                    zram_compr_mib = int(parts[1]) / 1048576.0
                except Exception:
                    pass
        return {
            "used_pct": (used_kib / total_kib) * 100.0,
            "used_mib": used_kib / 1024.0,
            "avail_mib": avail_kib / 1024.0,
            "cached_mib": cached_kib / 1024.0,
            "shmem_mib": shmem_kib / 1024.0,
            "zram_orig_mib": zram_orig_mib,
            "zram_compr_mib": zram_compr_mib,
            "zswap_mib": zswap_kib / 1024.0,
        }

    def read_primary_disk_stat():
        s = read_text(f"/sys/block/{PRIMARY_DISK}/stat")
        if not s:
            return None
        vals = [int(x) for x in s.split()]
        if len(vals) < 10:
            return None
        return {
            "rd_sectors": vals[2],
            "wr_sectors": vals[6],
            "io_ticks_ms": vals[9],
        }

    def scan_processes(proc_root="/proc"):
        clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        procs = {}
        drm_clients = {}
        for entry in os.scandir(proc_root):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            pdir = entry.path
            try:
                with open(pdir + "/stat", "r", encoding="utf-8", errors="replace") as f:
                    st = f.read()
                rparen = st.rfind(")")
                lparen = st.find("(")
                if lparen < 0 or rparen < 0:
                    continue
                comm = st[lparen + 1:rparen]
                rest = st[rparen + 2:].split()
                utime = int(rest[11])
                stime = int(rest[12])
                rss_pages = int(rest[21])
                start_ticks = int(rest[19])
                cpu_sec = (utime + stime) / float(clk_tck)
                rss_mib = (rss_pages * page_size) / 1048576.0
            except Exception:
                continue

            wr_bytes = 0
            try:
                with open(pdir + "/io", "r") as f:
                    for line in f:
                        if line.startswith("write_bytes:"):
                            wr_bytes = int(line.split()[1])
                            break
            except Exception:
                pass

            procs[pid] = {
                "comm": comm,
                "cpu_sec": cpu_sec,
                "rss_mib": rss_mib,
                "wr_bytes": wr_bytes,
                "start_ticks": start_ticks,
            }

            try:
                for fde in os.scandir(pdir + "/fd"):
                    try:
                        device = os.readlink(fde.path)
                        if not device.startswith("/dev/dri/"):
                            continue
                        # Browser sockets and regular files need no fdinfo read.
                        # DRM fields can extend beyond the first 512 bytes.
                        with open(pdir + "/fdinfo/" + fde.name, "r") as ff:
                            head = ff.read(4096)
                        if "drm-client-id:" not in head:
                            continue
                        cid = None
                        render_ns = 0
                        video_ns = 0
                        copy_ns = 0
                        for line in head.splitlines():
                            if line.startswith("drm-client-id:"):
                                cid = line.split(":", 1)[1].strip()
                            elif line.startswith("drm-pdev:"):
                                device = line.split(":", 1)[1].strip()
                            elif line.startswith("drm-engine-render:"):
                                render_ns = int(line.split(":", 1)[1].split()[0])
                            elif line.startswith("drm-engine-video:") or line.startswith("drm-engine-video-enhance:"):
                                video_ns += int(line.split(":", 1)[1].split()[0])
                            elif line.startswith("drm-engine-copy:"):
                                copy_ns = int(line.split(":", 1)[1].split()[0])
                        # Client IDs are scoped to a DRM device, not the host.
                        client_key = (device, cid)
                        if cid is not None and client_key not in drm_clients:
                            drm_clients[client_key] = {
                                "comm": comm,
                                "pid": pid,
                                "render_ns": render_ns,
                                "video_ns": video_ns,
                                "copy_ns": copy_ns,
                            }
                    except Exception:
                        continue
            except Exception:
                pass
        return procs, drm_clients

    def query_hyprland():
        if not ACTIVITY_ENABLED:
            return "", "", "", ""
        uid = pwd.getpwnam(OWNER_USER).pw_uid
        socks = glob.glob(f"/run/user/{uid}/hypr/*/.socket.sock")
        if not socks:
            return "", "", "", ""
        sock_path = socks[0]

        def ask(cmd):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(0.3)
                s.connect(sock_path)
                s.sendall(cmd.encode("utf-8"))
                chunks = []
                while True:
                    d = s.recv(8192)
                    if not d:
                        break
                    chunks.append(d)
                s.close()
                return json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
            except Exception:
                return None

        aw = ask("j/activewindow") or {}
        app = str(aw.get("class") or aw.get("initialClass") or "")
        title = str(aw.get("title") or "")

        fallback_domain = ""
        fallback_tab = ""
        if app.lower() in ("firefox", "navigator", "google-chrome", "chromium"):
            for suffix in (" — Mozilla Firefox", " - Mozilla Firefox", " - Google Chrome", " - Chromium"):
                if title.endswith(suffix):
                    fallback_tab = title[:-len(suffix)].strip()
                    break
        return app, title, fallback_domain, fallback_tab

    def query_activitywatch(active_app="", active_title=""):
        afk = 1
        domain = ""
        url = ""
        tab_title = ""
        audible = 0
        tab_count = 0
        if not ACTIVITY_ENABLED:
            return afk, domain, url, tab_title, audible, tab_count
        try:
            req = urllib.request.Request("http://127.0.0.1:5600/api/0/buckets/", headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=0.4) as resp:
                buckets = select_activity_buckets(read_json_bounded(resp), socket.gethostname())
        except Exception:
            return afk, domain, url, tab_title, audible, tab_count

        def recent_events(bucket_id, limit=1, max_age=30.0):
            u = "http://127.0.0.1:5600/api/0/buckets/" + urllib.parse.quote(bucket_id, safe="") + "/events?limit=" + str(limit)
            with urllib.request.urlopen(u, timeout=0.3) as response:
                events = read_json_bounded(response)
            if not isinstance(events, list):
                return []
            valid = []
            now_check = time.time()
            for event in events:
                if not isinstance(event, dict):
                    continue
                start = parse_iso(event.get("timestamp"))
                duration = float(event.get("duration") or 0)
                end = start + duration
                # Future, stale, malformed or absent evidence is not active time.
                if duration < 0 or not math.isfinite(start) or not math.isfinite(end) or not 0 <= now_check - end < max_age:
                    continue
                valid.append(event)
            return valid

        try:
            events = recent_events(buckets["afk"], limit=1, max_age=30.0) if "afk" in buckets else []
            if events and events[0].get("data", {}).get("status") == "not-afk":
                afk = 0
        except Exception:
            pass

        browser_family = next((name for name, apps in BROWSER_FAMILIES.items() if active_app.lower() in apps), None)
        try:
            events = recent_events(buckets[browser_family], limit=5, max_age=75.0) if browser_family in buckets else []
            if events:
                clean_win_title = strip_browser_suffix(active_title)
                chosen = events[0]
                if clean_win_title:
                    for ev in events:
                        if str(ev.get("data", {}).get("title") or "").strip() == clean_win_title:
                            chosen = ev
                            break
                data = chosen.get("data", {})
                url = str(data.get("url") or "")
                tab_title = str(data.get("title") or "")
                audible = 1 if data.get("audible") else 0
                tab_count = max(0, int(data.get("tabCount") or 0))
                domain = extract_site_domain(url)
                if audible == 1 and is_video_site(domain):
                    afk = 0
        except Exception:
            pass
        return afk, domain, url, tab_title, audible, tab_count

    def build_summary(conn, hours=24.0):
        # All queries must describe the same samples while the writer is active.
        own_transaction = not conn.in_transaction
        if own_transaction:
            conn.execute("BEGIN")
        try:
            return _build_summary(conn, hours)
        finally:
            if own_transaction:
                conn.rollback()

    def _build_summary(conn, hours=24.0):
        hours = validate_hours(hours, RETENTION_DAYS)
        now_ts = time.time()
        since = now_ts - hours * 3600.0
        window_duration = hours * 3600.0
        cur = conn.cursor()
        cur.execute("""
          SELECT
            COUNT(*),
            COALESCE(SUM(dt), 0),
            COALESCE(SUM(CASE WHEN afk=0 THEN dt ELSE 0 END), 0),
            AVG(cpu_busy_pct), MAX(cpu_busy_pct),
            AVG(cpu_max_thread_pct), MAX(cpu_max_thread_pct),
            AVG(cpu_avg_mhz), MAX(cpu_max_mhz),
            AVG(cpu_potential_pct), MAX(cpu_potential_pct),
            AVG(gpu_render_pct), MAX(gpu_render_pct),
            AVG(gpu_video_pct), MAX(gpu_video_pct),
            AVG(gpu_freq_mhz), MAX(gpu_freq_mhz),
            AVG(mem_used_pct), MAX(mem_used_pct),
            MAX(mem_used_mib), MIN(mem_avail_mib),
            MAX(zram_orig_mib), MAX(zram_compr_mib), MAX(zswap_mib),
            AVG(disk_read_kibs), MAX(disk_read_kibs),
            AVG(disk_write_kibs), MAX(disk_write_kibs),
            AVG(disk_busy_pct), MAX(disk_busy_pct),
            AVG(psi_cpu_some), MAX(psi_cpu_some),
            AVG(psi_mem_some), MAX(psi_mem_some),
            AVG(psi_io_some), MAX(psi_io_some),
            AVG(pkg_watts), MAX(pkg_watts),
            AVG(cpu_temp_c), MAX(cpu_temp_c),
            MAX(throttle_thermal), MAX(throttle_prochot), MAX(throttle_pwr),
            AVG(bat_health_pct)
          FROM samples WHERE ts > ? AND ts <= ?
        """, (since, now_ts))
        row = cur.fetchone()
        n = row[0] or 0
        cur.execute("""
          WITH intervals AS (
            SELECT ts, MAX(0, MIN(15, COALESCE(dt, 0))) AS duration,
                   LAG(ts, 1, ?) OVER (ORDER BY ts) AS previous_end
            FROM samples WHERE ts > ? AND ts <= ?
          )
          SELECT COALESCE(SUM(MAX(0, ts - MAX(?, previous_end, ts - duration))), 0)
          FROM intervals
        """, (since, since, now_ts, since))
        hardware_tracked_seconds = min(window_duration, cur.fetchone()[0])

        cur.execute("SELECT * FROM samples WHERE ts > ? AND ts <= ? ORDER BY ts DESC LIMIT 1", (since, now_ts))
        latest_row = cur.fetchone()
        cols = [d[0] for d in cur.description] if cur.description else []
        latest = dict(zip(cols, latest_row)) if latest_row else {}
        if latest and not ACTIVITY_ENABLED:
            for key in ("active_app", "active_title", "browser_domain", "browser_url", "browser_tab_title"):
                latest[key] = ""

        def p95(col):
            if n == 0:
                return 0.0
            # Nearest-rank percentile over available readings, ignoring NULL.
            cur.execute("SELECT COUNT(" + col + ") FROM samples WHERE ts > ? AND ts <= ?", (since, now_ts))
            count = cur.fetchone()[0]
            if not count:
                return 0.0
            offset = max(0, math.ceil(count * 0.95) - 1)
            cur.execute("SELECT " + col + " FROM samples WHERE ts > ? AND ts <= ? AND " + col + " IS NOT NULL ORDER BY " + col + " ASC LIMIT 1 OFFSET ?", (since, now_ts, offset))
            r = cur.fetchone()
            return float(r[0] or 0.0) if r else 0.0

        # Query hardware cost per app from samples
        app_stats = {}
        cur.execute("""
          SELECT
            CASE WHEN LENGTH(active_app) = 0 THEN "(unfocused/desktop)" ELSE active_app END AS app,
            AVG(cpu_busy_pct),
            AVG(cpu_potential_pct),
            AVG(gpu_render_pct + gpu_video_pct),
            AVG(pkg_watts),
            AVG(disk_write_kibs)
          FROM samples
          WHERE ts > ? AND ts <= ? AND (afk = 0 OR browser_audible = 1)
          GROUP BY app
        """, (since, now_ts))
        for r in cur:
            app_stats[r[0]] = {
                "avg_cpu_pct": round(r[1] or 0.0, 1),
                "avg_cpu_potential_pct": round(r[2] or 0.0, 1),
                "avg_gpu_pct": round(r[3] or 0.0, 1),
                "avg_watts": round(r[4] or 0.0, 2),
                "avg_write_kibs": round(r[5] or 0.0, 1),
            }

        # Query hardware cost per browser site from samples (ONLY when active_app was actually a browser)
        site_stats = {}
        cur.execute("""
          SELECT
            CASE
              WHEN LENGTH(browser_domain) > 0 THEN browser_domain
              WHEN LENGTH(browser_tab_title) > 0 THEN browser_tab_title
              ELSE active_title
            END AS site,
            AVG(cpu_busy_pct),
            AVG(gpu_render_pct + gpu_video_pct),
            AVG(pkg_watts)
          FROM samples
          WHERE ts > ? AND ts <= ? AND (afk = 0 OR browser_audible = 1) AND LOWER(active_app) IN ("firefox", "navigator", "google-chrome", "chromium", "chrome", "brave")
          GROUP BY site
        """, (since, now_ts))
        for r in cur:
            site_stats[r[0]] = {
                "avg_cpu_pct": round(r[1] or 0.0, 1),
                "avg_gpu_pct": round(r[2] or 0.0, 1),
                "avg_watts": round(r[3] or 0.0, 2),
            }

        # Fallback hardware stats keyed by stripped window title (for multi-window tabs & hands-off video sessions)
        title_stats = {}
        cur.execute("""
          SELECT
            active_title,
            AVG(cpu_busy_pct),
            AVG(gpu_render_pct + gpu_video_pct),
            AVG(pkg_watts)
          FROM samples
          WHERE ts > ? AND ts <= ? AND LENGTH(active_title) > 0 AND LOWER(active_app) IN ("firefox", "navigator", "google-chrome", "chromium", "chrome", "brave")
          GROUP BY active_title
        """, (since, now_ts))
        for r in cur:
            clean_t = strip_browser_suffix(r[0])
            if clean_t and clean_t not in title_stats:
                title_stats[clean_t] = {
                    "avg_cpu_pct": round(r[1] or 0.0, 1),
                    "avg_gpu_pct": round(r[2] or 0.0, 1),
                    "avg_watts": round(r[3] or 0.0, 2),
                }

        # Authoritative ActivityWatch clipping and interval intersection
        start_iso = datetime.fromtimestamp(since, timezone.utc).isoformat()
        end_iso = datetime.fromtimestamp(now_ts, timezone.utc).isoformat()

        aw_summary = None
        activity_warning = None
        aw_deadline = time.monotonic() + 5.0
        def read_activity(url):
            remaining = aw_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ActivityWatch query budget exceeded")
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=min(1.5, remaining)) as response:
                return read_json_bounded(response)
        try:
            if not ACTIVITY_ENABLED:
                raise RuntimeError("Activity tracking disabled")
            buckets = select_activity_buckets(
                read_activity("http://127.0.0.1:5600/api/0/buckets/"), socket.gethostname())
            afk_bucket = buckets.get("afk")
            window_bucket = buckets.get("window")

            def fetch_events(bid):
                base = "http://127.0.0.1:5600/api/0/buckets/" + urllib.parse.quote(bid, safe="") + "/events?"
                u = base + urllib.parse.urlencode({"start": start_iso, "end": end_iso, "limit": 20001})
                events = read_activity(u)
                if not isinstance(events, list) or len(events) > 20000:
                    raise ValueError("Activity history exceeds 20000 events; request fewer hours")
                # Include a spanning event even when the API filters by start time.
                previous = base + urllib.parse.urlencode({"end": start_iso, "limit": 1})
                boundary = read_activity(previous)
                if not isinstance(boundary, list):
                    raise ValueError("Invalid ActivityWatch boundary response")
                return boundary[:1] + events

            afk_events = fetch_events(afk_bucket) if afk_bucket else []
            window_events = fetch_events(window_bucket) if window_bucket else []
            web_by_browser = {}
            for browser in BROWSER_FAMILIES:
                if browser in buckets:
                    web_by_browser[browser] = fetch_events(buckets[browser])

            aw_summary = compute_screen_time_summary(
                afk_events=afk_events,
                window_events=window_events,
                web_events_by_browser=web_by_browser,
                window_start=since,
                window_end=now_ts,
            )
            if not aw_summary.get("active_measurement_available", True):
                raise ValueError("No idle/active evidence in the requested window")
        except Exception as error:
            aw_summary = None
            if ACTIVITY_ENABLED:
                activity_warning = "ActivityWatch unavailable or incomplete; using sampled activity: " + str(error)

        activity_source = "disabled" if not ACTIVITY_ENABLED else "telemetry-samples"
        if aw_summary and (aw_summary.get("total_tracked_seconds", 0) > 0 or aw_summary.get("active_screen_seconds", 0) > 0):
            activity_source = "activitywatch"
            total_tracked_seconds = aw_summary["total_tracked_seconds"]
            active_screen_seconds = aw_summary["active_screen_seconds"]
            breakdown = {
                "input_active_seconds": aw_summary.get("input_active_seconds", active_screen_seconds),
                "media_active_seconds": aw_summary.get("media_active_seconds", 0.0),
                "daytime_idle_seconds": aw_summary.get("daytime_idle_seconds", 0.0),
                "overnight_sleep_seconds": aw_summary.get("overnight_sleep_seconds", 0.0),
                "suspend_seconds": aw_summary.get("suspend_seconds", max(0.0, window_duration - total_tracked_seconds)),
                "waking_seconds": aw_summary.get("waking_seconds", active_screen_seconds),
                "waking_active_pct": aw_summary.get("waking_active_pct", 0.0),
                "sleep_blocks": aw_summary.get("sleep_blocks", []),
            }

            apps = []
            for a in aw_summary["apps"]:
                st = app_stats.get(a["app"], {})
                apps.append({
                    "app": a["app"],
                    "seconds": a["seconds"],
                    "hardware_available": bool(st),
                    "avg_cpu_pct": st.get("avg_cpu_pct", 0.0),
                    "avg_cpu_potential_pct": st.get("avg_cpu_potential_pct", 0.0),
                    "avg_gpu_pct": st.get("avg_gpu_pct", 0.0),
                    "avg_watts": st.get("avg_watts", 0.0),
                    "avg_write_kibs": st.get("avg_write_kibs", 0.0),
                })

            tabs = []
            for t in aw_summary["browser_tabs"]:
                st = site_stats.get(t["site"]) or title_stats.get(t.get("title", ""), {})
                tabs.append({
                    "site": t["site"],
                    "title": t.get("title", ""),
                    "seconds": t["seconds"],
                    "hardware_available": bool(st),
                    "avg_cpu_pct": st.get("avg_cpu_pct", 0.0),
                    "avg_gpu_pct": st.get("avg_gpu_pct", 0.0),
                    "avg_watts": st.get("avg_watts", 0.0),
                    "audible_seconds": t.get("audible_seconds", 0.0),
                })
        else:
            # Fallback path if ActivityWatch is unreachable: query SQLite with strict clipping and invariants
            cur.execute("""
              SELECT
                ts, dt, afk, active_app, browser_domain, browser_tab_title, browser_audible
              FROM samples
              WHERE ts > ? AND ts <= ?
              ORDER BY ts ASC
            """, (since, now_ts))

            total_tracked_raw = 0.0
            active_screen_raw = 0.0
            input_active_raw = 0.0
            media_active_raw = 0.0
            app_durations = {}
            site_durations = {}
            site_audible_durs = {}
            site_titles = {}

            previous_end = float(since)
            for r in cur:
                r_ts, r_dt, r_afk, r_app, r_domain, r_title, r_audible = r
                # Clock jumps, repeated samples and suspend must not double-count.
                end = min(now_ts, float(r_ts))
                start = max(previous_end, float(r_ts) - min(15.0, max(0.0, float(r_dt or 0.0))))
                effective_dt = max(0.0, end - start)
                previous_end = max(previous_end, end)
                if effective_dt <= 0:
                    continue
                total_tracked_raw += effective_dt
                is_media_active = is_browser_app(r_app) and r_audible == 1 and is_video_site(r_domain or r_title)
                if ACTIVITY_ENABLED and (r_afk == 0 or is_media_active):
                    active_screen_raw += effective_dt
                    if r_afk == 0:
                        input_active_raw += effective_dt
                    else:
                        media_active_raw += effective_dt
                    app_name = r_app or "(unfocused/desktop)"
                    app_durations[app_name] = app_durations.get(app_name, 0.0) + effective_dt

                    if is_browser_app(r_app):
                        site = r_domain or r_title or "(browser)"
                        site_durations[site] = site_durations.get(site, 0.0) + effective_dt
                        site_titles[site] = r_title or ""
                        if r_audible == 1:
                            site_audible_durs[site] = site_audible_durs.get(site, 0.0) + effective_dt

            total_tracked_seconds = min(window_duration, total_tracked_raw)
            active_screen_seconds = min(total_tracked_seconds, active_screen_raw)
            input_active_sec = min(active_screen_seconds, input_active_raw)
            media_active_sec = max(0.0, active_screen_seconds - input_active_sec)
            idle_total = max(0.0, total_tracked_seconds - active_screen_seconds)
            suspend_sec = max(0.0, window_duration - total_tracked_seconds)
            waking_sec = max(active_screen_seconds, window_duration - suspend_sec)
            breakdown = {
                "input_active_seconds": duration_seconds(input_active_sec),
                "media_active_seconds": duration_seconds(media_active_sec),
                "daytime_idle_seconds": duration_seconds(idle_total),
                "overnight_sleep_seconds": 0.0,
                "suspend_seconds": duration_seconds(suspend_sec),
                "waking_seconds": duration_seconds(waking_sec),
                "waking_active_pct": round((active_screen_seconds / waking_sec) * 100.0, 1) if waking_sec > 0 else 0.0,
                "sleep_blocks": [],
            }

            apps = []
            for app, sec in sorted(app_durations.items(), key=lambda x: x[1], reverse=True)[:20]:
                st = app_stats.get(app, {})
                apps.append({
                    "app": app,
                    "seconds": duration_seconds(sec),
                    "hardware_available": bool(st),
                    "avg_cpu_pct": st.get("avg_cpu_pct", 0.0),
                    "avg_cpu_potential_pct": st.get("avg_cpu_potential_pct", 0.0),
                    "avg_gpu_pct": st.get("avg_gpu_pct", 0.0),
                    "avg_watts": st.get("avg_watts", 0.0),
                    "avg_write_kibs": st.get("avg_write_kibs", 0.0),
                })

            tabs = []
            browser_focus_sec = sum(sec for app, sec in app_durations.items() if is_browser_app(app))
            sum_sites_raw = sum(site_durations.values())
            scale = (browser_focus_sec / sum_sites_raw) if sum_sites_raw > browser_focus_sec > 0 else 1.0

            for site, sec in sorted(site_durations.items(), key=lambda x: x[1], reverse=True)[:25]:
                st = site_stats.get(site) or title_stats.get(site_titles.get(site, ""), {})
                adj_sec = duration_seconds(sec * scale)
                tabs.append({
                    "site": site,
                    "title": site_titles.get(site, ""),
                    "seconds": adj_sec,
                    "hardware_available": bool(st),
                    "avg_cpu_pct": st.get("avg_cpu_pct", 0.0),
                    "avg_gpu_pct": st.get("avg_gpu_pct", 0.0),
                    "avg_watts": st.get("avg_watts", 0.0),
                    "audible_seconds": duration_seconds(site_audible_durs.get(site, 0.0) * scale),
                })

        # Top processes
        cur.execute("""
          SELECT top_cpu_comm, COUNT(*), AVG(top_cpu_pct), MAX(top_cpu_pct)
          FROM samples WHERE ts > ? AND ts <= ? AND LENGTH(top_cpu_comm) > 0
          GROUP BY top_cpu_comm ORDER BY SUM(top_cpu_pct) DESC LIMIT 10
        """, (since, now_ts))
        top_cpu_procs = [
            {"comm": r[0], "samples": r[1], "avg_pct": round(r[2] or 0, 1), "max_pct": round(r[3] or 0, 1)}
          for r in cur.fetchall()
        ]

        cur.execute("""
          SELECT top_io_comm, COUNT(*), AVG(top_io_write_kibs), MAX(top_io_write_kibs)
          FROM samples WHERE ts > ? AND ts <= ? AND LENGTH(top_io_comm) > 0 AND top_io_write_kibs > 0
          GROUP BY top_io_comm ORDER BY SUM(top_io_write_kibs) DESC LIMIT 10
        """, (since, now_ts))
        top_io_procs = [
            {"comm": r[0], "samples": r[1], "avg_kibs": round(r[2] or 0, 1), "max_kibs": round(r[3] or 0, 1)}
            for r in cur.fetchall()
        ]

        result = {
            "window_hours": hours,
            "window_start": since,
            "window_end": now_ts,
            "hardware_tracked_seconds": hardware_tracked_seconds,
            "hardware_coverage_pct": round(100 * hardware_tracked_seconds / window_duration, 2),
            "activity_enabled": ACTIVITY_ENABLED,
            "activity_source": activity_source,
            "activity_warning": activity_warning,
            "aggregation": "Hardware averages and nearest-rank p95 use samples in the requested window.",
            "compute_metric": "Frequency-weighted CPU busy estimate; not benchmarked compute capacity.",
            "sample_count": n,
            "total_tracked_seconds": total_tracked_seconds,
            "active_screen_seconds": active_screen_seconds,
            "breakdown": breakdown,
            "latest": latest,
            "potential": {
                "cpu_busy": {"avg": round(row[3] or 0, 1), "p95": round(p95("cpu_busy_pct"), 1), "max": round(row[4] or 0, 1)},
                "cpu_single_thread": {"avg": round(row[5] or 0, 1), "p95": round(p95("cpu_max_thread_pct"), 1), "max": round(row[6] or 0, 1)},
                "cpu_freq_mhz": {"avg": round(row[7] or 0, 0), "max": round(row[8] or 0, 0), "ceiling": CPU_SINGLE_TURBO_MHZ or None},
                "cpu_true_potential": {"avg": round(row[9] or 0, 1), "p95": round(p95("cpu_potential_pct"), 1), "max": round(row[10] or 0, 1)},
                "gpu_render": {"avg": round(row[11] or 0, 1), "p95": round(p95("gpu_render_pct"), 1), "max": round(row[12] or 0, 1)},
                "gpu_video_decode": {"avg": round(row[13] or 0, 1), "p95": round(p95("gpu_video_pct"), 1), "max": round(row[14] or 0, 1)},
                "gpu_freq_mhz": {"avg": round(row[15] or 0, 0), "max": round(row[16] or 0, 0)},
                "ram_pct": {"avg": round(row[17] or 0, 1), "p95": round(p95("mem_used_pct"), 1), "max": round(row[18] or 0, 1)},
                "ram_used_max_mib": round(row[19] or 0, 0),
                "ram_avail_min_mib": round(row[20] or 0, 0),
                "zram_orig_max_mib": round(row[21] or 0, 1),
                "zram_compr_max_mib": round(row[22] or 0, 1),
                "zswap_max_mib": round(row[23] or 0, 1),
                "nvme_read_kibs": {"avg": round(row[24] or 0, 1), "max": round(row[25] or 0, 1)},
                "nvme_write_kibs": {"avg": round(row[26] or 0, 1), "max": round(row[27] or 0, 1)},
                "nvme_busy_pct": {"avg": round(row[28] or 0, 1), "p95": round(p95("disk_busy_pct"), 1), "max": round(row[29] or 0, 1)},
                "psi_cpu_some": {"avg": round(row[30] or 0, 2), "max": round(row[31] or 0, 2)},
                "psi_mem_some": {"avg": round(row[32] or 0, 2), "max": round(row[33] or 0, 2)},
                "psi_io_some": {"avg": round(row[34] or 0, 2), "max": round(row[35] or 0, 2)},
                "pkg_watts": {"avg": round(row[36] or 0, 2), "p95": round(p95("pkg_watts"), 2), "max": round(row[37] or 0, 2)},
                "cpu_temp_c": {"avg": round(row[38] or 0, 1), "max": round(row[39] or 0, 1)},
                "throttled_ever": {"thermal": bool(row[40]), "prochot": bool(row[41]), "power_limit": bool(row[42])},
                "battery_health_pct": round(row[43] or 0, 1),
                "nvme_percent_used": NVME_PERCENT_USED,
            },
            "apps": apps,
            "browser_tabs": tabs,
            "top_cpu_processes": top_cpu_procs,
            "top_io_processes": top_io_procs,
        }
        if not n:
            # Empty windows have no measurements, not measured zero load.
            for key, value in result["potential"].items():
                result["potential"][key] = {field: None for field in value} if isinstance(value, dict) else None
        return result

    DASHBOARD_HTML = """<!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>nixcare — Hardware Potential & Screen Time</title>
      <style>
        :root {
          --bg: #0f172a; --card: #1e293b; --border: #334155;
          --text: #f8fafc; --muted: #94a3b8; --accent: #38bdf8;
        }
        * { box-sizing: border-box; }
        body { margin: 0; padding: 24px; font-family: Inter, system-ui, sans-serif; background: var(--bg); color: var(--text); }
        h1 { margin: 0 0 6px 0; font-size: 22px; color: var(--accent); }
        .sub { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 20px; }
        .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
        .card h2 { margin: 0 0 12px 0; font-size: 15px; color: var(--accent); text-transform: uppercase; letter-spacing: 0.04em; }
        .metric { margin-bottom: 12px; }
        .metric-hdr { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px; }
        .bar-bg { height: 8px; background: #0b1120; border-radius: 4px; overflow: hidden; }
        .bar-fill { height: 100%; background: linear-gradient(90deg, #38bdf8, #818cf8); border-radius: 4px; transition: width 0.3s; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--border); }
        th { color: var(--muted); font-weight: 600; }
        a { color: var(--accent); text-decoration: none; }
      </style>
    </head>
    <body>
      <h1>nixcare — Hardware Potential & Screen Time</h1>
      <div class="sub" id="status" role="status">Loading local telemetry...</div>
      <div class="sub">CPU capacity is a frequency-weighted estimate. Application rows show whole-system load while an app is focused, not energy attributable to that app. Missing sensor support may appear as zero. Reports are available to local users of this computer.</div>
      <div class="grid" id="gauges"></div>
      <div class="grid">
        <div class="card">
          <h2>Screen Time & System Load During Focus</h2>
          <table id="apps-table"><thead><tr><th>App</th><th>Time</th><th>CPU %</th><th>GPU %</th><th>Avg W</th></tr></thead><tbody></tbody></table>
        </div>
        <div class="card">
          <h2>Browser Tabs / Sites (Firefox & Chrome)</h2>
          <table id="tabs-table"><thead><tr><th>Site / Tab</th><th>Time</th><th>CPU %</th><th>GPU %</th><th>Audio</th></tr></thead><tbody></tbody></table>
        </div>
      </div>
      <div class="grid">
        <div class="card">
          <h2>Top CPU Processes</h2>
          <table id="cpu-table"><thead><tr><th>Process</th><th>Avg CPU %</th><th>Peak CPU %</th></tr></thead><tbody></tbody></table>
        </div>
        <div class="card">
          <h2>Top Disk Writers</h2>
          <table id="io-table"><thead><tr><th>Process</th><th>Avg Write</th><th>Peak Write</th></tr></thead><tbody></tbody></table>
      </div>
      </div>
      <script>
        function escapeHtml(value) {
          return String(value == null ? "" : value).replace(/[&<>"']/g, function(c) {
            return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
          });
        }
        function fmtDur(s) {
          if (s < 60) return Math.round(s) + "s";
          if (s < 3600) return Math.floor(s/60) + "m " + Math.round(s%60) + "s";
          return Math.floor(s/3600) + "h " + Math.floor((s%3600)/60) + "m";
        }
        function bar(label, cur, avg, max, unit) {
          var u = unit || "%";
          var pct = Math.min(100, Math.max(0, cur));
          return '<div class="metric">' +
            '<div class="metric-hdr"><span>' + label + '</span><span><b>' + cur + u + '</b> (avg ' + avg + u + ', peak ' + max + u + ')</span></div>' +
            '<div class="bar-bg"><div class="bar-fill" style="width:' + pct + '%"></div></div>' +
          '</div>';
        }
        async function refresh() {
          var r = await fetch("/api/summary?hours=24");
          if (!r.ok) throw new Error("Telemetry returned HTTP " + r.status);
          var d = await r.json();
          var L = d.latest || {};
          var P = d.potential;
          document.getElementById("status").innerHTML =
            "Samples: <b>" + d.sample_count + "</b> | Active screen time: <b>" + fmtDur(d.active_screen_seconds) + "</b> | " +
            "Hardware coverage: <b>" + escapeHtml(d.hardware_coverage_pct) + "%</b> | Activity: " + escapeHtml(d.activity_source) + " | " +
            "Profile: <b>" + escapeHtml(L.power_profile || "?") + "</b> (" + escapeHtml(L.epp || "?") + ") | Focused: <b>" + escapeHtml(L.active_app || "none") + "</b> | " +
            (d.activity_enabled ? 'ActivityWatch: <a href="http://127.0.0.1:5600" target="_blank" rel="noreferrer">127.0.0.1:5600</a>' : "Activity tracking disabled") +
            (d.activity_warning ? "<br>" + escapeHtml(d.activity_warning) : "");
          document.getElementById("gauges").innerHTML = !d.sample_count ? '<p>No hardware samples in this window.</p>' :
            '<div class="card">' +
              '<h2>CPU Utilization</h2>' +
              bar("Frequency-weighted busy estimate", (L.cpu_potential_pct||0).toFixed(1), P.cpu_true_potential.avg, P.cpu_true_potential.max) +
              bar("Thread Busy (all threads)", (L.cpu_busy_pct||0).toFixed(1), P.cpu_busy.avg, P.cpu_busy.max) +
              bar("Busiest Single Thread (single-core ceiling)", (L.cpu_max_thread_pct||0).toFixed(1), P.cpu_single_thread.avg, P.cpu_single_thread.max) +
              '<div class="sub">Clock: <b>' + Math.round(L.cpu_avg_mhz||0) + ' MHz</b> (peak ' + P.cpu_freq_mhz.max + ' MHz) | Top: <b>' + escapeHtml(L.top_cpu_comm||"-") + '</b></div>' +
            '</div>' +
            '<div class="card">' +
              '<h2>GPU Engines (when supported)</h2>' +
              bar("3D / Render Engine (Compositor / WebGL)", (L.gpu_render_pct||0).toFixed(1), P.gpu_render.avg, P.gpu_render.max) +
              bar("Hardware Video Decode (VA-API QuickSync)", (L.gpu_video_pct||0).toFixed(1), P.gpu_video_decode.avg, P.gpu_video_decode.max) +
              '<div class="sub">GPU Clock: <b>' + Math.round(L.gpu_freq_mhz||0) + ' MHz</b> | Top GPU: <b>' + escapeHtml(L.top_gpu_comm||"-") + '</b></div>' +
            '</div>' +
            '<div class="card">' +
              '<h2>RAM & Compressed Swap</h2>' +
              bar("RAM Used (excl. reclaimable cache)", (L.mem_used_pct||0).toFixed(1), P.ram_pct.avg, P.ram_pct.max) +
              '<div class="sub">Used: <b>' + Math.round(L.mem_used_mib||0) + ' MiB</b> | Avail: <b>' + Math.round(L.mem_avail_mib||0) + ' MiB</b> | zram: <b>' + (L.zram_orig_mib||0).toFixed(0) + "→" + (L.zram_compr_mib||0).toFixed(0) + ' MiB</b> | zswap: <b>' + (L.zswap_mib||0).toFixed(0) + ' MiB</b></div>' +
            '</div>' +
            '<div class="card">' +
              '<h2>Power, Thermal & NVMe I/O</h2>' +
              bar("NVMe Busy %", (L.disk_busy_pct||0).toFixed(1), P.nvme_busy_pct.avg, P.nvme_busy_pct.max) +
              '<div class="sub">CPU Power: <b>' + (L.pkg_watts||0).toFixed(1) + ' W</b> (avg ' + P.pkg_watts.avg + ' W, peak ' + P.pkg_watts.max + ' W) | Temp: <b>' + (L.cpu_temp_c||0).toFixed(0) + '°C</b> (max ' + P.cpu_temp_c.max + '°C) | Battery Health: <b>' + P.battery_health_pct + '%</b></div>' +
            '</div>';
          document.querySelector("#apps-table tbody").innerHTML = d.apps.map(function(a) {
            if (a.hardware_available === false) return "<tr><td>" + escapeHtml(a.app) + "</td><td>" + fmtDur(a.seconds) + '</td><td colspan="3">Hardware unavailable</td></tr>';
            return "<tr><td><b>" + escapeHtml(a.app) + "</b></td><td>" + fmtDur(a.seconds) + "</td><td>" + a.avg_cpu_pct + "%</td><td>" + a.avg_gpu_pct + "%</td><td>" + a.avg_watts + "W</td></tr>";
          }).join("");
          document.querySelector("#tabs-table tbody").innerHTML = d.browser_tabs.map(function(t) {
            if (t.hardware_available === false) return "<tr><td>" + escapeHtml(t.site) + "</td><td>" + fmtDur(t.seconds) + '</td><td colspan="3">Hardware unavailable</td></tr>';
            return '<tr><td title="' + escapeHtml(t.title) + '"><b>' + escapeHtml(t.site.slice(0,48)) + "</b></td><td>" + fmtDur(t.seconds) + "</td><td>" + t.avg_cpu_pct + "%</td><td>" + t.avg_gpu_pct + "%</td><td>" + fmtDur(t.audible_seconds) + "</td></tr>";
          }).join("");
          document.querySelector("#cpu-table tbody").innerHTML = d.top_cpu_processes.map(function(p) {
            return "<tr><td><b>" + escapeHtml(p.comm) + "</b></td><td>" + p.avg_pct + "%</td><td>" + p.max_pct + "%</td></tr>";
          }).join("");
          document.querySelector("#io-table tbody").innerHTML = d.top_io_processes.map(function(p) {
            return "<tr><td><b>" + escapeHtml(p.comm) + "</b></td><td>" + p.avg_kibs + " KiB/s</td><td>" + p.max_kibs + " KiB/s</td></tr>";
          }).join("");
        }
        async function tick() {
          try { await refresh(); }
          catch (error) { document.getElementById("status").textContent = "Telemetry unavailable: " + error.message; }
          setTimeout(tick, 15000);
        }
        tick();
      </script>
    </body>
    </html>
    """

    SUMMARY_CACHE = SummaryCache()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            super().end_headers()

        def do_GET(self):
            if not local_request_allowed(self.headers, PORT):
                self.send_error(403, "Only same-origin loopback requests are permitted")
                return
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/summary":
                qs = urllib.parse.parse_qs(parsed.query)
                try:
                    hours = validate_hours(qs.get("hours", ["24"])[0], RETENTION_DAYS)
                except ValueError as error:
                    self.send_error(400, str(error))
                    return
                def read_summary():
                    conn = sqlite3.connect("file:" + DB_PATH + "?mode=ro", uri=True, timeout=2)
                    deadline = time.monotonic() + 20
                    conn.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
                    try:
                        return build_summary(conn, hours)
                    finally:
                        conn.close()
                try:
                    data = SUMMARY_CACHE.get(hours, read_summary)
                except (sqlite3.Error, OSError):
                    self.send_error(503, "Telemetry database is temporarily unavailable")
                    return
                body = json.dumps(data, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/":
                body = DASHBOARD_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404, "Not found")

    class TelemetryServer(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, *args, **kwargs):
            self.request_slots = threading.BoundedSemaphore(4)
            super().__init__(*args, **kwargs)

        def process_request(self, request, client_address):
            if not self.request_slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            request.settimeout(5)
            try:
                super().process_request(request, client_address)
            except Exception:
                self.request_slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.request_slots.release()

    def serve_http():
        srv = TelemetryServer(("127.0.0.1", PORT), Handler)
        srv.serve_forever()

    def main():
        os.makedirs(DB_DIR, mode=0o700, exist_ok=True)
        enforce_private_permissions()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript(SCHEMA)
        conn.commit()
        enforce_private_permissions()

        t = threading.Thread(target=serve_http, daemon=True)
        t.start()

        prev_ts = time.time()
        last_prune_ts = 0.0
        prev_jiff = read_cpu_jiffies()
        prev_rapl = read_rapl_uj()
        prev_nvme = read_primary_disk_stat()
        prev_procs, prev_drm = scan_processes()

        while True:
            time.sleep(SAMPLE_INTERVAL)
            now = time.time()
            dt = sample_duration(now - prev_ts, SAMPLE_INTERVAL)

            # Daily retention prune + permission enforcement
            if now - last_prune_ts >= 3600.0:
                cutoff = int(now - RETENTION_DAYS * 86400)
                conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
                conn.commit()
                enforce_private_permissions()
                refresh_nvme_smart()
                last_prune_ts = now

            cur_jiff = read_cpu_jiffies()
            cur_rapl = read_rapl_uj()
            cur_nvme = read_primary_disk_stat()
            cur_procs, cur_drm = scan_processes()
            if dt is None:
                # Counter deltas spanning suspend or a clock adjustment are not
                # a 15-second burst of work. Establish a fresh baseline instead.
                prev_ts, prev_jiff, prev_rapl = now, cur_jiff, cur_rapl
                prev_nvme, prev_procs, prev_drm = cur_nvme, cur_procs, cur_drm
                continue

            cpu_busy_pct = 0.0
            if "total" in cur_jiff and "total" in prev_jiff:
                db = cur_jiff["total"][0] - prev_jiff["total"][0]
                dtot = cur_jiff["total"][1] - prev_jiff["total"][1]
                if dtot > 0:
                    cpu_busy_pct = min(100.0, max(0.0, (db / dtot) * 100.0))

            cpu_max_thread_pct = 0.0
            for k, v in cur_jiff.items():
                if k == "total" or k not in prev_jiff:
                    continue
                db = v[0] - prev_jiff[k][0]
                dtot = v[1] - prev_jiff[k][1]
                if dtot > 0:
                    pct = min(100.0, max(0.0, (db / dtot) * 100.0))
                    if pct > cpu_max_thread_pct:
                        cpu_max_thread_pct = pct

            avg_mhz, max_mhz = read_cpu_freqs()
            cpu_potential_pct = min(100.0, cpu_busy_pct * (avg_mhz / CPU_ALLCORE_TURBO_MHZ)) if CPU_ALLCORE_TURBO_MHZ else 0.0
            try:
                load1 = float(read_text("/proc/loadavg", "0").split()[0])
            except Exception:
                load1 = 0.0

            psi_cpu_some, _ = read_psi("cpu")
            psi_mem_some, psi_mem_full = read_psi("memory")
            psi_io_some, psi_io_full = read_psi("io")

            therm_msr = read_msr(0x1B1)
            limit_msr = read_msr(0x64F)
            throttle_thermal = 1 if (therm_msr is not None and (therm_msr & 0x1)) else 0
            throttle_prochot = 1 if (therm_msr is not None and (therm_msr & 0x4)) else 0
            throttle_pwr = 1 if (therm_msr is not None and (therm_msr & 0x400)) else 0
            if limit_msr is not None and (limit_msr & 0x1C00):
                throttle_pwr = 1

            top_cpu_comm, top_cpu_pct = "", 0.0
            top_io_comm, top_io_write_kibs = "", 0.0
            top_mem_comm, top_mem_mib = "", 0.0

            cpu_by_comm = {}
            io_by_comm = {}
            mem_by_comm = {}

            for pid, pinfo in cur_procs.items():
                comm = pinfo["comm"]
                mem_by_comm[comm] = mem_by_comm.get(comm, 0.0) + pinfo["rss_mib"]
                if pid in prev_procs and prev_procs[pid]["start_ticks"] == pinfo["start_ticks"]:
                    dcpu = max(0.0, pinfo["cpu_sec"] - prev_procs[pid]["cpu_sec"])
                    cpu_by_comm[comm] = cpu_by_comm.get(comm, 0.0) + (dcpu / dt) * 100.0
                    dwr = max(0, pinfo["wr_bytes"] - prev_procs[pid]["wr_bytes"])
                    io_by_comm[comm] = io_by_comm.get(comm, 0.0) + (dwr / 1024.0) / dt

            if cpu_by_comm:
                top_cpu_comm, top_cpu_pct = max(cpu_by_comm.items(), key=lambda x: x[1])
            if io_by_comm:
                top_io_comm, top_io_write_kibs = max(io_by_comm.items(), key=lambda x: x[1])
            if mem_by_comm:
                top_mem_comm, top_mem_mib = max(mem_by_comm.items(), key=lambda x: x[1])

            render_ns_delta = 0
            video_ns_delta = 0
            copy_ns_delta = 0
            gpu_by_comm = {}
            for cid, cinfo in cur_drm.items():
                if cid in prev_drm:
                    dr = max(0, cinfo["render_ns"] - prev_drm[cid]["render_ns"])
                    dv = max(0, cinfo["video_ns"] - prev_drm[cid]["video_ns"])
                    dc = max(0, cinfo["copy_ns"] - prev_drm[cid]["copy_ns"])
                    render_ns_delta += dr
                    video_ns_delta += dv
                    copy_ns_delta += dc
                    comm = cinfo["comm"]
                    gpu_by_comm[comm] = gpu_by_comm.get(comm, 0.0) + ((dr + dv) / (dt * 1e9)) * 100.0

            gpu_render_pct = min(100.0, (render_ns_delta / (dt * 1e9)) * 100.0)
            gpu_video_pct = min(100.0, (video_ns_delta / (dt * 1e9)) * 100.0)
            gpu_copy_pct = min(100.0, (copy_ns_delta / (dt * 1e9)) * 100.0)
            gpu_freq_mhz = max((read_float(p, 0.0) for p in glob.glob("/sys/class/drm/card*/gt_act_freq_mhz")), default=0.0)
            top_gpu_comm, top_gpu_pct = ("", 0.0)
            if gpu_by_comm:
                top_gpu_comm, top_gpu_pct = max(gpu_by_comm.items(), key=lambda x: x[1])

            mem = read_mem()

            disk_read_kibs = 0.0
            disk_write_kibs = 0.0
            disk_busy_pct = 0.0
            if cur_nvme and prev_nvme:
                dr_sec = max(0, cur_nvme["rd_sectors"] - prev_nvme["rd_sectors"])
                dw_sec = max(0, cur_nvme["wr_sectors"] - prev_nvme["wr_sectors"])
                dticks = max(0, cur_nvme["io_ticks_ms"] - prev_nvme["io_ticks_ms"])
                disk_read_kibs = (dr_sec * 512.0 / 1024.0) / dt
                disk_write_kibs = (dw_sec * 512.0 / 1024.0) / dt
                disk_busy_pct = min(100.0, (dticks / (dt * 1000.0)) * 100.0)

            def rapl_w(key):
                if key in cur_rapl and key in prev_rapl:
                    duj = cur_rapl[key] - prev_rapl[key]
                    if duj >= 0:
                        return (duj / 1e6) / dt
                return 0.0

            pkg_watts = rapl_w("package-0")
            core_watts = rapl_w("core")
            uncore_watts = rapl_w("uncore")
            dram_watts = rapl_w("dram")
            rapl_pl1_watts = read_int("/sys/class/powercap/intel-rapl-mmio:0/constraint_0_power_limit_uw", 0) / 1e6

            cpu_temp_c, nvme_temp_c = read_temps()
            ac_online = read_int("/sys/class/power_supply/AC/online", 0)
            power_profile = ""
            for line in read_text("/var/lib/power-profiles-daemon/state.ini").splitlines():
                if line.startswith("Profile="):
                    power_profile = line.split("=", 1)[1].strip()
            epp = read_text("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference", "")

            bat_pct = 100.0
            bat_watts = 0.0
            bat_health_pct = 100.0
            bats = glob.glob("/sys/class/power_supply/BAT*")
            if bats:
                bdir = bats[0]
                bat_pct = read_float(os.path.join(bdir, "capacity"), 100.0)
                bat_watts = read_int(os.path.join(bdir, "power_now"), 0) / 1e6
                efull = read_float(os.path.join(bdir, "energy_full"), 0.0)
                edesign = read_float(os.path.join(bdir, "energy_full_design"), 0.0)
                if efull == 0.0:
                    efull = read_float(os.path.join(bdir, "charge_full"), 0.0)
                    edesign = read_float(os.path.join(bdir, "charge_full_design"), 0.0)
                bat_health_pct = (efull / edesign * 100.0) if edesign > 0 else 100.0

            active_app, active_title, fb_domain, fb_tab = query_hyprland()
            afk, aw_domain, aw_url, aw_tab_title, aw_audible, aw_tab_count = query_activitywatch(active_app, active_title)

            is_browser = is_browser_app(active_app)
            browser_domain = (aw_domain if aw_domain else fb_domain) if is_browser else ""
            browser_tab_title = (aw_tab_title if aw_tab_title else fb_tab) if is_browser else ""
            browser_url_val = aw_url if is_browser else ""
            browser_audible_val = aw_audible if is_browser else 0
            browser_tab_cnt_val = aw_tab_count if is_browser else 0

            row_vals = (
                int(now), round(dt, 2),
                round(cpu_busy_pct, 2), round(cpu_max_thread_pct, 2),
                round(avg_mhz, 1), round(max_mhz, 1), round(cpu_potential_pct, 2),
                round(load1, 2), round(psi_cpu_some, 2),
                throttle_thermal, throttle_prochot, throttle_pwr,
                top_cpu_comm, round(top_cpu_pct, 1),
                round(gpu_render_pct, 2), round(gpu_video_pct, 2), round(gpu_copy_pct, 2),
                round(gpu_freq_mhz, 1), top_gpu_comm, round(top_gpu_pct, 1),
                round(mem["used_pct"], 2), round(mem["used_mib"], 1), round(mem["avail_mib"], 1),
                round(mem["cached_mib"], 1), round(mem["shmem_mib"], 1),
                round(mem["zram_orig_mib"], 2), round(mem["zram_compr_mib"], 2), round(mem["zswap_mib"], 2),
                round(psi_mem_some, 2), round(psi_mem_full, 2),
                top_mem_comm, round(top_mem_mib, 1),
                round(disk_read_kibs, 1), round(disk_write_kibs, 1), round(disk_busy_pct, 2),
                round(psi_io_some, 2), round(psi_io_full, 2),
                top_io_comm, round(top_io_write_kibs, 1),
                round(pkg_watts, 2), round(core_watts, 2), round(uncore_watts, 2), round(dram_watts, 2),
                round(rapl_pl1_watts, 1), round(cpu_temp_c, 1), round(nvme_temp_c, 1),
                ac_online, power_profile, epp,
                round(bat_pct, 1), round(bat_watts, 2), round(bat_health_pct, 1),
                afk, active_app, active_title,
                browser_domain, browser_url_val, browser_tab_title, browser_audible_val, browser_tab_cnt_val
            )
            placeholders = ",".join(["?"] * len(row_vals))
            conn.execute("INSERT OR REPLACE INTO samples VALUES (" + placeholders + ")", row_vals)
            conn.commit()

            prev_ts = now
            prev_jiff = cur_jiff
            prev_rapl = cur_rapl
            prev_nvme = cur_nvme
            prev_procs = cur_procs
            prev_drm = cur_drm

    if __name__ == "__main__":
        main()
  '';

  laptopReportBin = pkgs.writeScriptBin "laptop-report" ''
    #!${pkgs.python3}/bin/python3
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
        args = ap.parse_args()
        if not math.isfinite(args.hours) or not 0 < args.hours <= ${toString cfg.track.retentionDays} * 24:
            ap.error("--hours must be greater than 0 and at most " + str(${toString cfg.track.retentionDays} * 24))

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
  '';
in
{
  config = lib.mkIf (cfg.enable && cfg.track.enable) {
    systemd.user.services.aw-server-rust = lib.mkIf cfg.track.activity.enable {
      description = "ActivityWatch local time-tracking server (Rust)";
      wantedBy = [ "default.target" ];
      unitConfig.ConditionUser = cfg.user;
      serviceConfig = {
        ExecStart = "${pkgs.aw-server-rust}/bin/aw-server --host 127.0.0.1 --port 5600";
        Restart = "always";
        RestartSec = 2;
        UMask = "0077";
      };
    };

    systemd.user.services.awatcher = lib.mkIf cfg.track.activity.enable {
      description = "ActivityWatch Wayland/Hyprland window and idle watcher";
      after = [ "aw-server-rust.service" ];
      wants = [ "aw-server-rust.service" ];
      wantedBy = [ "default.target" ];
      unitConfig.ConditionUser = cfg.user;
      serviceConfig = {
        ExecStart = awatcherStart;
        Restart = "always";
        RestartSec = 3;
      };
    };

    programs.firefox.policies = lib.mkIf cfg.track.browserExtensions.enable {
      ExtensionSettings = {
        "{ef87d84c-2127-493f-b952-5b4e744245bc}" = {
          installation_mode = "force_installed";
          install_url = "https://addons.mozilla.org/firefox/downloads/latest/aw-watcher-web/latest.xpi";
        };
      };
      "3rdparty".Extensions."{ef87d84c-2127-493f-b952-5b4e744245bc}" = {
        consentOfflineDataCollection = true;
      };
    };

    programs.chromium = lib.mkIf cfg.track.browserExtensions.enable {
      # This enables policy files for Chromium/Chrome; it does not install a browser.
      enable = true;
      extensions = [
        "nglaklhklhcoonedhgnpgddginnjdadi;https://clients2.google.com/service/update2/crx"
      ];
    };

    systemd.services.laptop-telemetry = {
      description = "nixcare: Laptop Hardware Potential & Screen-Time Telemetry Daemon (0600 private DB)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      serviceConfig = {
        ExecStartPre = "-${pkgs.kmod}/bin/modprobe msr";
        ExecStart = "${pkgs.python3}/bin/python3 ${collectorPy}";
        StateDirectory = "laptop-telemetry";
        StateDirectoryMode = "0700";
        UMask = "0077";
        Restart = "always";
        RestartSec = 2;
        Nice = 10;
        IOSchedulingClass = "idle";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = "read-only";
        ReadWritePaths = [ "/var/lib/laptop-telemetry" ];
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" ];
      };
    };

    environment.systemPackages = [ laptopReportBin ] ++ lib.optionals cfg.track.activity.enable [
      pkgs.aw-server-rust
      pkgs.awatcher
    ];
  };
}
