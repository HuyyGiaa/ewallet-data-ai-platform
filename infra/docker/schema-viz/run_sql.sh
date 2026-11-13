#!/usr/bin/env bash
# Run a generated SQL file statement by statement in the task-owned Trino and print
# each statement before its result, so the output is reviewable evidence.
#
#   infra/docker/schema-viz/run_sql.sh viewer data_platform/schema/generated/trino_metadata_queries.sql
#
# USER is `viewer` (read-only) for queries and `registrar` only for
# trino_register_tables.sql. Statements are split on a semicolon at end of line,
# which holds for the generated files. Stops at the first failing statement.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 viewer|registrar FILE.sql" >&2
  exit 2
fi
user=$1
file=$2
container=${EDAI1_TRINO_CONTAINER:-edai1-schema-viz-trino}

statement=""
while IFS= read -r line || [[ -n $line ]]; do
  [[ $line =~ ^[[:space:]]*-- ]] && { [[ -z $statement ]] && echo "$line"; continue; }
  [[ -z ${line//[[:space:]]/} && -z $statement ]] && continue
  statement+="${line}"$'\n'
  if [[ $line =~ \;[[:space:]]*$ ]]; then
    printf '\ntrino> %s' "$statement"
    # The CLI prints statement status (CALL, CREATE TABLE) and errors on stderr.
    docker exec "$container" trino --user "$user" --output-format ALIGNED --execute "${statement%;*}" 2>&1
    statement=""
  fi
done < "$file"
