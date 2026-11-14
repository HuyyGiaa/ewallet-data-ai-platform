#!/usr/bin/env bash
# Prove the Feature Store's GCS permissions from inside GKE, as the Jobs' identity.
#
#   FEAST_ONLINE_IMAGE=...@sha256:... PROBE_BUCKET=<bucket> \
#   PROBE_SNAPSHOT_PREFIX=features/offline/snapshots/<dataset>/<feature-run>/<snapshot> \
#   PROBE_EXPECTED_MANIFEST_SHA256=<64 hex> \
#   scripts/mlops/feature_store_iam_probe.sh <probe-id> [--execute]
#
# Dry-run prints the Job. --execute applies it in ml-platform with service
# account `training` (Workload Identity), waits and prints the probe's JSON line.
# The probe creates exactly one object, <PROBE_PREFIX>/<probe-id>.json; it is
# create-only and is not deleted afterwards.
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

PROBE_ID="${1:?probe id, e.g. fs-iam-probe-$(date -u +%Y%m%d%H%M%S)}"
shift
EXECUTE=0
while (($#)); do
  case "$1" in
    --execute) EXECUTE=1; shift ;;
    *) die "unsupported argument: $1" ;;
  esac
done
[[ "${PROBE_ID}" =~ ^[a-z0-9-]{1,50}$ ]] || die "invalid probe id: ${PROBE_ID}"

require_env FEAST_ONLINE_IMAGE
require_env PROBE_BUCKET
require_env PROBE_SNAPSHOT_PREFIX
require_env PROBE_EXPECTED_MANIFEST_SHA256
require_digest_pinned_images FEAST_ONLINE_IMAGE
[[ "${PROBE_EXPECTED_MANIFEST_SHA256}" =~ ^[0-9a-f]{64}$ ]] \
  || die "PROBE_EXPECTED_MANIFEST_SHA256 must be 64 lowercase hex characters"
PROBE_PREFIX="${PROBE_PREFIX:-features/_iam-probe}"
KUBE_CONTEXT="${KUBE_CONTEXT:-gke_${EXPECTED_PROJECT_ID}_asia-southeast1-b_ewallet-dev}"

manifest="$(cat <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${PROBE_ID}
  namespace: ml-platform
  labels:
    app.kubernetes.io/name: feature-store-iam-probe
    app.kubernetes.io/part-of: ewallet-fs-verify
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 600
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        app.kubernetes.io/name: feature-store-iam-probe
        app.kubernetes.io/part-of: ewallet-fs-verify
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
        - name: probe
          image: ${FEAST_ONLINE_IMAGE}
          command: ["python", "-m", "ai_platform.features.feast.validation.iam_probe"]
          args:
            - "--bucket=${PROBE_BUCKET}"
            - "--snapshot-prefix=${PROBE_SNAPSHOT_PREFIX}"
            - "--expected-manifest-sha256=${PROBE_EXPECTED_MANIFEST_SHA256}"
            - "--probe-id=${PROBE_ID}"
            - "--probe-prefix=${PROBE_PREFIX}"
          env:
            - name: HOME
              value: /tmp
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {cpu: 500m, memory: 512Mi}
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: {drop: ["ALL"]}
EOF
)"

if ((!EXECUTE)); then
  printf '%s\n' "${manifest}"
  echo "DRY RUN: nothing applied; rerun with --execute" >&2
  exit 0
fi

kube() { kubectl --context "${KUBE_CONTEXT}" -n ml-platform "$@"; }
printf '%s\n' "${manifest}" | kube apply -f - >&2
if ! kube wait --for=condition=complete --timeout=300s "job/${PROBE_ID}" >&2; then
  kube logs "job/${PROBE_ID}" | tail -1
  die "probe ${PROBE_ID} did not complete; see its JSON line above"
fi
kube logs "job/${PROBE_ID}" | tail -1
