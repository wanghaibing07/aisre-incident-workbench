"""Offline checks for the parameterized Windows-to-lab read-only bridge."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("windows_holmes_bridge", ROOT / "scripts/windows-holmes-bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class WindowsBridgeTests(unittest.TestCase):
    def params(self, root):
        files = {}
        for name in ("identity", "known_hosts", "ssh"):
            path = root / (name + ".test")
            path.write_text("fixture", encoding="utf-8")
            files[name] = str(path.resolve())
        return {**files, "namespace": "incident-eval-test123", "host": "192.0.2.4", "user": "labuser",
                "context": "lab-context", "remote_fixture": "/tmp/incident-eval-test123"}

    def test_fixed_commands_credentials_and_pod_redaction(self):
        with tempfile.TemporaryDirectory(prefix="bridge-offline-test-") as name:
            params = self.params(Path(name))
            payload = {"items": [{"metadata": {"name": "demo", "labels": {"private": "not output"}},
                                  "status": {"phase": "Running"}}]}
            response = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sensitive-model-token", "PROGRAMDATA": "system-program-data"}), \
                 mock.patch.object(bridge.subprocess, "run", return_value=response) as run:
                output = json.loads(bridge.read_remote("pods", params))
            self.assertEqual(output, {"namespace": params["namespace"], "count": 1,
                                      "pods": [{"name": "demo", "phase": "Running"}]})
            command = run.call_args.args[0]
            self.assertEqual(command[-2], "labuser@192.0.2.4")
            self.assertEqual(shlex.split(command[-1]), [
                "bash", "/tmp/incident-eval-test123/scripts/agent-kubeconfig.sh", "--context", "lab-context", "--",
                "kubectl", "--request-timeout=10s", "-n", "incident-eval-test123", "get", "pods", "-o", "json"])
            self.assertNotIn("OPENAI_API_KEY", run.call_args.kwargs["env"])
            self.assertEqual(run.call_args.kwargs["env"]["PROGRAMDATA"], "system-program-data")
            self.assertNotIn("sensitive-model-token", json.dumps(command))
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("IdentitiesOnly=yes", command)

    def test_only_three_fixed_operations_and_invalid_input_rejected(self):
        with tempfile.TemporaryDirectory(prefix="bridge-offline-test-") as name:
            original = self.params(Path(name))
            with mock.patch.object(bridge.subprocess, "run") as run:
                for key, bad in (("namespace", "default"), ("namespace", "incident-eval-test123;id"),
                                 ("host", "-oProxyCommand=bad"), ("context", "lab;id"),
                                 ("remote_fixture", "/tmp/../etc")):
                    params = {**original, key: bad}
                    with self.assertRaises(ValueError):
                        bridge.read_remote("pods", params)
                with self.assertRaises(ValueError):
                    bridge.read_remote("delete", original)
                run.assert_not_called()
            self.assertEqual(set(bridge.REMOTE), {"pods", "routes", "logs"})

    def test_remote_failure_does_not_include_sensitive_stderr(self):
        with tempfile.TemporaryDirectory(prefix="bridge-offline-test-") as name:
            params = self.params(Path(name))
            result = subprocess.CompletedProcess([], 1, "", "sensitive remote output")
            with mock.patch.object(bridge.subprocess, "run", return_value=result):
                with self.assertRaisesRegex(RuntimeError, "Restricted Kubernetes read failed") as error:
                    bridge.read_remote("routes", params)
                self.assertNotIn("sensitive remote output", str(error.exception))


if __name__ == "__main__":
    unittest.main()
