import argparse
import os
from pathlib import Path
import re
import stat


def main():
    parser = argparse.ArgumentParser(description="Prepare a separate namespace fixture for a single lab node.")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"incident-eval-[a-z0-9]{6,30}", args.namespace):
        parser.error("namespace must start with incident-eval- and end in 6 to 30 lowercase letters or digits")
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", args.node) or len(args.node) > 63:
        parser.error("node must be a valid Kubernetes hostname label")
    source = Path(__file__).resolve().parent.parent
    target = args.output.resolve()
    if target == source or source in target.parents:
        parser.error("output must be outside the source repository")
    files = (
        "k8s/incident-lab.yaml", "k8s/agent-rbac.yaml", "scripts/agent-kubeconfig.sh",
        "scripts/verify-rbac.sh", "scripts/reproduce.py", "scripts/reproduce.sh",
        "scripts/smoke.sh", "scripts/investigate.sh", "scripts/wait-investigation.sh", "toolsets/incident-lab-readonly.yaml",
        "toolsets/holmes-lab-config.yaml",
    )
    prepared = {}
    for relative in files:
        content = (source / relative).read_text(encoding="utf-8").replace("incident-lab", args.namespace)
        if relative == "k8s/incident-lab.yaml":
            marker = "    spec:\n      containers:\n"
            if content.count(marker) != 2:
                raise SystemExit("Unexpected deployment manifest; no fixture was written")
            scheduling = (
                "    spec:\n"
                f"      nodeSelector:\n        kubernetes.io/hostname: {args.node}\n"
                "      tolerations:\n"
                "        - key: node-role.kubernetes.io/control-plane\n"
                "          operator: Exists\n"
                "          effect: NoSchedule\n"
                "      containers:\n"
            )
            content = content.replace(marker, scheduling)
        prepared[relative.replace("incident-lab", args.namespace)] = content
    target.mkdir(parents=True, exist_ok=False)
    for relative, content in prepared.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8", newline="\n") as output:
            output.write(content)
        if relative in ("scripts/investigate.sh", "scripts/wait-investigation.sh") and os.name == "posix":
            destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
    print(f"Prepared isolated fixture in {target} for namespace {args.namespace}")


if __name__ == "__main__":
    main()
