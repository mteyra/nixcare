#!/usr/bin/env python3
"""Portable, dependency-free Nix diagnostics. Mutation is always explicit."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

VERSION = "0.2.1"
SCHEMA_VERSION = 1
NIX_FEATURES = ["--extra-experimental-features", "nix-command flakes"]


def command(argv: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Bound subprocesses and keep diagnostics separate from structured output."""
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=timeout, check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"{argv[0]} is not installed or is not in PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} timed out after {timeout}s"
    except OSError as error:
        return 1, "", str(error)


def nix_command(arguments: list[str], timeout: int = 30) -> tuple[int, str, str]:
    return command(["nix", *NIX_FEATURES, *arguments], timeout=timeout)


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def find_flake(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not (path / "flake.nix").is_file():
            raise ValueError(f"No flake.nix in {path}")
        return path
    for path in (Path.cwd(), Path("/etc/nixos")):
        if (path / "flake.nix").is_file():
            return path.resolve()
    return None


def lock_pins(data: dict[str, Any], now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    if not isinstance(data, dict):
        raise ValueError("flake.lock must contain a JSON object")
    nodes = data.get("nodes")
    if not isinstance(nodes, dict):
        raise ValueError("flake.lock has no valid nodes object")
    root = data.get("root")
    if not isinstance(root, str) or root not in nodes:
        raise ValueError("flake.lock has no valid root node")
    pins = []
    for name, node in sorted(nodes.items()):
        if not isinstance(node, dict):
            raise ValueError(f"Invalid lock node: {name}")
        locked = node.get("locked", {})
        original = node.get("original", {})
        if not isinstance(locked, dict) or not isinstance(original, dict):
            raise ValueError(f"Invalid lock metadata: {name}")
        if not isinstance(node.get("inputs", {}), dict):
            raise ValueError(f"Invalid lock inputs: {name}")
        if not locked:
            continue
        modified = locked.get("lastModified")
        age = None
        if isinstance(modified, (float, int)) and not isinstance(modified, bool):
            age = round(max(0, now - modified) / 86400, 1)
        pins.append({
            "input": name,
            "revision": locked.get("rev"),
            "age_days": age,
            "ref": original.get("ref"),
            "fixed_revision": bool(original.get("rev")),
            "type": locked.get("type"),
        })
    return pins


def compare_locks(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    # Validate both documents, rather than interpreting malformed input as "current".
    lock_pins(before)
    lock_pins(after)
    old, new = before["nodes"], after["nodes"]
    changes = []
    for name in sorted(old.keys() | new.keys()):
        old_locked = old.get(name, {}).get("locked")
        new_locked = new.get(name, {}).get("locked")
        old_inputs = old.get(name, {}).get("inputs", {})
        new_inputs = new.get(name, {}).get("inputs", {})
        if old_locked != new_locked or old_inputs != new_inputs:
            change = {"input": name, "before": old_locked, "after": new_locked}
            if old_inputs != new_inputs:
                change.update(before_inputs=old_inputs, after_inputs=new_inputs)
            changes.append(change)
    return changes


def generations(profile: Path) -> list[dict[str, Any]]:
    """Read generation links for one profile; never traverse or delete their targets."""
    matches = []
    pattern = re.compile(re.escape(profile.name) + r"-(\d+)-link$")
    current_link = None
    try:
        target = Path(os.readlink(profile))
        current_link = Path(os.path.abspath(target if target.is_absolute() else profile.parent / target))
    except OSError:
        pass
    for path in profile.parent.glob(profile.name + "-*-link"):
        match = pattern.fullmatch(path.name)
        if not match or not path.is_symlink():
            continue
        try:
            matches.append({
                "generation": int(match.group(1)),
                "age_days": round(max(0, time.time() - path.lstat().st_mtime) / 86400, 1),
                # Different generations can point to the same store closure.
                # The active profile's immediate link identifies its generation.
                "current": path.absolute() == current_link,
            })
        except OSError:
            continue
    return sorted(matches, key=lambda item: item["generation"])


def linux_memory(meminfo: Path = Path("/proc/meminfo"),
                 pressure: Path = Path("/proc/pressure/memory")) -> dict[str, Any]:
    """Measure available memory and sustained stalls; swap occupancy alone is not pressure."""
    values = {}
    for line in meminfo.read_text().splitlines():
        name, separator, value = line.partition(":")
        fields = value.split()
        if separator and fields and fields[0].isdigit():
            values[name] = int(fields[0]) * (1024 if len(fields) > 1 and fields[1] == "kB" else 1)
    total, available = values.get("MemTotal", 0), values.get("MemAvailable")
    if not total or available is None:
        raise ValueError("MemTotal or MemAvailable is unavailable in /proc/meminfo")
    result: dict[str, Any] = {
        "total_bytes": total, "available_bytes": available,
        "available_percent": round(100 * available / total, 1),
        "swap_total_bytes": values.get("SwapTotal", 0),
        "swap_used_bytes": max(0, values.get("SwapTotal", 0) - values.get("SwapFree", 0)),
        "pressure": {},
    }
    try:
        for line in pressure.read_text().splitlines():
            fields = line.split()
            if not fields or fields[0] not in ("some", "full"):
                continue
            averages = {}
            for field in fields[1:]:
                key, separator, value = field.partition("=")
                if separator and key in ("avg10", "avg60", "avg300"):
                    try:
                        parsed = float(value)
                        if 0 <= parsed <= 100:
                            averages[key] = parsed
                    except ValueError:
                        pass
            result["pressure"][fields[0]] = averages
    except OSError:
        pass  # PSI is optional on older kernels and restricted containers.
    return result


def scan(flake: Path | None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "version": VERSION,
        "checked_at": timestamp(), "platform": platform.system(),
        "machine": platform.machine(), "flake": str(flake) if flake else None,
        "checks": [], "pins": [], "profiles": [],
        "scope": "Local Nix health diagnostics; no network update or vulnerability scan.",
    }

    def check(name: str, status: str, message: str, action: str | None = None, **data: Any) -> None:
        entry = {"name": name, "status": status, "message": message, **data}
        if action:
            entry["action"] = action
        report["checks"].append(entry)

    rc, out, err = command(["nix", "--version"])
    check("nix", "ok" if rc == 0 else "error", out or err,
          None if rc == 0 else "Install Nix from https://nixos.org/download/")
    if rc == 0:
        rc, out, err = nix_command(["store", "info", "--json"])
        # Nix versions before "info" was introduced use the old "ping" spelling.
        if rc != 0 and ("unrecognised command" in err or "unrecognized command" in err):
            rc, out, err = nix_command(["store", "ping", "--json"])
        store_data = {}
        if rc == 0:
            try:
                store_data = json.loads(out)
                if not isinstance(store_data, dict):
                    raise ValueError("Expected JSON object")
            except (ValueError, TypeError):
                rc, err = 1, "Nix returned invalid store metadata"
        check("store", "ok" if rc == 0 else "error",
              f"Nix store is reachable ({store_data.get('url', 'local')})" if rc == 0 else err,
              None if rc == 0 else "Check the Nix daemon service and your store permissions.",
              details=store_data)

    store = Path(os.environ.get("NIX_STORE_DIR", "/nix/store"))
    try:
        usage = shutil.disk_usage(store)
        free_pct = 100 * usage.free / usage.total
        low = free_pct < 10 or usage.free < 5 * 1024**3
        check("disk", "warning" if low else "ok",
              f"{usage.free / 1024**3:.1f} GiB free on the store filesystem ({free_pct:.1f}%)",
              "Review old profile generations and GC roots before garbage collection." if low else None,
              total_bytes=usage.total, free_bytes=usage.free, free_percent=round(free_pct, 1),
              path=str(store))
    except OSError as error:
        check("disk", "skipped", str(error))

    home = Path.home()
    candidates = [Path("/nix/var/nix/profiles/system")]
    xdg_state = Path(os.environ.get("XDG_STATE_HOME", str(home / ".local/state")))
    candidates.extend([xdg_state / "nix/profiles/profile", home / ".nix-profile"])
    seen: set[str] = set()
    for profile in candidates:
        if profile.name == ".nix-profile" and profile.is_symlink():
            target = Path(os.readlink(profile))
            profile = target if target.is_absolute() else profile.parent / target
        # The profile path itself points to a numbered generation, not directly to
        # the store. Keep its parent so generation history remains discoverable.
        key = str(profile.absolute())
        if key in seen:
            continue
        seen.add(key)
        history = generations(profile)
        if history:
            report["profiles"].append({"path": key, "generations": history})
            check("generations", "warning" if len(history) > 20 else "ok",
                  f"{len(history)} generations retained for {profile.name}",
                  "Keep working rollback generations; review older ones if disk space is tight."
                  if len(history) > 20 else None, profile=key, count=len(history))

    if flake:
        try:
            report["pins"] = lock_pins(load_json(flake / "flake.lock"))
            old = [p for p in report["pins"] if p["age_days"] is not None
                   and p["age_days"] > 30 and not p["fixed_revision"]]
            fixed = [p for p in report["pins"] if p["fixed_revision"]]
            check("lock", "warning" if old else "ok",
                  f"{len(report['pins'])} locked inputs; {len(old)} moving inputs older than 30 days; "
                  f"{len(fixed)} explicitly pinned",
                  "Run nixcare check-updates --flake PATH to compare upstream revisions. "
                  "Age alone does not prove an update is available." if old else None,
                  stale_inputs=[p["input"] for p in old],
                  fixed_inputs=[p["input"] for p in fixed])
        except (OSError, ValueError) as error:
            check("lock", "warning", str(error),
                  "Generate or repair this flake's lock file before relying on reproducible builds.")
    else:
        check("lock", "skipped", "No flake selected. Channel-based Nix is supported for local diagnostics.",
              "Pass --flake PATH to inspect a flake's input revisions.")

    if platform.system() == "Linux":
        try:
            memory = linux_memory()
            stalls = memory["pressure"].get("full", {}).get("avg60")
            pressure = memory["available_percent"] < 10 or (stalls is not None and stalls >= 1)
            message = (f"{memory['available_bytes'] / 1024**3:.1f} GiB RAM available "
                       f"({memory['available_percent']:.1f}%); "
                       f"{memory['swap_used_bytes'] / 1024**3:.1f} GiB swap in use")
            if stalls is not None:
                message += f"; full memory stalls {stalls:.2f}% over 60s"
            check("memory", "warning" if pressure else "ok", message,
                  "Inspect memory-heavy processes and close unused applications; "
                  "limit concurrent Nix builds (--max-jobs 1 --cores 2)."
                  if pressure else None, **memory)
        except (OSError, ValueError) as error:
            check("memory", "skipped", str(error))

    if platform.system() == "Linux" and Path("/run/systemd/system").is_dir():
        for scope, arguments in (("system", []), ("user", ["--user"])):
            rc, out, err = command(
                ["systemctl", *arguments, "--failed", "--no-legend", "--plain", "--no-pager"],
                timeout=10,
            )
            if rc:
                check(f"failed-{scope}-services", "skipped", err)
            else:
                units = [line.split()[0] for line in out.splitlines() if line.strip()]
                check(f"failed-{scope}-services", "warning" if units else "ok",
                      f"{len(units)} failed {scope} services",
                      f"Inspect failures with systemctl {'--user ' if scope == 'user' else ''}"
                      f"status UNIT and journalctl {'--user ' if scope == 'user' else ''}-u UNIT."
                      if units else None, units=units)
    else:
        check("failed-services", "skipped", "systemd diagnostics only run on systemd Linux hosts.")

    current_kernel = Path("/run/current-system/kernel")
    booted_kernel = Path("/run/booted-system/kernel")
    if current_kernel.exists() and booted_kernel.exists():
        needs_reboot = current_kernel.resolve() != booted_kernel.resolve()
        check("kernel", "warning" if needs_reboot else "ok",
              "An activated kernel update needs a reboot." if needs_reboot else
              "The activated kernel matches the booted kernel.",
              "Reboot when convenient to use the activated kernel." if needs_reboot else None)

    counts = {status: sum(c["status"] == status for c in report["checks"])
              for status in ("ok", "warning", "error", "skipped")}
    report["summary"] = counts
    report["status"] = "error" if counts["error"] else "warning" if counts["warning"] else "ok"
    return report


def check_updates(flake: Path, timeout: int = 300) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "version": VERSION, "checked_at": timestamp(),
        "flake": str(flake), "status": "error", "changes": [], "build_verified": False,
    }
    try:
        source_lock = flake / "flake.lock"
        source_definition = (flake / "flake.nix").read_bytes()
        original_bytes = source_lock.read_bytes()
        before = json.loads(original_bytes)
        lock_pins(before)
        with tempfile.TemporaryDirectory(prefix="nixcare-update-") as temporary:
            output_lock = Path(temporary) / "flake.lock"
            rc, _, err = nix_command(
                # Let Nix detect a parent Git repository. Forcing path: here
                # breaks subdirectory flakes whose inputs live beside them.
                ["flake", "update", "--flake", str(flake),
                 "--output-lock-file", str(output_lock)], timeout=timeout,
            )
            if rc:
                raise RuntimeError(err or f"Nix exited with status {rc}")
            after = load_json(output_lock)
            result["changes"] = compare_locks(before, after)
        # Never restore over a concurrent edit; report it instead.
        if source_lock.read_bytes() != original_bytes:
            raise RuntimeError("Source flake.lock changed during the check; rerun after other edits finish.")
        if (flake / "flake.nix").read_bytes() != source_definition:
            raise RuntimeError("Source flake.nix changed during the check; rerun after other edits finish.")
        result["status"] = "updates-available" if result["changes"] else "current"
        result["fixed_inputs"] = [p["input"] for p in lock_pins(before) if p["fixed_revision"]]
    except (OSError, ValueError, RuntimeError, TypeError) as error:
        result["error"] = str(error)
    return result


def validate_report_destination(path: Path | None, flake: Path | None) -> None:
    """Protect both a flake's file names and any external symlink targets."""
    if path is None or flake is None:
        return
    destination = path.expanduser().absolute()
    for name in ("flake.nix", "flake.lock"):
        source = (flake / name).absolute()
        if destination == source or destination.resolve() == source.resolve():
            raise ValueError("--output must not replace flake.nix or flake.lock")


def write_report(path: Path, report: dict[str, Any]) -> None:
    """Persist complete owner-readable reports, including when a check fails."""
    path = path.expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as file:
            temporary = file.name
            json.dump(report, file, indent=2, ensure_ascii=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def print_report(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    print(f"nixcare {VERSION} · {report['status']}")
    if "checks" in report:
        for item in report["checks"]:
            print(f"  {item['status'].upper():7} {item['message']}")
            if item.get("action"):
                print(f"          {item['action']}")
        print(f"\n{report['scope']}")
    elif report.get("error"):
        print(f"  {report['error']}")
    else:
        for change in report["changes"]:
            before, after = change["before"] or {}, change["after"] or {}
            if before != after:
                old = str(before.get("rev", before.get("narHash", "absent")))[:16]
                new = str(after.get("rev", after.get("narHash", "absent")))[:16]
                print(f"  {change['input']}: {old} -> {new}")
            if "before_inputs" in change:
                print(f"  {change['input']}: dependency links changed")
        if not report["changes"]:
            print("  All configured inputs match their upstream revisions.")
        if report.get("fixed_inputs"):
            print("  Explicit revision pins stay fixed: " + ", ".join(report["fixed_inputs"]))
        print("\nSource lock unchanged. Updates have not been built or activated.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nixcare",
        description="Practical Nix health checks on Linux and macOS. Runs a read-only scan by default.",
        epilog="NixOS users can enable services.nixcare for scheduled maintenance and verified system builds.",
    )
    parser.add_argument("--version", action="version", version=f"nixcare {VERSION}")
    subparsers = parser.add_subparsers(dest="command")
    for name in ("scan", "doctor"):
        sub = subparsers.add_parser(name, help="Inspect Nix, disk space, generations, and input age")
        sub.add_argument("--flake", help="Local flake directory (default: current directory or /etc/nixos)")
        sub.add_argument("--json", action="store_true", help="Print structured JSON")
        sub.add_argument("--output", type=Path, help="Atomically write a private JSON report")
        sub.add_argument("--strict", action="store_true", help="Exit 1 for warnings as well as errors")
    updates = subparsers.add_parser(
        "check-updates", help="Compare upstream flake inputs without changing your source lock or building",
    )
    updates.add_argument("--flake", required=True, help="Local flake directory")
    updates.add_argument("--json", action="store_true")
    updates.add_argument("--output", type=Path, help="Atomically write a private JSON report")
    updates.add_argument("--timeout", type=int, default=300, help="Network check timeout in seconds (default: 300)")
    updates.add_argument("--fail-on-updates", action="store_true", help="Exit 2 when newer inputs are available")
    optimise = subparsers.add_parser("optimise", help="Deduplicate identical Nix store files")
    optimise.add_argument("--apply", action="store_true", help="Run store optimisation (may take several minutes)")
    optimise.add_argument("--dry-run", action="store_true", help="Describe the operation; this is the default")
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        arguments = parser.parse_args(["scan"])
    if arguments.command == "optimise":
        if not arguments.apply or arguments.dry_run:
            print("Preview: nix store optimise shares identical store files using hardlinks.\n"
                  "It preserves packages and rollback generations, and may take several minutes.\n"
                  "Run nixcare optimise --apply to start. Daemon permissions may be required.")
            return 0
        try:
            rc, out, err = nix_command(["store", "optimise"], timeout=3600)
        except KeyboardInterrupt:
            print("nixcare: interrupted", file=sys.stderr)
            return 130
        if out:
            print(out)
        if err:
            print(err, file=sys.stderr)
        if rc == 0:
            print("Store optimisation completed.")
        return 0 if rc == 0 else 1
    write_attempted = False
    flake = None
    requested_flake = None
    try:
        if arguments.flake:
            requested_flake = Path(arguments.flake).expanduser().absolute()
        flake = find_flake(arguments.flake)
        validate_report_destination(arguments.output, flake)
        if arguments.command == "check-updates":
            if arguments.timeout < 1:
                parser.error("--timeout must be positive")
            assert flake is not None
            report = check_updates(flake, timeout=arguments.timeout)
        else:
            report = scan(flake)
        if arguments.output:
            write_attempted = True
            write_report(arguments.output, report)
        print_report(report, arguments.json)
        if report["status"] == "error":
            return 1
        if arguments.command == "check-updates":
            return 2 if arguments.fail_on_updates and report["status"] == "updates-available" else 0
        return 1 if arguments.strict and report["status"] == "warning" else 0
    except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as error:
        interrupted = isinstance(error, KeyboardInterrupt)
        message = "Interrupted; no completed result is available." if interrupted else str(error)
        report = {"schema_version": SCHEMA_VERSION, "version": VERSION,
                  "checked_at": timestamp(), "status": "interrupted" if interrupted else "error",
                  "error": message}
        if arguments.output and (not write_attempted or interrupted):
            try:
                validate_report_destination(arguments.output, flake or requested_flake)
                write_report(arguments.output, report)
            except (OSError, ValueError, RuntimeError) as output_error:
                report["output_error"] = str(output_error)
        if arguments.json:
            print(json.dumps(report))
        else:
            print(f"nixcare: {message}", file=sys.stderr)
        return 130 if interrupted else 1


if __name__ == "__main__":
    sys.exit(main())
