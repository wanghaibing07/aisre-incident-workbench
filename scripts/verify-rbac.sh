#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' 'Usage: bash scripts/verify-rbac.sh --context CONTEXT'
  printf '%s\n' 'Requires a running incident-lab, its RBAC manifests, and a context allowed to issue a token.'
}

if [[ ${1:-} == --help ]]; then
  usage
  exit 0
fi

if [[ $# == 2 && $1 == --context && -n $2 ]]; then
  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  exec bash "$script_dir/agent-kubeconfig.sh" --context "$2" -- bash "$script_dir/verify-rbac.sh" --as-agent
fi
if [[ $# != 1 || $1 != --as-agent ]]; then
  usage >&2
  exit 2
fi

agent_kubectl() {
  kubectl --request-timeout=10s "$@"
}

expect_denied() {
  local decision code=0
  decision=$(agent_kubectl auth can-i "$@") || code=$?
  if [[ $code != 1 || $decision != no ]]; then
    printf 'FAIL expected denial: %s (decision=%s, exit=%s)\n' "$*" "$decision" "$code" >&2
    exit 1
  fi
  printf 'PASS denied: %s\n' "$*"
}

namespace=incident-lab
identity=$(agent_kubectl auth whoami -o 'jsonpath={.status.userInfo.username}')
if [[ $identity != system:serviceaccount:incident-lab:holmes-investigator ]]; then
  printf 'FAIL unexpected authenticated identity: %s\n' "$identity" >&2
  exit 1
fi
printf 'PASS authenticated identity: %s\n' "$identity"

agent_kubectl -n "$namespace" get pods -o json >/dev/null
printf '%s\n' 'PASS pod snapshot read'
agent_kubectl -n "$namespace" get pods,services,endpointslices -o json >/dev/null
printf '%s\n' 'PASS service routes and EndpointSlice read'
agent_kubectl -n "$namespace" logs deployment/t3-gateway --since=15m --tail=80 --timestamps >/dev/null
printf '%s\n' 'PASS gateway log read'

expect_denied list secrets -n "$namespace"
expect_denied get secrets/example -n "$namespace"
expect_denied get configmaps/t3-gateway-config -n "$namespace"
expect_denied get deployments.apps/t1-web -n "$namespace"
expect_denied list deployments.apps -n "$namespace"
expect_denied create pods -n "$namespace"
expect_denied patch services/t1-web -n "$namespace"
expect_denied update deployments.apps/t3-gateway -n "$namespace"
expect_denied delete pods -n "$namespace"
expect_denied create pods --subresource=exec -n "$namespace"
expect_denied get pods --subresource=exec -n "$namespace"
expect_denied create pods --subresource=portforward -n "$namespace"
expect_denied get pods --subresource=portforward -n "$namespace"
expect_denied create serviceaccounts/holmes-investigator --subresource=token -n "$namespace"
expect_denied list pods -n default
expect_denied get pods --subresource=log -n default
expect_denied list pods --all-namespaces
expect_denied list nodes --all-namespaces
