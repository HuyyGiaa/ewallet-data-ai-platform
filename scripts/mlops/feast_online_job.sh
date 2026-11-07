#!/usr/bin/env bash
# Run one Feast Online operator command as a bounded Kubernetes Job.
#
#   FEAST_ONLINE_IMAGE=...@sha256:... ONLINE_REGISTRY_URI=gs://.../registry.db \
#   scripts/mlops/feast_online_job.sh <run-id> [--execute] -- <operator args>
#
# Dry-run prints the rendered Job. --execute applies it with the explicit
# cluster context, waits for completion and prints the evidence JSON line.
# Optional: FEAST_WINDOW_ID labels the Job so a retry can find it, and
# FEAST_REDIS_SECRET selects a Secret other than feast-online-redis.
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

RUN_ID="${1:?run id, e.g. feast-mat-261031}"
shift
EXECUTE=0
while (($#)); do
  case "$1" in
    --execute) EXECUTE=1; shift ;;
    --) shift; break ;;
    *) die "unsupported argument: $1" ;;
  esac
done
(($#)) || die "operator arguments are required after --"
[[ "${RUN_ID}" =~ ^[a-z0-9-]{1,50}$ ]] || die "invalid run id: ${RUN_ID}"

require_env FEAST_ONLINE_IMAGE
require_env ONLINE_REGISTRY_URI
require_env FEAST_OFFLINE_DATA_ROOT
[[ "${FEAST_ONLINE_IMAGE}" == *@sha256:* ]] || die "FEAST_ONLINE_IMAGE must be pinned by digest"
KUBE_CONTEXT="${KUBE_CONTEXT:-gke_${EXPECTED_PROJECT_ID}_asia-southeast1-b_ewallet-dev}"
GIT_SHA="${FEAST_ONLINE_GIT_SHA:-$(git -C "${REPO_ROOT}" rev-parse HEAD)}"
REDIS_SECRET="${FEAST_REDIS_SECRET:-feast-online-redis}"
[[ "${REDIS_SECRET}" =~ ^[a-z0-9-]{1,63}$ ]] || die "invalid Redis secret name: ${REDIS_SECRET}"
WINDOW_LABEL=""
if [[ -n "${FEAST_WINDOW_ID:-}" ]]; then
  [[ "${FEAST_WINDOW_ID}" =~ ^[0-9a-f]{12}$ ]] || die "invalid window id: ${FEAST_WINDOW_ID}"
  WINDOW_LABEL=$'\n'"    ewallet.feast/window-id: \"${FEAST_WINDOW_ID}\""
fi

args_yaml=""
for argument in "$@"; do
  args_yaml+="            - \"${argument}\""$'\n'
done

manifest="$(cat <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${RUN_ID}
  namespace: ml-platform
  labels:
    app.kubernetes.io/name: feast-online-operator
    app.kubernetes.io/part-of: ewallet-mlops${WINDOW_LABEL}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 3600
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        app.kubernetes.io/name: feast-online-operator
    spec:
      serviceAccountName: training
      restartPolicy: Never
      nodeSelector:
        iam.gke.io/gke-metadata-server-enabled: "true"
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: operator
          image: ${FEAST_ONLINE_IMAGE}
          command: ["python", "-m", "ai_platform.features.feast.online_operator"]
          args:
${args_yaml}          env:
            - name: EWALLET_FEAST_REGISTRY
              value: "${ONLINE_REGISTRY_URI}"
            - name: EWALLET_FEAST_OFFLINE_DATA_ROOT
              value: "${FEAST_OFFLINE_DATA_ROOT}"
            - name: EWALLET_GIT_SHA
              value: "${GIT_SHA}"
            - name: HOME
              value: /tmp
            - name: EWALLET_REDIS_CONNECTION_STRING
              valueFrom:
                secretKeyRef:
                  name: ${REDIS_SECRET}
                  key: connection_string
          resources:
            requests:
              cpu: "1"
              memory: 3Gi
            limits:
              cpu: "2"
              memory: 6Gi
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
EOF
)"

if ((!EXECUTE)); then
  printf '%s\n' "${manifest}"
  exit 0
fi

printf '%s\n' "${manifest}" | kubectl --context "${KUBE_CONTEXT}" apply -f -
# Poll succeeded/failed counts: newer Kubernetes reports both
# SuccessCriteriaMet and Complete, which breaks a jsonpath condition wait.
deadline=$((SECONDS + 3700))
while :; do
  read -r succeeded failed < <(kubectl --context "${KUBE_CONTEXT}" -n ml-platform get "job/${RUN_ID}" \
    -o jsonpath='{.status.succeeded} {.status.failed}{"\n"}')
  [[ "${succeeded:-0}" -ge 1 ]] && break
  if [[ "${failed:-0}" -ge 1 || ${SECONDS} -ge ${deadline} ]]; then
    kubectl --context "${KUBE_CONTEXT}" -n ml-platform logs "job/${RUN_ID}" --tail=80 >&2 || true
    die "job ${RUN_ID} did not complete"
  fi
  sleep 10
done
kubectl --context "${KUBE_CONTEXT}" -n ml-platform logs "job/${RUN_ID}" | tail -1
