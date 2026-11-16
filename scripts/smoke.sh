#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 || $1 != --context || -z $2 || $2 == -* ]]; then
    printf 'Usage: bash scripts/smoke.sh --context LAB_CONTEXT\n' >&2
    exit 2
fi

context=$2
repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
kube=(kubectl --context "$context" --request-timeout=15s)

existing_namespace=$("${kube[@]}" get namespace incident-lab --ignore-not-found -o name)
if [[ -n $existing_namespace ]]; then
    printf 'The context %s already has incident-lab. Use a fresh lab cluster.\n' "$context" >&2
    exit 1
fi

"${kube[@]}" create -f "$repository_root/k8s/incident-lab.yaml"
"${kube[@]}" create -f "$repository_root/k8s/agent-rbac.yaml"
kubectl --context "$context" -n incident-lab rollout status deployment/t1-web --timeout=120s
kubectl --context "$context" -n incident-lab rollout status deployment/t3-gateway --timeout=120s

bash "$repository_root/scripts/verify-rbac.sh" --context "$context"
for scenario in t1 t3; do
    bash "$repository_root/scripts/reproduce.sh" --context "$context" --case "$scenario"
done

printf 'PASS: restricted identity, T1/T3 evidence and HTTP restoration\n'
