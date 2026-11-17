"""Exercise the shipped fixed-namespace route tool with synthetic kubectl data."""
import contextlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import textwrap
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent


def route_program(namespace="incident-lab"):
    text = (ROOT / "toolsets/incident-lab-readonly.yaml").read_text(encoding="utf-8")
    if namespace != "incident-lab":
        text = text.replace("incident-lab", namespace)
    section = text.split("    - name: incident_lab_service_route_evidence\n", 1)[1]
    section = section.split("    - name: incident_lab_gateway_error_logs\n", 1)[0]
    command = textwrap.dedent(section.split("      command: |\n", 1)[1])
    argv = shlex.split(command)
    if len(argv) != 3 or argv[:2] != ["python3", "-c"]:
        raise AssertionError("Unexpected route tool command")
    return argv[2]


def run_tool(payload, namespace="incident-lab", *, failure=None):
    output = io.StringIO()
    fake = SimpleNamespace(stdout=payload)
    with mock.patch("subprocess.run", side_effect=failure, return_value=fake) as run, \
         contextlib.redirect_stdout(output):
        exec(compile(route_program(namespace), "<route-tool>", "exec"), {})
    run.assert_called_once_with(
        ["kubectl", "--request-timeout=10s", "-n", namespace, "get", "pods,services,endpointslices", "-o", "json"],
        stdout=subprocess.PIPE, text=True, check=True,
    )
    return json.loads(output.getvalue()), output.getvalue()


class RouteEvidenceTests(unittest.TestCase):
    def test_preserves_all_route_evidence_and_discards_unrelated_metadata(self):
        noise = "unrelated-managed-fields-" * 2000
        resources = {"items": [
            {"kind": "Pod", "metadata": {"name": "web-1", "labels": {"app": "web", "revision": "a"}, "managedFields": noise},
             "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}},
            {"kind": "Pod", "metadata": {"name": "web-2", "labels": {"app": "web", "revision": "b"}},
             "status": {"phase": "Pending", "conditions": [{"type": "Ready", "status": "False"}]}},
            {"kind": "Service", "metadata": {"name": "web"}, "spec": {"selector": {"app": "web"},
                "type": "ClusterIP", "clusterIP": "192.0.2.80", "clusterIPs": ["192.0.2.80", "2001:db8::80"], "ports": [
                {"name": "http", "port": 80, "targetPort": 8080, "protocol": "TCP"},
                {"name": "metrics", "port": 9090, "targetPort": "metrics", "protocol": "TCP"}]}},
            {"kind": "Service", "metadata": {"name": "other"}, "spec": {"selector": {"app": "other"},
                "type": "ClusterIP", "clusterIP": "192.0.2.81", "clusterIPs": ["192.0.2.81"], "ports": [{"port": 81}]}},
            {"kind": "EndpointSlice", "metadata": {"name": "web-a", "labels": {"kubernetes.io/service-name": "web"}},
             "addressType": "IPv4", "ports": [{"name": "http", "port": 8080, "protocol": "TCP"}, {"name": "metrics", "port": 9090}],
             "endpoints": [{"addresses": ["192.0.2.10"], "conditions": {"ready": False, "serving": True, "terminating": True},
                            "targetRef": {"kind": "Pod", "namespace": "incident-lab", "name": "web-1"}}]},
            {"kind": "EndpointSlice", "metadata": {"name": "web-b", "labels": {"kubernetes.io/service-name": "web"}},
             "addressType": "IPv6", "ports": [{"port": 8080}],
             "endpoints": [{"addresses": ["2001:db8::1"], "conditions": {"ready": True}}]},
        ]}
        result, serialized = run_tool(json.dumps(resources))
        self.assertEqual(result["namespace"], "incident-lab")
        self.assertEqual([(p["name"], p["labels"]["revision"], p["ready"]) for p in result["pods"]],
                         [("web-1", "a", "True"), ("web-2", "b", "False")])
        self.assertEqual([(s["name"], s["selector"]) for s in result["services"]],
                         [("web", {"app": "web"}), ("other", {"app": "other"})])
        self.assertEqual([p["targetPort"] for p in result["services"][0]["ports"]], [8080, "metrics"])
        self.assertEqual([(s["type"], s["cluster_ip"], s["cluster_ips"]) for s in result["services"]],
                         [("ClusterIP", "192.0.2.80", ["192.0.2.80", "2001:db8::80"]),
                          ("ClusterIP", "192.0.2.81", ["192.0.2.81"])])
        analysis = result["services"][0]["selector_analysis"]
        self.assertEqual(analysis["known_matching_pod_names"], ["web-1", "web-2"])
        self.assertEqual(analysis["matching_pod_count"], 2)
        self.assertTrue(analysis["complete"])
        self.assertEqual(result["services"][1]["selector_analysis"]["matching_pod_count"], 0)
        self.assertIn("does not establish readiness", analysis["scope"])
        slices = result["endpoint_slices"]
        self.assertEqual([s["name"] for s in slices], ["web-a", "web-b"])
        self.assertEqual(slices[0]["labels"]["kubernetes.io/service-name"], "web")
        self.assertEqual([p["port"] for p in slices[0]["ports"]], [8080, 9090])
        self.assertEqual(slices[0]["endpoints"][0]["conditions"], {"ready": False, "serving": True, "terminating": True})
        self.assertEqual(slices[1]["endpoints"][0]["addresses"], ["2001:db8::1"])
        self.assertNotIn("unrelated-managed-fields", serialized)
        self.assertLess(len(serialized), 4000)

    def test_missing_fields_stay_unknown_and_isolated_namespace_is_used(self):
        result, _ = run_tool(json.dumps({"items": [
            {"kind": "Pod", "metadata": {"name": "p"}},
            {"kind": "Service", "metadata": {"name": "external"}, "spec": {}},
            {"kind": "EndpointSlice", "metadata": {"name": "slice"}, "endpoints": [{"addresses": ["192.0.2.1"]}]},
        ]}), "incident-eval-test123")
        self.assertEqual(result["namespace"], "incident-eval-test123")
        self.assertIsNone(result["pods"][0]["ready"])
        self.assertIsNone(result["pods"][0]["phase"])
        self.assertIsNone(result["services"][0]["selector"])
        self.assertIsNone(result["services"][0]["ports"])
        self.assertIsNone(result["services"][0]["cluster_ip"])
        self.assertIsNone(result["services"][0]["selector_analysis"])
        self.assertIsNone(result["endpoint_slices"][0]["endpoints"][0]["conditions"])

    def test_selector_analysis_preserves_unknowns_and_checks_all_labels(self):
        resources = {"items": [
            {"kind": "Service", "metadata": {"name": "web"}, "spec": {"selector": {"app": "web", "tier": "api"}}},
            {"kind": "Service", "metadata": {"name": "missing"}, "spec": {}},
            {"kind": "Service", "metadata": {"name": "empty"}, "spec": {"selector": {}}},
            {"kind": "Service", "metadata": {"name": "invalid"}, "spec": {"selector": {"app": 123}}},
            {"kind": "Pod", "metadata": {"name": "exact", "labels": {"app": "web", "tier": "api", "version": "v2"}}},
            {"kind": "Pod", "metadata": {"name": "partial", "labels": {"app": "web", "tier": "worker"}}},
            {"kind": "Pod", "metadata": {"name": "no-labels"}},
            {"kind": "Pod", "metadata": {"labels": {"app": "web", "tier": "api"}}},
        ]}
        result, _ = run_tool(json.dumps(resources))
        analysis = result["services"][0]["selector_analysis"]
        self.assertEqual(analysis["known_matching_pod_names"], ["exact"])
        self.assertIsNone(analysis["matching_pod_count"])
        self.assertEqual(analysis["unknown_pod_count"], 2)
        self.assertFalse(analysis["complete"])
        self.assertTrue(all(service["selector_analysis"] is None for service in result["services"][1:]))

    def test_rejects_bad_or_truncated_data_and_propagates_kubectl_failure(self):
        for invalid in ('{"items":[', '{"items":{}}', '{"items":[{"kind":"Secret"}]}',
                        '{"metadata":{"continue":"next-page"},"items":[]}'):
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, json.JSONDecodeError)):
                run_tool(invalid)
        with self.assertRaises(subprocess.CalledProcessError):
            run_tool("", failure=subprocess.CalledProcessError(1, "kubectl"))


if __name__ == "__main__":
    unittest.main()
