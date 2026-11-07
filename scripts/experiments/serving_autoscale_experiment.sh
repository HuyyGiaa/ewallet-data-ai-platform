#!/usr/bin/env bash
# G5-B owner-run autoscaling experiment for one serving API, using the G5-A in-cluster runner.
#
# usage:
#   serving_autoscale_experiment.sh status  <feature-api|fraud-api>
#   serving_autoscale_experiment.sh watch   <feature-api|fraud-api>          # Ctrl-C to stop
#   serving_autoscale_experiment.sh run     <feature-api|fraud-api> <evidence-dir>
#   serving_autoscale_experiment.sh cleanup
#
# Kubernetes effects:
#   status, watch  read only.
#   run            creates the temporary load runner if absent (pod real-load-runner, its
#                  ConfigMaps and NetworkPolicy allow-real-load-runner-to-fraud-api in
#                  ml-platform), sends 20 users for 5 minutes, then only observes. KEDA and
#                  the HPA change the replica count; this script never does.
#   cleanup        deletes only those temporary runner objects. It never touches KEDA,
#                  ScaledObjects, HPAs, serving Deployments, Prometheus or evidence.
#
# Bounds and stops: one Locust run (G5-A "high": 20 users, spawn 5/s, 5 min). Observation
# after the load ends at 12 minutes or once the API is back at minReplicas. Refuses to start if
# a GKE operation is running, the ScaledObject is not Ready, or the API is not at minReplicas.
# Env: KUBE_CONTEXT (required), FIXTURE (default: the Package 4 verified fixture),
#      LOAD_PROFILE (default high), SCALE_DOWN_TIMEOUT (default 720 s).
set -euo pipefail
action=${1:?action required: status, watch, run or cleanup}
root=$(cd "$(dirname "$0")/../.." && pwd)
ns=ml-platform
: "${KUBE_CONTEXT:?KUBE_CONTEXT is required}"
k() { kubectl --context "$KUBE_CONTEXT" --request-timeout=30s "$@"; }
say() { printf '%s %s\n' "$(date -u +%T)" "$*"; }
api_target() { case "$1" in feature-api) echo feature ;; fraud-api) echo inference ;; *) echo "api must be feature-api or fraud-api" >&2; exit 2 ;; esac; }

status() {
  local api=$1
  k -n "$ns" get scaledobject "$api" -o wide
  k -n "$ns" get hpa "keda-hpa-$api"
  k -n "$ns" get deployment "$api"
  k -n "$ns" get pods -l "app.kubernetes.io/name=$api" -o wide
}

case "$action" in
  status) api_target "${2:-}" >/dev/null; status "$2" ;;
  watch)
    api=${2:-}; api_target "$api" >/dev/null
    while true; do
      clear 2>/dev/null || true; date -u +%FT%TZ
      k -n "$ns" get hpa "keda-hpa-$api" --no-headers -o custom-columns=HPA:.metadata.name,METRIC:.status.currentMetrics[0].external.current.averageValue,CURRENT:.status.currentReplicas,DESIRED:.status.desiredReplicas
      k -n "$ns" get pods -l "app.kubernetes.io/name=$api" --no-headers
      sleep 5
    done ;;
  cleanup) "$root/scripts/testing/run_incluster_load.sh" down ;;
  run)
    api=${2:-} evidence=${3:?evidence dir required}; target=$(api_target "$api")
    profile=${LOAD_PROFILE:-high} timeout=${SCALE_DOWN_TIMEOUT:-720}
    fixture=${FIXTURE:-$root/docs/evidence/edai2/serving/pkg4_20261107T155413Z/fixture/verified.jsonl}
    out=$evidence/$api; mkdir -p "$out"; out=$(cd "$out" && pwd)
    [ -z "$(gcloud container operations list --zone asia-southeast1-b --project ewallet-data-ai-platform-hg26 \
      --filter='status!=DONE' --format='value(operationType)' 2>/dev/null)" ] || { say "STOP: a GKE operation is running"; exit 3; }
    [ "$(k -n "$ns" get scaledobject "$api" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')" = True ] \
      || { say "STOP: ScaledObject $api is not Ready"; exit 3; }
    min=$(k -n "$ns" get scaledobject "$api" -o jsonpath='{.spec.minReplicaCount}')
    ready=$(k -n "$ns" get deployment "$api" -o jsonpath='{.status.readyReplicas}')
    [ "$ready" = "$min" ] || { say "STOP: $api has $ready ready replicas, expected $min; wait for scale-down"; exit 3; }
    k -n "$ns" get pod real-load-runner >/dev/null 2>&1 || "$root/scripts/testing/run_incluster_load.sh" up "$fixture"
    say "baseline: $api at $ready replica(s); recording to $out"
    status "$api" > "$out/state-before.txt" 2>&1
    k -n "$ns" get scaledobject "$api" -o yaml > "$out/scaledobject.yaml"
    k -n "$ns" get hpa "keda-hpa-$api" -o yaml > "$out/hpa-before.yaml"
    (cd "$root" && exec python3 -m ops.experiments.replica_sampler --context "$KUBE_CONTEXT" --namespace "$ns" \
      --deployment "$api" --hpa "keda-hpa-$api" --interval 5 --output "$out/replicas.jsonl") & sampler=$!
    trap 'kill $sampler 2>/dev/null || true' EXIT
    say "load: Locust $profile against $api (run 'watch $api' in another terminal)"
    status_code=0
    "$root/scripts/testing/run_incluster_load.sh" run "$target" "$profile" "$out" || status_code=$?
    run_dir=$out/$target-incluster-$profile
    say "load finished (exit $status_code); observing scale-down for up to ${timeout}s"
    waited=0
    while [ "$waited" -lt "$timeout" ]; do
      ready=$(k -n "$ns" get deployment "$api" -o jsonpath='{.status.readyReplicas}')
      say "ready=$ready (min $min)"
      [ "$ready" = "$min" ] && break
      sleep 15; waited=$((waited + 15))
    done
    sleep 10; kill "$sampler" 2>/dev/null || true; trap - EXIT
    k -n "$ns" get hpa "keda-hpa-$api" -o yaml > "$out/hpa-after.yaml"
    k -n "$ns" describe hpa "keda-hpa-$api" > "$out/hpa-describe.txt"
    k -n "$ns" get events --sort-by=.lastTimestamp --field-selector "involvedObject.name=keda-hpa-$api" > "$out/hpa-events.txt" 2>&1 || true
    k -n "$ns" get events --sort-by=.lastTimestamp --field-selector "involvedObject.name=$api" > "$out/deployment-events.txt" 2>&1 || true
    status "$api" > "$out/state-after.txt" 2>&1
    (cd "$root" && python3 -m ops.experiments.autoscale_summary --samples "$out/replicas.jsonl" \
      --manifest "$run_dir/run-manifest.json" --min-replicas "$min" --out "$out/autoscale-summary.json")
    say "Locust summary: $run_dir/summary.json; HTML report: $run_dir/report.html"
    exit "$status_code" ;;
  *) echo "action must be status, watch, run or cleanup" >&2; exit 2 ;;
esac
