# Canonical MLOps dependency environments

The MLOps boundary uses deployment-owned environments. There is no repository
wide runtime lock and no environment is created for individual helper modules.

| Unit | Project file | Runtime responsibility |
|---|---|---|
| Feast offline/snapshot | `ai_platform/features/feast/pyproject.toml` | DP3 export, PIT validation and bounded snapshot preparation |
| F9 shared training | `ai_platform/ml/pyproject.toml` | training, evaluation, MLflow client and KFP component runtime |
| KFP compiler | `ai_platform/ml/pipelines/pyproject.toml` | standalone KFP 2.17.3 compilation only |
| MLflow server | `infra/kubernetes/mlflow/image/pyproject.toml` | PostgreSQL/GCS-backed MLflow server image |

Every direct dependency remains exactly pinned. The four reviewed lockfiles are
committed at their owning deployment boundaries. Validate them without
changing resolution from the repository root:

```bash
(cd ai_platform/features/feast && uv lock --check)
(cd ai_platform/ml && uv lock --check)
(cd ai_platform/ml/pipelines && uv lock --check)
(cd infra/kubernetes/mlflow/image && uv lock --check)
```

Docker builds use `uv sync --frozen`, so they fail rather than resolve a
different dependency graph. Dependency upgrades require a separate reviewed
lock regeneration; Gate 2 never performs one implicitly.

Canonical runtime values come from `.env.example` keys or Kubernetes
ConfigMaps/Secrets. The typed MLflow and KFP settings reject missing values,
localhost tracking endpoints, non-GCS artifacts, non-PostgreSQL metadata,
floating images and a KFP version other than 2.17.3. PostgreSQL credentials
remain runtime Secrets and are never committed.
