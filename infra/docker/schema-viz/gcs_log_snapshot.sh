#!/usr/bin/env bash
# Print a sorted, list-only snapshot of the cloud objects the schema-viz Trino reads:
# every _delta_log object (name, generation, size) of the observed runs, the
# top-level layout of each Delta table, and the Source control files.
#
#   infra/docker/schema-viz/gcs_log_snapshot.sh > before.txt
#   ... register tables, run queries, use DBeaver ...
#   infra/docker/schema-viz/gcs_log_snapshot.sh > after.txt
#   diff before.txt after.txt && echo "GCS unchanged"
#
# Any Delta write (data, OPTIMIZE, VACUUM, ANALYZE statistics) adds or replaces a
# _delta_log object, so identical snapshots show that nothing was written.
# Runs only object listings; it never reads data files.
set -euo pipefail

observed=${1:-docs/edai1/schema/generated/observed_schemas.json}
mapfile -t roots < <(python3 -c '
import json, sys
document = json.load(open(sys.argv[1]))
print(document["source"]["root"])
for zone in document["zones"].values():
    print(zone["root"])
' "$observed")

{
  for root in "${roots[@]:1}"; do
    timeout 300 gcloud storage objects list "$root/*/_delta_log/**" --format="value(name,generation,size)"
    timeout 300 gcloud storage ls "$root/*/" | grep -E '/$' | sed 's|/[^/]*/$|/<dir>|' | sort | uniq -c \
      | sed 's/^/TOPDIRS /' || true
  done
  timeout 300 gcloud storage objects list "${roots[0]}/_control/*.json" --format="value(name,generation,size)"
  timeout 300 gcloud storage ls "${roots[0]}/"
} | sort
