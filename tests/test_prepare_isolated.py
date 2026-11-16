"""Check isolated fixture generation and the POSIX investigation hook contract."""
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
NAMESPACE = "incident-eval-test123"


class IsolatedFixtureTests(unittest.TestCase):
    def test_fixture_and_hook_are_usable(self):
        with tempfile.TemporaryDirectory(prefix="incident-fixture-test-") as temporary:
            output = Path(temporary) / "fixture"
            command = [sys.executable, str(ROOT / "scripts/prepare-isolated.py"),
                       "--namespace", NAMESPACE, "--node", "k8s-master", "--output", str(output)]
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(result.returncode, 0, result.stderr)
            hook = output / "scripts/investigate.sh"
            self.assertIn(NAMESPACE, hook.read_text(encoding="utf-8"))

            # Re-running must not overwrite the existing fixture.
            before = hook.read_bytes()
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(hook.read_bytes(), before)
            if os.name != "posix":
                return  # Windows cannot represent a portable POSIX executable bit.

            self.assertTrue(hook.stat().st_mode & stat.S_IXUSR)
            self.assertTrue(os.access(hook, os.X_OK))
            handoff = output / "scripts/wait-investigation.sh"
            self.assertTrue(handoff.stat().st_mode & stat.S_IXUSR)
            self.assertTrue(os.access(handoff, os.X_OK))
            spec = importlib.util.spec_from_file_location("fixture_reproduce", output / "scripts/reproduce.py")
            reproduce = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(reproduce)
            argv = ["reproduce.py", "--context", "unused", "--case", "t1", "--on-fault", str(hook)]
            with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {"LAB_CONTEXT": "unused"}), \
                 mock.patch.object(reproduce, "Lab") as lab, \
                 mock.patch.object(reproduce.signal, "signal"):
                self.assertEqual(reproduce.main(), 0)
                lab.return_value.reproduce.assert_called_once_with("t1", str(hook))


if __name__ == "__main__":
    unittest.main()
