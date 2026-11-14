"""Opt-in tests against real Git and Nix: NIXCARE_INTEGRATION=1 python -m unittest
discover -s test -p test_portable_integration.py -v

Kept separate from sandboxed unit checks because Nix needs a writable store.
All source fixtures and output locks are temporary.
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "portable_nixcare", Path(__file__).resolve().parents[1] / "cli/nixcare.py",
)
nc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nc)


@unittest.skipUnless(
    os.environ.get("NIXCARE_INTEGRATION") == "1",
    "Set NIXCARE_INTEGRATION=1 to run real Nix integration tests",
)
class PortableIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(shutil.which("git"), "Integration tests require Git")
        self.assertIsNotNone(shutil.which("nix"), "Integration tests require Nix")
        temporary = tempfile.TemporaryDirectory(prefix="nixcare-integration-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def run_tool(self, argv):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_git_subdirectory_flake_keeps_sibling_input_context(self):
        repository = self.root / "repo"
        config, shared = repository / "config", repository / "shared"
        config.mkdir(parents=True)
        shared.mkdir()
        (shared / "flake.nix").write_text("{ outputs = _: {}; }")
        (config / "flake.nix").write_text(
            '{ inputs.shared.url = "../shared"; outputs = _: {}; }',
        )
        self.run_tool(["git", "init", "-q", str(repository)])
        self.run_tool(["git", "-C", str(repository), "add", "."])
        self.run_tool(["nix", *nc.NIX_FEATURES, "flake", "lock", str(config)])
        self.run_tool(["git", "-C", str(repository), "add", "."])
        original = (config / "flake.lock").read_bytes()
        report = nc.check_updates(config)
        self.assertEqual(report["status"], "current", json.dumps(report, indent=2))
        self.assertFalse(report["build_verified"])
        self.assertEqual((config / "flake.lock").read_bytes(), original)

    def test_non_git_flake_detects_changed_input_without_editing_source(self):
        config, dependency = self.root / "config", self.root / "dependency"
        config.mkdir()
        dependency.mkdir()
        (dependency / "flake.nix").write_text("{ outputs = _: { value = 1; }; }")
        (config / "flake.nix").write_text(
            '{ inputs.dep.url = "path:' + str(dependency) + '"; outputs = _: {}; }',
        )
        self.run_tool(["nix", *nc.NIX_FEATURES, "flake", "lock", str(config)])
        original = (config / "flake.lock").read_bytes()
        (dependency / "flake.nix").write_text("{ outputs = _: { value = 2; }; }")
        report = nc.check_updates(config)
        self.assertEqual(report["status"], "updates-available", json.dumps(report, indent=2))
        self.assertEqual(report["changes"][0]["input"], "dep")
        self.assertEqual((config / "flake.lock").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
