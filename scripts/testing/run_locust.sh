#!/usr/bin/env bash
# B1.4: bounded Locust run. The default starts a mock-backed local API on
# loopback and stops it afterwards.
#
# Usage: scripts/testing/run_locust.sh [output-directory]
# Environment (defaults in brackets):
#   LOCUST_USERS [20]  LOCUST_SPAWN_RATE [5]  LOCUST_RUN_TIME [60s]
#   LOCUST_HOST [http://127.0.0.1:18080]      LOCUST_MOCK_PORT [18080]
#   LOCUST_MODEL_LATENCY_MS [5]
#   EWALLET_LOAD_MIX, EWALLET_LOAD_SEED, EWALLET_LOAD_PROFILE [local]
#   EWALLET_LOAD_ACKNOWLEDGE_REMOTE  Required for a non-loopback integration host.

source "$(dirname "${BASH_SOURCE[0]}")/b1_common.sh"
b1_require_uv

MAX_USERS=200
MAX_RUN_SECONDS=900

output="${1:-$B1_EVIDENCE_ROOT/locust}"
mkdir -p "$output"
output="$(cd "$output" && pwd)"
users="${LOCUST_USERS:-20}"
spawn_rate="${LOCUST_SPAWN_RATE:-5}"
run_time="${LOCUST_RUN_TIME:-60s}"
port="${LOCUST_MOCK_PORT:-18080}"
host="${LOCUST_HOST:-http://127.0.0.1:$port}"
profile="${EWALLET_LOAD_PROFILE:-local}"
latency="${LOCUST_MODEL_LATENCY_MS:-5}"

seconds_of() {
  case "$1" in
    *h) echo $((${1%h} * 3600)) ;;
    *m) echo $((${1%m} * 60)) ;;
    *s) echo "${1%s}" ;;
    *) echo "$1" ;;
  esac
}

if ((users > MAX_USERS)) || (($(seconds_of "$run_time") > MAX_RUN_SECONDS)); then
  echo "refused: limits are $MAX_USERS users and $MAX_RUN_SECONDS seconds" >&2
  exit 2
fi

cd "$REPO_ROOT"
b1_uv_run load python -m tests.load.settings "$host"

b1_record_provenance "$output"
{
  echo "profile=$profile"
  echo "host=$host"
  echo "users=$users"
  echo "spawn_rate=$spawn_rate"
  echo "run_time=$run_time"
  echo "model_latency_ms=$latency"
  echo "mix=${EWALLET_LOAD_MIX:-default}"
  echo "seed=${EWALLET_LOAD_SEED:-default}"
} >"$output/run-parameters.txt"

server_pid=""
stop_server() {
  if [[ -n "$server_pid" ]] && kill -0 "$server_pid" 2>/dev/null; then
    kill "$server_pid"
    wait "$server_pid" 2>/dev/null || true
  fi
}
trap stop_server EXIT

if [[ "$profile" == "local" ]]; then
  # Start the interpreter directly: "$!" must be the server itself, because a
  # signal sent to a uv or subshell wrapper would leave the server running.
  uv sync --locked --project "$B1_PROJECT" --group test --group load --quiet
  "$B1_PROJECT/.venv/bin/python" -m tests.load.mock_server --port "$port" \
    --model-latency-ms "$latency" >"$output/mock-server.log" 2>&1 &
  server_pid=$!
  for _ in $(seq 1 120); do
    if curl -fsS "$host/health/live" >/dev/null 2>&1; then break; fi
    kill -0 "$server_pid" 2>/dev/null || { cat "$output/mock-server.log" >&2; exit 1; }
    sleep 0.5
  done
  curl -fsS "$host/health/ready" >/dev/null
fi

locust_status=0
PYTHONPATH="$REPO_ROOT" b1_uv_run load locust \
  -f tests/load/locustfile.py \
  --headless \
  --host "$host" \
  --users "$users" \
  --spawn-rate "$spawn_rate" \
  --run-time "$run_time" \
  --stop-timeout 5 \
  --only-summary \
  --html "$output/report.html" \
  --csv "$output/locust" \
  --csv-full-history \
  2>&1 | tee "$output/terminal.txt" || locust_status=$?

b1_uv_run test python scripts/testing/locust_report.py \
  --csv-prefix "$output/locust" --profile "$profile"
exit "$locust_status"
