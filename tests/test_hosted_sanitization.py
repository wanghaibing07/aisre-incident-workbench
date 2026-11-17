"""Verify the artifact scrubber on wrapped synthetic addresses and test secrets."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class HostedSanitizationTests(unittest.TestCase):
    def test_wrapped_ip_and_model_key_do_not_leave_raw_values(self):
        script = (ROOT / "scripts/hosted-real-model.sh").read_text(encoding="utf-8")
        start = script.index("  python3 - \"$private\" \"$evidence\" \"$key_file\" <<'PY' || true\n")
        start = script.index("\n", start) + 1
        end = script.index("\nPY\n", start)
        scrubber = script[start:end]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            private, output = base / "private", base / "out"
            private.mkdir()
            output.mkdir()
            key = "synthetic-key-do-not-publish-12345"
            (private / "model-key").write_text(key, encoding="utf-8")
            wrapped = "10.24\n4.0.6"
            normal = "192.0.2.10"
            independent_metrics = "1.2\n3.4\n5.6"
            (private / "t3.log").write_bytes((
                f"upstream {wrapped}:81\nservice {normal}:80\nBearer {key}\n"
                "-----BEGIN PRIVATE KEY-----\nSYNTHETIC\n-----END PRIVATE KEY-----\n"
                f"diagnostic evidence retained\n{independent_metrics}\n").encode("utf-8"))
            subprocess.run([sys.executable, "-c", scrubber, str(private), str(output), str(private / "model-key")],
                           check=True, timeout=10)
            result = (output / "t3.sanitized.log").read_text(encoding="utf-8")
            self.assertNotIn(wrapped, result)
            self.assertNotIn(normal, result)
            self.assertNotIn(key, result)
            self.assertNotIn("SYNTHETIC", result)
            self.assertEqual(result.count("[synthetic-ip]"), 2)
            self.assertIn("diagnostic evidence retained", result)
            self.assertIn(":81", result)
            self.assertIn(":80", result)
            self.assertIn(independent_metrics, result)


if __name__ == "__main__":
    unittest.main()
