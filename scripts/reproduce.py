import argparse
from contextlib import contextmanager
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4


NAMESPACE = "incident-lab"
GATEWAY = "deployment/t3-gateway"
SERVICE = "service/t1-web"
CONFIG = "configmap/t3-gateway-config"
OWNER = "incident-lab/reproduction-run"
UPSTREAM = "proxy_pass http://t1-web.incident-lab.svc.cluster.local:80/;"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def poll(check, message, seconds=30):
    deadline = time.monotonic() + seconds
    while True:
        result = check()
        if result:
            return result
        require(time.monotonic() < deadline, message)
        time.sleep(0.5)


@contextmanager
def defer_interrupts():
    received = []
    previous = {}

    def record(signum, _frame):
        if not received:
            received.append(signum)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, record)
        yield received
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def signal_group(group_id, signum):
    try:
        os.killpg(group_id, signum)
    except ProcessLookupError:
        return False
    return True


def stop_hook(process, grace):
    if signal_group(process.pid, signal.SIGTERM):
        deadline = time.monotonic() + grace
        while True:
            process.poll()
            if not signal_group(process.pid, 0):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                signal_group(process.pid, signal.SIGKILL)
                break
            time.sleep(min(remaining, 0.05))
    process.wait(timeout=5)


def run_fault_hook(hook, case, context, timeout=300, grace=3):
    require(os.name == "posix", "--on-fault requires POSIX Python")
    command = [hook, case]
    with defer_interrupts() as received:
        scratch = TemporaryDirectory(prefix="incident-repro-hook-")
        process = None
        failed = False
        try:
            process = subprocess.Popen(
                command, env={**os.environ, "TMPDIR": scratch.name, "LAB_CONTEXT": context},
                start_new_session=True,
            )
            deadline = time.monotonic() + timeout
            while True:
                if received:
                    interrupt(received[0], None)
                status = process.poll()
                if status is not None:
                    if status:
                        raise subprocess.CalledProcessError(status, command)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(min(remaining, 0.05))
        except BaseException:
            failed = True
            raise
        finally:
            try:
                try:
                    if process is not None:
                        stop_hook(process, grace)
                finally:
                    scratch.cleanup()
            except (OSError, subprocess.SubprocessError) as error:
                if not failed:
                    raise
                print(f"Fault hook cleanup failed: {error}", file=sys.stderr)
        if received:
            interrupt(received[0], None)


class Lab:
    def __init__(self, context):
        self.context = context
        self.command = ["kubectl", "--context", context, "--namespace", NAMESPACE]
        self.run_id = uuid4().hex

    def kubectl(self, *args, timeout=20):
        task = subprocess.run(
            [*self.command, *args], capture_output=True, text=True,
            encoding="utf-8", timeout=timeout,
        )
        require(task.returncode == 0, f"kubectl {args[0]} failed: {task.stderr.strip()}")
        return task.stdout

    def get(self, *args):
        return json.loads(self.kubectl("get", *args, "-o", "json"))

    def rollout(self, target):
        self.kubectl("rollout", "status", target, "--timeout=120s", timeout=125)

    def restart_gateway(self):
        self.kubectl("rollout", "restart", GATEWAY)
        self.rollout(GATEWAY)

    @contextmanager
    def forward(self):
        with TemporaryDirectory(prefix="incident-repro-") as temp_dir:
            log_path = Path(temp_dir) / "port-forward.log"
            with log_path.open("wb") as output:
                process = subprocess.Popen(
                    [*self.command, "port-forward", "--address", "127.0.0.1",
                     "service/t3-gateway", ":8080"],
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                )

            def port():
                logs = log_path.read_text(encoding="utf-8", errors="replace")
                require(process.poll() is None, f"port-forward exited: {logs[-600:].strip()}")
                match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+) -> 8080", logs)
                return int(match[1]) if match else None

            try:
                yield poll(port, "Timed out waiting for gateway port-forward")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

    def status(self, port, path, expected):
        last = {"status": None, "error": None}

        def get_status():
            connection = HTTPConnection("127.0.0.1", port, timeout=9)
            try:
                connection.request("GET", path)
                status = connection.getresponse().status
                last.update(status=status, error=None)
                return status if status in expected else None
            except (OSError, HTTPException) as error:
                last.update(status=None, error=type(error).__name__)
                return None
            finally:
                connection.close()

        try:
            return poll(get_status, f"GET {path} did not return one of {expected}")
        except RuntimeError as error:
            observed = f"HTTP {last['status']}" if last["status"] is not None else (last["error"] or "no response")
            raise RuntimeError(f"{error}; last observed: {observed}") from error

    def ready_endpoints(self):
        slices = self.get("endpointslices", "-l", "kubernetes.io/service-name=t1-web")
        return sum(
            bool(endpoint.get("addresses")) and (endpoint.get("conditions") or {}).get("ready") is not False
            for item in slices["items"]
            for endpoint in (item.get("endpoints") or [])
        )

    def ready_web_pod(self):
        pods = self.get("pods", "-l", "app=t1-web")
        return any(
            pod["status"].get("phase") == "Running"
            and any(condition["type"] == "Ready" and condition["status"] == "True"
                    for condition in pod["status"].get("conditions", []))
            for pod in pods["items"]
        )

    def check_healthy(self, phase):
        poll(self.ready_endpoints, "t1-web has no ready endpoints")
        with self.forward() as port:
            self.status(port, "/healthz", (200,))
            self.status(port, "/api/", (200,))
        print(f"{phase}: /healthz=200 /api/=200", flush=True)

    def patch(self, target, resource, path, value, annotations):
        operations = [
            {"op": "test", "path": "/metadata/resourceVersion",
             "value": resource["metadata"]["resourceVersion"]},
            {"op": "replace", "path": path, "value": value},
            {"op": "add", "path": "/metadata/annotations", "value": annotations},
        ]
        self.kubectl("patch", target, "--type=json", "-p", json.dumps(operations))

    def check_fault(self, case):
        if case == "t1":
            poll(lambda: self.ready_endpoints() == 0, "t1-web still has ready endpoints")
            require(self.ready_web_pod(), "t1-web has no Ready Pod")
            with self.forward() as port:
                self.status(port, "/healthz", (200,))
                status = self.status(port, "/api/", (502, 504))
            print(f"T1: Pod Ready, EndpointSlice ready endpoints=0, /healthz=200 /api/={status}", flush=True)
            return

        self.restart_gateway()

        def log_evidence():
            logs = self.kubectl("logs", GATEWAY, "--since=2m", "--tail=80")
            matching_request = any(self.run_id in line for line in logs.splitlines())
            wrong_port = any(
                self.run_id in line and re.search(r'upstream: "http://[^"/]+:81/', line)
                for line in logs.splitlines()
            )
            return matching_request, wrong_port

        try:
            with self.forward() as port:
                self.status(port, "/healthz", (200,))
                # Depending on the network's rejection behavior, Nginx reports
                # an immediate connection failure as 502 or a timeout as 504.
                status = self.status(port, f"/api/?reproduction={self.run_id}", (502, 504))
        except RuntimeError:
            try:
                matching_request, wrong_port = log_evidence()
                print(f"T3 diagnostic: matching request={matching_request}, port 81 evidence={wrong_port}",
                      file=sys.stderr)
            except (RuntimeError, OSError, subprocess.SubprocessError):
                print("T3 diagnostic: gateway logs unavailable", file=sys.stderr)
            raise

        poll(lambda: log_evidence()[1], "Nginx did not log an upstream port 81 error for this run")
        print(f"T3: /healthz=200 /api/={status}, Nginx evidence shows upstream port 81", flush=True)

    def reproduce(self, case, hook):
        self.rollout("deployment/t1-web")
        self.rollout(GATEWAY)
        self.check_healthy("Baseline")

        if case == "t1":
            target, section, key = SERVICE, "spec", "selector"
            resource = self.get(target)
            healthy = resource[section][key]
            require(healthy == {"app": "t1-web"}, "Unexpected t1-web selector; refusing to change it")
            fault = {"app": "t1-web-fault"}
        else:
            target, section, key = CONFIG, "data", "default.conf"
            resource = self.get(target)
            healthy = resource[section][key]
            require(healthy.count(UPSTREAM) == 1, "Unexpected gateway upstream; refusing to change it")
            fault = healthy.replace(UPSTREAM, UPSTREAM.replace(":80/", ":81/"))

        path = f"/{section}/{key}"
        metadata = resource["metadata"]
        original_uid = metadata["uid"]
        annotations = metadata.get("annotations", {})
        require(OWNER not in annotations, f"{target} is marked by another reproduction run")

        failed = False
        try:
            self.patch(target, resource, path, fault, {**annotations, OWNER: self.run_id})
            self.check_fault(case)
            if hook:
                run_fault_hook(hook, case, self.context)
        except BaseException:
            failed = True
            raise
        finally:
            with defer_interrupts() as received:
                try:
                    current = self.get(target)
                    current_annotations = current["metadata"].get("annotations", {})
                    require(current["metadata"]["uid"] == original_uid,
                            f"{target} was recreated; refusing to overwrite it")
                    if current_annotations.get(OWNER) == self.run_id:
                        require(current[section][key] == fault,
                                f"{target} changed concurrently; refusing to overwrite it")
                        del current_annotations[OWNER]
                        self.patch(target, current, path, healthy, current_annotations)
                        if case == "t3":
                            self.restart_gateway()
                        self.check_healthy("Restored")
                    else:
                        require(current[section][key] == healthy,
                                f"{target} no longer matches this run; verify recovery manually")
                except Exception as error:
                    message = f"Recovery failed for {target}: {error}"
                    if not failed:
                        raise RuntimeError(message) from error
                    print(message, file=sys.stderr)
                if received and not failed:
                    interrupt(received[0], None)


def interrupt(signum, _frame):
    raise InterruptedError(f"Interrupted by signal {signum}")


def main():
    parser = argparse.ArgumentParser(description="Reproduce and restore an incident-lab fixture.")
    parser.add_argument("--context", required=True, help="Explicit kubeconfig context for the lab")
    parser.add_argument("--case", choices=("t1", "t3"), required=True)
    parser.add_argument("--on-fault", metavar="EXECUTABLE", help="POSIX only: run with the case name before restoring (max 300s)")
    args = parser.parse_args()
    if not args.context.strip():
        parser.error("--context must not be empty")
    if args.on_fault and os.name != "posix":
        parser.error("--on-fault requires POSIX Python")
    hook = shutil.which(args.on_fault) if args.on_fault else None
    if args.on_fault and not hook:
        parser.error("--on-fault must name an executable")
    if hook and os.environ.get("LAB_CONTEXT") and os.environ["LAB_CONTEXT"] != args.context:
        parser.error("LAB_CONTEXT must match --context when using --on-fault")

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        Lab(args.context).reproduce(args.case, hook)
    except (RuntimeError, OSError, subprocess.SubprocessError, KeyError, ValueError) as error:
        print(f"Reproduction failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
