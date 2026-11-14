#!/usr/bin/env python3
import argparse
import glob
import contextlib
import fcntl
import hashlib
import pwd
import tempfile
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import urllib.request

# The NixOS wrapper supplies a store-resident JSON configuration. Plain defaults
# make the module importable by regression tests without evaluating Nix.
CONFIG = {}
if os.environ.get("NIXCARE_CONFIG"):
    with open(os.environ["NIXCARE_CONFIG"], encoding="utf-8") as config_file:
        CONFIG = json.load(config_file)
FLAKE_PATH = CONFIG.get("flake_path", "/etc/nixos")
HOST_NAME = CONFIG.get("host_name", "nixos")
OWNER_USER = CONFIG.get("user", "root")
STATE_DIR = CONFIG.get("state_dir", "/var/lib/nixcare")
WORKTREE_DIR = CONFIG.get("worktree_dir", "/var/cache/nixcare/worktree")
TELEMETRY_DB = "/var/lib/laptop-telemetry/telemetry.db"
AGENT_BRIDGE_MODE = CONFIG.get("agent_bridge_mode", "off")
TRACK_ENABLED = CONFIG.get("track_enabled", False)
ACTIVITY_ENABLED = CONFIG.get("activity_enabled", False)
KEEP_GENS = CONFIG.get("keep_generations", 5)
KEEP_DAYS = CONFIG.get("keep_days", 14)
CACHE_MAX_AGE_DAYS = CONFIG.get("cache_max_age_days", 30)
CACHE_ALLOWLIST = CONFIG.get("cache_allowlist", [])
CLEAN_OPTIMISE = CONFIG.get("clean_optimise", False)
BUILD_MAX_JOBS = CONFIG.get("build_max_jobs", 1)
BUILD_CORES = CONFIG.get("build_cores", 2)
PROFILE = "/nix/var/nix/profiles/system"
CURRENT_SYSTEM = "/run/current-system"
BOOTED_SYSTEM = "/run/booted-system"

REGENERABLE_CACHES = set(CACHE_ALLOWLIST)

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
    # Commands work even when the user's global Nix configuration disables the
    # experimental CLI; enabling features for this invocation has no side effect.
    if cmd and os.path.basename(cmd[0]) == "nix":
        cmd = [cmd[0], "--extra-experimental-features", "nix-command flakes"] + list(cmd[1:])
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
        uid = pwd.getpwnam(OWNER_USER).pw_uid
        runtime = "/run/user/" + str(uid)
        if not os.path.exists(runtime + "/bus"):
            return
        command = ["env", "XDG_RUNTIME_DIR=" + runtime,
                   "DBUS_SESSION_BUS_ADDRESS=unix:path=" + runtime + "/bus",
                   "notify-send", "-a", "nixcare", "-u", urgency, title, body]
        if os.geteuid() != uid:
            command = ["runuser", "-u", OWNER_USER, "--"] + command
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (KeyError, OSError):
        pass


def atomic_json(path, data):
    os.makedirs(os.path.dirname(path), mode=0o750, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".nixcare-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o640)
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), -1, os.stat(os.path.dirname(path)).st_gid)
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def state_file(name):
    return os.path.join(STATE_DIR, name + "-latest.json")


def require_root():
    if os.geteuid() != 0:
        raise RuntimeError("This action changes system state. Run it with sudo.")


class TemporaryUnavailableError(RuntimeError):
    """Work has not started and can be retried without changing its outcome."""


@contextlib.contextmanager
def maintenance_lock():
    os.makedirs(STATE_DIR, mode=0o750, exist_ok=True)
    fd = os.open(os.path.join(STATE_DIR, "maintenance.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise TemporaryUnavailableError("Another nixcare maintenance operation is running.") from error
        yield
    finally:
        os.close(fd)


def checked_cmd(command, timeout=60, **kwargs):
    rc, out, error = run_cmd(command, timeout=timeout, **kwargs)
    if rc:
        raise RuntimeError("%s failed (exit %s): %s" % (command[0], rc, error or out))
    return out


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_digest(path):
    """Fingerprint source plus mutable symlink referents, ignoring build/VCS noise.

    External references may be rejected by Nix's pure evaluator, but if Nix
    accepts one it must still be unchanged when the candidate is activated.
    Store targets are immutable and identified by their full store path.
    """
    def fingerprint(full, ancestors):
        digest = hashlib.sha256()
        if os.path.islink(full):
            digest.update(b"link:" + os.readlink(full).encode())
            target = os.path.realpath(full)
            if target.startswith("/nix/store/"):
                digest.update(target.encode())
            elif target in ancestors:
                digest.update(b"cycle")
            elif os.path.exists(target):
                digest.update(fingerprint(target, ancestors | {target}))
            else:
                digest.update(b"missing")
        elif os.path.isfile(full):
            digest.update(b"file:" + digest_file(full).encode())
        elif os.path.isdir(full):
            digest.update(b"directory:")
            for name in sorted(os.listdir(full)):
                if name == ".git" or name == "result" or name.startswith("result-"):
                    continue
                child = os.path.join(full, name)
                digest.update(name.encode() + b"\0")
                digest.update(fingerprint(child, ancestors | {os.path.realpath(full)}))
        else:
            raise RuntimeError("Unsupported special file in flake source: " + str(full))
        return digest.digest()
    return fingerprint(path, set()).hex()


def snapshot_ignore(directory, names):
    return [name for name in names if name == ".git" or name == "result" or name.startswith("result-")]

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
    collection_errors = {}
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
        collection_errors["flake_lock"] = str(e)

    vulnix_count = None
    vulnix_top = []
    vulnix_error = None
    if not args.fast:
        rc, out, err = run_cmd(["vulnix", "--system", "--json"], timeout=300)
        if not out:
            vulnix_error = err or "vulnix returned no usable report"
        if out:
            try:
                vdata = json.loads(out)
                if not isinstance(vdata, list) or not all(isinstance(item, dict) for item in vdata):
                    raise ValueError("expected an array of package records")
                # vulnix uses exit 2 both for findings and runtime errors. Only
                # a nonempty, valid package report establishes the former.
                if rc not in (0, 2) or (rc == 2 and not vdata):
                    raise ValueError("vulnix exited %s: %s" % (rc, err or "no findings report"))
                vulnix_count = len(vdata)
                for item in vdata[:10]:
                    vulnix_top.append({
                        "package": item.get("pname") or item.get("name"),
                        "version": item.get("version"),
                        "cves": item.get("affected_by", [])[:5],
                    })
            except (ValueError, TypeError, AttributeError) as error:
                vulnix_count = None
                vulnix_error = "Could not read vulnix report: " + str(error)

    if vulnix_error:
        collection_errors["vulnix"] = vulnix_error
    report["freshness"] = {
        "pins": pins,
        "vulnix_affected_packages": vulnix_count,
        "vulnix_sample": vulnix_top,
        "vulnix_error": vulnix_error,
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

    st_vfs = os.statvfs("/nix/store")
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
        if not TRACK_ENABLED:
            raise LookupError("Optional telemetry is disabled")
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
            if P.get("nvme_percent_used") is not None:
                hw["nvme_percent_used"] = P.get("nvme_percent_used")
    except Exception as e:
        hw["telemetry_status"] = str(e)
        if TRACK_ENABLED:
            collection_errors["telemetry"] = str(e)

    rc, nvme_out, _ = run_cmd(["nvme", "smart-log", "/dev/nvme0", "-o", "json"], timeout=10)
    if rc == 0 and nvme_out:
        try:
            njson = json.loads(nvme_out)
            hw["nvme_percent_used"] = njson.get("percent_used")
            hw["nvme_available_spare"] = njson.get("avail_spare")
            hw["nvme_media_errors"] = njson.get("media_errors")
        except Exception:
            pass
    report["hardware"] = hw

    # 4. System. Failed collection is unknown, never an empty healthy result.
    system_rc, failed_sys, system_err = run_cmd(["systemctl", "list-units", "--state=failed", "--no-legend", "--plain"])
    journal_rc, j_err, journal_err = run_cmd(["journalctl", "-b", "-p", "err", "--no-pager", "-q"])
    if system_rc:
        collection_errors["systemd"] = "exit %s: %s" % (system_rc, system_err or failed_sys or "no result")
    if journal_rc:
        collection_errors["journal"] = "exit %s: %s" % (journal_rc, journal_err or j_err or "no result")
    err_lines = [line for line in j_err.splitlines() if "Ignoring duplicate name" not in line] if journal_rc == 0 else None
    failed_units = [line.split()[0] for line in failed_sys.splitlines() if line.strip()] if system_rc == 0 else None
    report["system"] = {
        "failed_system_units": failed_units,
        "boot_journal_error_count": len(err_lines) if err_lines is not None else None,
        "boot_journal_errors_sample": err_lines[-10:] if err_lines is not None else None,
    }

    # 5. Security (agent bridge mode, telemetry DB mode, listening sockets)
    db_mode = None
    db_owner = None
    if os.path.exists(TELEMETRY_DB):
        st = os.stat(TELEMETRY_DB)
        db_mode = oct(st.st_mode & 0o777)
        db_owner = st.st_uid

    ss_rc, ss_out, ss_err = run_cmd(["ss", "-tulnpH"])
    if ss_rc:
        collection_errors["sockets"] = "exit %s: %s" % (ss_rc, ss_err or ss_out or "no result")
    non_local_listeners = [] if ss_rc == 0 else None
    for line in ss_out.splitlines() if ss_rc == 0 else []:
        parts = line.split()
        if len(parts) >= 5:
            local_addr = parts[4]
            if not (local_addr.startswith("127.") or local_addr.startswith("[::1]") or local_addr.startswith("0.0.0.0:68") or "%lo" in local_addr):
                non_local_listeners.append({"proto": parts[0], "address": local_addr, "process": parts[6] if len(parts) > 6 else ""})

    report["security"] = {
        "agent_bridge_mode": AGENT_BRIDGE_MODE,
        "telemetry_db_mode": db_mode,
        "telemetry_db_private_ok": db_mode in ("0o600", "0600") if TRACK_ENABLED else None,
        "non_loopback_listeners": non_local_listeners,
    }

    # 6. Trackers status
    lt_active = "disabled"
    if TRACK_ENABLED:
        _, lt_active, _ = run_cmd(["systemctl", "is-active", "laptop-telemetry.service"])
    aw_ok = None
    try:
        if not ACTIVITY_ENABLED:
            raise LookupError("Activity tracking disabled")
        aw_ok = False
        with urllib.request.urlopen("http://127.0.0.1:5600/api/0/buckets/", timeout=1.5) as r:
            b = json.loads(r.read().decode("utf-8"))
            aw_ok = len(b) > 0
    except Exception as error:
        if ACTIVITY_ENABLED:
            collection_errors["activitywatch"] = str(error)
    report["trackers"] = {
        "laptop_telemetry_service": lt_active,
        "activitywatch_responding": aw_ok,
    }

    findings = []
    if root_total_gib and root_free_gib / root_total_gib < 0.1:
        findings.append("Nix store filesystem has less than 10% free space. Preview sudo nixcare clean --dry-run.")
    if report["system"]["failed_system_units"]:
        findings.append("Failed system services: " + ", ".join(report["system"]["failed_system_units"]))
    if vulnix_count:
        findings.append("vulnix matched %d packages to vulnerability records; inspect the JSON and check updates." % vulnix_count)
    if hw.get("nvme_media_errors", 0):
        findings.append("NVMe reports media errors. Verify backups and inspect disk health.")
    report["findings"] = findings
    report["collection_errors"] = collection_errors
    report["status"] = "partial" if collection_errors else "attention" if findings else "ok"
    report["schema_version"] = 1
    if collection_errors:
        notify_user("nixcare: health check incomplete", "Unavailable checks: " + ", ".join(collection_errors))
    elif findings:
        notify_user("nixcare: health check needs attention", "\n".join(findings[:3]))
    try:
        atomic_json(state_file("scan"), report)
    except OSError as error:
        if not args.json:
            print("Report was not saved: %s (use sudo for the shared system report)." % error, file=sys.stderr)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        nixpkgs_age = pins.get("nixpkgs_2", pins.get("nixpkgs", {})).get("age_days", "?")
        print("=" * 74)
        print(" NIXCARE SCAN — SYSTEM HEALTH & SECURITY REPORT")
        print("=" * 74)
        print(" Freshness : nixpkgs pin age = %s days | vulnix package matches = %s" % (nixpkgs_age, vulnix_count))
        print(" Disk      : %s GiB free / %s GiB | %d NixOS generations | %d caches >10MiB" % (
            root_free_gib, root_total_gib, len(gens), len(caches)))
        print(" Hardware  : Battery health = %s%% | Peak CPU temp = %s C | NVMe used = %s%%" % (
            hw.get("battery_health_pct", "?"), hw.get("cpu_temp_max_c", "?"), hw.get("nvme_percent_used", "?")))
        print(" Status    : " + report["status"])
        print(" System    : Failed units = %s | Boot error lines = %s" % (
            len(failed_units) if failed_units is not None else "unknown",
            len(err_lines) if err_lines is not None else "unknown"))
        print(" Security  : Agent bridge = %s | Telemetry DB mode = %s (private=%s)" % (
            AGENT_BRIDGE_MODE, db_mode, report["security"]["telemetry_db_private_ok"]))
        print(" Trackers  : laptop-telemetry = %s | ActivityWatch = %s" % (
            lt_active, "disabled" if aw_ok is None else "active" if aw_ok else "unreachable"))
        for finding in findings:
            print(" Action    : " + finding)
        for check, error in collection_errors.items():
            print(" Missing   : %s: %s" % (check, str(error)[-300:]))
        print("=" * 74)
    return 1 if collection_errors else 0

def generation_plan(now=None):
    now = time.time() if now is None else now
    protected = {os.path.realpath(path) for path in (PROFILE, CURRENT_SYSTEM, BOOTED_SYSTEM)
                 if os.path.exists(path)}
    generations = []
    for link in glob.glob(PROFILE + "-*-link"):
        try:
            number = int(os.path.basename(link).split("-")[-2])
            age = (now - os.lstat(link).st_mtime) / 86400.0
            generations.append({"generation": number, "age_days": age,
                                "protected": os.path.realpath(link) in protected})
        except (ValueError, OSError):
            continue
    generations.sort(key=lambda item: item["generation"])
    keep = {item["generation"] for item in generations[-KEEP_GENS:]}
    keep.update(item["generation"] for item in generations
                if item["age_days"] <= KEEP_DAYS or item["protected"])
    return generations, [str(item["generation"]) for item in generations if item["generation"] not in keep]


def cache_plan(now=None):
    now = time.time() if now is None else now
    owner_uid = pwd.getpwnam(OWNER_USER).pw_uid
    root = os.path.join(pwd.getpwnam(OWNER_USER).pw_dir, ".cache")
    plan = []
    # Cache deletion is opt-in per directory, never inferred from folder names.
    for name in CACHE_ALLOWLIST:
        if name in ("", ".", "..") or os.path.basename(name) != name:
            raise RuntimeError("Cache allowlist entries must be single directory names")
        path = os.path.join(root, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        latest = 0
        size = 0
        safe = True
        for directory, dirs, files in os.walk(path, followlinks=False):
            for entry in [directory] + [os.path.join(directory, item) for item in files]:
                info = os.lstat(entry)
                if info.st_uid != owner_uid:
                    safe = False
                latest = max(latest, info.st_atime, info.st_mtime, info.st_ctime)
                if not stat.S_ISLNK(info.st_mode):
                    size += info.st_size
        age = (now - latest) / 86400.0
        if safe and age >= CACHE_MAX_AGE_DAYS:
            plan.append({"path": path, "size_mib": round(size / 1048576.0, 1), "age_days": age})
    return plan


def cmd_clean(args):
    apply = args.apply and not args.dry_run
    if apply:
        require_root()
    context = maintenance_lock() if apply else contextlib.nullcontext()
    with context:
        generations, to_delete = generation_plan()
        caches = cache_plan()
        print("nixcare clean — %s" % ("applying policy" if apply else "preview (use --apply to run)"))
        print("Generations: %d total; removing %s; retaining running, booted, recent and newest %d." %
              (len(generations), ", ".join(to_delete) or "none", KEEP_GENS))
        for cache in caches:
            print("Cache: %s (%.1f MiB, idle %.1f days)" % (cache["path"], cache["size_mib"], cache["age_days"]))
        if not apply:
            print("Will collect unreferenced store paths; rollback generations above remain rooted.")
            return 0
        report = {"schema_version": 1, "timestamp": int(time.time()), "status": "running",
                  "generations_removed": [], "caches_removed": []}
        atomic_json(state_file("clean"), report)
        try:
            if to_delete:
                checked_cmd(["nix-env", "--profile", PROFILE, "--delete-generations"] + to_delete, timeout=300)
                report["generations_removed"] = to_delete
                # Refresh boot entries before GC so no menu entry points at deleted store paths.
                checked_cmd([os.path.join(PROFILE, "bin/switch-to-configuration"), "boot"], timeout=300)
            for cache in caches:
                # Recheck after boot-menu work, which can take minutes; a user
                # may have started using the cache since the initial plan.
                if cache["path"] not in {item["path"] for item in cache_plan()}:
                    continue
                shutil.rmtree(cache["path"])
                report["caches_removed"].append(cache["path"])
            print("Collecting unreferenced Nix store paths...")
            report["garbage_collection"] = checked_cmd(["nix-store", "--gc"], timeout=3600)
            if CLEAN_OPTIMISE and not args.skip_optimise:
                print("Deduplicating Nix store files...")
                report["optimise"] = checked_cmd(["nix", "store", "optimise"], timeout=7200)
            report["status"] = "ok"
        except (RuntimeError, OSError, KeyboardInterrupt) as error:
            report["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "error"
            report["error"] = str(error) or "Cleanup interrupted"
            raise
        finally:
            report["completed_at"] = int(time.time())
            atomic_json(state_file("clean"), report)
    print("Cleanup completed. Report: " + state_file("clean"))
    return 0



def cmd_optimise(args):
    if not args.apply or args.dry_run:
        print("Preview: nixcare will deduplicate identical Nix store files using hardlinks.")
        print("No generations are deleted. Run sudo nixcare optimise --apply to proceed.")
        return 0
    require_root()
    with maintenance_lock():
        report = {"schema_version": 1, "timestamp": int(time.time()), "status": "running"}
        atomic_json(state_file("optimise"), report)
        try:
            report["output"] = checked_cmd(["nix", "store", "optimise"], timeout=7200)
            report["status"] = "ok"
            print(report["output"] or "Nix store optimisation completed.")
        except (RuntimeError, KeyboardInterrupt) as error:
            report.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "error",
                          error=str(error) or "Optimisation interrupted")
            raise
        finally:
            report["completed_at"] = int(time.time())
            atomic_json(state_file("optimise"), report)
    return 0

def cmd_update(args):
    require_root()
    print("nixcare update — building an isolated candidate; your running system stays active")
    with maintenance_lock():
        report = {"schema_version": 1, "timestamp": int(time.time()), "status": "checking",
                  "build_verified": False, "host": HOST_NAME, "flake": FLAKE_PATH}
        # Invalidate the last candidate before ANY operation that could fail.
        atomic_json(state_file("update"), report)
        out_link = os.path.join(STATE_DIR, "next-system")
        try:
            if not os.path.isfile(os.path.join(FLAKE_PATH, "flake.lock")):
                raise RuntimeError("A flake.lock is required. Lock your system flake before checking updates.")
            if os.path.islink(os.path.join(FLAKE_PATH, "flake.lock")):
                raise RuntimeError("Candidate builds require a regular flake.lock; a linked lock could edit its external target. Use check-updates for a read-only check.")
            with open(os.path.join(FLAKE_PATH, "flake.lock"), encoding="utf-8") as stream:
                lock_data = json.load(stream)
            if not isinstance(lock_data, dict) or not isinstance(lock_data.get("nodes"), dict):
                raise ValueError("flake.lock must contain a nodes object")
            nodes = lock_data["nodes"]
            direct_inputs = nodes.get(lock_data.get("root", "root"), {}).get("inputs", {})
            if not isinstance(direct_inputs, dict):
                raise ValueError("flake.lock root inputs must be an object")
            for name, node_id in direct_inputs.items():
                if not isinstance(node_id, str):
                    continue
                original = nodes.get(node_id, {}).get("original", {})
                relative = original.get("path", "")
                if original.get("type") != "path" or not relative or os.path.isabs(relative):
                    continue
                source_root = os.path.abspath(FLAKE_PATH)
                resolved = os.path.abspath(os.path.join(source_root, relative))
                if os.path.commonpath([source_root, resolved]) != source_root:
                    raise RuntimeError("Input %s uses %s outside the flake directory. Isolated candidate builds require a self-contained flake; use nixcare check-updates --flake %s for source-safe discovery." % (name, relative, FLAKE_PATH))
            initial_digest = source_digest(FLAKE_PATH)
            os.makedirs(os.path.dirname(WORKTREE_DIR), mode=0o750, exist_ok=True)
            if os.path.exists(WORKTREE_DIR):
                shutil.rmtree(WORKTREE_DIR)
            if os.path.lexists(out_link):
                os.unlink(out_link)
            shutil.copytree(FLAKE_PATH, WORKTREE_DIR, symlinks=True, ignore=snapshot_ignore)
            if source_digest(WORKTREE_DIR) != initial_digest or source_digest(FLAKE_PATH) != initial_digest:
                raise RuntimeError("Configuration changed while copying. Retry when edits are complete.")
            checked_cmd(["nix", "flake", "update", "--flake", "path:" + WORKTREE_DIR], timeout=900)
            checked_cmd(["nix", "build", "path:" + WORKTREE_DIR + "#nixosConfigurations." + HOST_NAME +
                         ".config.system.build.toplevel", "--out-link", out_link,
                         "--max-jobs", str(BUILD_MAX_JOBS), "--cores", str(BUILD_CORES)], timeout=7200)
            candidate = os.path.realpath(out_link)
            if not os.path.isfile(os.path.join(candidate, "bin/switch-to-configuration")):
                raise RuntimeError("Build did not produce an activatable NixOS system")
            if source_digest(FLAKE_PATH) != initial_digest:
                raise RuntimeError("Configuration changed during the build. Run update again.")
            diff = checked_cmd(["nvd", "diff", CURRENT_SYSTEM, out_link], timeout=120)
            security_changes = [line.strip() for line in diff.splitlines()
                                if any(package in line.lower() for package in SECURITY_PKGS)]
            report.update(status="ready", build_verified=True, source_digest=initial_digest,
                          candidate=candidate, candidate_lock_digest=digest_file(os.path.join(WORKTREE_DIR, "flake.lock")),
                          nvd_diff=diff, security_relevant_changes=security_changes)
            print(diff)
            print("Candidate built successfully. Review it, then run sudo nixcare apply-update.")
            if security_changes:
                print("Security-sensitive packages changed; this does not establish that a vulnerability was fixed.")
            notify_user("nixcare: update built", "Review with nixcare status; apply with sudo nixcare apply-update.")
        except (RuntimeError, OSError, ValueError, KeyboardInterrupt) as error:
            report.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "error",
                          build_verified=False, error=str(error) or "Update check interrupted")
            notify_user("nixcare: update check failed", "Current system unchanged. See sudo nixcare status.")
            raise
        finally:
            report["completed_at"] = int(time.time())
            atomic_json(state_file("update"), report)
    return 0


def copy_lock_atomic(source, destination):
    """Replace a lock without changing the original config owner's permissions."""
    original = os.stat(destination)
    fd, temporary = tempfile.mkstemp(prefix=".nixcare-lock-", dir=os.path.dirname(destination))
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, stat.S_IMODE(original.st_mode))
        if os.geteuid() == 0:
            os.chown(temporary, original.st_uid, original.st_gid)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_candidate():
    try:
        with open(state_file("update"), encoding="utf-8") as stream:
            report = json.load(stream)
    except (OSError, ValueError) as error:
        raise RuntimeError("No verified candidate is available. Run sudo nixcare update first.") from error
    if report.get("status") != "ready" or report.get("build_verified") is not True:
        raise RuntimeError("Latest update did not produce an unused verified candidate. Run sudo nixcare update.")
    if report.get("flake") != FLAKE_PATH or report.get("host") != HOST_NAME:
        raise RuntimeError("Candidate belongs to a different configuration. Run sudo nixcare update.")
    if report.get("source_digest") != source_digest(FLAKE_PATH):
        raise RuntimeError("Configuration changed since the build. Run sudo nixcare update again.")
    lock = os.path.join(WORKTREE_DIR, "flake.lock")
    if not os.path.isfile(lock) or digest_file(lock) != report.get("candidate_lock_digest"):
        raise RuntimeError("Candidate lock changed or is missing. Run sudo nixcare update again.")
    candidate = report.get("candidate", "")
    if (not candidate.startswith("/nix/store/") or
            candidate != os.path.realpath(os.path.join(STATE_DIR, "next-system")) or
            not os.path.isfile(os.path.join(candidate, "bin/switch-to-configuration"))):
        raise RuntimeError("Prebuilt candidate is missing or changed. Run sudo nixcare update again.")
    return report, lock, candidate


def cmd_apply_update(args):
    require_root()
    with maintenance_lock():
        report, lock, candidate = read_candidate()
        destination = os.path.join(FLAKE_PATH, "flake.lock")
        backup = os.path.join(STATE_DIR, "flake.lock.before-update")
        shutil.copy2(destination, backup)
        previous = os.path.realpath(PROFILE)
        report["status"] = "applying"
        atomic_json(state_file("update"), report)
        switched_profile = False
        try:
            copy_lock_atomic(lock, destination)
            checked_cmd(["nix-env", "--profile", PROFILE, "--set", candidate], timeout=300)
            switched_profile = True
            # Activate the exact closure tested above, never an untested rebuild.
            checked_cmd([os.path.join(candidate, "bin/switch-to-configuration"), "switch"], timeout=600)
            report.update(status="applied", applied_at=int(time.time()))
        except (RuntimeError, OSError, KeyboardInterrupt) as error:
            report.update(status="apply-failed", error=str(error) or "Activation interrupted")
            recovery_errors = []
            try:
                copy_lock_atomic(backup, destination)
            except OSError as recovery_error:
                recovery_errors.append("lock restore: " + str(recovery_error))
            if switched_profile or os.path.realpath(PROFILE) != previous:
                for command in (["nix-env", "--profile", PROFILE, "--set", previous],
                                [os.path.join(previous, "bin/switch-to-configuration"), "switch"]):
                    try:
                        checked_cmd(command, timeout=600)
                    except RuntimeError as recovery_error:
                        recovery_errors.append(str(recovery_error))
            report["recovery_errors"] = recovery_errors
            if recovery_errors:
                print("Automatic recovery needs attention: " + "; ".join(recovery_errors), file=sys.stderr)
            raise
        finally:
            atomic_json(state_file("update"), report)
    print("Verified update activated. Previous generations remain available for rollback.")
    return 0


def cmd_rollback(args):
    require_root()
    with maintenance_lock():
        checked_cmd(["nixos-rebuild", "switch", "--rollback"], timeout=600)
    print("Previous system generation activated. Your flake.lock is unchanged; review it before rebuilding.")
    return 0


def cmd_status(args):
    reports = {}
    for name in ("scan", "check-updates", "update", "clean", "optimise"):
        try:
            with open(state_file(name), encoding="utf-8") as stream:
                reports[name] = json.load(stream)
        except FileNotFoundError:
            reports[name] = {"status": "not-run"}
        except (OSError, ValueError) as error:
            reports[name] = {"status": "unavailable", "error": str(error)}
    if args.json:
        print(json.dumps({"schema_version": 1, "reports": reports}, indent=2))
    else:
        print("nixcare — latest maintenance results")
        for name, report in reports.items():
            when = report.get("completed_at", report.get("timestamp", report.get("checked_at")))
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(when)) if isinstance(when, (int, float)) else str(when or "")
            print("  %-14s %-14s %s" % (name, report.get("status", "recorded"), stamp))
            if report.get("error"):
                print("    " + str(report["error"])[-1000:])
        print("Reports: " + STATE_DIR + " (use sudo if unavailable)")
        print("Timers: systemctl list-timers 'nixcare-*'")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nixcare", description="NixOS health and reversible system maintenance",
                                 epilog="Also available: doctor, check-updates --flake PATH, and --version (portable commands).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    scan = sub.add_parser("scan", help="Read-only system health and security scan")
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--fast", action="store_true", help="Skip vulnerability database scan")
    scan.set_defaults(func=cmd_scan)
    clean = sub.add_parser("clean", help="Preview policy cleanup; use --apply to collect unused paths")
    clean.add_argument("--apply", action="store_true")
    clean.add_argument("--dry-run", action="store_true", help="Preview only (the default)")
    clean.add_argument("--skip-optimise", action="store_true")
    clean.set_defaults(func=cmd_clean)
    optimise = sub.add_parser("optimise", help="Deduplicate Nix store files; preview by default")
    optimise.add_argument("--apply", action="store_true")
    optimise.add_argument("--dry-run", action="store_true", help="Preview only (the default)")
    optimise.set_defaults(func=cmd_optimise)
    sub.add_parser("update", help="Build an isolated update without activation").set_defaults(func=cmd_update)
    sub.add_parser("apply-update", help="Activate the last successfully built, unchanged candidate").set_defaults(func=cmd_apply_update)
    sub.add_parser("rollback", help="Activate the previous NixOS generation").set_defaults(func=cmd_rollback)
    status = sub.add_parser("status", help="Show the latest saved maintenance results")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)
    report = sub.add_parser("report", help="Show optional local hardware and activity telemetry")
    report.add_argument("--hours", type=float, default=24.0)
    report.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "report":
            if not TRACK_ENABLED:
                raise RuntimeError("Telemetry is disabled. Set services.nixcare.track.enable = true to opt in.")
            if args.hours <= 0:
                ap.error("--hours must be positive")
            command = ["laptop-report", "--hours", str(args.hours)]
            if args.json:
                command.append("--json")
            return subprocess.call(command)
        return args.func(args) or 0
    except (RuntimeError, OSError, ValueError) as error:
        print("nixcare: " + str(error), file=sys.stderr)
        # Interactive callers fail immediately; scheduled services retry only
        # this temporary condition, never an operation that has already failed.
        return 75 if isinstance(error, TemporaryUnavailableError) else 1
    except KeyboardInterrupt:
        print("nixcare: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
