#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 1 ]]; then
    printf 'Usage: LAB_CONTEXT=CONTEXT HOLMES_MODEL=MODEL bash scripts/investigate.sh t1|t3\n' >&2
    exit 2
fi

case "$1" in
    t1) question='In the incident-lab demo the t1-web workload looks ready, but requests through the gateway fail. Use the incident-lab tools to investigate. Cite observed evidence and identify the cause; do not perform any changes.' ;;
    t3) question='In the incident-lab demo the gateway health check is 200 but /api/ returns 502. Use the incident-lab tools to investigate. Cite observed evidence and identify the cause; do not perform any changes.' ;;
    *) printf 'Unknown case: %s\n' "$1" >&2; exit 2 ;;
esac

: "${LAB_CONTEXT:?Set LAB_CONTEXT to the isolated cluster context}"
: "${HOLMES_MODEL:?Set HOLMES_MODEL to your configured Holmes model}"
: "${HOLMES_PYTHON:=python3}"
command -v "$HOLMES_PYTHON" >/dev/null || { printf 'Python with HolmesGPT 0.43.0 is required.\n' >&2; exit 1; }

repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
umask 077
runtime=$(mktemp -d "${TMPDIR:-/tmp}/holmes-lab.XXXXXXXX")
cleanup() {
    rm -rf -- "$runtime"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

bash "$repository_root/scripts/agent-kubeconfig.sh" --context "$LAB_CONTEXT" -- \
    env HOME="$runtime" HOLMES_CONFIGPATH_DIR="$runtime" \
    XDG_CONFIG_HOME="$runtime" XDG_CACHE_HOME="$runtime" XDG_DATA_HOME="$runtime" \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    CUSTOM_TOOLSET_LOCATION="$runtime/no-extra-toolsets.yaml" \
    "$HOLMES_PYTHON" - "$runtime" "$repository_root/toolsets/holmes-lab-config.yaml" \
    "$repository_root/toolsets/incident-lab-readonly.yaml" "$question" "$HOLMES_MODEL" <<'PY'
import importlib.metadata
import json
from pathlib import Path
import runpy
import sys

if importlib.metadata.version("holmesgpt") != "0.43.0":
    raise SystemExit("This restricted entry requires HolmesGPT 0.43.0; refusing to run.")

import yaml
from holmes.plugins.toolsets import load_builtin_toolsets
from holmes.core.tools_utils.tool_executor import ToolExecutor

runtime, template, custom, question, model = sys.argv[1:]
config = yaml.safe_load(Path(template).read_text(encoding="utf-8"))
if config != {"toolsets": {}}:
    raise SystemExit("Unexpected Holmes config template; refusing to run.")
config["toolsets"] = {toolset.name: {"enabled": False} for toolset in load_builtin_toolsets(None)}
config_path = Path(runtime) / "restricted-config.json"
config_path.write_text(json.dumps(config), encoding="utf-8")

# Check the actual executor assembled by Holmes immediately before it can be used.
original_init = ToolExecutor.__init__
def guarded_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    allowed = {"incident_lab_pod_snapshot", "incident_lab_service_route_evidence", "incident_lab_gateway_error_logs"}
    if ([t.name for t in self.enabled_toolsets] != ["incident-lab/readonly"]
            or set(self.tools_by_name) != allowed):
        raise RuntimeError("Unexpected Holmes tools; refusing to investigate.")
ToolExecutor.__init__ = guarded_init

sys.argv = ["holmes", "ask", question, "--model", model, "--no-interactive", "--show-tool-output",
            "--config", str(config_path), "--custom-toolsets", custom]
runpy.run_module("holmes.main", run_name="__main__")
PY
