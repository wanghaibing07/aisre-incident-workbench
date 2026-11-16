"""Run a restricted Holmes investigation on Windows against an isolated SSH lab.

Requires HolmesGPT 0.43.0, Git Bash, OpenSSH and a loopback model endpoint.
Configure OPENAI_API_BASE and OPENAI_API_KEY in the process environment; never
put a model key in command arguments or a project file. See --help for the
remaining connection settings. The remote fixture and namespace must already
be prepared and authorized for the selected lab context.
"""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


TOOLS = {
    "pods": ("incident_lab_pod_snapshot", "Read pod names and phases in the fixed lab namespace."),
    "routes": ("incident_lab_service_route_evidence", "Read pods, services and endpoint slices in the fixed lab namespace."),
    "logs": ("incident_lab_gateway_error_logs", "Read recent t3-gateway logs in the fixed lab namespace."),
}
REMOTE = {
    "pods": ["get", "pods", "-o", "json"],
    "routes": ["get", "pods,services,endpointslices", "-o", "json"],
    "logs": ["logs", "deployment/t3-gateway", "--since=15m", "--tail=80", "--timestamps"],
}


def validate_params(params):
    namespace = params["namespace"]
    if not re.fullmatch(r"incident-eval-[a-z0-9]{6,30}", namespace):
        raise ValueError("Use an isolated incident-eval-* namespace.")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", params["user"]):
        raise ValueError("Invalid SSH user.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", params["host"]):
        raise ValueError("Invalid SSH host; use an IPv4 address or hostname.")
    if not re.fullmatch(r"[A-Za-z0-9_:@./-]{1,200}", params["context"]) or params["context"].startswith("-"):
        raise ValueError("Invalid Kubernetes context.")
    remote = params["remote_fixture"]
    if (not re.fullmatch(r"/[A-Za-z0-9_./-]+", remote) or ".." in PurePosixPath(remote).parts
            or PurePosixPath(remote).name != namespace):
        raise ValueError("Remote fixture must be an absolute path ending in the isolated namespace.")
    for name in ("identity", "known_hosts", "ssh"):
        path = Path(params[name])
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f"{name} must name an existing absolute file path.")


def read_remote(alias, params):
    validate_params(params)
    if alias not in REMOTE:
        raise ValueError("Unknown fixed read-only operation.")
    helper = str(PurePosixPath(params["remote_fixture"]) / "scripts" / "agent-kubeconfig.sh")
    remote = ["bash", helper, "--context", params["context"], "--", "kubectl", "--request-timeout=10s",
              "-n", params["namespace"], *REMOTE[alias]]
    command = [params["ssh"], "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=yes",
               "-o", "IdentitiesOnly=yes", "-o", f"UserKnownHostsFile={params['known_hosts']}",
               "-i", params["identity"], f"{params['user']}@{params['host']}", shlex.join(remote)]
    # The model credential is not needed by SSH and must not be passed to it.
    child_env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "COMSPEC", "PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "PROGRAMDATA")
                 if key in os.environ}
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=25, env=child_env)
    if result.returncode:
        raise RuntimeError("Restricted Kubernetes read failed.")
    if alias == "pods":
        payload = json.loads(result.stdout)
        pods = [{"name": p["metadata"]["name"], "phase": p.get("status", {}).get("phase", "Unknown")}
                for p in payload["items"]]
        return json.dumps({"namespace": params["namespace"], "count": len(pods), "pods": pods})
    return result.stdout


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--case", choices=("t1", "t3"), required=True)
    p.add_argument("--namespace", required=True, help="Prepared isolated incident-eval-* namespace")
    p.add_argument("--host", required=True, help="SSH hostname or IPv4 address")
    p.add_argument("--user", required=True, help="SSH login with permission to issue the lab reader token")
    p.add_argument("--identity", required=True, help="Absolute OpenSSH private key path")
    p.add_argument("--known-hosts", required=True, help="Absolute known_hosts file path")
    p.add_argument("--ssh", required=True, help="Absolute path to ssh.exe")
    p.add_argument("--bash", required=True, help="Absolute path to Git Bash bash.exe")
    p.add_argument("--remote-fixture", required=True, help="Absolute Linux path of the prepared fixture")
    p.add_argument("--context", required=True, help="Authorized Kubernetes context on the remote host")
    p.add_argument("--model", required=True, help="Holmes model identifier")
    return p


def run_bridge(args):
    if os.name != "nt":
        raise RuntimeError("This compatibility bridge requires Windows.")
    params = {key: getattr(args, key) for key in ("namespace", "host", "user", "identity", "known_hosts", "ssh", "remote_fixture", "context")}
    validate_params(params)
    bash = Path(args.bash)
    if not bash.is_absolute() or not bash.is_file():
        raise ValueError("bash must name an existing absolute file path.")
    model_url = urlsplit(os.environ.get("OPENAI_API_BASE", ""))
    if model_url.scheme != "http" or model_url.hostname not in ("localhost", "127.0.0.1", "::1") or model_url.username or model_url.password:
        raise ValueError("OPENAI_API_BASE must be a loopback HTTP endpoint.")
    model_key = os.environ.get("OPENAI_API_KEY", "")
    if not model_key:
        raise ValueError("Set OPENAI_API_KEY in the process environment.")
    if importlib.metadata.version("holmesgpt") != "0.43.0":
        raise RuntimeError("This restricted entry requires HolmesGPT 0.43.0.")

    temp_root = Path(tempfile.gettempdir()).resolve()
    runtime = Path(tempfile.mkdtemp(prefix="holmes-windows-lab-", dir=temp_root)).resolve()
    try:
        for name in ("home", "temp", "appdata", "localappdata"):
            (runtime / name).mkdir()
        system_root = os.environ["SystemRoot"]
        program_data = os.environ.get("PROGRAMDATA", "")
        base_url = os.environ["OPENAI_API_BASE"]
        os.environ.clear()
        os.environ.update({
            "SystemRoot": system_root, "WINDIR": system_root, "COMSPEC": str(Path(system_root) / "System32" / "cmd.exe"),
            "PROGRAMDATA": program_data,
            "PATH": os.pathsep.join([str(Path(system_root) / "System32"), str(bash.parent.parent / "usr" / "bin"), str(Path(sys.executable).parent)]),
            "HOME": str(runtime / "home"), "USERPROFILE": str(runtime / "home"),
            "APPDATA": str(runtime / "appdata"), "LOCALAPPDATA": str(runtime / "localappdata"),
            "TEMP": str(runtime / "temp"), "TMP": str(runtime / "temp"),
            "OPENAI_API_BASE": base_url, "OPENAI_API_KEY": model_key,
            "INCIDENT_BRIDGE_PARAMS": json.dumps(params), "CUSTOM_TOOLSET_LOCATION": str(runtime / "none.yaml"),
            "HOLMES_CONFIGPATH_DIR": str(runtime / "home"), "ENABLE_TELEMETRY": "false",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1", "NO_PROXY": "localhost,127.0.0.1,::1",
        })
        import yaml
        from holmes.plugins.toolsets import load_builtin_toolsets
        from holmes.core.tools_utils.tool_executor import ToolExecutor

        config = {"toolsets": {item.name: {"enabled": False} for item in load_builtin_toolsets(None)}}
        config_path = runtime / "config.yaml"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        bridge_path = Path(__file__).resolve().as_posix()
        python_path = Path(sys.executable).resolve().as_posix()
        toolset = {"toolsets": {"incident-windows/readonly": {"enabled": True,
                    "description": "Fixed read-only incident evaluation tools", "tags": ["cli"], "tools": [
                        {"name": name, "description": description,
                         "command": shlex.join([python_path, bridge_path, "--tool", alias])}
                        for alias, (name, description) in TOOLS.items()]}}}
        toolset_path = runtime / "tools.yaml"
        toolset_path.write_text(yaml.safe_dump(toolset), encoding="utf-8")
        original_run = subprocess.run

        def run_with_windows_bash(command, *run_args, **run_kwargs):
            if run_kwargs.get("shell") and run_kwargs.get("executable") == "/bin/bash":
                run_kwargs.pop("executable")
                run_kwargs["shell"] = False
                command = [str(bash), "-c", command]
            return original_run(command, *run_args, **run_kwargs)

        subprocess.run = run_with_windows_bash
        original_init = ToolExecutor.__init__

        def guarded_init(executor, *init_args, **init_kwargs):
            original_init(executor, *init_args, **init_kwargs)
            if ([item.name for item in executor.enabled_toolsets] != ["incident-windows/readonly"]
                    or set(executor.tools_by_name) != {name for name, _ in TOOLS.values()}):
                raise RuntimeError("Unexpected Holmes tools; refusing to investigate.")

        ToolExecutor.__init__ = guarded_init
        symptoms = ("the web workload is ready but requests through the gateway fail" if args.case == "t1"
                    else "the gateway health check is 200 but /api/ returns a 5xx error")
        question = (f"In namespace {args.namespace}, {symptoms}. Investigate with the available read-only tools. "
                    "Explain the root cause with evidence. Do not propose or perform changes.")
        sys.argv = ["holmes", "ask", question, "--model", args.model, "--no-interactive", "--max-steps", "8",
                    "--show-tool-output", "--config", str(config_path), "--custom-toolsets", str(toolset_path)]
        runpy.run_module("holmes.main", run_name="__main__")
    finally:
        if runtime.parent != temp_root or not runtime.name.startswith("holmes-windows-lab-") or runtime.is_symlink():
            raise RuntimeError("Refusing to remove an unexpected temporary directory.")
        shutil.rmtree(runtime)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--tool":
        try:
            print(read_remote(sys.argv[2], json.loads(os.environ["INCIDENT_BRIDGE_PARAMS"])))
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, KeyError, json.JSONDecodeError):
            print("Restricted Kubernetes read failed.", file=sys.stderr)
            return 1
        return 0
    try:
        run_bridge(parser().parse_args())
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, KeyError) as error:
        print(f"Bridge failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
