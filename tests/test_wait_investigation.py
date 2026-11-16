"""Check the bounded investigation handoff without touching Kubernetes."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name == "posix", "The handoff script requires POSIX Bash")
class InvestigationHandoffTests(unittest.TestCase):
    def test_ready_release_and_stale_signal_rejection(self):
        with tempfile.TemporaryDirectory(prefix="incident-handoff-test-") as directory:
            env = {**os.environ, "BRIDGE_SIGNAL_DIR": directory}
            command = ["bash", str(ROOT / "scripts/wait-investigation.sh"), "t1"]
            process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                ready = Path(directory) / "t1.ready"
                deadline = time.monotonic() + 5
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), "The hook did not announce readiness")
                (Path(directory) / "t1.done").touch()
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
                self.assertIn(b"READY_FOR_BRIDGE: t1", stdout)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            retry = subprocess.run(command, env=env, capture_output=True, timeout=5)
            self.assertNotEqual(retry.returncode, 0)
            self.assertIn(b"Signal files already exist", retry.stderr)


if __name__ == "__main__":
    unittest.main()
