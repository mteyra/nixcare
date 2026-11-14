"""Regression tests for portable checks, isolation, and honest failure reporting."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "portable_nixcare", Path(__file__).resolve().parents[1] / "cli/nixcare.py",
)
nc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nc)


def lock(revision="old"):
    return {
        "version": 7, "root": "root",
        "nodes": {
            "root": {"inputs": {"nixpkgs": "nixpkgs"}},
            "nixpkgs": {
                "original": {"type": "github", "owner": "NixOS", "repo": "nixpkgs", "ref": "nixos-unstable"},
                "locked": {"type": "github", "rev": revision, "lastModified": 1000},
            },
        },
    }


class LockTests(unittest.TestCase):
    def test_fixed_revisions_and_future_timestamps(self):
        data = lock()
        data["nodes"]["nixpkgs"]["original"]["rev"] = "pinned"
        pin = nc.lock_pins(data, now=500)[0]
        self.assertTrue(pin["fixed_revision"])
        self.assertEqual(pin["age_days"], 0)

    def test_reports_added_changed_removed_inputs(self):
        before, after = lock(), lock("new")
        before["nodes"]["removed"] = {"locked": {"rev": "removed"}}
        after["nodes"]["added"] = {"locked": {"rev": "added"}}
        changes = nc.compare_locks(before, after)
        self.assertEqual([c["input"] for c in changes], ["added", "nixpkgs", "removed"])
        self.assertIsNone(changes[0]["before"])
        self.assertIsNone(changes[2]["after"])

    def test_malformed_lock_is_not_considered_current(self):
        for data in ([], {}, {"nodes": []}, {"nodes": {"nixpkgs": []}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                nc.lock_pins(data)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.flake = Path(self.temporary.name)
        (self.flake / "flake.nix").write_text("{ outputs = _: {}; }")
        (self.flake / "flake.lock").write_text(json.dumps(lock()))
        self.original = (self.flake / "flake.lock").read_bytes()

    def test_isolated_output_lock_preserves_source_on_success(self):
        captured = {}

        def update(arguments, timeout):
            self.assertEqual(arguments[:4], ["flake", "update", "--flake", str(self.flake)])
            destination = Path(arguments[arguments.index("--output-lock-file") + 1])
            self.assertNotEqual(destination.parent, self.flake)
            captured["destination"] = destination
            destination.write_text(json.dumps(lock("new")))
            return 0, "", ""

        with patch.object(nc, "nix_command", side_effect=update):
            result = nc.check_updates(self.flake)
        self.assertEqual(result["status"], "updates-available")
        self.assertFalse(result["build_verified"])
        self.assertEqual((self.flake / "flake.lock").read_bytes(), self.original)
        self.assertFalse(captured["destination"].exists())

    def test_network_failure_is_error_not_current(self):
        with patch.object(nc, "nix_command", return_value=(1, "", "network unavailable")):
            result = nc.check_updates(self.flake)
        self.assertEqual(result["status"], "error")
        self.assertIn("network unavailable", result["error"])
        self.assertEqual((self.flake / "flake.lock").read_bytes(), self.original)

    def test_concurrent_source_edit_is_preserved_and_reported(self):
        def update(arguments, timeout):
            Path(arguments[-1]).write_text(json.dumps(lock("new")))
            (self.flake / "flake.lock").write_text(json.dumps(lock("someone-else")))
            return 0, "", ""

        with patch.object(nc, "nix_command", side_effect=update):
            result = nc.check_updates(self.flake)
        self.assertEqual(result["status"], "error")
        self.assertIn("changed during the check", result["error"])
        self.assertIn("someone-else", (self.flake / "flake.lock").read_text())

    def test_malformed_output_is_error(self):
        def update(arguments, timeout):
            Path(arguments[-1]).write_text("[]")
            return 0, "", ""

        with patch.object(nc, "nix_command", side_effect=update):
            result = nc.check_updates(self.flake)
        self.assertEqual(result["status"], "error")

    def test_fixed_upstream_revision_can_be_current(self):
        data = lock()
        data["nodes"]["nixpkgs"]["original"]["rev"] = "old"
        (self.flake / "flake.lock").write_text(json.dumps(data))

        def update(arguments, timeout):
            Path(arguments[-1]).write_text(json.dumps(data))
            return 0, "", ""

        with patch.object(nc, "nix_command", side_effect=update):
            result = nc.check_updates(self.flake)
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["fixed_inputs"], ["nixpkgs"])

    def test_missing_lock_is_error_without_running_nix(self):
        (self.flake / "flake.lock").unlink()
        with patch.object(nc, "nix_command") as run:
            result = nc.check_updates(self.flake)
        run.assert_not_called()
        self.assertEqual(result["status"], "error")


class ReportTests(unittest.TestCase):
    def test_linux_memory_reads_available_swap_and_psi(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            meminfo = folder / "meminfo"
            meminfo.write_text("MemTotal: 1000 kB\nMemAvailable: 80 kB\nSwapTotal: 500 kB\nSwapFree: 20 kB\n")
            pressure = folder / "pressure"
            pressure.write_text("some avg10=3.00 avg60=2.50 avg300=1.20 total=123\n"
                                "full avg10=1.50 avg60=1.20 avg300=0.30 total=5\n")
            data = nc.linux_memory(meminfo, pressure)
            self.assertEqual(data["available_percent"], 8)
            self.assertEqual(data["swap_used_bytes"], 480 * 1024)
            self.assertEqual(data["pressure"]["full"]["avg60"], 1.2)

    def test_memory_psi_is_optional_and_malformed_averages_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            meminfo, pressure = folder / "meminfo", folder / "pressure"
            meminfo.write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\n")
            self.assertEqual(nc.linux_memory(meminfo, pressure)["pressure"], {})
            pressure.write_text("full avg10=NaN avg60=garbage avg300=150.00")
            self.assertEqual(nc.linux_memory(meminfo, pressure)["pressure"]["full"], {})

    def test_swap_occupancy_alone_does_not_trigger_pressure_warning(self):
        data = {"total_bytes": 1000, "available_bytes": 500, "available_percent": 50,
                "swap_total_bytes": 100, "swap_used_bytes": 99, "pressure": {}}
        with patch.object(nc, "linux_memory", return_value=data), \
                patch.object(nc.platform, "system", return_value="Linux"), \
                patch.object(nc, "command", return_value=(127, "", "not installed")):
            report = nc.scan(None)
        memory = next(item for item in report["checks"] if item["name"] == "memory")
        self.assertEqual(memory["status"], "ok")

    def test_private_atomic_output_replaces_symlink_without_following_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            sensitive = folder / "unrelated"
            sensitive.write_text("preserve this")
            destination = folder / "report.json"
            destination.symlink_to(sensitive)
            nc.write_report(destination, {"status": "ok"})
            self.assertFalse(destination.is_symlink())
            self.assertEqual(sensitive.read_text(), "preserve this")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(json.loads(destination.read_text()), {"status": "ok"})

    def test_missing_nix_is_an_error_without_crashing(self):
        with patch.object(nc, "command", return_value=(127, "", "nix not found")), \
                patch.object(nc.platform, "system", return_value="Darwin"):
            result = nc.scan(None)
        self.assertEqual(result["status"], "error")
        self.assertGreater(result["summary"]["skipped"], 0)

    def test_generations_only_matches_links_in_requested_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            target = folder / "store-path"
            target.mkdir()
            (folder / "profile-4-link").symlink_to(target)
            (folder / "profile-3-link").mkdir()
            (folder / "other-9-link").symlink_to(target)
            (folder / "profile").symlink_to("profile-4-link")
            history = nc.generations(folder / "profile")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["generation"], 4)
            self.assertTrue(history[0]["current"])

    def test_command_timeout_is_bounded_and_visible(self):
        with patch.object(nc.subprocess, "run", side_effect=subprocess.TimeoutExpired("nix", 3)):
            rc, out, err = nc.command(["nix"], timeout=3)
        self.assertEqual(rc, 124)
        self.assertIn("3s", err)

    def test_optimise_requires_explicit_apply(self):
        with patch.object(nc, "nix_command") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(nc.main(["optimise"]), 0)
            self.assertEqual(nc.main(["optimise", "--apply", "--dry-run"]), 0)
        run.assert_not_called()

    def test_update_error_has_nonzero_exit_and_persisted_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "flake.nix").write_text("{}")
            result = folder / "report.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = nc.main(["check-updates", "--flake", str(folder), "--json", "--output", str(result)])
            self.assertEqual(rc, 1)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "error")
            self.assertEqual(json.loads(result.read_text())["status"], "error")

    def test_report_output_cannot_replace_flake_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "flake.nix"
            source.write_text("{}")
            with contextlib.redirect_stdout(io.StringIO()), \
                    patch.object(nc, "check_updates", return_value={"status": "current"}):
                rc = nc.main(["check-updates", "--flake", str(folder), "--json", "--output", str(source)])
            self.assertEqual(rc, 1)
            self.assertEqual(source.read_text(), "{}")

    def test_missing_flake_replaces_previous_success_report_with_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            output = folder / "report.json"
            output.write_text('{"status": "current"}')
            with contextlib.redirect_stdout(io.StringIO()):
                rc = nc.main(["check-updates", "--flake", str(folder / "missing"),
                              "--json", "--output", str(output)])
            self.assertEqual(rc, 1)
            self.assertEqual(json.loads(output.read_text())["status"], "error")


if __name__ == "__main__":
    unittest.main()
