#!/usr/bin/env bash

set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

parse_execute_flag "$@"
require_command kubectl
require_env MLFLOW_DB_USERNAME

echo "$([[ ${EXECUTE} -eq 1 ]] && echo EXECUTE || echo 'DRY RUN'): validate bounded MLOps rollouts"
run_or_plan kubectl -n ml-platform rollout status statefulset/mlflow-postgresql --timeout=10m
run_or_plan kubectl -n ml-platform rollout status deployment/mlflow --timeout=10m
run_or_plan kubectl -n kubeflow wait --for=condition=Available deployment --all --timeout=20m
run_or_plan kubectl -n ml-platform exec statefulset/mlflow-postgresql -- pg_isready -U "${MLFLOW_DB_USERNAME}" -d mlflow
run_or_plan kubectl -n ml-platform get pods,service,statefulset,deployment,pvc -o wide
run_or_plan kubectl -n kubeflow get pods,service,deployment -o wide
