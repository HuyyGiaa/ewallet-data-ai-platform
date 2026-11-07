#!/usr/bin/env bash
# Owner-run LLM A/B (package P10). Each stage has its own approval; none implies another.
# usage: llm_ab.sh preflight                     read-only: quota, zone, cost
#        llm_ab.sh render <evidence-dir>         local: both EXP-V3A arms and their diff
#        llm_ab.sh create-gpu-pool               GPU_POOL_APPROVED=yes (one L4 node, billable)
#        llm_ab.sh run-v3a <evidence-dir>        EXPERIMENT_APPROVED=yes (needs the pool)
#        llm_ab.sh run-v3b <evidence-dir>        EXPERIMENT_APPROVED=yes (after the runbook's V3B deploy)
#        llm_ab.sh cleanup                       removes the namespace and the pool, reads back zero
# This driver never changes a production release. Deploying the V3B routes is a separate
# owner step in the C-C approval packages.
# Env: KUBE_CONTEXT, VLLM_GPU_IMAGE (digest), GPU_SPOT (default yes), EXP_RUNS (default 3),
#      EXP_MAX_MINUTES (default 180).
set -euo pipefail
action=${1:-} evidence=${2:-}
PROJECT=ewallet-data-ai-platform-hg26 ZONE=asia-southeast1-b REGION=asia-southeast1 CLUSTER=ewallet-dev
POOL=gpu-pool NS=ewallet-bench OWNER_LABEL=ewallet.io/bench=true GW_PORT=13310 METRICS_PORT=13311
NODE_SA=ewallet-gke-nodes@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com
EXPECTED=gke_${PROJECT}_${ZONE}_${CLUSTER}
GATEWAY_IMAGE=ghcr.io/agentgateway/agentgateway:v1.7.0@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b
root=$(cd "$(dirname "$0")/../.." && pwd)
python=${EXP_PYTHON:-python3}; export PYTHONPATH=$root${PYTHONPATH:+:$PYTHONPATH}
runs=${EXP_RUNS:-3} max_minutes=${EXP_MAX_MINUTES:-180}
die() { echo "llm_ab: $*" >&2; exit 1; }
k() { kubectl --context "$KUBE_CONTEXT" --request-timeout=60s "$@"; }
need_context() { [ "${KUBE_CONTEXT:-}" = "$EXPECTED" ] || die "KUBE_CONTEXT must be $EXPECTED"; }
need_image() { [[ "${VLLM_GPU_IMAGE:-}" == *@sha256:* ]] || die "VLLM_GPU_IMAGE must be pinned by digest"; }
approved() { [ "${!1:-}" = yes ] || die "$1=yes is required; this stage is approved separately"; }
gpu_quota() {
  gcloud compute project-info describe --project "$PROJECT" --format=json \
    | jq '[.quotas[]|select(.metric=="GPUS_ALL_REGIONS")|.limit][0]'
}

case "$action" in
  preflight)
    quota=$(gpu_quota)
    echo "GPUS_ALL_REGIONS limit=$quota (needs at least 1)"
    gcloud compute regions describe "$REGION" --project "$PROJECT" --format=json \
      | jq -c '.quotas[]|select(.metric|test("NVIDIA_L4"))'
    gcloud compute accelerator-types describe nvidia-l4 --zone "$ZONE" --project "$PROJECT" --format='value(name)'
    echo "cost: g2-standard-4 + L4 about 0.5233 USD/h Spot, 0.8720 USD/h on demand (catalog, 2026-11-06)"
    [ "${quota%.*}" -ge 1 ] || die "GPU quota is 0: no GPU stage can run"
    ;;
  render)
    [ -n "$evidence" ] || die "evidence directory required"
    need_image; mkdir -p "$evidence/plan"
    for arm in baseline optimized; do
      "$python" "$root/scripts/experiments/render_bench.py" exp-v3a "$arm" --vllm-image "$VLLM_GPU_IMAGE" \
        --gateway-image "$GATEWAY_IMAGE" --run-id plan --api-server-cidr 192.0.2.1/32 --api-server-port 443 \
        > "$evidence/plan/$arm.yaml"
    done
    diff "$evidence/plan/baseline.yaml" "$evidence/plan/optimized.yaml" > "$evidence/plan/arm-difference.diff" || true
    cat "$evidence/plan/arm-difference.diff"
    ;;
  create-gpu-pool)
    approved GPU_POOL_APPROVED; need_context
    [ "$(gpu_quota | cut -d. -f1)" -ge 1 ] || die "GPU quota is 0"
    spot=(); [ "${GPU_SPOT:-yes}" = yes ] && spot=(--spot)
    gcloud container node-pools create "$POOL" --cluster "$CLUSTER" --zone "$ZONE" --project "$PROJECT" \
      --machine-type g2-standard-4 --accelerator type=nvidia-l4,count=1,gpu-driver-version=latest \
      --num-nodes 1 --disk-type pd-balanced --disk-size 100 --image-type COS_CONTAINERD \
      --service-account "$NODE_SA" --workload-metadata GKE_METADATA \
      --shielded-secure-boot --shielded-integrity-monitoring --node-labels "$OWNER_LABEL" "${spot[@]}" --quiet
    k wait --for=condition=Ready nodes -l cloud.google.com/gke-accelerator=nvidia-l4 --timeout=600s
    k get nodes -l cloud.google.com/gke-accelerator=nvidia-l4 -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}'; echo
    ;;
  run-v3a)
    approved EXPERIMENT_APPROVED; need_context; need_image
    [ -n "$evidence" ] || die "evidence directory required"
    [[ "$runs" =~ ^[2-5]$ ]] || die "EXP_RUNS must be 2-5"
    deadline=$(( $(date +%s) + max_minutes * 60 )); mkdir -p "$evidence"; stop=$evidence/STOP
    api_ip=$(k get endpoints kubernetes -n default -o 'jsonpath={.subsets[0].addresses[0].ip}')
    api_port=$(k get endpoints kubernetes -n default -o 'jsonpath={.subsets[0].ports[0].port}')
    client_key=$(openssl rand -hex 24)
    pids=(); stop_children() { for p in "${pids[@]:-}"; do [ -z "$p" ] || kill "$p" 2>/dev/null || true; done; pids=(); }
    trap stop_children EXIT
    run_arm() {  # <baseline|optimized> <run>
      local arm=$1 run=$2 label dir=$evidence/$1/raw/run-$2 cfg=$evidence/$1/config/run-$2.yaml
      [ "$arm" = baseline ] && label=qwen3-0.6b || label=qwen3-1.7b
      mkdir -p "$dir" "$(dirname "$cfg")"
      "$python" "$root/scripts/experiments/render_bench.py" exp-v3a "$arm" --vllm-image "$VLLM_GPU_IMAGE" \
        --gateway-image "$GATEWAY_IMAGE" --run-id "$arm-$run" --api-server-cidr "$api_ip/32" --api-server-port "$api_port" > "$cfg"
      k apply -f "$cfg" >/dev/null
      k -n "$NS" rollout status deployment/vllm-primary --timeout=1800s
      k -n "$NS" port-forward svc/agentgateway "$GW_PORT:4000" >/dev/null 2>&1 & pids+=("$!")
      k -n "$NS" port-forward deployment/vllm-primary "$METRICS_PORT:8000" >/dev/null 2>&1 & pids+=("$!")
      sleep 5
      "$python" -m ops.experiments.gpu_sampler --context "$KUBE_CONTEXT" --output "$dir/gpu.jsonl" --stop-file "$dir/DONE" & pids+=("$!")
      "$python" -m ops.experiments.resource_sampler --context "$KUBE_CONTEXT" --namespace "$NS" \
        --output "$dir/resources.jsonl" --stop-file "$dir/DONE" & pids+=("$!")
      EWALLET_GATEWAY_KEY=$client_key "$python" "$root/scripts/llm_agent/o1_benchmark.py" --config "$label" \
        --gateway "http://127.0.0.1:$GW_PORT" --metrics "http://127.0.0.1:$METRICS_PORT" --levels 1,2,4 --timeout 120 \
        --allocated-cpu 2.5 --resources "$dir/resources.jsonl" --stop-file "$stop" --output "$dir"
      touch "$dir/DONE"; sleep 6
      "$python" -m ops.experiments.gpu_sampler --output "$dir/gpu.jsonl" --attach-to "$dir/summary.json"
      stop_children
    }
    k create namespace "$NS" --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n "$NS" create secret generic agentgateway-client-key --from-file=api-key=<(printf %s "$client_key") \
      --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n "$NS" create secret generic vllm-api-key --from-file=api-key=<(openssl rand -hex 24 | tr -d '\n') \
      --dry-run=client -o yaml | k apply -f - >/dev/null
    for number in $(seq 1 "$runs"); do
      run=$(printf '%02d' "$number")
      if [ $((number % 2)) = 1 ]; then order="baseline optimized"; else order="optimized baseline"; fi
      for arm in $order; do
        [ "$(date +%s)" -lt "$deadline" ] || { touch "$stop"; die "time limit reached before run $run $arm"; }
        run_arm "$arm" "$run"
      done
    done
    "$python" -m ops.experiments.cli compare --spec "$root/configs/experiments/exp-v3a-model-l4.json" --dir "$evidence"
    ;;
  run-v3b)
    approved EXPERIMENT_APPROVED; need_context
    [ -n "$evidence" ] || die "evidence directory required"
    mkdir -p "$evidence"
    k -n ewallet-agents port-forward svc/agentgateway "$GW_PORT:3000" >/dev/null 2>&1 & forward=$!
    trap 'kill "$forward" 2>/dev/null || true' EXIT
    sleep 5
    "$python" -m ops.experiments.llm_ab_traffic --gateway "http://127.0.0.1:$GW_PORT" --model ewallet-llm-ab \
      --expect primary=90 --expect qwen3-1-7b=10 --requests "${EXP_REQUESTS:-200}" --out "$evidence"
    ;;
  cleanup)
    need_context
    k delete namespace "$NS" --ignore-not-found --wait=true
    if gcloud container node-pools describe "$POOL" --cluster "$CLUSTER" --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1; then
      gcloud container node-pools delete "$POOL" --cluster "$CLUSTER" --zone "$ZONE" --project "$PROJECT" --quiet
    fi
    left=$(k get nodes -l cloud.google.com/gke-accelerator=nvidia-l4 --no-headers 2>/dev/null | wc -l)
    [ "$left" = 0 ] || die "$left GPU nodes remain"
    echo "GPU nodes: 0. Roll back a V3B release as the approval package describes."
    ;;
  *) die "usage: llm_ab.sh <preflight|render|create-gpu-pool|run-v3a|run-v3b|cleanup> [evidence-dir]" ;;
esac
