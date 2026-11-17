"""Check the fault hook's cluster context without contacting Kubernetes."""
from contextlib import nullcontext, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("incident_reproduce", ROOT / "scripts/reproduce.py")
reproduce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reproduce)


class HookContextTests(unittest.TestCase):
    def invoke_main(self, env, hook=True):
        argv = ["reproduce.py", "--context", "target-cluster", "--case", "t1"]
        if hook:
            argv += ["--on-fault", "/path/custom-hook"]
        fake_os = SimpleNamespace(name="posix", environ=env)
        with mock.patch.object(sys, "argv", argv), mock.patch.object(reproduce, "os", fake_os), \
             mock.patch.object(reproduce.shutil, "which", return_value="/path/custom-hook"), \
             mock.patch.object(reproduce.signal, "signal"), mock.patch.object(reproduce, "Lab") as lab:
            if hook and env.get("LAB_CONTEXT") and env["LAB_CONTEXT"] != "target-cluster":
                with redirect_stderr(io.StringIO()) as error:
                    with self.assertRaises(SystemExit) as exit_code:
                        reproduce.main()
                self.assertEqual(exit_code.exception.code, 2)
                self.assertIn("LAB_CONTEXT must match --context", error.getvalue())
                lab.assert_not_called()  # No Kubernetes calls or fault injection.
            else:
                self.assertEqual(reproduce.main(), 0)
                lab.assert_called_once_with("target-cluster")
                lab.return_value.reproduce.assert_called_once_with(
                    "t1", "/path/custom-hook" if hook else None
                )

    def test_mismatched_context_rejected_before_connecting(self):
        self.invoke_main({"LAB_CONTEXT": "another-cluster"})

    def test_matching_or_missing_context_and_no_hook(self):
        self.invoke_main({"LAB_CONTEXT": "target-cluster"})
        self.invoke_main({})
        self.invoke_main({"LAB_CONTEXT": "another-cluster"}, hook=False)

    def test_custom_hook_gets_authoritative_context(self):
        fake_os = SimpleNamespace(name="posix", environ={"LAB_CONTEXT": "stale", "OTHER": "preserved"})
        process = mock.Mock()
        process.poll.return_value = 0
        with mock.patch.object(reproduce, "os", fake_os), \
             mock.patch.object(reproduce, "defer_interrupts", return_value=nullcontext([])), \
             mock.patch.object(reproduce.subprocess, "Popen", return_value=process) as popen, \
             mock.patch.object(reproduce, "stop_hook"):
            reproduce.run_fault_hook("/path/custom-hook", "t1", "target-cluster")
        self.assertEqual(popen.call_args.args[0], ["/path/custom-hook", "t1"])
        self.assertEqual(popen.call_args.kwargs["env"]["LAB_CONTEXT"], "target-cluster")
        self.assertEqual(popen.call_args.kwargs["env"]["OTHER"], "preserved")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_hook_timeout_cli_validation_happens_before_lab_creation(self):
        base = ["reproduce.py", "--context", "target-cluster", "--case", "t1"]
        cases = [
            (["--hook-timeout", "5"], "requires --on-fault"),
            (["--on-fault", "/hook", "--hook-timeout", "0"], "1 to 900"),
            (["--on-fault", "/hook", "--hook-timeout", "-1"], "1 to 900"),
            (["--on-fault", "/hook", "--hook-timeout", "901"], "1 to 900"),
            (["--on-fault", "/hook", "--hook-timeout", "1.5"], "1 to 900"),
        ]
        for arguments, message in cases:
            with self.subTest(arguments=arguments), mock.patch.object(sys, "argv", base + arguments), \
                 mock.patch.object(reproduce, "Lab") as lab, redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as exit_code:
                    reproduce.main()
                self.assertEqual(exit_code.exception.code, 2)
                self.assertIn(message, error.getvalue())
                lab.assert_not_called()

    def test_cli_accepts_explicit_bounded_timeout_and_passes_it_to_lab(self):
        base = ["reproduce.py", "--context", "target-cluster", "--case", "t1", "--on-fault", "/hook"]
        fake_os = SimpleNamespace(name="posix", environ={})
        for timeout in (1, 600, 900):
            with self.subTest(timeout=timeout), \
                 mock.patch.object(sys, "argv", base + ["--hook-timeout", str(timeout)]), \
                 mock.patch.object(reproduce, "os", fake_os), \
                 mock.patch.object(reproduce.shutil, "which", return_value="/hook"), \
                 mock.patch.object(reproduce.signal, "signal"), mock.patch.object(reproduce, "Lab") as lab:
                self.assertEqual(reproduce.main(), 0)
                lab.return_value.reproduce.assert_called_once_with("t1", "/hook", hook_timeout=timeout)

    def test_hook_timeout_stops_hook_and_removes_temporary_directory(self):
        fake_os = SimpleNamespace(name="posix", environ={})
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(reproduce, "os", fake_os), \
             mock.patch.object(reproduce, "defer_interrupts", return_value=nullcontext([])), \
             mock.patch.object(reproduce.subprocess, "Popen", return_value=process), \
             mock.patch.object(reproduce.time, "monotonic", side_effect=[10, 16]), \
             mock.patch.object(reproduce, "stop_hook") as stop, \
             mock.patch.object(reproduce, "TemporaryDirectory") as temporary:
            temporary.return_value.name = "/synthetic/scratch"
            with self.assertRaises(subprocess.TimeoutExpired):
                reproduce.run_fault_hook("/hook", "t1", "target-cluster", timeout=5)
        stop.assert_called_once_with(process, 3)
        temporary.return_value.cleanup.assert_called_once_with()

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "killpg"), "requires POSIX process groups")
    def test_real_timed_out_hook_receives_sigterm_and_scratch_is_removed(self):
        with tempfile.TemporaryDirectory(prefix="incident-hook-test-") as folder:
            root = Path(folder)
            marker = root / "marker.json"
            hook = root / "hook.py"
            hook.write_text(
                f"#!{sys.executable}\n"
                "import json, os, signal, time\n"
                "from pathlib import Path\n"
                "marker=Path(os.environ['HOOK_TEST_MARKER'])\n"
                "scratch=os.environ['TMPDIR']\n"
                "def stop(_signum, _frame):\n"
                " marker.write_text(json.dumps({'scratch': scratch, 'sigterm': True}))\n"
                " raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "while True: time.sleep(0.05)\n",
                encoding="utf-8",
            )
            hook.chmod(0o700)
            with mock.patch.dict(os.environ, {"HOOK_TEST_MARKER": str(marker)}):
                with self.assertRaises(subprocess.TimeoutExpired):
                    reproduce.run_fault_hook(str(hook), "t1", "target-cluster", timeout=2)
            recorded = json.loads(marker.read_text(encoding="utf-8"))
            self.assertTrue(recorded["sigterm"])
            self.assertFalse(Path(recorded["scratch"]).exists())

    def test_invalid_direct_timeout_rejected_before_cluster_calls(self):
        lab = reproduce.Lab("target-cluster")
        for hook, timeout in [("/hook", 0), ("/hook", 901), ("/hook", True), (None, 600), ("", 600)]:
            with self.subTest(hook=hook, timeout=timeout), mock.patch.object(lab, "rollout") as rollout:
                with self.assertRaises(ValueError):
                    lab.reproduce("t1", hook, hook_timeout=timeout)
                rollout.assert_not_called()

    def test_fault_path_passes_lab_context_to_hook(self):
        lab = reproduce.Lab("target-cluster")
        baseline = {"metadata": {"uid": "one", "resourceVersion": "1"},
                    "spec": {"selector": {"app": "t1-web"}}}
        faulted = {"metadata": {"uid": "one", "resourceVersion": "2",
                                "annotations": {reproduce.OWNER: lab.run_id}},
                   "spec": {"selector": {"app": "t1-web-fault"}}}
        with mock.patch.object(lab, "rollout"), mock.patch.object(lab, "check_healthy"), \
             mock.patch.object(lab, "check_fault"), mock.patch.object(lab, "patch"), \
             mock.patch.object(lab, "get", side_effect=[baseline, faulted]), \
             mock.patch.object(reproduce, "defer_interrupts", return_value=nullcontext([])), \
             mock.patch.object(reproduce, "run_fault_hook") as run_hook:
            lab.reproduce("t1", "/path/custom-hook")
        run_hook.assert_called_once_with("/path/custom-hook", "t1", "target-cluster", timeout=300)

    def test_timed_out_hook_still_restores_owned_fault(self):
        lab = reproduce.Lab("target-cluster")
        baseline = {"metadata": {"uid": "one", "resourceVersion": "1"},
                    "spec": {"selector": {"app": "t1-web"}}}
        faulted = {"metadata": {"uid": "one", "resourceVersion": "2",
                                "annotations": {reproduce.OWNER: lab.run_id}},
                   "spec": {"selector": {"app": "t1-web-fault"}}}
        with mock.patch.object(lab, "rollout"), mock.patch.object(lab, "check_healthy") as health, \
             mock.patch.object(lab, "check_fault"), mock.patch.object(lab, "patch") as patch, \
             mock.patch.object(lab, "get", side_effect=[baseline, faulted]), \
             mock.patch.object(reproduce, "defer_interrupts", return_value=nullcontext([])), \
             mock.patch.object(reproduce, "run_fault_hook", side_effect=subprocess.TimeoutExpired("/hook", 600)) as hook:
            with self.assertRaises(subprocess.TimeoutExpired):
                lab.reproduce("t1", "/hook", hook_timeout=600)
        hook.assert_called_once_with("/hook", "t1", "target-cluster", timeout=600)
        self.assertEqual(patch.call_count, 2)
        self.assertEqual(patch.call_args_list[1].args[2:], ("/spec/selector", {"app": "t1-web"}, {}))
        self.assertEqual(health.call_args_list, [mock.call("Baseline"), mock.call("Restored")])


if __name__ == "__main__":
    unittest.main()
