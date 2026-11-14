import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "modules"))
import schedule_guard as guard


class ScheduleGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)
        (self.proc / "pressure").mkdir()
        self.memory(available=8000)
        (self.proc / "loadavg").write_text("0.5 0.5 0.5 1/100 1\n")
        self.pressure("memory", 0)
        self.pressure("io", 0)

    def memory(self, available):
        (self.proc / "meminfo").write_text(f"MemTotal: 16000 kB\nMemAvailable: {available} kB\n")

    def pressure(self, kind, value):
        (self.proc / "pressure" / kind).write_text(f"some avg60=0 total=0\nfull avg10=0 avg60={value} avg300=0 total=0\n")

    def reasons(self):
        return guard.busy_reasons(self.proc, cpu_count=4)

    def test_ready_system_and_kernels_without_psi_can_run(self):
        self.assertEqual(self.reasons(), [])
        (self.proc / "pressure" / "memory").unlink()
        (self.proc / "pressure" / "io").unlink()
        self.assertEqual(self.reasons(), [])

    def test_memory_pressure_defers_even_when_cpu_idle(self):
        self.memory(available=1000)
        self.assertIn("memory is available", self.reasons()[0])
        self.memory(available=8000)
        self.pressure("memory", 3)
        self.assertIn("memory stalls", self.reasons()[0])

    def test_cpu_and_io_contention_defer(self):
        (self.proc / "loadavg").write_text("4.0 0.5 0.5 1/100 1\n")
        self.pressure("io", 12)
        self.assertEqual(len(self.reasons()), 2)

    def test_limits_are_explicit_and_memory_limit_is_inclusive(self):
        self.memory(available=2400)
        self.assertEqual(self.reasons(), [])
        self.pressure("memory", 2)
        self.assertTrue(self.reasons())

    def test_invalid_readings_are_errors_instead_of_success_or_retry(self):
        for value in ("nan", "-1", "inf"):
            (self.proc / "loadavg").write_text(value)
            with self.assertRaises(ValueError):
                self.reasons()
        with patch.object(guard, "busy_reasons", side_effect=ValueError("bad")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(guard.main(), 1)

    def test_only_actual_busy_state_returns_temporary_exit(self):
        with patch.object(guard, "busy_reasons", return_value=["busy"]), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(guard.main(), 75)
        with patch.object(guard, "busy_reasons", return_value=[]):
            self.assertEqual(guard.main(), 0)


if __name__ == "__main__":
    unittest.main()
