# Canonical MLOps dependency environments

The MLOps boundary uses deployment-owned environments. There is no repository
wide runtime lock and no environment is created for individual helper modules.

| Unit | Project file | Runtime responsibility |
|---|---|---|
| Feast offline/snapshot | `ai_platform/features/feast/pyproject.toml` | DP3 export, PIT validation and bounded snapshot preparation |
| F9 shared training | `ai_platform/ml/pyproject.toml` | training, evaluation, MLflow client and KFP component runtime |
| KFP compiler | `ai_platform/ml/pipelines/pyproject.toml` | standalone KFP 2.17.3 compilation only |
| MLflow server | `infra/kubernetes/mlflow/image/pyproject.toml` | PostgreSQL/GCS-backed MLflow server image |

Every direct dependency remains exactly pinned. Lockfiles are intentionally not
written by source reconciliation. Generate them from the repository root:

```bash
(cd ai_platform/features/feast && uv lock)
(cd ai_platform/ml && uv lock)
(cd ai_platform/ml/pipelines && uv lock)
(cd infra/kubernetes/mlflow/image && uv lock)
```

Commit each generated `uv.lock` only after reviewing its project file and
running the focused verification suite. Docker builds use `uv sync --frozen`,
so they fail rather than resolve a different dependency graph.

Canonical runtime values come from `.env.example` keys or Kubernetes
ConfigMaps/Secrets. The typed MLflow and KFP settings reject missing values,
localhost tracking endpoints, non-GCS artifacts, non-PostgreSQL metadata,
floating images and a KFP version other than 2.17.3. PostgreSQL credentials
remain runtime Secrets and are never committed.
