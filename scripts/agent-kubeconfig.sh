#!/usr/bin/env bash
set +x
set -euo pipefail

usage() {
  printf '%s\n' 'Usage: bash scripts/agent-kubeconfig.sh --context CONTEXT -- COMMAND [ARG ...]'
  printf '%s\n' 'Runs COMMAND with a temporary KUBECONFIG and a short-lived incident-lab reader token.'
}

if [[ ${1:-} == --help ]]; then
  usage
  exit 0
fi
if [[ $# -lt 4 || $1 != --context || -z $2 || $3 != -- ]]; then
  usage >&2
  exit 2
fi

lab_context=$2
shift 3
if ! command -v kubectl >/dev/null 2>&1; then
  printf '%s\n' 'kubectl is required.' >&2
  exit 1
fi
python_bin=python3
if ! "$python_bin" --version >/dev/null 2>&1; then
  python_bin=python
  if ! "$python_bin" --version >/dev/null 2>&1; then
    printf '%s\n' 'Python 3 is required.' >&2
    exit 1
  fi
fi

umask 077
agent_dir=$(mktemp -d "${TMPDIR:-/tmp}/holmes-kubeconfig.XXXXXXXX")
cleanup() {
  rm -f -- "$agent_dir/cluster.json" "$agent_dir/config" "$agent_dir/token"
  rmdir -- "$agent_dir"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if ! kubectl --context "$lab_context" config view --minify --flatten \
    -o 'jsonpath-as-json={.clusters[0].cluster}' > "$agent_dir/cluster.json" 2>/dev/null; then
  printf '%s\n' 'Could not load cluster TLS configuration for the selected context.' >&2
  exit 1
fi

"$python_bin" - "$agent_dir/cluster.json" > "$agent_dir/config" <<'PY'
import json
import sys
from urllib.parse import urlsplit

with open(sys.argv[1], encoding="utf-8") as cluster_file:
    (cluster,) = json.load(cluster_file)
endpoint = urlsplit(cluster["server"])
if (
    endpoint.scheme != "https"
    or not endpoint.hostname
    or endpoint.username is not None
    or endpoint.password is not None
    or cluster.get("insecure-skip-tls-verify")
):
    sys.exit("The selected cluster must use HTTPS with verified TLS.")

name = "holmes-investigator"
namespace = "incident-lab"
json.dump(
    {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": namespace, "cluster": cluster}],
        "users": [{"name": name, "user": {"tokenFile": "token"}}],
        "contexts": [
            {"name": name, "context": {"cluster": namespace, "namespace": namespace, "user": name}}
        ],
        "current-context": name,
    },
    sys.stdout,
)
PY

if ! kubectl --context "$lab_context" --request-timeout=15s -n incident-lab \
    create token holmes-investigator --duration=15m > "$agent_dir/token" 2>/dev/null; then
  printf '%s\n' 'Could not issue a token. Apply the lab RBAC and use a context allowed to create serviceaccounts/token.' >&2
  exit 1
fi
if [[ ! -s $agent_dir/token ]]; then
  printf '%s\n' 'TokenRequest returned an empty token.' >&2
  exit 1
fi

KUBECONFIG="$agent_dir/config" "$@"
