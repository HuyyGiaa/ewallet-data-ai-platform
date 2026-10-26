#!/usr/bin/env bash

set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

parse_execute_flag "$@"
require_command kubectl
require_command curl

if ((!EXECUTE)); then
  echo "DRY RUN: port-forward internal MLflow service and call read-only health endpoints."
  print_command kubectl -n ml-platform port-forward service/mlflow 5000:5000
  print_command curl --fail --silent --show-error http://127.0.0.1:5000/health
  exit 0
fi

log_file="$(mktemp)"
kubectl -n ml-platform port-forward service/mlflow 5000:5000 >"${log_file}" 2>&1 &
port_forward_pid=$!
trap 'kill "${port_forward_pid}" 2>/dev/null || true; rm -f "${log_file}"' EXIT
sleep 3
curl --fail --silent --show-error http://127.0.0.1:5000/health
echo
