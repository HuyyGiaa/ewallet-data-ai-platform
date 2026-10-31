#!/usr/bin/env bash
# Local Flink baseline/optimized benchmark driver (run from a WSL2 shell on the
# benchmark PC, inside any clone of the repository).
#
#   scripts/streaming/flink_bench.sh prereq
#   scripts/streaming/flink_bench.sh up
#   scripts/streaming/flink_bench.sh functional baseline|optimized
#   scripts/streaming/flink_bench.sh perf baseline|optimized <100|500|1000|2000> [duration_s]
#   scripts/streaming/flink_bench.sh compare <baseline_run_dir> <optimized_run_dir>
#   scripts/streaming/flink_bench.sh down
#
# Every run is bounded (fixed event count), uses a fresh namespace (topics,
# consumer group, checkpoint dir) and restarts the Flink cluster first so no
# JVM/Python-worker warm state carries over between profiles.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE=(docker compose -f "$ROOT/infra/docker/flink-benchmark/compose.yml")
EVIDENCE="$ROOT/evidence/flink-benchmark"
FLINK_URL="http://localhost:8081"

die() { echo "ERROR: $*" >&2; exit 1; }

flink_get() { curl -fsS "$FLINK_URL$1"; }

running_jobs() {
  flink_get /jobs/overview | python3 -c 'import json,sys; print(" ".join(j["jid"] for j in json.load(sys.stdin)["jobs"] if j["state"] in ("RUNNING","CREATED","RESTARTING","INITIALIZING")))'
}

wait_flink() {
  for _ in $(seq 1 60); do
    slots=$(flink_get /overview 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["slots-available"])' 2>/dev/null || echo 0)
    [[ "$slots" -ge 4 ]] && return 0
    sleep 5
  done
  die "Flink did not report 4 free task slots"
}

cmd_prereq() {
  docker version --format 'docker client={{.Client.Version}} server={{.Server.Version}}'
  docker compose version
  docker info --format 'docker cpus={{.NCPU}} mem_bytes={{.MemTotal}} os={{.OperatingSystem}}'
  command -v python3 >/dev/null || die "python3 is required on the host (WSL2) for JSON parsing"
  command -v curl >/dev/null || die "curl is required on the host (WSL2)"
  mem=$(docker info --format '{{.MemTotal}}')
  [[ "$mem" -ge $((14 * 1024 * 1024 * 1024)) ]] || echo "WARN: Docker has <14 GiB; raise memory in %UserProfile%\\.wslconfig"
  for port in 8081 19092 19644; do
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then echo "WARN: port $port already in use"; fi
  done
  if command -v uv >/dev/null; then
    uv --version
    uv lock --check --project "$ROOT/infra/docker/flink-benchmark"
  else
    echo "INFO: uv not on host PATH; containers install from the committed uv.lock"
  fi
  "${COMPOSE[@]}" config --quiet && echo "compose config OK"
}

cmd_up() {
  mkdir -p "$EVIDENCE"
  "${COMPOSE[@]}" build jobmanager
  "${COMPOSE[@]}" up -d redpanda jobmanager taskmanager
  wait_flink
  echo "Flink UI: $FLINK_URL   Redpanda (host): localhost:19092"
}

cmd_down() { "${COMPOSE[@]}" --profile tools down; }

image_ref() {
  docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || echo unavailable
}

run_benchmark() {
  local profile="$1" mode="$2" rate="${3:-}" duration="${4:-120}"
  [[ "$profile" == baseline || "$profile" == optimized ]] || die "profile must be baseline|optimized"
  [[ -z "$(running_jobs)" ]] || die "a Flink job is already running; benchmarks run sequentially"
  local stamp ns run replay_args
  stamp=$(date -u +%Y%m%dt%H%M%Sz)
  if [[ "$mode" == functional ]]; then
    ns="bench-func-${profile}-${stamp}"
    replay_args=(--mode functional)
  else
    ns="bench-perf-${profile}-${rate}-${stamp}"
    replay_args=(--mode performance --rate "$rate" --duration-seconds "$duration")
  fi
  run="$EVIDENCE/$ns"
  mkdir -p "$run/input"
  local failures=()

  echo "== restart Flink cluster (no warm state) =="
  "${COMPOSE[@]}" restart jobmanager taskmanager
  wait_flink

  local config_sha
  config_sha=$("${COMPOSE[@]}" exec -T jobmanager python -c "from data_platform.streaming.config.settings import profile_by_name; print(profile_by_name('$profile').config_sha256)")
  python3 - "$run/run.json" <<EOF
import json, subprocess, sys
def sh(*a):
    try: return subprocess.run(a, capture_output=True, text=True, check=True, cwd="$ROOT").stdout.strip()
    except Exception as e: return f"unavailable: {e}"
json.dump({
  "namespace": "$ns", "profile": "$profile", "mode": "$mode", "target_rate_eps": "${rate}" or None,
  "git_sha": sh("git", "rev-parse", "HEAD"), "git_dirty": bool(sh("git", "status", "--porcelain")),
  "config_sha256": "$config_sha",
  "images": {"flink_bench": "$(image_ref ewallet-flink-bench:local)",
             "redpanda": "$(image_ref docker.redpanda.com/redpandadata/redpanda@sha256:3387a7ac6b2307129032562bf957653de13373a2748f82de7ab588a39d50b88d)"},
  "compose_sha256": sh("sha256sum", "infra/docker/flink-benchmark/compose.yml").split()[0],
  "uv_lock_sha256": sh("sha256sum", "infra/docker/flink-benchmark/uv.lock").split()[0],
}, open(sys.argv[1], "w"), indent=2)
EOF

  echo "== create isolated topics for $ns =="
  "${COMPOSE[@]}" run --rm -T generator "${replay_args[@]}" --namespace "$ns" \
    --out "/evidence/$ns/input" --create-topics-only

  echo "== submit $profile job =="
  local submit job_id
  submit=$("${COMPOSE[@]}" exec -T jobmanager flink run -d \
    -py /opt/ewallet/data_platform/streaming/flink/job.py \
    --mode BENCHMARK_REPLAY --profile "$profile" --namespace "$ns" \
    --bootstrap-servers redpanda:29092 \
    --checkpoint-storage "file:///flink-data/checkpoints/$ns" 2>&1 | tee "$run/flink_submit.log")
  job_id=$(grep -oE 'JobID [0-9a-f]{32}' <<<"$submit" | awk '{print $2}' | tail -1)
  [[ -n "$job_id" ]] || die "could not parse JobID; see $run/flink_submit.log"
  python3 - "$run/run.json" "$job_id" <<'EOF'
import json, sys
path, job = sys.argv[1], sys.argv[2]
data = json.load(open(path)); data["job_id"] = job; json.dump(data, open(path, "w"), indent=2)
EOF
  for _ in $(seq 1 60); do
    state=$(flink_get "/jobs/$job_id" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
    [[ "$state" == RUNNING ]] && break
    [[ "$state" == FAILED ]] && die "job $job_id failed during startup"
    sleep 2
  done

  echo "== sample container CPU/RAM on host =="
  ( while :; do docker stats --no-stream --format '{{json .}}' | sed "s/^/{\"ts\":\"$(date -u +%FT%TZ)\",\"stat\":/; s/\$/}/"; sleep 5; done ) \
    >"$run/docker_stats.jsonl" 2>/dev/null &
  local stats_pid=$!
  trap 'kill $stats_pid 2>/dev/null || true' RETURN

  echo "== publish bounded replay =="
  "${COMPOSE[@]}" run --rm -T generator "${replay_args[@]}" --namespace "$ns" \
    --out "/evidence/$ns/input" --skip-create-topics

  echo "== wait for output quiescence =="
  "${COMPOSE[@]}" run --rm -T sink-reader data_platform.streaming.benchmark.outputs \
    --namespace "$ns" --run-dir "/evidence/$ns" --wait-only || failures+=("outputs_not_quiesced")

  echo "== wait for >=1 completed checkpoint (max 300 s) =="
  local done_cp=0
  for _ in $(seq 1 60); do
    done_cp=$(flink_get "/jobs/$job_id/checkpoints" | python3 -c 'import json,sys; print(json.load(sys.stdin)["counts"]["completed"])')
    [[ "$done_cp" -ge 1 ]] && break
    sleep 5
  done
  [[ "$done_cp" -ge 1 ]] || failures+=("no_completed_checkpoint")

  echo "== collect Flink REST metrics =="
  "${COMPOSE[@]}" run --rm -T sink-reader data_platform.streaming.benchmark.metrics \
    --flink-url http://jobmanager:8081 --namespace "$ns" --job-id "$job_id" --run-dir "/evidence/$ns" || failures+=("metrics_collection_failed")
  flink_get "/jobs/$job_id/exceptions" >"$run/flink_exceptions.json" || true

  echo "== read isolated output topics =="
  local oracle=()
  [[ "$mode" == functional ]] && oracle=(--oracle)
  "${COMPOSE[@]}" run --rm -T sink-reader data_platform.streaming.benchmark.outputs \
    --namespace "$ns" --run-dir "/evidence/$ns" "${oracle[@]}" || failures+=("functional_checks_failed")

  echo "== cancel job =="
  curl -fsS -X PATCH "$FLINK_URL/jobs/$job_id?mode=cancel" >/dev/null || true
  "${COMPOSE[@]}" logs --no-color jobmanager taskmanager >"$run/flink_logs.txt" 2>&1 || true
  finalize_run "$run" "${failures[@]}"
}

# Record run validity in run.json; compare refuses anything not "complete".
finalize_run() {
  local run="$1"; shift
  python3 - "$run/run.json" "$@" <<'PYEOF'
import json, sys
path, failures = sys.argv[1], sys.argv[2:]
data = json.load(open(path))
data["status"] = "invalid" if failures else "complete"
data["failures"] = failures
json.dump(data, open(path, "w"), indent=2)
PYEOF
  echo "RUN_DIR=$run"
  [[ $# -eq 0 ]] || die "run INVALID ($*); partial evidence kept in $run"
}

cmd_compare() {
  [[ $# -eq 2 ]] || die "usage: compare <baseline_run_dir> <optimized_run_dir>"
  local b o
  b="$(cd "$1" && pwd)"; o="$(cd "$2" && pwd)"
  [[ "$b" == "$EVIDENCE"/* && "$o" == "$EVIDENCE"/* ]] || die "run dirs must be under $EVIDENCE"
  "${COMPOSE[@]}" run --rm -T sink-reader data_platform.streaming.benchmark.compare \
    --baseline "/evidence/${b#"$EVIDENCE"/}" --optimized "/evidence/${o#"$EVIDENCE"/}" \
    --out "/evidence/compare-$(basename "$b")-vs-$(basename "$o").md"
}

case "${1:-}" in
  prereq) cmd_prereq ;;
  up) cmd_up ;;
  down) cmd_down ;;
  functional) run_benchmark "${2:?profile}" functional ;;
  perf)
    case "${3:-}" in 100|500|1000|2000) ;; *) die "rate must be one of 100 500 1000 2000" ;; esac
    run_benchmark "${2:?profile}" performance "$3" "${4:-120}" ;;
  compare) shift; cmd_compare "$@" ;;
  *) sed -n '2,15p' "$0"; exit 1 ;;
esac
