# Canonical standalone Kubeflow Pipelines workflow

## Scope and execution gate

This milestone packages the already validated Feast/F7/F9/MLflow behavior as a
standalone Kubeflow Pipelines DAG. It does not install the full Kubeflow
distribution and does not change model-selection logic.

Run it only after canonical DP1/DP2/DP3, cloud Feast validation, and the bounded
training snapshot gate have passed. The immutable snapshot manifest supplies
the dataset, Silver, feature, and snapshot lineage values used below.

The pre-training gates verify manifest SHA-256, immutable lineage, 41-feature
contract, declared row/split/class counts against the Parquet data, exact
schema, unique transaction IDs, disjoint split IDs, and chronological split
assignment. All four configured candidates fit on train and report validation
metrics. Dummy remains ineligible. Validation PR-AUC then F1 chooses one
winner. Only that winner is evaluated on the test split. MLflow creates four
candidate runs, logs test metrics/model only for the winner, registers that
winner, and assigns no production stage or alias.

## Pinned runtime and bounded resources

- Standalone KFP manifests and SDK: `2.17.3`.
- Runtime image: an explicit, immutable deployment input. The example tag is
  `f9-kfp-runtime:kfp-2.17.3-f9-v1`; `latest` is rejected.
- Python base: `python:3.10.19-slim-bookworm`.
- Python ML packages are exact pins in `ai_platform/ml/pyproject.toml`; the
  compiler is separately pinned by `ai_platform/ml/pipelines/pyproject.toml`.
- Train, winner-test, and publish tasks request 500m CPU/1 GiB and are limited
  to 2 CPU/3 GiB. Validation/selection tasks request 100m/256 MiB and are
  limited to 500m/1 GiB. No task requests a GPU and KFP caching is disabled.

Four train pods can run concurrently, so a single `e2-standard-4` node may
queue some tasks rather than run every CPU limit simultaneously. This preserves
bounded capacity and correctness.

## Future USER_RUN

These commands are prepared for the later approved execution window. They have
not been run by this milestone.

Set reviewed runtime values. Never infer or reuse a run ID:

```bash
cd ~/Develop/ewallet-data-ai-platform
export PROJECT_ID="ewallet-data-ai-platform-hg26"
export REGION="asia-southeast1"
export ZONE="asia-southeast1-b"
export GKE_CLUSTER="ewallet-dev"
export KFP_VERSION="2.17.3"
export KFP_NAMESPACE="kubeflow"
export DATASET_VERSION="canonical-100gb-v1"
export SILVER_RUN_ID="<passed-canonical-dp2-run-id>"
export FEATURE_RUN_ID="<passed-canonical-dp3-run-id>"
export SNAPSHOT_ID="<passed-bounded-training-snapshot-id>"
export SNAPSHOT_URI="gs://${PROJECT_ID}-data/ml/training-snapshots/${DATASET_VERSION}/${SNAPSHOT_ID}"
export SNAPSHOT_MANIFEST_SHA256="<sha256-from-passed-snapshot-evidence>"
export SOURCE_GIT_SHA="$(git rev-parse HEAD)"
export MLFLOW_TRACKING_URI="http://mlflow.ml-platform.svc.cluster.local:5000"
export EXPERIMENT_NAME="ewallet-fraud-comparison"
export REGISTERED_MODEL_NAME="ewallet-fraud-detector"
export F9_RUNTIME_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/ewallet-platform/f9-kfp-runtime:kfp-2.17.3-f9-v1"
export TRAINING_GSA="ewallet-training@${PROJECT_ID}.iam.gserviceaccount.com"
export PIPELINE_PACKAGE="ai_platform/ml/pipelines/compiled/f9_canonical_pipeline.yaml"
```

Build and push the one pinned runtime image:

```bash
(cd ai_platform/ml && uv lock)
gcloud builds submit . \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --config=infra/kubernetes/kfp/image/cloudbuild.yaml \
  --substitutions="_IMAGE=${F9_RUNTIME_IMAGE}"
```

Scale the existing pool, connect to GKE, and install standalone KFP only:

```bash
gcloud container clusters resize "$GKE_CLUSTER" --node-pool=cpu-pool \
  --num-nodes=1 --zone="$ZONE" --project="$PROJECT_ID"
gcloud container clusters get-credentials "$GKE_CLUSTER" \
  --zone="$ZONE" --project="$PROJECT_ID"
kubectl apply -k "github.com/kubeflow/pipelines/manifests/kustomize/cluster-scoped-resources?ref=${KFP_VERSION}"
kubectl wait --for=condition=established --timeout=90s \
  crd/applications.app.k8s.io
kubectl apply -k "github.com/kubeflow/pipelines/manifests/kustomize/env/dev?ref=${KFP_VERSION}"
kubectl -n "$KFP_NAMESPACE" wait --for=condition=Available \
  deployment --all --timeout=15m
```

Authorize the KFP runner to use the explicitly selected bounded training GSA. This is a
future IAM mutation and therefore requires explicit approval at execution time:

```bash
gcloud iam service-accounts add-iam-policy-binding \
  "$TRAINING_GSA" \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:${PROJECT_ID}.svc.id.goog[kubeflow/pipeline-runner]" \
  --project="$PROJECT_ID"
kubectl apply -f infra/kubernetes/kfp/pipeline-runner-service-account.yaml
kubectl -n "$KFP_NAMESPACE" annotate serviceaccount pipeline-runner \
  iam.gke.io/gcp-service-account="$TRAINING_GSA" \
  --overwrite
```

Generate the KFP compiler lock first (outside this reconciliation task), then
compile with the pinned SDK/runtime image and record the package digest. The
generated YAML is intentionally marked `PENDING_REGENERATION` until this
command is run; never edit it manually:

```bash
(cd ai_platform/ml/pipelines && uv lock)
uv run --project ai_platform/ml/pipelines \
  python -m ai_platform.ml.pipelines.compile_pipeline \
  --runtime-image "$F9_RUNTIME_IMAGE" \
  --output "$PIPELINE_PACKAGE"
sha256sum "$PIPELINE_PACKAGE" | tee "/tmp/kfp-pipeline-${SNAPSHOT_ID}.sha256"
```

Expose the internal standalone API locally, upload the compiled pipeline, and
create exactly one canonical run:

```bash
kubectl -n "$KFP_NAMESPACE" port-forward service/ml-pipeline-ui 8080:80 \
  > /tmp/kfp-port-forward.log 2>&1 &
export KFP_PORT_FORWARD_PID=$!
export KFP_HOST="http://127.0.0.1:8080"

/tmp/ewallet-kfp-2.17.3/bin/python - <<'PY'
import json, os
from pathlib import Path
from kfp import Client

client = Client(host=os.environ["KFP_HOST"])
pipeline = client.upload_pipeline(
    os.environ["PIPELINE_PACKAGE"],
    pipeline_name="f9-canonical-bounded-experiment",
    description="Immutable Feast snapshot to validation-selected MLflow winner",
)
experiment = client.create_experiment("f9-canonical")
run = client.run_pipeline(
    experiment_id=experiment.experiment_id,
    job_name=f"f9-{os.environ['SNAPSHOT_ID']}",
    pipeline_id=pipeline.pipeline_id,
    service_account="pipeline-runner",
    enable_caching=False,
    params={
        "dataset_version": os.environ["DATASET_VERSION"],
        "silver_run_id": os.environ["SILVER_RUN_ID"],
        "feature_run_id": os.environ["FEATURE_RUN_ID"],
        "snapshot_id": os.environ["SNAPSHOT_ID"],
        "snapshot_uri": os.environ["SNAPSHOT_URI"],
        "snapshot_manifest_sha256": os.environ["SNAPSHOT_MANIFEST_SHA256"],
        "mlflow_tracking_uri": os.environ["MLFLOW_TRACKING_URI"],
        "runtime_image": os.environ["F9_RUNTIME_IMAGE"],
        "experiment_name": os.environ["EXPERIMENT_NAME"],
        "registered_model_name": os.environ["REGISTERED_MODEL_NAME"],
        "git_commit_sha": os.environ["SOURCE_GIT_SHA"],
    },
)
payload = {"pipeline_id": pipeline.pipeline_id, "run_id": run.run_id}
Path("/tmp/kfp-canonical-run.json").write_text(json.dumps(payload, indent=2))
print(json.dumps(payload))
PY
export KFP_RUN_ID="$(python3 -c 'import json; print(json.load(open("/tmp/kfp-canonical-run.json"))["run_id"])')"
```

## Validation and evidence

Wait for the API result and capture component state/logs:

```bash
/tmp/ewallet-kfp-2.17.3/bin/python - <<'PY'
import os
from kfp import Client
run = Client(host=os.environ["KFP_HOST"]).wait_for_run_completion(
    os.environ["KFP_RUN_ID"], timeout=7200, sleep_duration=15
)
print(run.state)
assert str(run.state).upper().endswith("SUCCEEDED"), run.state
PY
kubectl -n "$KFP_NAMESPACE" get pods \
  -l "pipelines.kubeflow.org/runid=${KFP_RUN_ID}" -o wide \
  | tee "/tmp/kfp-${KFP_RUN_ID}-pods.txt"
for pod in $(kubectl -n "$KFP_NAMESPACE" get pods \
  -l "pipelines.kubeflow.org/runid=${KFP_RUN_ID}" -o name); do
  kubectl -n "$KFP_NAMESPACE" logs "$pod" --all-containers=true \
    > "/tmp/kfp-${KFP_RUN_ID}-${pod##*/}.log"
done
```

Verify MLflow's four validation runs, one tested winner, registration, lineage,
and absence of automatic production promotion:

```bash
kubectl -n ml-platform port-forward service/mlflow 5000:5000 \
  > /tmp/mlflow-port-forward.log 2>&1 &
export MLFLOW_PORT_FORWARD_PID=$!
export LOCAL_MLFLOW_URI="http://127.0.0.1:5000"
uv run --project ai_platform/ml python - <<'PY'
import os
from mlflow import MlflowClient
c = MlflowClient(tracking_uri=os.environ["LOCAL_MLFLOW_URI"])
e = c.get_experiment_by_name(os.environ["EXPERIMENT_NAME"])
runs = c.search_runs([e.experiment_id])
children = [r for r in runs if "mlflow.parentRunId" in r.data.tags]
assert len(children) == 4, len(children)
assert all("validation_pr_auc" in r.data.metrics for r in children)
tested = [r for r in children if "test_pr_auc" in r.data.metrics]
assert len(tested) == 1, [r.info.run_id for r in tested]
required = {"dataset_version", "feature_run_id", "snapshot_id", "git_commit_sha"}
assert all(required <= set(r.data.params) for r in children)
versions = list(c.search_model_versions(
    f"name='{os.environ['REGISTERED_MODEL_NAME']}'"))
assert versions and versions[-1].run_id
assert "production" not in c.get_registered_model(
    os.environ["REGISTERED_MODEL_NAME"]).aliases
print({"candidates": 4, "winner": tested[0].info.run_id,
       "model_version": versions[-1].version})
PY
```

Preserve `/tmp/kfp-canonical-run.json`, the compiled-package digest, pod list,
component logs, MLflow assertion output, KFP graph screenshot, and MLflow winner
page screenshot. Together they satisfy
`docs/evidence/cloud/kfp_canonical_contract.json`.

## Cleanup and rollback

Archive/delete the bounded run record only after evidence review, uninstall only
the standalone KFP installation, remove the future Workload Identity binding,
and return the existing pool to zero:

```bash
/tmp/ewallet-kfp-2.17.3/bin/python - <<'PY'
import os
from kfp import Client
c = Client(host=os.environ["KFP_HOST"])
c.archive_run(os.environ["KFP_RUN_ID"])
c.delete_run(os.environ["KFP_RUN_ID"])
PY
kill "$KFP_PORT_FORWARD_PID" "$MLFLOW_PORT_FORWARD_PID" 2>/dev/null || true
kubectl delete -f infra/kubernetes/kfp/pipeline-runner-service-account.yaml \
  --ignore-not-found
kubectl delete -k "github.com/kubeflow/pipelines/manifests/kustomize/env/dev?ref=${KFP_VERSION}"
kubectl delete -k "github.com/kubeflow/pipelines/manifests/kustomize/cluster-scoped-resources?ref=${KFP_VERSION}"
gcloud iam service-accounts remove-iam-policy-binding \
  "$TRAINING_GSA" \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:${PROJECT_ID}.svc.id.goog[kubeflow/pipeline-runner]" \
  --project="$PROJECT_ID"
gcloud container clusters resize "$GKE_CLUSTER" --node-pool=cpu-pool \
  --num-nodes=0 --zone="$ZONE" --project="$PROJECT_ID"
```

Do not delete the MLflow run/model or GCS snapshot/artifacts; they are canonical
lineage evidence. Standalone KFP has no built-in authentication, so this plan
keeps it internal and uses local port-forward only.
