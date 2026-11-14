"""Regression tests for the NixOS maintenance transaction and retention policy.

Every mutating command is mocked. These tests never change the host's Nix store,
system profile, boot entries, caches, or system configuration.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "modules"))
import nixcare_core as core


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.flake = self.root / "config"
        self.flake.mkdir()
        (self.flake / "flake.nix").write_text("{ outputs = _: {}; }")
        (self.flake / "flake.lock").write_text('{"nodes":{}}')
        self.state = self.root / "state"
        self.state.mkdir()
        self.worktree = self.root / "cache" / "worktree"
        self.profile = self.root / "profiles" / "system"
        self.profile.parent.mkdir()
        self.current = self.root / "current"
        self.booted = self.root / "booted"
        for key, value in {"FLAKE_PATH": str(self.flake), "STATE_DIR": str(self.state),
                           "WORKTREE_DIR": str(self.worktree), "PROFILE": str(self.profile),
                           "CURRENT_SYSTEM": str(self.current), "BOOTED_SYSTEM": str(self.booted),
                           "KEEP_GENS": 2, "KEEP_DAYS": 14, "CACHE_ALLOWLIST": [],
                           "CLEAN_OPTIMISE": False}.items():
            patcher = patch.object(core, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for method in ("notify_user", "require_root"):
            patcher = patch.object(core, method)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def add_generation(self, number, age=100):
        target = self.root / ("store-system-%d" % number)
        target.mkdir()
        link = self.profile.parent / ("system-%d-link" % number)
        link.symlink_to(target)
        stamp = time.time() - age * 86400
        os.utime(link, (stamp, stamp), follow_symlinks=False)
        return target

    def candidate(self):
        self.worktree.mkdir(parents=True)
        lock = self.worktree / "flake.lock"
        lock.write_text('{"nodes":{"updated":{}}}')
        candidate = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-test-system"
        (self.state / "next-system").symlink_to(candidate)
        report = {"status": "ready", "build_verified": True, "flake": str(self.flake),
                  "host": core.HOST_NAME, "source_digest": core.source_digest(str(self.flake)),
                  "candidate_lock_digest": core.digest_file(lock), "candidate": candidate}
        core.atomic_json(core.state_file("update"), report)
        return report, str(lock), candidate

    def scan_result(self, command_results=None, fast=False):
        command_results = command_results or {}
        def run(command, **kwargs):
            return command_results.get(command[0], (0, "[]" if command[0] == "vulnix" else "", ""))
        disk = SimpleNamespace(f_blocks=100, f_frsize=1024**3, f_bavail=50)
        with patch.object(core, "run_cmd", side_effect=run), patch.object(core.os, "statvfs", return_value=disk), \
                patch.object(core.glob, "glob", return_value=[]), \
                patch.object(core.os.path, "expanduser", return_value=str(self.root / "absent-cache")), \
                patch.object(core, "TELEMETRY_DB", str(self.root / "absent.db")), \
                patch.object(core, "TRACK_ENABLED", False), patch.object(core, "ACTIVITY_ENABLED", False):
            result = core.cmd_scan(argparse.Namespace(fast=fast, json=True))
        return result, json.loads(Path(core.state_file("scan")).read_text())

    def test_scan_failed_collectors_are_unknown_partial_and_nonzero(self):
        result, report = self.scan_result({"systemctl": (1, "", "bus unavailable"),
                                           "journalctl": (1, "", "permission denied"),
                                           "ss": (1, "", "socket query failed")}, fast=True)
        self.assertEqual(result, 1)
        self.assertEqual(report["status"], "partial")
        self.assertIsNone(report["system"]["failed_system_units"])
        self.assertIsNone(report["system"]["boot_journal_error_count"])
        self.assertIsNone(report["security"]["non_loopback_listeners"])
        self.assertEqual(set(report["collection_errors"]), {"systemd", "journal", "sockets"})

    def test_scan_known_failed_unit_warns_without_claiming_collection_failed(self):
        result, report = self.scan_result({"systemctl": (0, "broken.service loaded failed failed Example", "")})
        self.assertEqual(result, 0)
        self.assertEqual(report["status"], "attention")
        self.assertEqual(report["system"]["failed_system_units"], ["broken.service"])
        self.assertIn("broken.service", report["findings"][0])
        self.assertEqual(report["collection_errors"], {})

    def test_vulnix_error_is_partial_even_with_empty_json_stdout(self):
        result, report = self.scan_result({"vulnix": (2, "[]", "NVD download failed")})
        self.assertEqual(result, 1)
        self.assertEqual(report["status"], "partial")
        self.assertIsNone(report["freshness"]["vulnix_affected_packages"])
        self.assertIn("vulnix", report["collection_errors"])

    def test_vulnix_findings_exit_two_is_not_a_collection_error(self):
        findings = json.dumps([{"pname": "example", "version": "1.0", "affected_by": ["CVE-TEST"]}])
        result, report = self.scan_result({"vulnix": (2, findings, "")})
        self.assertEqual(result, 0)
        self.assertEqual(report["status"], "attention")
        self.assertEqual(report["freshness"]["vulnix_affected_packages"], 1)
        self.assertEqual(report["collection_errors"], {})

    def test_interrupted_maintenance_is_recorded_as_interrupted(self):
        for arguments, report_name in [(["clean", "--apply"], "clean"),
                                       (["optimise", "--apply"], "optimise"),
                                       (["update"], "update")]:
            with self.subTest(command=arguments):
                with patch.object(core, "checked_cmd", side_effect=KeyboardInterrupt):
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(core.main(arguments), 130)
                report = json.loads(Path(core.state_file(report_name)).read_text())
                self.assertEqual(report["status"], "interrupted")
                self.assertIn("completed_at", report)

    def test_retention_keeps_running_booted_profile_recent_and_newest(self):
        targets = {n: self.add_generation(n, age=1 if n == 4 else 100) for n in range(1, 9)}
        self.current.symlink_to(targets[1])
        self.booted.symlink_to(targets[2])
        self.profile.symlink_to(targets[3])
        generations, deleted = core.generation_plan()
        self.assertEqual(len(generations), 8)
        self.assertEqual(deleted, ["5", "6"])

    def test_clean_defaults_to_preview_and_never_runs_mutations(self):
        self.add_generation(1)
        with patch.object(core, "checked_cmd") as command, patch.object(core, "atomic_json") as save:
            self.assertEqual(core.main(["clean"]), 0)
        command.assert_not_called()
        save.assert_not_called()
        self.assertIn("preview", self.output.getvalue())

    def test_explicit_dry_run_overrides_apply(self):
        with patch.object(core, "checked_cmd") as command:
            self.assertEqual(core.main(["clean", "--apply", "--dry-run"]), 0)
        command.assert_not_called()

    def test_cleanup_collects_unused_paths_even_without_old_generations(self):
        with patch.object(core, "checked_cmd", return_value="freed unused paths") as command:
            self.assertEqual(core.main(["clean", "--apply"]), 0)
        self.assertEqual(command.call_args.args[0], ["nix-store", "--gc"])
        report = json.loads(Path(core.state_file("clean")).read_text())
        self.assertEqual(report["status"], "ok")

    def test_failed_generation_removal_stops_cleanup_and_records_failure(self):
        for n in range(1, 4):
            self.add_generation(n)
        with patch.object(core, "checked_cmd", side_effect=RuntimeError("deletion failed")) as command:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(core.main(["clean", "--apply"]), 1)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0][0], "nix-env")
        self.assertEqual(json.loads(Path(core.state_file("clean")).read_text())["status"], "error")

    def test_cache_deletion_has_no_implicit_allowlist(self):
        self.assertEqual(core.cache_plan(), [])

    def test_cache_path_escape_rejected(self):
        with patch.object(core, "CACHE_ALLOWLIST", ["../../Documents"]):
            with self.assertRaisesRegex(RuntimeError, "single directory names"):
                core.cache_plan()

    def test_source_digest_detects_edits_and_untracked_files(self):
        before = core.source_digest(self.flake)
        (self.flake / "hardware.nix").write_text("{}")
        after = core.source_digest(self.flake)
        self.assertNotEqual(before, after)
        (self.flake / "hardware.nix").write_text("{ changed = true; }")
        self.assertNotEqual(after, core.source_digest(self.flake))

    def test_source_digest_ignores_git_and_result_links(self):
        before = core.source_digest(self.flake)
        (self.flake / ".git").mkdir()
        (self.flake / ".git" / "index").write_text("changed index")
        (self.flake / "result").symlink_to("/nix/store/example")
        self.assertEqual(before, core.source_digest(self.flake))

    def test_source_digest_detects_symlink_retargeting(self):
        link = self.flake / "linked.nix"
        link.symlink_to("hardware-a.nix")
        before = core.source_digest(self.flake)
        link.unlink()
        link.symlink_to("hardware-b.nix")
        self.assertNotEqual(before, core.source_digest(self.flake))

    def test_source_digest_detects_external_symlink_referent_changes(self):
        external = self.root / "external.nix"
        external.write_text("{ old = true; }")
        (self.flake / "module.nix").symlink_to(external)
        before = core.source_digest(self.flake)
        external.write_text("{ new = true; }")
        self.assertNotEqual(before, core.source_digest(self.flake))

    def test_source_digest_handles_symlink_cycles(self):
        (self.flake / "recursive").symlink_to(self.flake)
        self.assertEqual(core.source_digest(self.flake), core.source_digest(self.flake))

    def test_optimise_default_previews_without_mutations(self):
        with patch.object(core, "checked_cmd") as command:
            self.assertEqual(core.main(["optimise"]), 0)
        command.assert_not_called()

    def test_optimise_failure_saved_and_nonzero(self):
        with patch.object(core, "checked_cmd", side_effect=RuntimeError("optimisation failed")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(core.main(["optimise", "--apply"]), 1)
        report = json.loads(Path(core.state_file("optimise")).read_text())
        self.assertEqual(report["status"], "error")

    def test_failed_update_invalidates_previous_candidate_and_returns_failure(self):
        self.candidate()
        with patch.object(core, "checked_cmd", side_effect=RuntimeError("network unavailable")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(core.main(["update"]), 1)
        report = json.loads(Path(core.state_file("update")).read_text())
        self.assertEqual(report["status"], "error")
        self.assertFalse(report["build_verified"])
        self.assertFalse((self.state / "next-system").exists())
        self.assertEqual((self.flake / "flake.lock").read_text(), '{"nodes":{}}')

    def test_linked_lock_rejected_before_any_update_can_touch_external_target(self):
        lock = self.flake / "flake.lock"
        lock.unlink()
        external = self.root / "external.lock"
        external.write_text("private lock")
        lock.symlink_to(external)
        with patch.object(core, "checked_cmd") as command:
            with self.assertRaisesRegex(RuntimeError, "regular flake.lock"):
                core.cmd_update(argparse.Namespace())
        command.assert_not_called()
        self.assertEqual(external.read_text(), "private lock")

    def test_external_relative_flake_input_is_actionably_rejected(self):
        lock = {"root": "root", "nodes": {"root": {"inputs": {"shared": "shared"}},
                "shared": {"original": {"type": "path", "path": "../shared"}}}}
        (self.flake / "flake.lock").write_text(json.dumps(lock))
        with patch.object(core, "checked_cmd") as command:
            with self.assertRaisesRegex(RuntimeError, "self-contained flake"):
                core.cmd_update(argparse.Namespace())
        command.assert_not_called()
        report = json.loads(Path(core.state_file("update")).read_text())
        self.assertEqual(report["status"], "error")
        self.assertFalse(report["build_verified"])

    def test_update_snapshot_excludes_git_metadata(self):
        (self.flake / ".git").mkdir()
        (self.flake / ".git" / "index").write_text("index")
        with patch.object(core, "checked_cmd", side_effect=RuntimeError("stop before build")):
            with self.assertRaises(RuntimeError):
                core.cmd_update(argparse.Namespace())
        self.assertFalse((self.worktree / ".git").exists())
        self.assertTrue((self.worktree / "flake.nix").exists())

    def test_failed_build_candidate_cannot_be_applied(self):
        report, _, _ = self.candidate()
        report["status"] = "error"
        core.atomic_json(core.state_file("update"), report)
        with self.assertRaisesRegex(RuntimeError, "unused verified"):
            core.read_candidate()

    def test_config_changed_since_build_rejected(self):
        self.candidate()
        (self.flake / "flake.nix").write_text("{ changed = true; }")
        with self.assertRaisesRegex(RuntimeError, "Configuration changed"):
            core.read_candidate()

    def test_candidate_lock_changed_rejected(self):
        self.candidate()
        (self.worktree / "flake.lock").write_text("changed")
        with self.assertRaisesRegex(RuntimeError, "Candidate lock changed"):
            core.read_candidate()

    def test_candidate_for_other_host_rejected(self):
        report, _, _ = self.candidate()
        report["host"] = "other-host"
        core.atomic_json(core.state_file("update"), report)
        with self.assertRaisesRegex(RuntimeError, "different configuration"):
            core.read_candidate()

    def test_missing_candidate_closure_rejected(self):
        self.candidate()
        with self.assertRaisesRegex(RuntimeError, "missing or changed"):
            core.read_candidate()

    def test_activation_failure_restores_lock_and_profile(self):
        report, lock, candidate = self.candidate()
        previous = "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-old-system"
        self.profile.symlink_to(previous)
        commands = []
        def execute(command, **kwargs):
            commands.append(command)
            if command == [candidate + "/bin/switch-to-configuration", "switch"]:
                raise RuntimeError("activation failed")
            return ""
        with patch.object(core, "read_candidate", return_value=(report, lock, candidate)), patch.object(core, "checked_cmd", side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, "activation failed"):
                core.cmd_apply_update(argparse.Namespace())
        self.assertEqual((self.flake / "flake.lock").read_text(), '{"nodes":{}}')
        self.assertIn(["nix-env", "--profile", str(self.profile), "--set", previous], commands)
        self.assertIn([previous + "/bin/switch-to-configuration", "switch"], commands)
        saved = json.loads(Path(core.state_file("update")).read_text())
        self.assertEqual(saved["status"], "apply-failed")
        self.assertEqual(saved["recovery_errors"], [])

    def test_successful_activation_uses_exact_built_system_and_consumes_candidate(self):
        report, lock, candidate = self.candidate()
        with patch.object(core, "read_candidate", return_value=(report, lock, candidate)), patch.object(core, "checked_cmd", return_value="") as command:
            self.assertEqual(core.cmd_apply_update(argparse.Namespace()), 0)
        self.assertEqual(command.call_args_list[-1].args[0], [candidate + "/bin/switch-to-configuration", "switch"])
        self.assertEqual((self.flake / "flake.lock").read_text(), Path(lock).read_text())
        saved = json.loads(Path(core.state_file("update")).read_text())
        self.assertEqual(saved["status"], "applied")
        with self.assertRaisesRegex(RuntimeError, "unused verified"):
            core.read_candidate()

    def test_second_maintenance_operation_rejected(self):
        with core.maintenance_lock():
            with self.assertRaisesRegex(RuntimeError, "Another nixcare"):
                with core.maintenance_lock():
                    self.fail("concurrent maintenance entered")

    def test_atomic_lock_replacement_keeps_permissions(self):
        destination = self.flake / "flake.lock"
        destination.chmod(0o640)
        source = self.root / "new.lock"
        source.write_text("updated")
        core.copy_lock_atomic(source, destination)
        self.assertEqual(destination.read_text(), "updated")
        self.assertEqual(destination.stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
