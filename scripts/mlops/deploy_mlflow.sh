#!/usr/bin/env bash

set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

parse_execute_flag "$@"
assert_canonical_environment
require_env MLFLOW_ARTIFACT_ROOT
require_env MLFLOW_DB_USERNAME
[[ "${MLFLOW_ARTIFACT_ROOT}" == gs://* ]] || die "MLFLOW_ARTIFACT_ROOT must be a gs:// URI"
[[ "${MLFLOW_DB_USERNAME}" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || die "unsafe MLFLOW_DB_USERNAME"
require_command kubectl

namespace_manifest="${REPO_ROOT}/infra/kubernetes/mlflow/namespace.yaml"
service_account_manifest="${REPO_ROOT}/infra/kubernetes/mlflow/service-account.yaml"
postgres_manifest="${REPO_ROOT}/infra/kubernetes/mlflow/postgresql.yaml"
service_manifest="${REPO_ROOT}/infra/kubernetes/mlflow/service.yaml"
deployment_manifest="${REPO_ROOT}/infra/kubernetes/mlflow/deployment.yaml"

if ((!EXECUTE)); then
  echo "DRY RUN: deploy PostgreSQL and MLflow; secret values are intentionally redacted."
  print_command kubectl apply -f "${namespace_manifest}"
  print_command kubectl apply -f "${service_account_manifest}"
  echo "  kubectl create/apply secret mlflow-postgresql (username + REDACTED password)"
  echo "  kubectl create/apply secret mlflow-backend-store (PostgreSQL DNS URI; password REDACTED)"
  echo "  kubectl create/apply configmap mlflow-runtime (GCS artifact destination)"
  print_command kubectl apply -f "${postgres_manifest}"
  print_command kubectl apply -f "${service_manifest}"
  echo "  kubectl set image --local then apply ${MLFLOW_IMAGE}"
  exit 0
fi

require_env MLFLOW_DB_PASSWORD
[[ "${MLFLOW_DB_PASSWORD}" =~ ^[A-Za-z0-9_-]{24,}$ ]] || \
  die "MLFLOW_DB_PASSWORD must be at least 24 URL-safe characters"

kubectl apply -f "${namespace_manifest}"
kubectl apply -f "${service_account_manifest}"
kubectl -n ml-platform create secret generic mlflow-postgresql \
  --from-literal="username=${MLFLOW_DB_USERNAME}" \
  --from-literal="password=${MLFLOW_DB_PASSWORD}" \
  --dry-run=client -o yaml | kubectl apply -f -
backend_uri="postgresql+psycopg2://${MLFLOW_DB_USERNAME}:${MLFLOW_DB_PASSWORD}@mlflow-postgresql.ml-platform.svc.cluster.local:5432/mlflow"
kubectl -n ml-platform create secret generic mlflow-backend-store \
  --from-literal="uri=${backend_uri}" \
  --dry-run=client -o yaml | kubectl apply -f -
unset backend_uri
kubectl -n ml-platform create configmap mlflow-runtime \
  --from-literal="artifactsDestination=${MLFLOW_ARTIFACT_ROOT}" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "${postgres_manifest}"
kubectl apply -f "${service_manifest}"
kubectl set image -f "${deployment_manifest}" mlflow="${MLFLOW_IMAGE}" \
  --local -o yaml | kubectl apply -f -
