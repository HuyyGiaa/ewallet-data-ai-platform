# MLOps and fraud serving runbook

Execution order after a published ML-2 Training Snapshot exists. Every cloud
command uses explicit project, zone, namespace and kube context
`gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev`.

The Feature Store (Feast Offline/Online, Airflow incremental materialization,
streaming Offline/Online writers and consistency validation) has its own
procedure: [Feature Store runbook](../../../runbooks/edai2/FEATURE_STORE_RUNBOOK.md).
The design and the verified results are in [FEATURE_STORE.md](../FEATURE_STORE.md).

## Python environments (uv, one lock per deployment boundary)

| Boundary | Project | Used by |
|---|---|---|
| Feast offline + ML-2 | `ai_platform/features/feast` | snapshot export/materialize, `ml2-materializer` image |
| F9 training | `ai_platform/ml` | KFP component image `f9-kfp-runtime` |
| KFP compiler | `ai_platform/ml/pipelines` | `compile_pipeline`, `ops/mlops/submit_f9_run.py` |
| DVC operator | `ai_platform/ml/dvc-operator` | DVC 3.63.0 pointer |
| KServe predictor | `ai_platform/serving` | `fraud-detector` model-inside-image |
| Fraud API + Feast Online | `ai_platform/inference` | `fraud-api` image, `online_operator` Jobs |

`uv lock --check --project <p>` and `uv run --locked --no-sync --project <p>`
for every boundary; images run `uv sync --locked --no-dev`.

## 1. ML-2 Training Snapshot

`docs/ml/ml2_training_snapshot_operator_runbook.md` for export. PIT
materialization runs next to the snapshot as a Cloud Run Job
(`ops/gcp/run_ml2_materialization.py`, image `ml2-materializer`), with the
bounded Dask reader; publication and DVC stay local and create-only.

## 2. F9 on KFP + MLflow

`ops/gcp/prepare_f9_immutable_artifacts.sh` builds the image by digest and
recompiles the package. Submit with `ops/mlops/submit_f9_run.py` through
`kubectl port-forward svc/ml-pipeline-ui 8888:80 -n kubeflow`. The winner is
registered with alias `candidate`; `production` is only set by a human:

```bash
python - <<'PY'
from mlflow import MlflowClient
MlflowClient("http://127.0.0.1:5000").set_registered_model_alias(
    "fraud-detector", "production", "<reviewed version>")
PY
```

## 3. Feature Store

See the [Feature Store runbook](../../../runbooks/edai2/FEATURE_STORE_RUNBOOK.md).

## 4. KServe Standard + Fraud API

```bash
scripts/mlops/install_kserve.sh --execute          # cert-manager + KServe 0.16.0, Standard, no ingress
python -m ops.mlops.bake_serving_image --alias candidate --execute ...
kubectl apply -f infra/kubernetes/fraud-serving/namespace.yaml -f infra/kubernetes/fraud-serving/network-policy.yaml
envsubst < infra/kubernetes/fraud-serving/inferenceservice.yaml.tmpl | kubectl apply -f -
envsubst < infra/kubernetes/fraud-serving/fraud-api.yaml.tmpl | kubectl apply -f -
```

Wire contract `ewallet-fraud-v2-wire-v1` (`ai_platform/inference/wire.py`):
41 named V2 inputs in training order, JSON null for missing values, response
`model_version` must equal the deployed version.

Rollback: re-apply the previous InferenceService image digest and set
`EWALLET_MODEL_VERSION` back; Redis is re-materialized, never flushed as a
rollback step.

## 5. Cleanup

Scale `cpu-pool` back to 0 after evidence collection. Durable GCS objects,
registries, PostgreSQL and MLflow artifacts are never deleted.
