#!/usr/bin/env bash
# Manual GitHub-hosted experiment. Only synthetic incident-lab resources are used.
set -euo pipefail
set +x
umask 077

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
evidence="$RUNNER_TEMP/incident-evidence"
mkdir -p -- "$evidence"
chmod 700 "$evidence"

event() {
  python3 - "$evidence/events.jsonl" "$1" "$2" <<'PY'
import json,sys,time
with open(sys.argv[1], 'a', encoding='utf8') as f:
    f.write(json.dumps({'stage':sys.argv[2], 'result':sys.argv[3], 'unix_time':int(time.time())})+'\n')
PY
}

resources() {
  local mem disk
  mem=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  disk=$(df -Pk "$RUNNER_TEMP" | awk 'NR==2 {print $4}')
  if [[ ! $mem =~ ^[0-9]+$ || ! $disk =~ ^[0-9]+$ ]] || (( mem < 8388608 || disk < 9437184 )); then
    event resources insufficient
    printf '%s\n' 'Insufficient runner memory or disk for this experiment.'
    return 1
  fi
  event resources sufficient
}

case "${1:-}" in
  --resources) resources; exit ;;
  --run) resources ;;
  *) printf 'Usage: bash scripts/hosted-real-model.sh --resources|--run\n' >&2; exit 2 ;;
esac

private=$(mktemp -d "$RUNNER_TEMP/incident-real.XXXXXXXX")
model_pid=''
forward_pid=''
key_file="$private/model-key"
cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  if [[ -n $forward_pid ]]; then kill "$forward_pid" 2>/dev/null || true; wait "$forward_pid" 2>/dev/null || true; fi
  if [[ -n $model_pid ]] && kill -0 "$model_pid" 2>/dev/null; then
    kill -TERM "$model_pid" 2>/dev/null || true
    for _ in {1..20}; do kill -0 "$model_pid" 2>/dev/null || break; sleep 1; done
    if kill -0 "$model_pid" 2>/dev/null; then kill -KILL "$model_pid" 2>/dev/null || true; fi
    wait "$model_pid" 2>/dev/null || true
  fi
  # Only experiment logs are exported. They contain synthetic cluster data;
  # scrub the one-time key, token-like strings and private keys defensively.
  python3 - "$private" "$evidence" "$key_file" <<'PY' || true
import pathlib,re,sys
root,out,keyfile=map(pathlib.Path,sys.argv[1:])
key=keyfile.read_text(encoding='utf8').strip() if keyfile.is_file() else ''
for name in ('smoke','t1','t3','model'):
    source=root/(name+'.log')
    if not source.is_file(): continue
    # The tail captures model load/startup failures while bounding artifacts.
    with source.open('rb') as f:
        if name=='model':
            f.seek(0,2); f.seek(max(0,f.tell()-262144))
        text=f.read(1000000).decode('utf8',errors='replace')
    if key: text=text.replace(key,'[REDACTED]')
    text=re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----','[REDACTED]',text,flags=re.S)
    text=re.sub(r'(?i)bearer\s+[A-Za-z0-9._~+/=-]+','Bearer [REDACTED]',text)
    text=re.sub(r'\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b','[REDACTED]',text)
    text=re.sub(r'\b[A-Za-z0-9+/=_-]{100,}\b','[REDACTED]',text)
    text=re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b','[synthetic-ip]',text)
    (out/(name+'.sanitized.log')).write_text(text,encoding='utf8')
PY
  event experiment_exit "$status" || true
  case "$private" in "$RUNNER_TEMP"/incident-real.*) rm -rf -- "$private" ;; *) printf '%s\n' 'Refusing unsafe cleanup path.' >&2 ;; esac
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

printf '%s\n' "$(git rev-parse HEAD)" > "$evidence/source-commit.txt"
cat > "$evidence/model-provenance.json" <<'JSON'
{"model":"Qwen/Qwen2.5-7B-Instruct-GGUF Q4_K_M","revision":"bb5d59e06d9551d752d08b292a50eb208b07ab1f","shard_sha256":["dfce12e3862a5283ccfb88221b48480e58745165de856439950d0f22590580db","539cf93f78e887edea1c04e2d7d8cdaca9d01dae9c9025bcb8accbe29df3d72a"],"llama_cpp":"b12433","llama_archive_sha256":"209023cfc6061720802c103e5b7ba9f99e9304a3bdcd035fc50c3fba064cb79f","limitations":"Synthetic data; no human UAT or accuracy claim. Model logs require independent review."}
JSON
python3 -m venv "$private/venv"
"$private/venv/bin/python" -m pip install --disable-pip-version-check -q 'holmesgpt==0.43.0'
"$private/venv/bin/python" - "$evidence/versions.json" <<'PY'
import importlib.metadata as m,json,platform,sys
items=sorted((d.metadata.get('Name',''),d.version) for d in m.distributions())
with open(sys.argv[1],'w',encoding='utf8') as f:
    json.dump({'python':platform.python_version(),'packages':items},f,indent=2)
PY
event holmes_install complete

llama_url='https://github.com/ggml-org/llama.cpp/releases/download/b12433/llama-b12433-bin-ubuntu-x64.tar.gz'
revision='bb5d59e06d9551d752d08b292a50eb208b07ab1f'
base="https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/$revision"
curl --fail --location --retry 2 --connect-timeout 20 --max-time 600 --silent --show-error "$llama_url" -o "$private/llama.tgz"
printf '%s  %s\n' '209023cfc6061720802c103e5b7ba9f99e9304a3bdcd035fc50c3fba064cb79f' "$private/llama.tgz" | sha256sum --check --status
tar -xzf "$private/llama.tgz" -C "$private"
for part in 00001 00002; do
  file="qwen2.5-7b-instruct-q4_k_m-$part-of-00002.gguf"
  curl --fail --location --retry 2 --connect-timeout 20 --max-time 900 --silent --show-error "$base/$file" -o "$private/$file"
done
printf '%s  %s\n' 'dfce12e3862a5283ccfb88221b48480e58745165de856439950d0f22590580db' "$private/qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf" | sha256sum --check --status
printf '%s  %s\n' '539cf93f78e887edea1c04e2d7d8cdaca9d01dae9c9025bcb8accbe29df3d72a' "$private/qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf" | sha256sum --check --status
event pinned_model_and_server verified

openssl rand -hex 32 > "$key_file"
api_key=$(<"$key_file")
printf '::add-mask::%s\n' "$api_key"
port=$(python3 - <<'PY'
import socket
with socket.socket() as s:
    s.bind(('127.0.0.1',0)); print(s.getsockname()[1])
PY
)
mkdir -p "$private/home"
export NO_PROXY='127.0.0.1,localhost' no_proxy='127.0.0.1,localhost'
env -i PATH="$PATH" HOME="$private/home" LANG=C.UTF-8 LD_LIBRARY_PATH="$private/llama-b12433" \
  bash -c 'ulimit -v 12582912; exec "$@"' _ "$private/llama-b12433/llama-server" \
  --offline --model "$private/qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf" \
  --alias qwen2.5-7b --host 127.0.0.1 --port "$port" --api-key-file "$key_file" \
  --n-gpu-layers 0 --ctx-size 16384 --threads 4 --threads-batch 4 --parallel 1 --no-warmup --jinja \
  > "$private/model.log" 2>&1 &
model_pid=$!
for _ in {1..180}; do
  if ! kill -0 "$model_pid" 2>/dev/null; then event model_server exited; exit 1; fi
  if curl --noproxy '*' --fail --silent --max-time 2 "http://127.0.0.1:$port/health" >/dev/null; then break; fi
  sleep 2
done
curl --noproxy '*' --fail --silent --max-time 2 "http://127.0.0.1:$port/health" >/dev/null || { event model_server not_ready; exit 1; }
event model_server ready

# A real model preflight, not the later Kubernetes investigation or accuracy test.
if python3 - "$port" "$key_file" "$evidence/model-preflight.json" <<'PY'
import json,re,sys,time,urllib.error,urllib.request
port,keyfile,evidence=sys.argv[1:]
key=open(keyfile,encoding='ascii').read().strip()
tools=[{'type':'function','function':{'name':name,'description':desc,'parameters':{'type':'object','properties':{},'additionalProperties':False}}} for name,desc in [
 ('incident_lab_pod_snapshot','Read the lab pod snapshot.'),
 ('incident_lab_service_route_evidence','Read lab pod labels and service routes.'),
 ('incident_lab_gateway_error_logs','Read lab gateway error logs.')]]
body={'model':'qwen2.5-7b','messages':[{'role':'user','content':'Use incident_lab_pod_snapshot to check the lab pods.'}],
      'tools':tools,'tool_choice':'auto','max_tokens':256,'stream':False}
req=urllib.request.Request('http://127.0.0.1:'+port+'/v1/chat/completions',data=json.dumps(body).encode(),
                           headers={'Content-Type':'application/json','Authorization':'Bearer '+key})
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
record={'result':'failed','unauthenticated_request_rejected':False,
        'scope':'One synthetic tool-call preflight; not an investigation or accuracy score.'}
def scrub(value):
    value=str(value).replace(key,'[REDACTED]')[:800]
    value=re.sub(r'(?i)bearer\s+[A-Za-z0-9._~+/=-]+','Bearer [REDACTED]',value)
    value=re.sub(r'\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b','[REDACTED]',value)
    return re.sub(r'\b[A-Za-z0-9+/=_-]{100,}\b','[REDACTED]',value)
started=time.monotonic()
try:
    try:
        opener.open('http://127.0.0.1:'+port+'/v1/models',timeout=10)
        raise ValueError('unauthenticated request unexpectedly succeeded')
    except urllib.error.HTTPError as error:
        record['unauthenticated_http_status']=error.code
        if error.code != 401: raise
        record['unauthenticated_request_rejected']=True
    with opener.open(req,timeout=240) as response:
        record['model_http_status']=response.status
        data=json.load(response)
    choice=data['choices'][0]; calls=choice['message'].get('tool_calls') or []
    if choice['message'].get('content') is not None:
        record['response_summary']=scrub(choice['message']['content'])
    record['finish_reason']=choice.get('finish_reason') if choice.get('finish_reason') in ('stop','length','tool_calls') else 'other'
    allowed={x['function']['name'] for x in tools}
    record['tool_names']=[name if name in allowed else 'other' for name in (c.get('function',{}).get('name') for c in calls)]
    record['tool_calls']=[{'type':c.get('type') if c.get('type')=='function' else 'other',
                           'name':c.get('function',{}).get('name') if c.get('function',{}).get('name') in allowed else 'other',
                           'arguments':scrub(c.get('function',{}).get('arguments',''))} for c in calls[:5]]
    assert choice.get('finish_reason')=='tool_calls' and len(calls)==1
    assert calls[0].get('type')=='function' and calls[0].get('function',{}).get('name')=='incident_lab_pod_snapshot'
    assert json.loads(calls[0]['function']['arguments']) == {}
    record['result']='passed'
except Exception as error:
    record['error_type']=type(error).__name__
    if isinstance(error,urllib.error.HTTPError):
        record['model_http_status']=error.code
        try: record['response_summary']=scrub(error.read(1024).decode('utf8','replace'))
        finally: error.close()
    raise SystemExit(1)
finally:
    record['elapsed_ms']=round((time.monotonic()-started)*1000)
    with open(evidence,'w',encoding='utf8') as f: json.dump(record,f,indent=2)
PY
then event real_tool_preflight passed; else event real_tool_preflight failed; exit 1; fi

kubeconfig=${KUBECONFIG:-$HOME/.kube/config}
context='kind-incident-workbench'
# Do not pass GitHub runner credentials or unrelated model/provider settings to
# the investigation and its fixed read-only tools. The one-time key is read
# from a private file after env has been cleared, never as a command argument.
clean_command() {
  env -i PATH="$PATH" HOME="$private/home" TMPDIR="$private" LANG=C.UTF-8 \
    KUBECONFIG="$kubeconfig" MODEL_KEY_FILE="$key_file" \
    OPENAI_API_BASE="http://127.0.0.1:$port/v1" HOLMES_MODEL='openai/qwen2.5-7b' \
    HOLMES_PYTHON="$private/venv/bin/python" LAB_CONTEXT="$context" \
    NO_PROXY='127.0.0.1,localhost' no_proxy='127.0.0.1,localhost' \
    DO_NOT_TRACK=1 OTEL_SDK_DISABLED=true LITELLM_LOCAL_MODEL_COST_MAP=True \
    bash -c 'export OPENAI_API_KEY="$(<"$MODEL_KEY_FILE")"; unset MODEL_KEY_FILE; exec "$@"' _ "$@"
}

check_recovery() {
  local local_port ok=0
  clean_command kubectl --context "$context" -n incident-lab get service t1-web -o json | \
    python3 -c 'import json,sys; assert json.load(sys.stdin)["spec"]["selector"] == {"app":"t1-web"}' || return 1
  clean_command kubectl --context "$context" -n incident-lab get configmap t3-gateway-config -o json | \
    python3 -c 'import json,sys; assert "proxy_pass http://t1-web.incident-lab.svc.cluster.local:80/;" in json.load(sys.stdin)["data"]["default.conf"]' || return 1
  clean_command kubectl --context "$context" -n incident-lab get endpointslices -l kubernetes.io/service-name=t1-web -o json | \
    python3 -c 'import json,sys; d=json.load(sys.stdin); assert any(e.get("addresses") and (e.get("conditions") or {}).get("ready") is not False for i in d["items"] for e in i.get("endpoints",[]))' || return 1
  local_port=$(python3 - <<'PY'
import socket
with socket.socket() as s:
    s.bind(('127.0.0.1',0)); print(s.getsockname()[1])
PY
)
  clean_command kubectl --context "$context" -n incident-lab port-forward --address 127.0.0.1 \
    service/t3-gateway "$local_port:8080" > "$private/forward.log" 2>&1 &
  forward_pid=$!
  for _ in {1..30}; do
    if curl --noproxy '*' --fail --silent --max-time 2 "http://127.0.0.1:$local_port/api/" >/dev/null; then ok=1; break; fi
    kill -0 "$forward_pid" 2>/dev/null || break
    sleep 1
  done
  kill "$forward_pid" 2>/dev/null || true
  wait "$forward_pid" 2>/dev/null || true
  forward_pid=''
  [[ $ok == 1 ]]
}

if clean_command bash scripts/smoke.sh --context "$context" > "$private/smoke.log" 2>&1; then
  event smoke passed
else
  event smoke failed
  exit 1
fi

failed=0
for scenario in t1 t3; do
  event "$scenario" started
  if clean_command timeout --signal=TERM --kill-after=180s 600s bash scripts/reproduce.sh --context "$context" \
       --case "$scenario" --on-fault scripts/investigate.sh > "$private/$scenario.log" 2>&1; then
    event "$scenario" hook_completed_needs_human_review
  else
    event "$scenario" hook_failed
    failed=1
  fi
  # Hook exit alone does not prove restoration; independently check the
  # selector, configuration, endpoints, rollouts and business HTTP response.
  if grep -Fq 'Restored: /healthz=200 /api/=200' "$private/$scenario.log" && \
     clean_command kubectl --context "$context" -n incident-lab rollout status deployment/t1-web --timeout=120s >/dev/null && \
     clean_command kubectl --context "$context" -n incident-lab rollout status deployment/t3-gateway --timeout=120s >/dev/null && \
     check_recovery; then
    event "$scenario" restoration_observed
  else
    event "$scenario" restoration_unconfirmed
    exit 1
  fi
done
event model_answers require_independent_review
exit "$failed"
