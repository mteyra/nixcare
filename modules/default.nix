{ config, pkgs, lib, ... }:

let
  cfg = config.services.nixcare;

  nixcarePy = pkgs.writeText "nixcare-cli.py" ''
    #!/usr/bin/env python3
    import argparse
    import glob
    import json
    import os
    import shutil
    import stat
    import subprocess
    import sys
    import time
    import urllib.request

    FLAKE_PATH = "${cfg.flakePath}"
    HOST_NAME = "${cfg.hostName}"
    OWNER_USER = "${cfg.user}"
    STATE_DIR = "/var/lib/nixcare"
    WORKTREE_DIR = "/var/cache/nixcare/worktree"
    TELEMETRY_DB = "/var/lib/laptop-telemetry/telemetry.db"
    AGENT_BRIDGE_MODE = "${cfg.agentBridge.mode}"
    KEEP_GENS = ${toString cfg.clean.keepGenerations}
    KEEP_DAYS = ${toString cfg.clean.keepDays}
    CACHE_MAX_AGE_DAYS = ${toString cfg.clean.cacheMaxAgeDays}

    REGENERABLE_CACHES = {
        "rattler",
        "uv",
        "pip",
        "ZapZap",
        "mozilla",
        "thumbnails",
        "fontconfig",
        "mesa_shader_cache",
        "mesa_shader_cache_db",
        "nix",
    }

    SECURITY_PKGS = (
        "firefox",
        "google-chrome",
        "chromium",
        "linux-",
        "openssl",
        "systemd",
        "openssh",
        "sudo",
        "polkit",
        "glibc",
        "curl",
    )

    def run_cmd(cmd, timeout=60, env=None, cwd=None):
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=cwd,
            )
            return r.returncode, r.stdout.strip(), r.stderr.strip()
        except Exception as e:
            return 1, "", str(e)

    def notify_user(title, body, urgency="normal"):
        try:
            for d in glob.glob("/run/user/*"):
                uid = os.path.basename(d)
                if not uid.isdigit():
                    continue
                subprocess.Popen(
                    [
                        "${pkgs.util-linux}/bin/runuser",
                        "-u",
                        OWNER_USER,
                        "--",
                        "env",
                        "XDG_RUNTIME_DIR=" + d,
                        "DBUS_SESSION_BUS_ADDRESS=unix:path=" + d + "/bus",
                        "${pkgs.libnotify}/bin/notify-send",
                        "-a",
                        "nixcare",
                        "-u",
                        urgency,
                        title,
                        body,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    def dir_size_and_mtime(path):
        total = 0
        latest_mtime = 0.0
        try:
            for root, dirs, files in os.walk(path):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        st = os.lstat(fp)
                        if not stat.S_ISLNK(st.st_mode):
                            total += st.st_size
                        if st.st_mtime > latest_mtime:
                            latest_mtime = st.st_mtime
                    except Exception:
                        pass
        except Exception:
            pass
        return total, latest_mtime

    def cmd_scan(args):
        now = time.time()
        report = {
            "timestamp": int(now),
            "iso_time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
            "host": HOST_NAME,
            "freshness": {},
            "disk": {},
            "hardware": {},
            "system": {},
            "security": {},
            "trackers": {},
        }

        # 1. Freshness (flake.lock pins + vulnix)
        lock_path = os.path.join(FLAKE_PATH, "flake.lock")
        pins = {}
        try:
            with open(lock_path, "r") as f:
                lock_data = json.load(f)
            for name, node in lock_data.get("nodes", {}).items():
                locked = node.get("locked", {})
                lm = locked.get("lastModified")
                if lm:
                    age_days = round((now - lm) / 86400.0, 1)
                    pins[name] = {
                        "age_days": age_days,
                        "rev": str(locked.get("rev", ""))[:12],
                        "ref": node.get("original", {}).get("ref", ""),
                    }
        except Exception as e:
            pins["error"] = str(e)

        vulnix_count = None
        vulnix_top = []
        if not args.fast:
            rc, out, _ = run_cmd(["${pkgs.vulnix}/bin/vulnix", "--system", "--json"], timeout=120)
            if out:
                try:
                    vdata = json.loads(out)
                    vulnix_count = len(vdata)
                    for item in vdata[:10]:
                        vulnix_top.append({
                            "package": item.get("pname") or item.get("name"),
                            "version": item.get("version"),
                            "cves": item.get("affected_by", [])[:5],
                        })
                except Exception:
                    pass

        report["freshness"] = {
            "pins": pins,
            "vulnix_affected_packages": vulnix_count,
            "vulnix_sample": vulnix_top,
        }

        # 2. Disk (generations, Btrfs usage, caches)
        gens = []
        for link in sorted(glob.glob("/nix/var/nix/profiles/system-*-link")):
            base = os.path.basename(link)
            try:
                gnum = int(base.split("-")[1])
                st = os.lstat(link)
                age_d = round((now - st.st_mtime) / 86400.0, 1)
                gens.append({"generation": gnum, "age_days": age_d})
            except Exception:
                pass
        gens.sort(key=lambda x: x["generation"])

        st_vfs = os.statvfs("/")
        root_total_gib = round((st_vfs.f_blocks * st_vfs.f_frsize) / (1024**3), 2)
        root_free_gib = round((st_vfs.f_bavail * st_vfs.f_frsize) / (1024**3), 2)

        cache_root = os.path.expanduser("~" + OWNER_USER + "/.cache")
        caches = []
        if os.path.isdir(cache_root):
            for entry in sorted(os.listdir(cache_root)):
                ep = os.path.join(cache_root, entry)
                if not os.path.isdir(ep) or os.path.islink(ep):
                    continue
                sz, mtime = dir_size_and_mtime(ep)
                mib = round(sz / 1048576.0, 1)
                if mib < 10.0:
                    continue
                age_d = round((now - mtime) / 86400.0, 1) if mtime > 0 else 0.0
                caches.append({
                    "name": entry,
                    "size_mib": mib,
                    "unmodified_days": age_d,
                    "policy": "regenerable" if entry in REGENERABLE_CACHES else "review",
                })
        caches.sort(key=lambda x: x["size_mib"], reverse=True)

        report["disk"] = {
            "root_total_gib": root_total_gib,
            "root_free_gib": root_free_gib,
            "generation_count": len(gens),
            "oldest_generation": gens[0] if gens else None,
            "newest_generation": gens[-1] if gens else None,
            "caches_over_10mib": caches,
        }

        # 3. Hardware (from tracker API + sysfs)
        hw = {}
        try:
            with urllib.request.urlopen("http://127.0.0.1:5601/api/summary?hours=24", timeout=2.0) as resp:
                tdata = json.loads(resp.read().decode("utf-8"))
                P = tdata.get("potential", {})
                hw["battery_health_pct"] = P.get("battery_health_pct")
                hw["cpu_temp_max_c"] = P.get("cpu_temp_c", {}).get("max")
                hw["pkg_watts_avg"] = P.get("pkg_watts", {}).get("avg")
                hw["pkg_watts_max"] = P.get("pkg_watts", {}).get("max")
                hw["throttled_ever"] = P.get("throttled_ever")
                hw["psi_cpu_avg"] = P.get("psi_cpu_some", {}).get("avg")
                hw["psi_mem_avg"] = P.get("psi_mem_some", {}).get("avg")
                hw["psi_io_avg"] = P.get("psi_io_some", {}).get("avg")
        except Exception as e:
            hw["telemetry_query_error"] = str(e)

        rc, nvme_out, _ = run_cmd(["${pkgs.nvme-cli}/bin/nvme", "smart-log", "/dev/nvme0", "-o", "json"], timeout=10)
        if rc == 0 and nvme_out:
            try:
                njson = json.loads(nvme_out)
                hw["nvme_percent_used"] = njson.get("percent_used")
                hw["nvme_available_spare"] = njson.get("avail_spare")
                hw["nvme_media_errors"] = njson.get("media_errors")
            except Exception:
                pass
        report["hardware"] = hw

        # 4. System (failed units, journal errors, fwupd)
        _, failed_sys, _ = run_cmd(["systemctl", "list-units", "--state=failed", "--no-legend", "--plain"])
        _, j_err, _ = run_cmd(["journalctl", "-b", "-p", "err", "--no-pager", "-q"])
        err_lines = [l for l in j_err.splitlines() if "Ignoring duplicate name" not in l] if j_err else []
        report["system"] = {
            "failed_system_units": [l.split()[0] for l in failed_sys.splitlines() if l.strip()],
            "boot_journal_error_count": len(err_lines),
            "boot_journal_errors_sample": err_lines[-10:],
        }

        # 5. Security (agent bridge mode, telemetry DB mode, listening sockets)
        db_mode = None
        db_owner = None
        if os.path.exists(TELEMETRY_DB):
            st = os.stat(TELEMETRY_DB)
            db_mode = oct(st.st_mode & 0o777)
            db_owner = st.st_uid

        _, ss_out, _ = run_cmd(["${pkgs.iproute2}/bin/ss", "-tulnpH"])
        non_local_listeners = []
        for line in ss_out.splitlines():
            parts = line.split()
            if len(parts) >= 5:
                local_addr = parts[4]
                if not (local_addr.startswith("127.") or local_addr.startswith("[::1]") or local_addr.startswith("0.0.0.0:68") or "%lo" in local_addr):
                    non_local_listeners.append({"proto": parts[0], "address": local_addr, "process": parts[6] if len(parts) > 6 else ""})

        report["security"] = {
            "agent_bridge_mode": AGENT_BRIDGE_MODE,
            "telemetry_db_mode": db_mode,
            "telemetry_db_private_ok": db_mode in ("0o600", "0600"),
            "non_loopback_listeners": non_local_listeners,
        }

        # 6. Trackers status
        _, lt_active, _ = run_cmd(["systemctl", "is-active", "laptop-telemetry.service"])
        aw_ok = False
        try:
            with urllib.request.urlopen("http://127.0.0.1:5600/api/0/buckets/", timeout=1.5) as r:
                b = json.loads(r.read().decode("utf-8"))
                aw_ok = len(b) > 0
        except Exception:
            pass
        report["trackers"] = {
            "laptop_telemetry_service": lt_active,
            "activitywatch_responding": aw_ok,
        }

        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(os.path.join(STATE_DIR, "scan-latest.json"), "w") as f:
                json.dump(report, f, indent=2)
        except Exception:
            pass

        if args.json:
            print(json.dumps(report, indent=2))
        else:
            nixpkgs_age = pins.get("nixpkgs_2", pins.get("nixpkgs", {})).get("age_days", "?")
            print("=" * 74)
            print(" NIXCARE SCAN — SYSTEM HEALTH & SECURITY REPORT")
            print("=" * 74)
            print(" Freshness : nixpkgs pin age = %s days | vulnix CVEs = %s" % (nixpkgs_age, vulnix_count))
            print(" Disk      : %s GiB free / %s GiB | %d NixOS generations | %d caches >10MiB" % (
                root_free_gib, root_total_gib, len(gens), len(caches)))
            print(" Hardware  : Battery health = %s%% | Peak CPU temp = %s C | NVMe used = %s%%" % (
                hw.get("battery_health_pct", "?"), hw.get("cpu_temp_max_c", "?"), hw.get("nvme_percent_used", "?")))
            print(" System    : Failed units = %d | Boot error lines = %d" % (
                len(report["system"]["failed_system_units"]), len(err_lines)))
            print(" Security  : Agent bridge = %s | Telemetry DB mode = %s (private=%s)" % (
                AGENT_BRIDGE_MODE, db_mode, report["security"]["telemetry_db_private_ok"]))
            print(" Trackers  : laptop-telemetry = %s | ActivityWatch = %s" % (
                lt_active, "active" if aw_ok else "unreachable"))
            print("=" * 74)

    def cmd_clean(args):
        now = time.time()
        print("=== nixcare clean (dry_run=%s) ===" % args.dry_run)

        # 1. Generations policy: keep last KEEP_GENS + anything newer than KEEP_DAYS
        gens = []
        cur_target = os.path.realpath("/nix/var/nix/profiles/system")
        for link in sorted(glob.glob("/nix/var/nix/profiles/system-*-link")):
            base = os.path.basename(link)
            try:
                gnum = int(base.split("-")[1])
                st = os.lstat(link)
                age_d = (now - st.st_mtime) / 86400.0
                gens.append((gnum, age_d, link, os.path.realpath(link) == cur_target))
            except Exception:
                pass
        gens.sort(key=lambda x: x[0])
        keep_set = set(g[0] for g in gens[-KEEP_GENS:])
        for gnum, age_d, _, is_cur in gens:
            if age_d <= KEEP_DAYS or is_cur:
                keep_set.add(gnum)

        to_delete = [str(g[0]) for g in gens if g[0] not in keep_set]
        print("Generations total: %d | Keeping: %d (last %d + <=%dd) | Deleting: %d (%s)" % (
            len(gens), len(keep_set), KEEP_GENS, KEEP_DAYS, len(to_delete), ", ".join(to_delete) or "none"))

        if to_delete and not args.dry_run:
            run_cmd(["nix-env", "--profile", "/nix/var/nix/profiles/system", "--delete-generations"] + to_delete, timeout=300)
            run_cmd(["nix-collect-garbage"], timeout=600)
            run_cmd(["/run/current-system/bin/switch-to-configuration", "boot"], timeout=120)

        # 2. Regenerable caches untouched for >= CACHE_MAX_AGE_DAYS
        cache_root = os.path.expanduser("~" + OWNER_USER + "/.cache")
        if os.path.isdir(cache_root):
            for entry in sorted(os.listdir(cache_root)):
                if entry not in REGENERABLE_CACHES:
                    continue
                ep = os.path.join(cache_root, entry)
                if not os.path.isdir(ep) or os.path.islink(ep):
                    continue
                sz, mtime = dir_size_and_mtime(ep)
                age_d = (now - mtime) / 86400.0 if mtime > 0 else 0.0
                if age_d >= CACHE_MAX_AGE_DAYS:
                    print("Pruning stale regenerable cache: %s (%.1f MiB, untouched %.1f days)" % (ep, sz / 1048576.0, age_d))
                    if not args.dry_run:
                        shutil.rmtree(ep, ignore_errors=True)

        # 3. Store optimise
        if not args.dry_run and not args.skip_optimise:
            print("Running nix store optimise...")
            run_cmd(["nix", "store", "optimise"], timeout=1800)

    def cmd_update(args):
        print("=== nixcare update (isolated build-only check) ===")
        os.makedirs(os.path.dirname(WORKTREE_DIR), exist_ok=True)
        os.makedirs(STATE_DIR, exist_ok=True)

        # Check if nixos-26.11 branch exists upstream yet
        rc, ls_out, _ = run_cmd(["${pkgs.git}/bin/git", "ls-remote", "--heads", "https://github.com/NixOS/nixpkgs", "refs/heads/nixos-26.11"], timeout=20)
        branch_26_11_exists = (rc == 0 and "nixos-26.11" in ls_out)

        if os.path.exists(WORKTREE_DIR):
            run_cmd(["${pkgs.git}/bin/git", "-C", FLAKE_PATH, "worktree", "remove", "--force", WORKTREE_DIR], timeout=30)
            shutil.rmtree(WORKTREE_DIR, ignore_errors=True)

        shutil.copytree(FLAKE_PATH, WORKTREE_DIR, symlinks=True)
        rc, _, err = run_cmd(["nix", "flake", "update", "--flake", WORKTREE_DIR], timeout=300)
        if rc != 0:
            msg = "⚠️ Flake update failed: " + err.splitlines()[-1] if err else "unknown error"
            print(msg)
            notify_user("nixcare update: ⚠️ Flake update failed", msg, "normal")
            sys.exit(1)

        out_link = os.path.join(STATE_DIR, "next-system")
        rc, _, build_err = run_cmd([
            "nix", "build",
            WORKTREE_DIR + "#nixosConfigurations." + HOST_NAME + ".config.system.build.toplevel",
            "--out-link", out_link
        ], timeout=3600)

        status = "ok"
        if rc != 0:
            status = "broken"
            last_lines = "\n".join(build_err.splitlines()[-10:])
            report = {
                "timestamp": int(time.time()),
                "status": "broken",
                "nixos_26_11_available": branch_26_11_exists,
                "error_tail": last_lines,
            }
            with open(os.path.join(STATE_DIR, "update-latest.json"), "w") as f:
                json.dump(report, f, indent=2)
            print("⚠️ Build failed. Keeping current lock.\n" + last_lines)
            notify_user("nixcare update: ⚠️ Package broken", "Build failed in weekly check; current system untouched.", "normal")
            return

        _, diff_out, _ = run_cmd(["${pkgs.nvd}/bin/nvd", "diff", "/run/current-system", out_link], timeout=60)
        sec_hits = []
        for line in diff_out.splitlines():
            for sp in SECURITY_PKGS:
                if sp in line.lower():
                    sec_hits.append(line.strip())
                    break

        status = "security" if sec_hits else "clean"
        report = {
            "timestamp": int(time.time()),
            "status": status,
            "nixos_26_11_available": branch_26_11_exists,
            "security_relevant_changes": sec_hits,
            "nvd_diff": diff_out,
        }
        with open(os.path.join(STATE_DIR, "update-latest.json"), "w") as f:
            json.dump(report, f, indent=2)

        print(diff_out)
        if branch_26_11_exists:
            print("NOTE: Upstream branch nixos-26.11 now exists on NixOS/nixpkgs!")
        if sec_hits:
            print("\n🔒 Security-relevant updates ready (%d packages). Run `sudo nixcare apply-update` to switch." % len(sec_hits))
            notify_user("nixcare update: 🔒 Security update ready", "%d security-relevant packages updated and pre-built. Run `nixcare apply-update`." % len(sec_hits), "critical")
        else:
            print("\n✅ Builds fine. Run `sudo nixcare apply-update` whenever ready.")
            notify_user("nixcare update: ✅ Builds fine", "Weekly system update pre-built cleanly.", "low")

    def cmd_apply_update(args):
        lock_src = os.path.join(WORKTREE_DIR, "flake.lock")
        lock_dst = os.path.join(FLAKE_PATH, "flake.lock")
        if not os.path.exists(lock_src):
            print("No pre-built worktree found at " + lock_src + ". Run `nixcare update` first.", file=sys.stderr)
            sys.exit(1)
        shutil.copy2(lock_src, lock_dst)
        rc = subprocess.call(["nixos-rebuild", "switch", "--flake", FLAKE_PATH + "#" + HOST_NAME])
        sys.exit(rc)

    def cmd_rollback(args):
        rc = subprocess.call(["nixos-rebuild", "switch", "--rollback"])
        sys.exit(rc)

    def main():
        ap = argparse.ArgumentParser(prog="nixcare", description="Declarative NixOS health scanner, cleaner, updater & telemetry")
        sub = ap.add_subparsers(dest="cmd", required=True)

        p_scan = sub.add_parser("scan", help="Run read-only health & security scan")
        p_scan.add_argument("--json", action="store_true", help="Output raw JSON")
        p_scan.add_argument("--fast", action="store_true", help="Skip full vulnix store scan")

        p_clean = sub.add_parser("clean", help="Run policy-based generation & cache cleanup")
        p_clean.add_argument("--dry-run", action="store_true", help="Preview without deleting")
        p_clean.add_argument("--skip-optimise", action="store_true", help="Skip nix store optimise")

        sub.add_parser("update", help="Build-only update check in isolated worktree")
        sub.add_parser("apply-update", help="Apply pre-built flake.lock from nixcare update")
        sub.add_parser("rollback", help="Roll back to previous NixOS generation")

        p_rep = sub.add_parser("report", help="Show hardware potential & screen time report")
        p_rep.add_argument("--hours", type=float, default=24.0)
        p_rep.add_argument("--json", action="store_true")

        args = ap.parse_args()
        if args.cmd == "scan":
            cmd_scan(args)
        elif args.cmd == "clean":
            cmd_clean(args)
        elif args.cmd == "update":
            cmd_update(args)
        elif args.cmd == "apply-update":
            cmd_apply_update(args)
        elif args.cmd == "rollback":
            cmd_rollback(args)
        elif args.cmd == "report":
            cmd = ["/run/current-system/sw/bin/laptop-report", "--hours", str(args.hours)]
            if args.json:
                cmd.append("--json")
            sys.exit(subprocess.call(cmd))

    if __name__ == "__main__":
        main()
  '';

  nixcareBin = pkgs.writeScriptBin "nixcare" ''
    #!${pkgs.bash}/bin/bash
    exec ${pkgs.python3}/bin/python3 ${nixcarePy} "$@"
  '';
in
{
  imports = [
    ./track.nix
  ];

  options.services.nixcare = {
    enable = lib.mkEnableOption "nixcare system health scanner, cleaner, safe updater, and local hardware-potential + screen-time tracker";

    user = lib.mkOption {
      type = lib.types.str;
      default = "haunted";
      description = "Primary desktop user who owns private telemetry files and receives desktop notifications.";
    };

    flakePath = lib.mkOption {
      type = lib.types.str;
      default = "/etc/nixos";
      description = "Path to the system NixOS flake.";
    };

    hostName = lib.mkOption {
      type = lib.types.str;
      default = config.networking.hostName;
      description = "NixOS flake configuration host name.";
    };

    scan = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Run daily read-only nixcare scan.";
      };
      dates = lib.mkOption {
        type = lib.types.str;
        default = "daily";
        description = "Systemd calendar expression for nixcare scan.";
      };
    };

    clean = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Enable policy-based generation & cache cleanup timer.";
      };
      keepGenerations = lib.mkOption {
        type = lib.types.int;
        default = 5;
        description = "Minimum number of most-recent NixOS generations to always keep.";
      };
      keepDays = lib.mkOption {
        type = lib.types.int;
        default = 14;
        description = "Also keep all NixOS generations created within this many days.";
      };
      cacheMaxAgeDays = lib.mkOption {
        type = lib.types.int;
        default = 30;
        description = "Delete allowlisted regenerable caches untouched for this many days.";
      };
      journalMaxUse = lib.mkOption {
        type = lib.types.str;
        default = "500M";
        description = "Maximum disk space used by systemd-journald.";
      };
      btrfsAutoScrub = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Run monthly Btrfs scrub on /.";
      };
    };

    update = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Run weekly build-only update check in an isolated worktree.";
      };
      dates = lib.mkOption {
        type = lib.types.str;
        default = "weekly";
        description = "Systemd calendar expression for nixcare update.";
      };
    };

    track = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Enable local Hardware-Potential + ActivityWatch + Browser Tab tracker.";
      };
      retentionDays = lib.mkOption {
        type = lib.types.int;
        default = 90;
        description = "Number of days of telemetry history to retain in SQLite.";
      };
    };

    agentBridge = {
      mode = lib.mkOption {
        type = lib.types.enum [ "auth" "open" "off" ];
        default = "auth";
        description = ''
          Polkit authorization mode for agent-bridge systemd units:
          - "auth": require admin password once per session (polkit.Result.AUTH_ADMIN_KEEP)
          - "open": passwordless start for active AI optimization sessions (polkit.Result.YES)
          - "off": standard root-only authentication
        '';
      };
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [
      nixcareBin
      pkgs.nvd
      pkgs.vulnix
    ];

    services.journald.extraConfig = "SystemMaxUse=${cfg.clean.journalMaxUse}";

    services.btrfs.autoScrub = lib.mkIf cfg.clean.btrfsAutoScrub {
      enable = true;
      interval = "monthly";
      fileSystems = [ "/" ];
    };

    systemd.services.nixcare-scan = lib.mkIf cfg.scan.enable {
      description = "nixcare: Daily read-only health & security scan";
      path = with pkgs; [ git config.nix.package systemd nvme-cli iproute2 vulnix coreutils ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${nixcareBin}/bin/nixcare scan";
        StateDirectory = "nixcare";
        StateDirectoryMode = "0750";
      };
    };

    systemd.timers.nixcare-scan = lib.mkIf cfg.scan.enable {
      description = "nixcare: Daily health scan timer";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.scan.dates;
        Persistent = true;
        RandomizedDelaySec = "15m";
      };
    };

    systemd.services.nixcare-clean = lib.mkIf cfg.clean.enable {
      description = "nixcare: Policy-driven NixOS generation & cache cleaner";
      path = with pkgs; [ config.nix.package coreutils ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${nixcareBin}/bin/nixcare clean";
      };
    };

    systemd.timers.nixcare-clean = lib.mkIf cfg.clean.enable {
      description = "nixcare: Weekly policy cleanup timer";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "weekly";
        Persistent = true;
        RandomizedDelaySec = "30m";
      };
    };

    systemd.services.nixcare-update = lib.mkIf cfg.update.enable {
      description = "nixcare: Weekly build-only NixOS update checker";
      path = with pkgs; [ git config.nix.package nvd coreutils ];
      environment.HOME = "/root";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${nixcareBin}/bin/nixcare update";
        StateDirectory = "nixcare";
        CacheDirectory = "nixcare";
      };
    };

    systemd.timers.nixcare-update = lib.mkIf cfg.update.enable {
      description = "nixcare: Weekly build-only update timer";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.update.dates;
        Persistent = true;
        RandomizedDelaySec = "1h";
      };
    };
  };
}
