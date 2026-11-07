#!/usr/bin/env bash
# Real Locust runs from a temporary pod inside the cluster: requests go to the Service DNS
# name, so neither the laptop link nor a port-forward is in the measured path.
#
# usage:
#   run_incluster_load.sh up <verified-fixture.jsonl>
#   run_incluster_load.sh run <feature|inference> <smoke|low|medium|high> <evidence-dir>
#   run_incluster_load.sh down
#
# Env: KUBE_CONTEXT (default: current), IMAGE_DIGEST, EWALLET_LOAD_MODEL_VERSION,
#      LOAD_RUN_TIME (overrides the profile), LOAD_WAIT_SECONDS (think time, default 0),
#      LOAD_SEED (entity order, default 20261107), UV (default: uv).
# Metrics are scraped through a port-forward only before and after each run, never during it.
set -euo pipefail
action=${1:?action required: up, run or down}
root=$(cd "$(dirname "$0")/../.." && pwd)
ns=ml-platform pod=real-load-runner label=ewallet.io/temporary=real-load
ctx=${KUBE_CONTEXT:-$(kubectl config current-context)}
k() { kubectl --context "$ctx" "$@"; }

up() {
  local fixture=${1:?verified fixture required} tmp
  tmp=$(mktemp -d)
  "${UV:-uv}" export --project "$root/infra/load-test" --locked --format requirements-txt --no-header \
    --quiet > "$tmp/requirements.txt"
  k -n "$ns" create configmap real-load-code --from-file="$root/tests/load/__init__.py" \
    --from-file="$root/tests/load/locustfile_real.py" --from-file="$root/tests/load/real_targets.py"
  k -n "$ns" create configmap real-load-fixture --from-file=entities.jsonl="$fixture"
  k -n "$ns" create configmap real-load-requirements --from-file=requirements.txt="$tmp/requirements.txt"
  k -n "$ns" label configmap real-load-code real-load-fixture real-load-requirements "$label"
  k apply -f "$root/infra/kubernetes/load-test/real-load-runner.yaml"
  k -n "$ns" wait "pod/$pod" --for=condition=Ready --timeout=600s
  k -n "$ns" get pod "$pod" -o wide
}

scrape() {  # scrape <port> <file>
  curl -sSfL --max-time 20 "http://127.0.0.1:$1/metrics/" -o "$2" || curl -sSfL --max-time 20 "http://127.0.0.1:$1/metrics" -o "$2"
}

state() {  # nodes and restart counts of everything the run depends on
  k get nodes --no-headers
  local dependency
  for dependency in "ml-platform app.kubernetes.io/name=fraud-api" "ml-platform app.kubernetes.io/name=feature-api" \
    "fraud-serving serving.kserve.io/inferenceservice=fraud-detector" "feature-store statefulset.kubernetes.io/pod-name=redis-0"; do
    set -- $dependency
    k -n "$1" get pods -l "$2" --no-headers -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName,RESTARTS:.status.containerStatuses[*].restartCount,READY:.status.containerStatuses[*].ready
  done
}

run() {
  local target=${1:?target required} profile=${2:?profile required} evidence=${3:?evidence dir required}
  local svc families out node started status=0 pids=() pf=()
  # shellcheck source=load_profiles.sh
  . "$root/scripts/testing/load_profiles.sh"
  load_profile "$profile" || exit 2
  case "$target" in
    feature) svc=feature-api families=(feature_api_request_latency_seconds backend_client_latency_seconds) ;;
    inference) svc=fraud-api families=(fraud_inference_stage_latency_seconds backend_client_latency_seconds) ;;
    *) echo "target must be feature or inference" >&2; exit 2 ;;
  esac
  local run_time=${LOAD_RUN_TIME:-$default_run_time}
  out=$evidence/$target-incluster-$profile; mkdir -p "$out/metrics"; out=$(cd "$out" && pwd)
  node=$(k -n "$ns" get pod "$pod" -o jsonpath='{.spec.nodeName}')
  state > "$out/state-before.txt"
  started=$(date -u +%FT%TZ)
  jq -n --arg target "$target" --arg profile "$profile" --arg host "http://$svc.$ns.svc:80" --arg started "$started" \
    --argjson users "$users" --argjson spawn "$spawn" --arg run_time "$run_time" --arg node "$node" \
    --arg wait "${LOAD_WAIT_SECONDS:-0}" --arg seed "${LOAD_SEED:-20261107}" \
    --arg fixture_sha256 "$(k -n "$ns" exec "$pod" -- sha256sum /work/fixture/entities.jsonl | cut -d' ' -f1)" \
    --argjson fixture_rows "$(k -n "$ns" exec "$pod" -- sh -c 'grep -c . /work/fixture/entities.jsonl')" \
    --arg source_sha "$(git -C "$root" rev-parse HEAD)" --arg image_digest "${IMAGE_DIGEST:-unrecorded}" \
    --arg model_version "${EWALLET_LOAD_MODEL_VERSION:-unrecorded}" \
    '{target:$target,network_path:"in-cluster",client:("pod real-load-runner on "+$node+", Service DNS, no port-forward"),
      profile:$profile,host:$host,users:$users,spawn_rate:$spawn,run_time:$run_time,think_time_seconds:($wait|tonumber),
      entity_order_seed:($seed|tonumber),fixture:"verified.jsonl",fixture_sha256:$fixture_sha256,fixture_rows:$fixture_rows,
      source_sha:$source_sha,image_digest:$image_digest,model_version:$model_version,
      request_profile:"valid requests only",started_at:$started}' > "$out/run-manifest.json"

  # kubectl itself, not the k() wrapper: a backgrounded function is a subshell, and killing it leaves kubectl running.
  kubectl --context "$ctx" -n "$ns" port-forward "deploy/$svc" 18190:8080 >/dev/null 2>&1 & pf+=($!)
  if [ "$target" = inference ]; then
    kubectl --context "$ctx" -n fraud-serving port-forward "$(k -n fraud-serving get pods \
      -l serving.kserve.io/inferenceservice=fraud-detector -o name | head -1)" 18192:8080 >/dev/null 2>&1 & pf+=($!)
  fi
  sleep 4
  scrape 18190 "$out/metrics/$svc-before.txt"
  [ "$target" = inference ] && scrape 18192 "$out/metrics/predictor-before.txt"
  for sampled in ml-platform fraud-serving feature-store; do
    python3 -m ops.experiments.resource_sampler --context "$ctx" --namespace "$sampled" \
      --output "$out/resources-$sampled.jsonl" --stop-file "$out/.stop" --interval 10 & pids+=($!)
  done

  k -n "$ns" exec "$pod" -- sh -c 'rm -rf /tmp/out && mkdir -p /tmp/out'
  k -n "$ns" exec "$pod" -- env EWALLET_LOAD_TARGET="$target" EWALLET_LOAD_ENTITIES=/work/fixture/entities.jsonl \
    EWALLET_LOAD_ACKNOWLEDGE_REMOTE=I_OWN_THIS_TARGET EWALLET_LOAD_MODEL_VERSION="${EWALLET_LOAD_MODEL_VERSION:-}" \
    EWALLET_LOAD_WAIT_SECONDS="${LOAD_WAIT_SECONDS:-0}" EWALLET_LOAD_SEED="${LOAD_SEED:-20261107}" \
    sh -c "cd /work && /tmp/venv/bin/locust -f tests/load/locustfile_real.py --headless \
      --host http://$svc.$ns.svc:80 --users $users --spawn-rate $spawn --run-time $run_time --stop-timeout 10 \
      --only-summary --html /tmp/out/report.html --csv /tmp/out/locust --csv-full-history" \
    2>&1 | tee "$out/terminal.log" || status=$?

  scrape 18190 "$out/metrics/$svc-after.txt" || true
  [ "$target" = inference ] && { scrape 18192 "$out/metrics/predictor-after.txt" || true; }
  touch "$out/.stop"; wait "${pids[@]}" || true; rm -f "$out/.stop"
  kill "${pf[@]}" 2>/dev/null || true
  state > "$out/state-after.txt"
  k -n "$ns" cp "$pod:/tmp/out/." "$out/" >/dev/null
  jq --arg ended "$(date -u +%FT%TZ)" '. + {ended_at:$ended}' "$out/run-manifest.json" > "$out/manifest.tmp"
  mv "$out/manifest.tmp" "$out/run-manifest.json"
  local args=(); for family in "${families[@]}"; do args+=(--family "$family"); done
  python3 -m ops.experiments.server_metrics --before "$out/metrics/$svc-before.txt" \
    --after "$out/metrics/$svc-after.txt" "${args[@]}" --out "$out/server-metrics.json"
  [ "$target" = inference ] && python3 -m ops.experiments.server_metrics --before "$out/metrics/predictor-before.txt" \
    --after "$out/metrics/predictor-after.txt" --family request_predict_seconds --out "$out/kserve-metrics.json"
  (cd "$root" && python3 scripts/testing/real_load_report.py --run-dir "$out")
  return "$status"
}

down() {
  k delete -f "$root/infra/kubernetes/load-test/real-load-runner.yaml" --ignore-not-found --wait=true
  k -n "$ns" delete configmap -l "$label" --ignore-not-found
}

case "$action" in
  up) up "${2:-}" ;;
  run) cd "$root" && run "${2:-}" "${3:-}" "${4:-}" ;;
  down) down ;;
  *) echo "action must be up, run or down" >&2; exit 2 ;;
esac
