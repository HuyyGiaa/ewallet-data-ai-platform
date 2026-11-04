# Submission integration — 2026-11-01

Branch `claude/submission-integration-20261101`, created from the canonical
local checkpoint `feature/phase2-fraud-platform` @ `03f6b3c`, in a new isolated
worktree. Target: GitHub `main` (`db394d7`), tag `submission-20261101-v1`.

## Verified references

| Reference | SHA | Relationship |
|---|---|---|
| `origin/main` | `db394d7` | Merge of PR #1. Its tree adds nothing beyond merge-base `c6f4bec`; 147 commits behind `03f6b3c` |
| `origin/feature/phase2-fraud-platform` | `2306a18` | Ancestor of `03f6b3c` (51 commits behind) |
| Canonical checkpoint | `03f6b3c` | Integration base |
| `origin/claude/flink-local-benchmark-20261031` | `46fe7b4` | 4 commits on `1e08659`, which is an ancestor of `03f6b3c` |

## Integration steps

| Step | Source | Result |
|---|---|---|
| 1 | Canonical `03f6b3c` | Base |
| 2 | GitHub Flink `46fe7b4` (`f763d41`, `faca241`, `46bc732`, `46fe7b4`) | `--no-ff` merge. All 56 paths are additions except `.gitignore`, and none of them changed on the canonical side since `1e08659`, so the merge had no conflicts |
| 3 | Contract reconciliation | Register `infra/docker/flink-benchmark` in the uv contract and protect `apache-flink==1.19.1`; allowlist the container-internal `localhost:8081` healthcheck in the Flink compose file; guard the `exceptiongroup` backport import for Python < 3.11 |
| 4 | Documentation | Root README rewritten; this record |

The Flink scope is the reviewed remote implementation only. The newer local PC
Flink work (including the unfinished deterministic watermark changes) is not
included.

## Deliberately excluded

- `claude/mlops-campaign-20261031` (`d41f697`, 15 commits beyond `03f6b3c`):
  this branch belongs to an active MLOps session (Dataproc → ML-2 → KFP/MLflow
  → Feast Online → KServe) and is still in progress. It includes the KServe and
  Fraud API deployment manifests and images, which will be integrated after
  that session finishes.
- Every other worktree and branch listed in the
  [2026-10-31 consolidation](20261031_source_consolidation.md) remains deferred
  for the same reasons recorded there.
- The untracked evidence directories in the primary checkout.

## Dependency validation (uv 0.13.5)

All eight projects pass both `uv lock --check` and `uv sync --locked`. No lock
was regenerated or upgraded.

- `ai_platform/features/feast`
- `ai_platform/ml`
- `ai_platform/ml/pipelines`
- `ai_platform/ml/dvc-operator`
- `infra/docker/flink-benchmark`
- `infra/docker/llm-agent-tooling`
- `infra/docker/vllm-cpu-model`
- `infra/kubernetes/mlflow/image`

`tests.unit.test_uv_dependency_contract` passes (7/7) in the 3.12 tooling
environment.

## Offline regression (no cloud, cluster, image build or benchmark)

`unittest discover -s tests -t .` ran in nine environments:

- uv-locked: feast, ml, ml/pipelines, flink-benchmark, llm-agent-tooling
  (`--extra feast`);
- conda: `fintech`, `ewallet-feast`, `airflow` (isolated `AIRFLOW_HOME`);
- system Python 3.13.

A test counts as passing if it passes in at least one suitable environment.

| Result | Count |
|---|---:|
| Distinct tests | 949 |
| Pass in at least one environment | 929 |
| Skip-only | 19 |
| Fail everywhere | 1 (pre-existing, below) |

Additional runs:

- `tests/streaming` under `uv run --locked --project infra/docker/flink-benchmark`
  with pytest: 53 passed, 1 skipped.
- The Jenkins LLM stages, run as written:
  - system `python3` `tests/llm_agent`: OK (298 tests, 92 skipped);
  - kagent pilot and rubric matrix: OK;
  - locked tooling MCP/RAG/vLLM modules: 95 OK.
- pytest-style `tests/test_historical_fraud_features.py` and
  `tests/unit/test_verified_cloud_metadata.py` (`fintech`, with `PYSPARK_PYTHON`
  pinned to the driver interpreter): 9 passed.

Protected contracts, all passing:

- Airflow DAG import (10);
- Feast PIT validation (11);
- ML-1 snapshot contract (7);
- F9 canonical (9);
- KFP canonical (4);
- inference contracts including the frozen 41-column order (55);
- streaming contracts (53);
- Spark semantic equivalence (4);
- runtime-literal contract.

Pre-existing failure, identical on `03f6b3c`: `test_fraud_generator`
`test_v1_v2_nonempty_and_phase1_schemas_unchanged`. The test expects
`timestamp[ns]` but the installed pyarrow writes `timestamp[us]`.

## Static validation

- Helm lint `llm-agent-platform`: 0 failures across 6 value profiles (`dev`,
  `gcp`, `gke-cpu`, `gke-cpu-g1`, `local`, `r1-local`).
- `docker compose config`: passes for all six Compose files.
- `terraform fmt -check -recursive infra`: clean.
- `ops/validation/validate_runtime_config.py`: PASS.
- `ops.llm_agent.preflight`: `SOURCE_ONLY_PASS`, `runtime_verified: false`.
- `git diff --check`: clean.
- Trivy 0.75 offline secret scan of the tracked tree: the only matches are the
  reviewed exclusion regexes inside `infra/docker/vllm-cpu-model/trivy-secret.yaml`
  (self-referential, not credentials).

Nothing on this branch is deployed or runtime-verified by this integration.
