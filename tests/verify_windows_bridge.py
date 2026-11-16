"""Opt-in: verify the actual Holmes 0.43.0 model request using a local fake model.

Run on Windows with the Python environment containing HolmesGPT 0.43.0. No SSH
connection, Kubernetes cluster, real model service or model credential is used.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading


ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {"incident_lab_pod_snapshot", "incident_lab_service_route_evidence", "incident_lab_gateway_error_logs"}
requests = []


class FakeModel(BaseHTTPRequestHandler):
    def do_POST(self):
        requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        message = ({"role": "assistant", "content": None, "tool_calls": [{"id": "call-pods", "type": "function",
                    "function": {"name": "incident_lab_pod_snapshot", "arguments": "{}"}}]}
                   if len(requests) == 1 else {"role": "assistant", "content": "Test complete"})
        body = json.dumps({"id": "local-test", "object": "chat.completion", "created": 0, "model": "test",
                           "choices": [{"index": 0, "message": message,
                                        "finish_reason": "tool_calls" if len(requests) == 1 else "stop"}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    if os.name != "nt":
        raise SystemExit("This integration check requires Windows.")
    bash = Path(os.environ.get("GIT_BASH_PATH", str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe")))
    if not bash.is_file():
        raise SystemExit("Set GIT_BASH_PATH to Git Bash bash.exe.")
    csc = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not csc.is_file():
        raise SystemExit("The Windows .NET Framework C# compiler is required for the local SSH stub.")
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="windows-bridge-check-") as name:
            temp = Path(name)
            for file in ("identity", "known_hosts"):
                (temp / file).write_text("synthetic", encoding="utf-8")
            stub = temp / "stub.cs"
            stub.write_text('using System; class Stub { static void Main() { Console.Write("{\\\"items\\\":[]}"); } }', encoding="utf-8")
            compiled = subprocess.run([str(csc), "/nologo", f"/out:{temp / 'ssh.exe'}", str(stub)], capture_output=True)
            if compiled.returncode:
                raise SystemExit("Could not compile local SSH stub")
            env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "COMSPEC", "PATH", "TEMP", "TMP")
                   if key in os.environ}
            env.update({"OPENAI_API_BASE": f"http://127.0.0.1:{server.server_port}/v1", "OPENAI_API_KEY": "synthetic",
                        "PYTHONUTF8": "1", "NO_PROXY": "localhost,127.0.0.1,::1"})
            cmd = [sys.executable, str(ROOT / "scripts/windows-holmes-bridge.py"), "--case", "t1",
                   "--namespace", "incident-eval-test123", "--host", "192.0.2.4", "--user", "labuser",
                   "--identity", str(temp / "identity"), "--known-hosts", str(temp / "known_hosts"),
                   "--ssh", str(temp / "ssh.exe"), "--bash", str(bash),
                   "--remote-fixture", "/tmp/incident-eval-test123", "--context", "lab-context", "--model", "openai/test"]
            result = subprocess.run(cmd, env=env, capture_output=True, timeout=120)
            if result.returncode != 0:
                raise SystemExit("Bridge failed: " + (result.stderr + result.stdout).decode("utf-8", "replace")[-2500:])
            if len(requests) != 2:
                raise SystemExit(f"Expected two model requests, got {len(requests)}")
            names = [entry["function"]["name"] for entry in requests[0].get("tools", [])]
            if len(names) != 3 or set(names) != EXPECTED:
                raise SystemExit(f"Unexpected tool list: {names}")
            tool_results = [item for item in requests[1].get("messages", []) if item.get("role") == "tool"]
            if len(tool_results) != 1 or '"count": 0' not in str(tool_results[0].get("content")):
                raise SystemExit("Expected the fixed pod tool result from the local SSH stub")
            print("PASS: Holmes 0.43.0 sent three restricted tools and invoked the fixed pod tool through the local SSH stub")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
