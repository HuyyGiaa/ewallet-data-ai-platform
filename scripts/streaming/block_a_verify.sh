#!/usr/bin/env bash
# Run the Block A dual-store verification on the local stack.
#
#   scripts/streaming/block_a_verify.sh up
#   scripts/streaming/block_a_verify.sh run <namespace>      # namespace: verify-...
#   scripts/streaming/block_a_verify.sh replay <namespace>   # re-read both sink topics
#   scripts/streaming/block_a_verify.sh down
#
# BLOCK_A_RESTART_AFTER_SECONDS=<n> stops the job with a savepoint n seconds into
# publishing and restores it, so the run also proves Flink recovery. Pair it with a
# BLOCK_A_PHASE_DELAY larger than the restart takes, about 40 seconds. Restart after
# the too_late phase: a savepoint does not hold the watermark, so events arriving
# right after a restore are classified more leniently.
#
# Real Redpanda, Flink, Feast and a loopback Redis, with a local history directory.
# This is local validation. It never contacts a cloud service, and it refuses any
# Redis that is not on loopback. Output goes to $BLOCK_A_VERIFY_ROOT.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE=(docker compose -f "$ROOT/infra/docker/flink-benchmark/compose.yml"
         -f "$ROOT/infra/docker/flink-benchmark/compose.verify.yml")
FLINK_URL="http://localhost:18081"
BOOTSTRAP_HOST="localhost:19092"
BOOTSTRAP_INTERNAL="redpanda:29092"
PROFILE="${BLOCK_A_PROFILE:-baseline}"
VERIFY_ROOT="${BLOCK_A_VERIFY_ROOT:-$HOME/ewallet-evidence/block-a-verify}"
SINKS_PYTHON="${BLOCK_A_SINKS_PYTHON:-$ROOT/infra/docker/streaming-sinks/.venv/bin/python}"
REDIS_CONNECTION="${BLOCK_A_REDIS_CONNECTION:-127.0.0.1:16399,db=5}"
SETTLE_SECONDS="${BLOCK_A_SETTLE_SECONDS:-45}"

die() { echo "ERROR: $*" >&2; exit 1; }

require_loopback_redis() {
  case "${REDIS_CONNECTION%%:*}" in
    127.0.0.1|localhost) ;;
    *) die "local verification only accepts a loopback Redis" ;;
  esac
}

cmd_up() {
  "${COMPOSE[@]}" up -d --wait redpanda jobmanager taskmanager
}

cmd_down() {
  "${COMPOSE[@]}" down
}


collect_flink_evidence() {
  local job="$1" out="$2"
  curl -fsS "$FLINK_URL/jobs/$job" >"$out/flink-job.json"
  python3 - "$FLINK_URL" "$job" "$out/flink-metrics.json" <<'PY'
import json, sys, urllib.parse, urllib.request
base, job, path = sys.argv[1:]
def get(url):
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.load(response)
names = ("too_late_total", "late_accepted_total", "on_time_total", "duplicates_detected_total",
         "dlq_total", "online_updates_total", "historical_records_total", "late_reconciliation_total")
result = {"job_id": job, "vertices": {}}
for vertex in get(f"{base}/jobs/{job}")["vertices"]:
    available = get(f"{base}/jobs/{job}/vertices/{vertex['id']}/subtasks/metrics")
    wanted = [item["id"] for item in available
              if item["id"].rsplit(".", 1)[-1] in names or item["id"].endswith("currentOutputWatermark")]
    if not wanted:
        continue
    query = urllib.parse.quote(",".join(wanted))
    values = get(f"{base}/jobs/{job}/vertices/{vertex['id']}/subtasks/metrics?get={query}")
    result["vertices"][vertex["name"][:80]] = {item["id"]: item.get("sum") for item in values}
json.dump(result, open(path, "w"), indent=2, sort_keys=True)
PY
}

restart_from_savepoint() {
  local job="$1" ns="$2" run="$3" stopped savepoint submit restored
  stopped=$("${COMPOSE[@]}" exec -T jobmanager flink stop \
    --savepointPath "file:///flink-data/savepoints/$ns" "$job" 2>&1 | tee "$run/flink-stop.log")
  savepoint=$(grep -oE 'file:[^ ]*savepoint-[0-9a-z-]+' <<<"$stopped" | tail -1)
  [[ -n "$savepoint" ]] || die "no savepoint path; see $run/flink-stop.log"
  echo "$savepoint" >"$run/savepoint.txt"
  submit=$("${COMPOSE[@]}" exec -T jobmanager flink run -d \
    -py /opt/ewallet/data_platform/streaming/flink/job.py \
    --mode ISOLATED_VERIFICATION --profile "$PROFILE" --namespace "$ns" \
    --bootstrap-servers "$BOOTSTRAP_INTERNAL" --savepoint-path "$savepoint" \
    --checkpoint-storage "file:///flink-data/checkpoints/$ns" 2>&1 | tee "$run/flink-restore.log")
  restored=$(grep -oE 'JobID [0-9a-f]{32}' <<<"$submit" | awk '{print $2}' | tail -1)
  [[ -n "$restored" ]] || die "no restored JobID; see $run/flink-restore.log"
  echo "$restored"
}

cmd_replay() {
  local ns="${1:?namespace}" run="$VERIFY_ROOT/${1:-}"
  require_loopback_redis
  [[ -d "$run" ]] || die "$run does not exist"
  cd "$ROOT"
  export PYTHONPATH="$ROOT" PYTHONDONTWRITEBYTECODE=1
  export EWALLET_FEAST_REGISTRY="$run/registry.db"
  export EWALLET_FEAST_OFFLINE_DATA_ROOT="$run/offline-unused"
  export EWALLET_REDIS_CONNECTION_STRING="$REDIS_CONNECTION"
  echo "== rewind both sink groups to the start of their topics =="
  for group in "ewallet-block-a-history-v1.$ns" "ewallet-block-a-feast-online-v1.$ns"; do
    "${COMPOSE[@]}" exec -T redpanda rpk group seek "$group" --to start \
      --brokers localhost:29092 | tee -a "$run/replay-seek.log"
  done
  echo "== read every record a second time =="
  "$SINKS_PYTHON" -W ignore -m data_platform.streaming.sinks.historical_service \
    --bootstrap-servers "$BOOTSTRAP_HOST" --namespace "$ns" --run-id "$ns" \
    --history-root "file://$run/history" --batch-size 7 --flush-interval-seconds 5 \
    --max-idle-seconds 20 | tee "$run/replay-history.log"
  "$SINKS_PYTHON" -W ignore -m data_platform.streaming.feast.service \
    --bootstrap-servers "$BOOTSTRAP_HOST" --namespace "$ns" \
    --feast-repo ai_platform/features/feast/online_repo --max-idle-seconds 20 | tee "$run/replay-feast.log"
  echo "== validate after replay =="
  "$SINKS_PYTHON" -W ignore -m data_platform.streaming.verification.cli validate \
    --bootstrap-servers "$BOOTSTRAP_HOST" --namespace "$ns" --manifest "$run/manifest.json" \
    --history-root "file://$run/history" --run-id "$ns" \
    --feast-repo ai_platform/features/feast/online_repo --report "$run/consistency-report-after-replay.json"
}

cmd_run() {
  local ns="${1:?namespace, for example verify-local-1}"
  [[ "$ns" =~ ^verify-[a-z0-9][a-z0-9-]{0,46}$ ]] || die "invalid namespace: $ns"
  require_loopback_redis
  [[ -x "$SINKS_PYTHON" ]] || die "sync the locked environment: uv sync --locked --project infra/docker/streaming-sinks"
  local run="$VERIFY_ROOT/$ns"
  [[ ! -e "$run" ]] || die "$run exists; every run needs a new namespace"
  mkdir -p "$run"
  cd "$ROOT"
  export PYTHONPATH="$ROOT" PYTHONDONTWRITEBYTECODE=1
  export EWALLET_FEAST_REGISTRY="$run/registry.db"
  export EWALLET_FEAST_OFFLINE_DATA_ROOT="$run/offline-unused"
  export EWALLET_REDIS_CONNECTION_STRING="$REDIS_CONNECTION"
  export EWALLET_GIT_SHA
  EWALLET_GIT_SHA="$(git -C "$ROOT" rev-parse HEAD)"

  echo "== register FeatureViews in a run-scoped registry =="
  "$SINKS_PYTHON" -W ignore - <<'PY'
from feast import FeatureStore
from ai_platform.features.feast.online_repo.feature_definitions import build_online_feature_objects
store = FeatureStore(repo_path="ai_platform/features/feast/online_repo")
entities, _, pushes, views = build_online_feature_objects()
store.apply([*entities.values(), *pushes.values(), *views.values()])
PY

  echo "== create isolated topics =="
  "$SINKS_PYTHON" -W ignore -m data_platform.streaming.verification.cli publish \
    --bootstrap-servers "$BOOTSTRAP_HOST" --namespace "$ns" --profile "$PROFILE" \
    --manifest "$run/manifest.json" --create-topics --create-topics-only

  echo "== submit Flink job with real watermarks =="
  local submit job
  submit=$("${COMPOSE[@]}" exec -T jobmanager flink run -d \
    -py /opt/ewallet/data_platform/streaming/flink/job.py \
    --mode ISOLATED_VERIFICATION --profile "$PROFILE" --namespace "$ns" \
    --bootstrap-servers "$BOOTSTRAP_INTERNAL" \
    --checkpoint-storage "file:///flink-data/checkpoints/$ns" 2>&1 | tee "$run/flink-submit.log")
  job=$(grep -oE 'JobID [0-9a-f]{32}' <<<"$submit" | awk '{print $2}' | tail -1)
  [[ -n "$job" ]] || die "no JobID; see $run/flink-submit.log"
  for _ in $(seq 1 60); do
    [[ "$(curl -fsS "$FLINK_URL/jobs/$job" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')" == RUNNING ]] && break
    sleep 2
  done

  echo "== start sinks =="
  "$SINKS_PYTHON" -W ignore -m data_platform.streaming.sinks.historical_service \
    --bootstrap-servers "$BOOTSTRAP_HOST" --namespace "$ns" --run-id "$ns" \
    --history-root "file://$run/history" --batch-size 5 --flush-interval-seconds 5 \
    >"$run/history-service.log" 2>&1 &
  local history_pid=$!
  "$SINKS_PYTHON" -W ignore -m data_platform.streaming.feast.service \
    --bootstrap-servers "$BOOTSTRAP_HOST" --namespace "$ns" \
    --feast-repo ai_platform/features/feast/online_repo \
    >"$run/feast-service.log" 2>&1 &
  local feast_pid=$!
  trap 'kill "$history_pid" "$feast_pid" 2>/dev/null || true' EXIT

  echo "== publish scenario phases =="
  mv "$run/manifest.json" "$run/manifest.topics.json"
  "$SINKS_PYTHON" -W ignore -m data_platform.streaming.verification.cli publish \
    --bootstrap-servers "$BOOTSTRAP_HOST" --namespace "$ns" --profile "$PROFILE" \
    --manifest "$run/manifest.json" --phase-delay-seconds "${BLOCK_A_PHASE_DELAY:-12}" >"$run/publish.log" &
  local publish_pid=$!
  if [[ -n "${BLOCK_A_RESTART_AFTER_SECONDS:-}" ]]; then
    sleep "$BLOCK_A_RESTART_AFTER_SECONDS"
    job="$(restart_from_savepoint "$job" "$ns" "$run")"
    echo "restored as job $job"
  fi
  wait "$publish_pid" || die "publish failed; see $run/publish.log"
  cmp "$run/manifest.json" "$run/manifest.topics.json"

  echo "== let timers fire, then stop the sinks with SIGTERM =="
  sleep "$SETTLE_SECONDS"
  kill -TERM "$history_pid" "$feast_pid"
  wait "$history_pid" || die "history sink failed; see $run/history-service.log"
  wait "$feast_pid" || die "Feast sink failed; see $run/feast-service.log"
  trap - EXIT
  collect_flink_evidence "$job" "$run"

  echo "== validate offline history against Feast online =="
  "$SINKS_PYTHON" -W ignore -m data_platform.streaming.verification.cli validate \
    --bootstrap-servers "$BOOTSTRAP_HOST" --namespace "$ns" --manifest "$run/manifest.json" \
    --history-root "file://$run/history" --run-id "$ns" \
    --feast-repo ai_platform/features/feast/online_repo --report "$run/consistency-report.json"
  curl -fsS -X PATCH "$FLINK_URL/jobs/$job?mode=cancel" >/dev/null || true
}

case "${1:-}" in
  up) cmd_up ;;
  down) cmd_down ;;
  run) shift; cmd_run "$@" ;;
  replay) shift; cmd_replay "$@" ;;
  *) die "usage: $0 up|run <namespace>|replay <namespace>|down" ;;
esac
