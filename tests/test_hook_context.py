"""Check the fault hook's cluster context without contacting Kubernetes."""
from contextlib import nullcontext, redirect_stderr
import importlib.util
import io
from pathlib import Path
import sys
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
        run_hook.assert_called_once_with("/path/custom-hook", "t1", "target-cluster")


if __name__ == "__main__":
    unittest.main()
