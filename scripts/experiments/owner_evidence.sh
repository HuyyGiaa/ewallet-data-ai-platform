# shellcheck shell=bash
# Read-only display commands for the owner's EXP-V1 / EXP-V2 screenshots.
# usage: source scripts/experiments/owner_evidence.sh   (in T2, while a session runs)
#
# Every function only reads (kubectl get/logs/exec of a metrics read, local files) or, for
# the ev_policy_* functions, sends one-token requests to the benchmark gateway through the
# runner's own port-forward. Run those only at a checkpoint: after the measured window,
# before the next run restarts the pods. No function prints an API key: keys go from the
# Secret into a local variable and from there into a request header only.
#
# Ports opened by the runner: gateway 13300, vLLM metrics 13301, EPP metrics 13302,
# Grafana 13030, Prometheus 13090.

: "${KUBE_CONTEXT:=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev}"
EV_NS=ewallet-bench
EV_GW=http://127.0.0.1:13300/v1/chat/completions
EV_BODY='{"model":"ewallet-fraud-llm","messages":[{"role":"user","content":"Reply with OK."}],"max_tokens":1,"temperature":0}'

ev_k() { kubectl --context "$KUBE_CONTEXT" -n "$EV_NS" "$@"; }

ev_header() { printf '\n===== %s  (%s UTC) =====\n' "$1" "$(date -u +%FT%TZ)"; }

# Pods, nodes, image digest and the model revision the runner read from the running pod.
ev_provenance() {  # [run-dir, e.g. $CAMPAIGN/exp-v1/baseline/raw/run-01]
  ev_header "bench node, pods, image"
  kubectl --context "$KUBE_CONTEXT" get nodes -l ewallet.io/bench=true -o wide
  ev_k get pods -o wide
  ev_k get pods -l app.kubernetes.io/name=vllm \
    -o jsonpath='{range .items[*]}{.metadata.name}{"  image="}{.status.containerStatuses[0].imageID}{"  run="}{.metadata.annotations.ewallet\.io/run-id}{"\n"}{end}'
  if [ -n "${1:-}" ] && [ -f "$1/controls.json" ]; then
    python3 - "$1/controls.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
for key in ("model_revision", "image_digest", "thread_bind", "replicas", "node_names", "cpu_request", "memory_request",
            "max_num_seqs", "prefix_cache", "gateway_token_budget"):
    print(f"{key:22} {c.get(key)}")
PY
  fi
}

# The one variable of EXP-V1 and the serving settings both arms share.
ev_config() {
  ev_header "vLLM deployment: thread binding, args, resources"
  ev_k get deploy vllm-primary -o jsonpath='{range .spec.template.spec.containers[?(@.name=="vllm")].env[*]}{.name}={.value}{"\n"}{end}' \
    | grep -E '^(VLLM_CPU_OMP_THREADS_BIND|OMP_NUM_THREADS|VLLM_CPU_KVCACHE_SPACE|VLLM_CPU_NUM_OF_RESERVED_CPU)='
  ev_k get deploy vllm-primary -o jsonpath='{.spec.template.spec.containers[?(@.name=="vllm")].args}' \
    | tr ',' '\n' | grep -A1 -E 'max-num-seqs|max-num-queued-reqs|max-model-len|enable-prefix-caching' | tr -d '[]"' | paste -sd' '
  ev_k get deploy vllm-primary -o jsonpath='replicas={.spec.replicas}  resources={.spec.template.spec.containers[?(@.name=="vllm")].resources}{"\n"}'
}

# Where the gateway sends /v1/chat/completions, and its timeout and rate limits (keys removed).
ev_route() {
  ev_header "agentgateway route, timeout and limits"
  ev_k get configmap agentgateway-llm-config -o 'jsonpath={.data.config\.yaml}' \
    | grep -vE 'key:|apiKey|Bearer' \
    | grep -E 'baseUrl|host:|x-ewallet-backend|backendRequestTimeout|requestTimeout|maxTokens|tokensPerFill|fillInterval|type: (requests|tokens)'
}

ev_pods() {
  ev_header "pods and bench nodes"
  ev_k get pods -o wide
  kubectl --context "$KUBE_CONTEXT" get nodes -l ewallet.io/bench=true -o wide
}

# Per-replica completed requests and admission rejections, read on each pod's loopback.
ev_replicas() {
  ev_header "per-replica counters (vLLM /metrics)"
  local pod
  for pod in $(ev_k get pods -l app.kubernetes.io/name=vllm -o 'jsonpath={.items[*].metadata.name}'); do
    echo "--- $pod"
    ev_k exec "$pod" -c vllm -- python3 -c \
      'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=20).read().decode())' \
      | grep -E '^vllm:(request_success_total|admission_rejections_total)' | grep -v ' 0.0$'
  done
}

# EXP-V2 optimized arm: EPP ready endpoints, decisions per endpoint, errors, and its log.
ev_epp() {
  ev_header "llm-d EPP metrics (port-forward 13302)"
  python3 -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:13302/metrics", timeout=10).read().decode())' \
    | grep -E '^llm_d_epp_(ready_endpoints|scheduler_attempts_total|request_total|request_error_total)'
  ev_header "llm-d EPP decisions (last 40 lines with an endpoint)"
  ev_k logs deploy/llmd-router-epp -c epp --tail=400 | grep -i '"endpoint"' | tail -40 \
    | python3 -c 'import json,sys
for line in sys.stdin:
    try: d = json.loads(line)
    except ValueError: continue
    print(d.get("timestamp", "")[:23], d.get("body"), "->", d.get("endpoint"))'
}

ev_targets() {
  ev_header "Prometheus scrape targets (port-forward 13090)"
  python3 - <<'PY'
import json, urllib.request
data = json.load(urllib.request.urlopen("http://127.0.0.1:13090/api/v1/targets?state=active", timeout=10))
for target in data["data"]["activeTargets"]:
    labels = target["labels"]
    print(f'{labels.get("job", ""):13} {labels.get("pod", ""):44} run={labels.get("run_id", "-"):12} '
          f'{target["health"]:5} {target["lastScrape"][:19]}')
PY
}

# Gateway policy, one case at a time. Run at a checkpoint only. The key goes from the Secret
# into a private header file (mode 600, removed afterwards), so it is in neither the
# terminal nor the process list.
_ev_hdr() {  # <secret name>: prints the path of a header file holding that key
  local file; file=$(mktemp); chmod 600 "$file"
  printf 'Authorization: Bearer %s\n' "$(ev_k get secret "$1" -o jsonpath='{.data.api-key}' | base64 -d)" > "$file"
  echo "$file"
}
_ev_call() {  # <label> [curl args...]
  local label=$1; shift
  curl -sS -o /dev/null -w "$label -> HTTP %{http_code}\n" -H 'Content-Type: application/json' "$@" -d "$EV_BODY" "$EV_GW"
}
ev_policy_valid() {
  ev_header "P1 valid client key"
  local hdr; hdr=$(_ev_hdr agentgateway-client-key)
  curl -sS -D - -o /dev/null -H 'Content-Type: application/json' -H "@$hdr" -d "$EV_BODY" "$EV_GW" \
    | grep -iE '^HTTP/|^x-ewallet-backend|^x-ratelimit'
  rm -f "$hdr"
}
ev_policy_missing() { ev_header "P2 no API key"; _ev_call "missing key"; }
ev_policy_wrong() { ev_header "P3 invalid API key"; _ev_call "wrong key" -H "Authorization: Bearer not-a-valid-key"; }
ev_policy_backend_key() {
  ev_header "P3b vLLM backend key used as a client key"
  local hdr; hdr=$(_ev_hdr vllm-api-key)
  _ev_call "backend key" -H "@$hdr"
  rm -f "$hdr"
}
ev_policy_burst() {  # [n=16]: n concurrent requests against the 12/s limit
  local n=${1:-16} hdr; hdr=$(_ev_hdr agentgateway-client-key)
  ev_header "P4 burst of $n concurrent requests against localRateLimit 12/s"
  seq "$n" | xargs -P "$n" -I{} curl -sS -o /dev/null -w '%{http_code}\n' -H 'Content-Type: application/json' \
    -H "@$hdr" -d "$EV_BODY" "$EV_GW" | sort | uniq -c
  rm -f "$hdr"
}
ev_policy_route() {  # three requests; prints the backend header the gateway set
  local hdr i; hdr=$(_ev_hdr agentgateway-client-key)
  ev_header "P5 routing: x-ewallet-backend on 3 requests"
  for i in 1 2 3; do
    curl -sS -D - -o /dev/null -H 'Content-Type: application/json' -H "@$hdr" -d "$EV_BODY" "$EV_GW" \
      | grep -iE '^HTTP/|^x-ewallet-backend' | paste -sd' '
  done
  rm -f "$hdr"
}

# After a session: everything the session created must be gone and production unchanged.
ev_cleanup() {  # <session-dir, e.g. $CAMPAIGN/exp-v1>
  local dir=$1 project=ewallet-data-ai-platform-hg26
  ev_header "cleanup check for $dir"
  tail -2 "$dir/session.log"
  cat "$dir/safety/cleanup.txt"
  gcloud container node-pools list --cluster ewallet-dev --zone asia-southeast1-b --project "$project" --format='value(name)'
  echo "bench VMs:   $(gcloud compute instances list --project "$project" --format='value(name)' | grep -c bench-pool)"
  echo "bench disks: $(gcloud compute disks list --project "$project" --format='value(name)' | grep -c bench-pool)"
  kubectl --context "$KUBE_CONTEXT" get namespace "$EV_NS" 2>&1 | tail -1
  diff "$dir/production-before.txt" "$dir/production-after.txt" >/dev/null && echo "production unchanged (before = after)"
  echo "armed bench timers: $(systemctl --user list-timers --all 2>/dev/null | grep -c ewallet-bench)"
}
