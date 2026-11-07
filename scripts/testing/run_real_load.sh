#!/usr/bin/env bash
# One bounded Locust run against a real target; never starts a backend of its own.
# usage: run_real_load.sh <feature|inference> <internal|external|local> <low|medium|high> <evidence-dir>
#
# Env: LOAD_HOST (http://127.0.0.1:18080), EWALLET_LOAD_ENTITIES (verified fixture),
#      EWALLET_LOAD_ACKNOWLEDGE_REMOTE, EWALLET_LOAD_BASIC_AUTH_FILE, EWALLET_LOAD_CA_FILE,
#      EWALLET_LOAD_HOST_HEADER, EWALLET_LOAD_MODEL_VERSION, IMAGE_DIGEST, LOAD_RUN_TIME (override).
set -euo pipefail
target=${1:?target required} path=${2:?network path required} profile=${3:?profile required} evidence=${4:?evidence dir required}
root=$(cd "$(dirname "$0")/../.." && pwd)
case "$profile" in
  low) users=5 spawn=2 ;; medium) users=10 spawn=5 ;; high) users=20 spawn=5 ;;
  *) echo "profile must be low, medium or high" >&2; exit 2 ;;
esac
case "$path" in internal|external|local) ;; *) echo "path must be internal, external or local" >&2; exit 2 ;; esac
run_time=${LOAD_RUN_TIME:-5m} host=${LOAD_HOST:-http://127.0.0.1:18080}
fixture=${EWALLET_LOAD_ENTITIES:?EWALLET_LOAD_ENTITIES must name a verified fixture}
out=$evidence/$target-$path-$profile; mkdir -p "$out"; out=$(cd "$out" && pwd)
started=$(date -u +%FT%TZ)
jq -n --arg target "$target" --arg path "$path" --arg profile "$profile" --arg host "$host" --arg started "$started" \
  --argjson users "$users" --argjson spawn "$spawn" --arg run_time "$run_time" \
  --arg fixture "$(basename "$fixture")" --arg fixture_sha256 "$(sha256sum "$fixture" | cut -d' ' -f1)" \
  --argjson fixture_rows "$(grep -c . "$fixture")" --arg source_sha "$(git -C "$root" rev-parse HEAD)" \
  --arg image_digest "${IMAGE_DIGEST:-unrecorded}" --arg model_version "${EWALLET_LOAD_MODEL_VERSION:-unrecorded}" \
  '{target:$target,network_path:$path,profile:$profile,host:$host,users:$users,spawn_rate:$spawn,run_time:$run_time,
    fixture:$fixture,fixture_sha256:$fixture_sha256,fixture_rows:$fixture_rows,source_sha:$source_sha,
    image_digest:$image_digest,model_version:$model_version,request_profile:"valid requests only",started_at:$started}' \
  > "$out/run-manifest.json"
status=0
(cd "$root" && EWALLET_LOAD_TARGET=$target PYTHONPATH=$root uv run --locked --project infra/load-test locust \
  -f tests/load/locustfile_real.py --headless --host "$host" --users "$users" --spawn-rate "$spawn" \
  --run-time "$run_time" --stop-timeout 10 --only-summary --html "$out/report.html" --csv "$out/locust" \
  --csv-full-history) 2>&1 | tee "$out/terminal.log" || status=$?
jq --arg ended "$(date -u +%FT%TZ)" '. + {ended_at:$ended}' "$out/run-manifest.json" > "$out/manifest.tmp"
mv "$out/manifest.tmp" "$out/run-manifest.json"
python3 "$root/scripts/testing/real_load_report.py" --run-dir "$out"
exit "$status"
