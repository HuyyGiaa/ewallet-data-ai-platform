# Source consolidation — 2026-10-31

Branch `claude/integration-20261031`, created from canonical
`feature/phase2-fraud-platform` @ `e58a500`.

| Step | Source | Result |
|---|---|---|
| 1 | ML-1 `codex/ml1-training-snapshot` @ `2c38c71` (`01f364b`, `2c38c71`; based on `e58a500`) | Fast-forward |
| 2 | LLM + Agent `codex/llm-agent-rubric-20261030` @ `572ae7d`, containing consolidation `acad36b` (9 commits) | Merge `457125b`. No file had changed on both sides since merge-base `2306a18`, so there were no conflicts |
| 3 | uv dependency contract | Commit `6ed3cc4` |

## Included source

- Consolidation, through `acad36b`:
  - EDAI-1 evidence and lineage tooling;
  - canonical size remediation;
  - final-gate optimisation;
  - bounded kagent pilot;
  - Block B online inference contracts.
- ML-1: immutable training snapshot and its P1 source-readiness fixes.
- LLM + Agent: Phases A–D, llm-d, agents, gateways, registry, KEDA, RAG
  retrieval, CI/CD and OTel.

## Deferred

- `codex/dp2-method2-recovery` beyond canonical: none. `a3f77ce` is already
  in canonical.
- Dirty Streaming Block A worktree.
- `codex/block-b-inference` `139ba7e`: one commit outside consolidation; it
  needs its own review.
- `codex/kagent-bounded-pilot` `df512c3`: sibling of `eb342e9`, differing only
  in `apiKeySecretRef`.
- `claude/dp3-readiness`.
- The untracked `requirements-mcp.in/.lock` in the LLM worktree. Their 67 pins
  all equal `infra/docker/llm-agent-tooling/uv.lock` at the same versions, so
  they are redundant. They are kept in place and not deleted.

## Dependency contract (uv)

- Six locked projects:
  - feast;
  - ml;
  - ml/pipelines;
  - ml/dvc-operator (new);
  - llm-agent-tooling;
  - mlflow image.
- `uv lock --check` passes for all six, and no lock was regenerated.
- Images install with `uv sync --locked`, and Jenkins uses `uv run --locked`.
- The requirements files are declared mirrors of the lock.
- DVC 3.63.0 lives in its own locked operator project, outside the F9 and KFP
  images. `.venv/bin/dvc --version` reports 3.63.0, which the operator guard
  accepts.
- Enforced by `tests/unit/test_uv_dependency_contract.py`.

## Offline regression (no cloud, model, cluster or image build)

Each Python environment ran the same `unittest discover` on the canonical
baseline (`git archive e58a500`) and on this branch. A test counts as passing
if it passes in at least one suitable environment:

| Environment | Baseline | Integration | Regressions |
|---|---|---|---|
| uv-locked Feast project (`uv sync --locked`) | 255 ok | 627 ok | 0 |
| `ewallet-feast` conda | 276 ok | 567 ok | 0 |
| `fintech` conda (pyspark, delta) | 316 ok | 562 ok | 0 |
| uv-locked ML project (mlflow) | 5 ok | 5 ok | 0 |
| uv-locked KFP project | 4 ok | 4 ok | 0 |
| Airflow env, isolated `AIRFLOW_HOME` (DAG import) | 10 ok | 10 ok | 0 |
| pytest-style modules (fintech) | — | 13 passed | — |
| LLM: system Python `tests/llm_agent` | — | OK (88 skipped, run below) | — |
| LLM: `uv run --locked` llm-agent-tooling (MCP/RAG) | — | OK | — |
| Helm lint (gcp, dev, local) | — | 0 failures | — |

Across environments, 750 of 773 distinct integration tests pass. The rest
are:
- skips;
- `test_fraud_generator` `test_v1_v2_nonempty_and_phase1_schemas_unchanged`,
  which also fails on the canonical baseline (a pre-existing failure).

Baseline failures in git-state tests disappear on this branch only because the
baseline export is not a git checkout; they are not counted as fixes.

Protected contracts, all passing:
- Feast PIT validation (11);
- ML-1 snapshot contract (7);
- cloud Feast training boundary (10);
- Feast training GCP runtime (16);
- publish training snapshot (10);
- F9 canonical contract (9);
- inference contracts and 41-feature schema (55);
- Spark runtime config (7);
- volatile audit fingerprint (4 ok, 1 skipped);
- canonical Airflow scripts (31).

## Runtime defaults

`llmd`, `agents`, `mcpGateway`, `a2aGateway`, `otelCollector`, `dashboards`,
`mcpServices` and `agents.autoscaling` are all `enabled: false`, and no
overlay enables them. MCP servers keep `DenyAllAuthenticator`. Knowledge
search defaults to the fail-closed retriever. Nothing on this branch is
deployed or runtime-verified by this integration.
