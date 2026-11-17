"""Exercise the fixed read-only gateway tool with synthetic Kubernetes data."""
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


def gateway_program(namespace="incident-lab"):
    text = (ROOT / "toolsets/incident-lab-readonly.yaml").read_text(encoding="utf-8")
    if namespace != "incident-lab":
        text = text.replace("incident-lab", namespace)
    section = text.split("    - name: incident_lab_gateway_error_logs\n", 1)[1]
    command = textwrap.dedent(section.split("      command: |\n", 1)[1])
    argv = shlex.split(command)
    if len(argv) != 3 or argv[:2] != ["python3", "-c"]:
        raise AssertionError("Unexpected gateway tool command")
    return argv[2]


def service(name, ip, ports, *, ips=None, service_type="ClusterIP"):
    spec = {"type": service_type, "clusterIP": ip, "ports": ports}
    if ips is not None:
        spec["clusterIPs"] = ips
    return {"kind": "Service", "metadata": {"name": name}, "spec": spec}


def log(url):
    return f'2026-01-01T00:00:00Z [error] connection failed, upstream: "{url}", host: "gateway"\n'


def run_tool(logs, data, namespace="incident-lab"):
    output = io.StringIO()
    responses = [SimpleNamespace(stdout=logs), SimpleNamespace(stdout=json.dumps(data))]
    with mock.patch("subprocess.run", side_effect=responses) as run, contextlib.redirect_stdout(output):
        exec(compile(gateway_program(namespace), "<gateway-tool>", "exec"), {})
    common = {"stdout": subprocess.PIPE, "text": True, "encoding": "utf-8", "errors": "strict", "check": True, "timeout": 20}
    assert run.call_args_list == [
        mock.call(["kubectl", "--request-timeout=10s", "-n", namespace, "logs", "deployment/t3-gateway", "--since=15m", "--tail=80", "--timestamps"], **common),
        mock.call(["kubectl", "--request-timeout=10s", "-n", namespace, "get", "services", "-o", "json"], **common),
    ]
    prefix, derived = output.getvalue().split("Current Service correlation (JSON):\n", 1)
    return prefix, json.loads(derived)


class GatewayEvidenceTests(unittest.TestCase):
    def test_distinguishes_service_port_from_target_port_and_preserves_log(self):
        logs = log("http://192.0.2.80:81/path?secret=synthetic-query") + log("http://192.0.2.81:443/")
        data = {"items": [
            service("web", "192.0.2.80", [{"port": 80, "targetPort": 81, "protocol": "TCP"}]),
            service("other", "192.0.2.81", [{"port": 443, "targetPort": 8443, "protocol": "TCP"}]),
        ]}
        raw, result = run_tool(logs, data)
        self.assertEqual(raw, "Historical gateway log (last 15 minutes, up to 80 lines):\n" + logs)
        self.assertEqual(result["upstreams"], [
            {"ip": "192.0.2.80", "port": 81, "status": "matched", "service": "web", "current_tcp_service_ports": [80], "port_comparison": "differs_from_current_tcp_service_ports"},
            {"ip": "192.0.2.81", "port": 443, "status": "matched", "service": "other", "current_tcp_service_ports": [443], "port_comparison": "equals_current_tcp_service_port"},
        ])
        self.assertNotIn("synthetic-query", json.dumps(result))
        self.assertIn("historical", result["caveat"])
        self.assertIn("one deployment Pod", result["caveat"])
        self.assertIn("does not prove a root cause", result["caveat"])

    def test_ipv6_multistack_and_multiple_tcp_ports(self):
        data = {"items": [service("dual", "192.0.2.10", [
            {"port": 80, "protocol": "TCP"}, {"port": 443, "protocol": "TCP"}, {"port": 53, "protocol": "UDP"},
        ], ips=["192.0.2.10", "2001:db8::1"])]}
        _, result = run_tool(log("https://[2001:db8::1]:443/a?q=hidden"), data, "incident-eval-test123")
        self.assertEqual(result["namespace"], "incident-eval-test123")
        self.assertEqual(result["upstreams"][0]["ip"], "2001:db8::1")
        self.assertEqual(result["upstreams"][0]["current_tcp_service_ports"], [80, 443])
        self.assertEqual(result["upstreams"][0]["port_comparison"], "equals_current_tcp_service_port")
        self.assertNotIn("hidden", json.dumps(result))

    def test_domains_missing_ports_and_non_ip_upstreams_are_unknown(self):
        logs = "".join(log(value) for value in ("http://example.invalid:80/a", "http://192.0.2.1/path", "-", "http://[2001:db8::1]/"))
        _, result = run_tool(logs, {"items": []})
        self.assertEqual([entry["status"] for entry in result["upstreams"]], ["unknown"] * 4)
        self.assertTrue(all(entry["reason"] == "upstream_not_explicit_http_ip_and_port" for entry in result["upstreams"]))
        _, result = run_tool(log("http://192.0.2.1:80/, http://192.0.2.2:80/"), {"items": []})
        self.assertEqual(result["upstreams"][0]["reason"], "multiple_or_noncanonical_upstream_values")

    def test_request_query_and_quoted_text_do_not_create_upstream_fields(self):
        access = '2026-01-01 access "GET /api/?q=upstream:foo HTTP/1.1" 200\n'
        error = (r'2026-01-01 [error] failed, request: "GET /api/?q=upstream:foo&v=\"fake, upstream: nope\" HTTP/1.1", '
                 r'upstream: "http://192.0.2.80:81/path?private=synthetic", host: "example, upstream: ignored"' + '\n')
        _, only_access = run_tool(access, {"items": []})
        self.assertEqual(only_access["upstreams"], [])
        raw, result = run_tool(access + error, {"items": [
            service("web", "192.0.2.80", [{"port": 80, "protocol": "TCP"}]),
        ]})
        self.assertEqual(raw, "Historical gateway log (last 15 minutes, up to 80 lines):\n" + access + error)
        self.assertEqual(len(result["upstreams"]), 1)
        self.assertEqual(result["upstreams"][0]["port_comparison"], "differs_from_current_tcp_service_ports")
        self.assertNotIn("synthetic", json.dumps(result))

    def test_escaped_backslashes_in_other_fields_and_upstream_values(self):
        line = (r'2026-01-01 [error], request: "GET /api/?path=abc\\def HTTP/1.1", '
                r'upstream: "http://192.0.2.1:80/", host: "gateway"' + '\n')
        _, result = run_tool(line, {"items": [service("a", "192.0.2.1", [{"port": 80, "protocol": "TCP"}])]})
        self.assertEqual(result["upstreams"][0]["status"], "matched")
        _, result = run_tool(log(r'http://192.0.2.1:80/path\x22unsafe'), {"items": []})
        self.assertEqual(result["upstreams"][0]["status"], "unknown")

    def test_ambiguous_and_incomplete_service_addresses_are_not_claimed_unique(self):
        logs = log("http://192.0.2.1:81/")
        tcp = [{"port": 80, "protocol": "TCP"}]
        _, result = run_tool(logs, {"items": [service("a", "192.0.2.1", tcp), service("b", "192.0.2.1", tcp)]})
        self.assertEqual(result["upstreams"][0]["status"], "ambiguous")
        _, result = run_tool(logs, {"items": [service("a", "192.0.2.1", tcp), service("pending", None, tcp)]})
        self.assertEqual(result["upstreams"][0]["reason"], "incomplete_service_addresses")
        _, result = run_tool(logs, {"items": [service("headless", "None", tcp, ips=["None"]),
                                              service("external", None, None, service_type="ExternalName")]})
        self.assertEqual(result["upstreams"][0]["reason"], "no_current_service_ip_match")

    def test_missing_ports_protocols_and_udp_only_remain_unknown(self):
        logs = log("http://192.0.2.1:81/")
        for ports, reason in [(None, "incomplete_service_ports_or_protocols"),
                              ([], "incomplete_service_ports_or_protocols"),
                              ([{"port": 80}], "incomplete_service_ports_or_protocols"),
                              ([{"protocol": "TCP"}], "incomplete_service_ports_or_protocols"),
                              ([{"port": 80, "protocol": "TCP"}, {"port": 81}], "incomplete_service_ports_or_protocols"),
                              ([{"port": 81, "protocol": "UDP"}], "no_current_tcp_service_port")]:
            with self.subTest(ports=ports):
                _, result = run_tool(logs, {"items": [service("a", "192.0.2.1", ports)]})
                self.assertEqual(result["upstreams"][0]["status"], "unknown")
                self.assertEqual(result["upstreams"][0]["reason"], reason)

    def test_incomplete_input_and_kubectl_errors_fail_closed(self):
        for data in ({"items": {}}, {"items": [], "metadata": {"continue": "next"}},
                     {"items": [], "metadata": {"remainingItemCount": 1}},
                     {"items": [{"kind": "Service", "metadata": {"name": "x"}}]},
                     {"items": [{"kind": "Service", "metadata": {"name": "x", "namespace": "other"}, "spec": {}}]}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                run_tool(log("http://192.0.2.1:81/"), data)
        for line, reason in (
            ('2026-01-01 [error], upstream: "http://192.0.2.1:81/\n', 'incomplete_or_malformed_log_line'),
            ('2026-01-01 [error], upstream: "http://192.0.2.1:81/"', 'possibly_truncated_log_line'),
            ('2026-01-01 [error], upstream: "http://192.0.2.1:81/", upstream: "truncated\n', 'incomplete_or_malformed_log_line'),
            ('2026-01-01 [error], upstream: http://192.0.2.1:81/\n', 'incomplete_or_malformed_log_line'),
            ('2026-01-01 [error], upstream: "http://192.0.2.1:81/" trailing\n', 'incomplete_or_malformed_log_line'),
            ('2026-01-01 [error], request: "GET /?q=upstream:foo HTTP/1.1\n', 'incomplete_or_malformed_log_line'),
        ):
            with self.subTest(line=line):
                _, result = run_tool(line, {"items": []})
                self.assertEqual(result["upstreams"], [{"status": "unknown", "reason": reason}])
        for effects in ([subprocess.CalledProcessError(1, "kubectl")],
                        [SimpleNamespace(stdout=""), subprocess.CalledProcessError(1, "kubectl")],
                        [SimpleNamespace(stdout=""), SimpleNamespace(stdout='{"items":[')]):
            with self.subTest(effects=effects), mock.patch("subprocess.run", side_effect=effects), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    exec(compile(gateway_program(), "<gateway-tool>", "exec"), {})

    def test_corrupted_log_lines_do_not_hide_later_valid_evidence(self):
        malformed_request = '2026-01-01 [error], request: "GET /?q=upstream:fake HTTP/1.1\n'
        mixed_field = ('2026-01-01 [error], upstream: "http://192.0.2.1:80/", '
                       'upstream: "truncated\n')
        valid = log("http://192.0.2.1:81/path?private=synthetic")
        truncated_last = '2026-01-01 [error], upstream: "http://192.0.2.1:80/"'
        logs = malformed_request + mixed_field + valid + truncated_last
        raw, result = run_tool(logs, {"items": [
            service("a", "192.0.2.1", [{"port": 80, "protocol": "TCP"}]),
        ]})
        self.assertEqual(raw, "Historical gateway log (last 15 minutes, up to 80 lines):\n" + logs + "\n")
        entries = result["upstreams"]
        self.assertEqual([entry["status"] for entry in entries], ["unknown", "unknown", "matched", "unknown"])
        self.assertEqual(entries[0]["reason"], "incomplete_or_malformed_log_line")
        self.assertEqual(entries[1]["reason"], "incomplete_or_malformed_log_line")
        self.assertEqual(entries[2]["port_comparison"], "differs_from_current_tcp_service_ports")
        self.assertEqual(entries[3]["reason"], "possibly_truncated_log_line")
        self.assertEqual(sum(entry.get("status") == "matched" for entry in entries), 1)
        self.assertNotIn("synthetic", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
