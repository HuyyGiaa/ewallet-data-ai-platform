#!/usr/bin/env bash

set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

parse_execute_flag "$@"
require_command kubectl
require_command curl

if ((!EXECUTE)); then
  echo "DRY RUN: port-forward the internal KFP API/UI and perform read-only HTTP checks."
  print_command kubectl -n kubeflow port-forward service/ml-pipeline 8888:8888
  print_command curl --fail --silent --show-error http://127.0.0.1:8888/apis/v2beta1/healthz
  print_command kubectl -n kubeflow port-forward service/ml-pipeline-ui 8080:80
  print_command curl --fail --silent --show-error http://127.0.0.1:8080/
  exit 0
fi

api_log="$(mktemp)"
ui_log="$(mktemp)"
kubectl -n kubeflow port-forward service/ml-pipeline 8888:8888 >"${api_log}" 2>&1 &
api_pid=$!
kubectl -n kubeflow port-forward service/ml-pipeline-ui 8080:80 >"${ui_log}" 2>&1 &
ui_pid=$!
trap 'kill "${api_pid}" "${ui_pid}" 2>/dev/null || true; rm -f "${api_log}" "${ui_log}"' EXIT
sleep 3
curl --fail --silent --show-error http://127.0.0.1:8888/apis/v2beta1/healthz >/dev/null
curl --fail --silent --show-error http://127.0.0.1:8080/ >/dev/null
echo "KFP API/UI health: PASS"
