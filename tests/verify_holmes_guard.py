"""Exercise the actual Holmes 0.43 entry with a local fake model endpoint.

Run with the Python environment containing HolmesGPT 0.43.0:
    python tests/verify_holmes_guard.py
No Kubernetes cluster or model API key is required.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

import yaml  # HolmesGPT dependency


ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {
    "incident_lab_pod_snapshot",
    "incident_lab_service_route_evidence",
    "incident_lab_gateway_error_logs",
}
requests = []


class FakeModel(BaseHTTPRequestHandler):
    def do_POST(self):
        requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        body = json.dumps({
            "id": "local-test", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Test complete"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    shell = (ROOT / "scripts/investigate.sh").read_text(encoding="utf-8")
    start, end = "<<'PY'\n", "\nPY"
    if shell.count(start) != 1 or shell.count(end) != 1:
        raise SystemExit("Cannot identify the guarded Python entry in investigate.sh")
    entry = shell.split(start, 1)[1].split(end, 1)[0]
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="holmes-guard-test-") as temp:
            root = Path(temp)
            entry_path = root / "guard.py"
            entry_path.write_text(entry, encoding="utf-8")
            fixture = yaml.safe_load((ROOT / "toolsets/incident-lab-readonly.yaml").read_text(encoding="utf-8"))
            config = ROOT / "toolsets/holmes-lab-config.yaml"
            custom = ROOT / "toolsets/incident-lab-readonly.yaml"

            def run_case(name, template, tools):
                case = root / name
                case.mkdir()
                home = case / "home"
                home.mkdir()
                env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP") if key in os.environ}
                env.update({
                    "HOME": str(home), "USERPROFILE": str(home), "HOLMES_CONFIGPATH_DIR": str(home),
                    "XDG_CONFIG_HOME": str(home), "XDG_CACHE_HOME": str(home), "XDG_DATA_HOME": str(home),
                    "CUSTOM_TOOLSET_LOCATION": str(case / "none.yaml"),
                    "OPENAI_API_BASE": f"http://127.0.0.1:{server.server_port}/v1", "OPENAI_API_KEY": "test",
                    "ENABLE_TELEMETRY": "false", "LITELLM_LOCAL_MODEL_COST_MAP": "True", "PYTHONUTF8": "1",
                    "NO_PROXY": "localhost,127.0.0.1,::1",
                })
                before = len(requests)
                result = subprocess.run(
                    [sys.executable, str(entry_path), str(case), str(template), str(tools),
                     "Explain the problem with available tools.", "openai/test"],
                    env=env, capture_output=True, timeout=120,
                )
                return result, requests[before:]

            result, captured = run_case("allowed", config, custom)
            assert result.returncode == 0 and len(captured) == 1, result.stderr.decode("utf-8", "replace")[-2000:]
            names = [item["function"]["name"] for item in captured[0].get("tools", [])]
            assert len(names) == 3 and set(names) == EXPECTED, names
            print("PASS: actual model request contains exactly the three allowed tools")

            fixture["toolsets"]["incident-lab/readonly"]["tools"].append({
                "name": "unexpected_tool", "description": "unexpected", "command": "echo unexpected",
            })
            extra = root / "extra.yaml"
            extra.write_text(yaml.safe_dump(fixture), encoding="utf-8")
            result, captured = run_case("extra-tool", config, extra)
            output = (result.stdout + result.stderr).decode("utf-8", "replace")
            assert result.returncode != 0 and not captured and "Unexpected Holmes tools" in output
            print("PASS: an extra tool stops execution before the model request")

            modified = root / "modified.yaml"
            modified.write_text("toolsets:\n  internet:\n    enabled: true\n", encoding="utf-8")
            result, captured = run_case("modified-template", modified, custom)
            output = (result.stdout + result.stderr).decode("utf-8", "replace")
            assert result.returncode != 0 and not captured and "Unexpected Holmes config template" in output
            print("PASS: an altered template stops execution before the model request")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
