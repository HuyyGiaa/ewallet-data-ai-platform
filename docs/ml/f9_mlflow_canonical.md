# Canonical F9 experiment and MLflow

## Execution gate

Run this milestone only after the cloud Feast bounded training snapshot has
passed its manifest, temporal, leakage, and point-in-time gates. F9 consumes
that immutable snapshot; it does not read the canonical lakehouse directly.

The checked-in F9 configuration resolves all paths, IDs, and the tracking URI
from environment variables. Its four candidates and hyperparameters are the
existing F9 values. All four models fit on train and report validation metrics.
The Dummy candidate is logged but is ineligible for selection. Validation
PR-AUC, then validation F1, selects the winner. Only that winner is evaluated
on test, logged as a model, and registered. No stage or alias is assigned.

## Future USER_RUN

Set real immutable Feast-boundary values; do not reuse an output directory:

```bash
export PROJECT_ID="ewallet-data-ai-platform-hg26"
export REGION="asia-southeast1"
export ZONE="asia-southeast1-b"
export GKE_CLUSTER="ewallet-dev"
export EWALLET_DATA_BUCKET="${PROJECT_ID}-data"
export DATASET_VERSION="canonical-100gb-v1"
export SILVER_RUN_ID="<canonical-dp2-pass-run-id>"
export FEATURE_RUN_ID="<canonical-dp3-pass-run-id>"
export SNAPSHOT_ID="<passed-feast-training-snapshot-id>"
export F9_TRAINING_SNAPSHOT_URI="gs://${EWALLET_DATA_BUCKET}/ml/training-snapshots/${DATASET_VERSION}/${SNAPSHOT_ID}"
export F9_DATASET_PATH="$PWD/.runtime/f9/${SNAPSHOT_ID}/dataset"
export F9_OUTPUT_PATH="$PWD/.runtime/f9/${SNAPSHOT_ID}/evidence"
export MLFLOW_ARTIFACT_ROOT="gs://${EWALLET_DATA_BUCKET}/mlflow/artifacts"
export MLFLOW_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/ewallet-platform/mlflow:v3.17.1-gcs-postgres-1"
```

Build the pinned MLflow runtime image, scale the already-approved CPU pool when
needed, get credentials, then deploy the internal stack:

```bash
gcloud builds submit infra/kubernetes/mlflow/image --tag "$MLFLOW_IMAGE"
gcloud container clusters resize "$GKE_CLUSTER" --node-pool=cpu-pool --num-nodes=1 --zone="$ZONE" --project="$PROJECT_ID"
gcloud container clusters get-credentials "$GKE_CLUSTER" --zone="$ZONE" --project="$PROJECT_ID"

kubectl apply -f infra/kubernetes/mlflow/namespace.yaml
kubectl apply -f infra/kubernetes/mlflow/service-account.yaml
export MLFLOW_DB_PASSWORD="$(openssl rand -hex 24)"
kubectl -n ml-platform create secret generic mlflow-postgresql \
  --from-literal=username=mlflow --from-literal=password="$MLFLOW_DB_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n ml-platform create secret generic mlflow-backend-store \
  --from-literal="uri=postgresql+psycopg2://mlflow:${MLFLOW_DB_PASSWORD}@mlflow-postgresql.ml-platform.svc.cluster.local:5432/mlflow" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n ml-platform create configmap mlflow-runtime \
  --from-literal="artifactsDestination=${MLFLOW_ARTIFACT_ROOT}" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f infra/kubernetes/mlflow/postgresql.yaml
kubectl -n ml-platform rollout status statefulset/mlflow-postgresql --timeout=5m
kubectl apply -f infra/kubernetes/mlflow/deployment.yaml
kubectl apply -f infra/kubernetes/mlflow/service.yaml
kubectl -n ml-platform rollout status deployment/mlflow --timeout=5m
```

Verify health and stage the immutable snapshot locally:

```bash
kubectl -n ml-platform port-forward service/mlflow 5000:5000 > /tmp/f9-mlflow-port-forward.log 2>&1 &
export MLFLOW_PORT_FORWARD_PID=$!
export MLFLOW_TRACKING_URI="http://127.0.0.1:5000"
curl --fail --silent --show-error "$MLFLOW_TRACKING_URI/health"
test ! -e "$F9_DATASET_PATH" && mkdir -p "$F9_DATASET_PATH"
gcloud storage rsync --recursive "$F9_TRAINING_SNAPSHOT_URI" "$F9_DATASET_PATH"
```

Run the bounded experiment once:

```bash
conda run -n ewallet-ml python -m ai_platform.ml.tracking.run \
  --config ai_platform/ml/config/f9_mlflow_experiment.yaml \
  --repo-root "$PWD" | tee "/tmp/f9-${SNAPSHOT_ID}.log"
```

## Evidence commands

```bash
curl --fail --silent --show-error "$MLFLOW_TRACKING_URI/health"
kubectl -n ml-platform exec statefulset/mlflow-postgresql -- \
  psql -U mlflow -d mlflow -c 'select count(*) from runs;'
gcloud storage ls --recursive "${MLFLOW_ARTIFACT_ROOT}/**"

conda run -n ewallet-ml python - <<'PY'
import os
from mlflow import MlflowClient
c = MlflowClient(tracking_uri=os.environ["MLFLOW_TRACKING_URI"])
e = c.get_experiment_by_name("ewallet-fraud-comparison")
runs = c.search_runs([e.experiment_id])
children = [r for r in runs if "mlflow.parentRunId" in r.data.tags]
assert len(children) == 4, len(children)
assert all("validation_pr_auc" in r.data.metrics for r in children)
tested = [r for r in children if "test_pr_auc" in r.data.metrics]
assert len(tested) == 1, [r.info.run_id for r in tested]
required = {"dataset_version", "feature_run_id", "snapshot_id", "git_commit_sha"}
assert all(required <= set(r.data.params) for r in children)
versions = list(c.search_model_versions("name='ewallet-fraud-detector'"))
assert versions and versions[-1].run_id
assert "production" not in c.get_registered_model("ewallet-fraud-detector").aliases
print("PASS", {"candidate_runs": 4, "winner_run": tested[0].info.run_id, "model_version": versions[-1].version})
PY

cp "$F9_OUTPUT_PATH/f9_mlflow_evidence.json" "docs/evidence/cloud/f9_${SNAPSHOT_ID}.json"
kubectl -n ml-platform get pods,service,statefulset,deployment -o wide
kubectl -n ml-platform logs deployment/mlflow --tail=200
kubectl -n ml-platform logs statefulset/mlflow-postgresql --tail=100
```

Also capture two UI screenshots through the existing port-forward: the
`ewallet-fraud-comparison` child-run metrics table and the
`ewallet-fraud-detector` version page showing lineage tags and no production
alias. Save the command log and generated `f9_mlflow_evidence.json` with them.

The manual production promotion command is prepared below and must run only
after review; this milestone does not execute it:

```bash
export APPROVED_MODEL_VERSION="<reviewed-winner-version>"
conda run -n ewallet-ml python - <<'PY'
import os
from mlflow import MlflowClient
MlflowClient(tracking_uri=os.environ["MLFLOW_TRACKING_URI"]).set_registered_model_alias(
    "ewallet-fraud-detector", "production", os.environ["APPROVED_MODEL_VERSION"]
)
PY
```

## Rollback

Preserve evidence before cleanup. Then remove only this bounded stack and its
PVC; the GCS artifact prefix and registered model remain audit evidence unless
an explicit retention decision is made:

```bash
kill "$MLFLOW_PORT_FORWARD_PID" 2>/dev/null || true
kubectl delete -f infra/kubernetes/mlflow/service.yaml --ignore-not-found
kubectl delete -f infra/kubernetes/mlflow/deployment.yaml --ignore-not-found
kubectl delete -f infra/kubernetes/mlflow/postgresql.yaml --ignore-not-found
kubectl -n ml-platform delete secret mlflow-backend-store mlflow-postgresql --ignore-not-found
kubectl -n ml-platform delete configmap mlflow-runtime --ignore-not-found
kubectl delete -f infra/kubernetes/mlflow/service-account.yaml --ignore-not-found
kubectl -n ml-platform delete pvc data-mlflow-postgresql-0 --ignore-not-found
gcloud container clusters resize "$GKE_CLUSTER" --node-pool=cpu-pool --num-nodes=0 --zone="$ZONE" --project="$PROJECT_ID"
```

Do not delete the shared `ml-platform` namespace unless it contains no other
workload. PostgreSQL credentials are runtime Secrets and are never committed.
