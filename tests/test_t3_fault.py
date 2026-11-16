"""Verify T3's observable failure and evidence contract without a cluster."""
from contextlib import nullcontext, redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("incident_reproduce_t3", ROOT / "scripts/reproduce.py")
reproduce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reproduce)


def poll_once(check, message, seconds=30):
    result = check()
    if not result:
        raise RuntimeError(message)
    return result


class T3EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.lab = reproduce.Lab("test-context")

    def gateway_log(self, run_id, port=81):
        return (f'[error] request: "GET /api/?reproduction={run_id} HTTP/1.1", '
                f'upstream: "http://192.0.2.10:{port}/?reproduction={run_id}"')

    def test_gateway_failure_accepts_502_or_504_with_matching_port_evidence(self):
        for response in (502, 504):
            with self.subTest(response=response), \
                 mock.patch.object(self.lab, "restart_gateway"), \
                 mock.patch.object(self.lab, "forward", return_value=nullcontext(12345)), \
                 mock.patch.object(self.lab, "status", side_effect=[200, response]) as status, \
                 mock.patch.object(self.lab, "kubectl", return_value=self.gateway_log(self.lab.run_id)), \
                 mock.patch.object(reproduce, "poll", side_effect=poll_once), \
                 redirect_stdout(io.StringIO()) as output:
                self.lab.check_fault("t3")
            self.assertEqual(status.call_args_list, [
                mock.call(12345, "/healthz", (200,)),
                mock.call(12345, f"/api/?reproduction={self.lab.run_id}", (502, 504)),
            ])
            self.assertIn(f"/api/={response}", output.getvalue())
            self.assertIn("Nginx evidence shows upstream port 81", output.getvalue())

    def test_gateway_error_without_matching_port_is_not_accepted(self):
        for log in (self.gateway_log(self.lab.run_id, port=80), self.gateway_log("another-run")):
            with self.subTest(log=log), \
                 mock.patch.object(self.lab, "restart_gateway"), \
                 mock.patch.object(self.lab, "forward", return_value=nullcontext(12345)), \
                 mock.patch.object(self.lab, "status", side_effect=[200, 504]), \
                 mock.patch.object(self.lab, "kubectl", return_value=log), \
                 mock.patch.object(reproduce, "poll", side_effect=poll_once):
                with self.assertRaisesRegex(RuntimeError, "did not log an upstream port 81"):
                    self.lab.check_fault("t3")

    def test_timeout_reports_status_and_safe_gateway_diagnostics(self):
        with mock.patch.object(self.lab, "restart_gateway"), \
             mock.patch.object(self.lab, "forward", return_value=nullcontext(12345)), \
             mock.patch.object(self.lab, "status", side_effect=[200, RuntimeError("last observed: HTTP 200")]), \
             mock.patch.object(self.lab, "kubectl", return_value=self.gateway_log(self.lab.run_id, port=80)), \
             redirect_stderr(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, "last observed: HTTP 200"):
                self.lab.check_fault("t3")
        self.assertIn("matching request=True, port 81 evidence=False", output.getvalue())
        self.assertNotIn("192.0.2.10", output.getvalue())

    def test_status_timeout_reports_last_response(self):
        connection = mock.Mock()
        connection.getresponse.return_value.status = 200
        with mock.patch.object(reproduce, "HTTPConnection", return_value=connection), \
             mock.patch.object(reproduce, "poll", side_effect=poll_once):
            with self.assertRaisesRegex(RuntimeError, "last observed: HTTP 200"):
                self.lab.status(12345, "/api/", (502, 504))
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
