# EDAI one-pass rubric audit and implementation report

Audit date: 2026-10-29 (Asia/Ho_Chi_Minh)

Implementation worktree: `codex/feast-training-runtime` at
`3d16a3c3f2eae844fc4a13f6cfb2f79bf9142f46`

Workbook: `EDAI K11.xlsx`, active sheets `edai-1 (50%)` and
`edai-2 (50%)`; the third sheet is reference-only.

This report distinguishes implementation source from runtime proof. A source
file, test double, manifest, YAML, plan, or old screenshot does not by itself
earn runtime credit. The machine-readable row-by-row record is
[`rubric_evidence_matrix.csv`](rubric_evidence_matrix.csv), with a deterministic
JSON rendering in [`rubric_evidence_matrix.json`](rubric_evidence_matrix.json).

## A. Exact scored-row audit

The validator covers all 93 scored workbook rows and both 100-point raw sheet
totals. Current conservative evidence result:

| Sheet | VERIFIED | PARTIAL | OPEN | Verified raw | Weighted |
|---|---:|---:|---:|---:|---:|
| edai-1 (50%) | 11 rows / 26 points | 27 rows / 74 points | 0 | 26/100 | 13.0/50 |
| edai-2 (50%) | 1 row / 1 point | 45 rows / 82 points | 9 rows / 17 points | 1/100 | 0.5/50 |
| Total | 12 rows / 27 raw | 72 rows / 156 raw | 9 rows / 17 raw | 27/200 | **13.5/100** |

This is an internal, evidence-gated audit estimate, not a promised lecturer
grade. It intentionally gives zero points to local/source-only work. Verified
rows are edai-1 r5–8, r11–14, r20, r37–38 and edai-2 r44. The matrix records
the exact source commit, runtime artifact, timestamp, metrics and correctness
evidence for each. Spark r11–15 were audited only; no Spark source or runtime
was changed or executed in this task.

Largest exposed groups:

- edai-1 novel ideas r39–40: 20 points; candidate work exists but the novelty,
  final runtime proof and reviewer narrative are not consolidated.
- edai-1 DP1/DP2/DP3 plus DataHub r22–33: 24 points. Bounded Airflow and
  dataset-lineage evidence exists, but it is not fully bound to executing Git
  SHAs; DataFlow/DataJob-to-dataset lineage and complete run-scoped assertions
  are missing.
- edai-1 Flink r16–19: 10 points. Block A contains unreviewed dirty source and
  local tests, but no real Redpanda/Flink baseline/optimized runtime, UI,
  recovery, lag, state or correctness evidence.
- edai-1 r35–36: 4 points. No genuine SCD2 dimension; historical feature
  contracts have `event_timestamp` but not the rubric-required `created`
  timestamp.
- edai-2 direct Feast/KFP/MLflow/KServe r11–15: 10 points. Feast Offline is
  published, and registry source is reviewed, but registry publication, PIT,
  training snapshot, F9, registered model, Redis parity and KServe runtime are
  not complete.
- edai-2 test/performance r28–32: 9 points. Block B local tests pass, but there
  is no >90% coverage report, mutation score, property-based report, or Locust
  HTML/runtime result.
- edai-2 LLM/RAG/Agent/gateway/telemetry/CI future scope remains mostly
  source-only or absent. The complete exposure is retained row-by-row rather
  than hidden behind a platform summary.

Architecture/rubric conflicts are explicit in the matrix:

- r24 requests agentgateway as an A2A gateway, while Living Decision 21.12
  requires native kagent A2A and removes a standalone A2A runtime. Compliance
  needs instructor acceptance of native A2A plus Istio policy equivalence.
- r37–43 name NGINX, while the frozen architecture uses Istio Ambient and
  Gateway API. Equivalent routing, AuthZ, mTLS, rate limiting and UI access
  must be demonstrated; NGINX must not be added silently.
- r45 requests Ansible-deployed VM services. The approved scope keeps Ansible
  for the Jenkins VM, while applications stay on GKE. No Ansible source or
  runtime evidence currently exists.

## B. Actual implementation in this task

The audit continued into concrete source implementation:

- `ops/rubric/evidence_matrix.py`: standard-library, fail-closed evidence
  contract for all active rows. It enforces exact row coverage/points, unique
  identities, legal statuses, zero credit for OPEN/PARTIAL, full runtime
  identity for VERIFIED, matched measurements for explicit optimization rows,
  and equivalence decisions for architecture conflicts.
- `ops/rubric/validate_evidence_matrix.py`: read-only-by-default CLI. It also
  validates that every recorded source path exists in the recorded Git commit,
  not merely in the current checkout. `--json-output` is explicit.
- The same CLI can initialize a local, run-scoped evidence document. It binds
  the full Git SHA/worktree, optionally creates matched baseline/optimized
  sections, uses exclusive file creation and refuses overwrite. It has no
  network or cloud code path.
- `scripts/validation/validate_rubric_evidence.sh`: thin CI/Jenkins-compatible
  read-only entrypoint with no external dependency.
- `tests/unit/test_rubric_evidence_matrix.py`: regression tests for omissions,
  duplicates, source-only credit, missing runtime identity, missing benchmark
  measurements, architecture conflicts, deterministic JSON, Git source-path
  binding, invalid Git identity and create-only local evidence.
- `runtime_evidence_template.json`: a human-readable copy of the run-scoped
  evidence schema. The CLI should be used to create real instances.

Commands:

```bash
# Read-only validation; no file or cloud mutation
bash scripts/validation/validate_rubric_evidence.sh

# Explicitly refresh deterministic JSON after editing the CSV
python3 -m ops.rubric.validate_evidence_matrix \
  --matrix docs/rubric/rubric_evidence_matrix.csv \
  --json-output docs/rubric/rubric_evidence_matrix.json

# Create one local evidence file; fails if the path already exists
python3 -m ops.rubric.validate_evidence_matrix \
  --init-evidence /absolute/new/evidence.json \
  --rubric-id edai-2-row-32 \
  --run-id block-b-load-YYYYMMDDTHHMMSSZ \
  --benchmark
```

No feature/model semantics, 31/41 contract, dependency, lockfile, Spark source,
published snapshot or cloud object was changed.

## C. Verification and local artifacts

Final bounded verification results are recorded here after execution:

- `python3 -m unittest -v tests.unit.test_rubric_evidence_matrix`: **12 PASS,
  0 FAIL, 0 SKIP**.
- `bash -n scripts/validation/validate_rubric_evidence.sh`: **PASS**.
- `bash scripts/validation/validate_rubric_evidence.sh`: **PASS**, 93 rows,
  exact 100+100 points, all source bindings valid.
- Locked Feast/MLOps non-Spark regression (six explicit modules): **77 PASS,
  0 FAIL, 0 SKIP**.
- Wider seven-module Feast/MLOps attempt: **88 loaded, 86 PASS, 2 ERROR**.
  Both errors are `ModuleNotFoundError: pyspark` in the two Managed Spark
  exporter tests. This is `BLOCKED_ENV`: the locked Feast venv intentionally
  does not contain PySpark, and no dependency was installed or lockfile changed.
- `python3 -m compileall -q ops/rubric tests/unit/test_rubric_evidence_matrix.py`,
  JSON parsing, shell syntax, `git diff --check` and untracked-file whitespace
  checks: **PASS**.

Generated local source artifacts are the CSV and deterministic JSON matrix.
They contain no secrets, cloud tokens, fabricated screenshots or invented
runtime metrics.

## D. Git/worktree audit

Initial implementation worktree was clean at branch
`codex/feast-training-runtime`, HEAD
`3d16a3c3f2eae844fc4a13f6cfb2f79bf9142f46`. Canonical repository
`feature/phase2-fraud-platform` at
`0ff916dc840edebc3559ee3bf317f239e0fa1a51` was clean and read-only.
Block B was clean at `139ba7e20ceae78da673372783fe2d8d7a934292`.
KFP, F9/MLflow, GKE runtime, storage optimization and other specialized
worktrees inspected during the audit were clean.

Block A was deliberately not edited. It remains at
`3fa7c483d9e1596fb2ebd06b931403a38680b51b` with 12 modified and 2 untracked
paths spanning streaming contracts/generator/Flink/Silver validation/tests and
`data_platform/contracts`. Those changes have no immutable source SHA and are
therefore PARTIAL only.

No commit, merge, push, worktree creation/cleanup, cloud mutation, deployment,
Spark run, Dataproc run, Kafka/Redis mutation or expensive workload occurred.

## E. Strict user-run queue

Run only after independent review, in this dependency order. Preserve every
JSON/log and never repair an immutable prefix in place.

1. **Feast Registry local plan — read-only.** From the clean reviewed Feast
   commit, export the exact variables in
   `docs/ml/cloud_feast_training_snapshot.md` and run its default
   `python -m ops.gcp.publish_feast_registry ...` command. Required result:
   `READY_TO_PUBLISH`, four ordered FeatureViews, 31 historical and 41 model
   inputs, exact snapshot sources, manifest SHA
   `10184577ce9e4ae1234376f427111931becdaa84be87ee0923e0e5b27442d62b`,
   generation `1793210258915282`, and target absent. Direct Feast plan/apply
   against GCS remains forbidden.
2. **Explicit create-only registry publication.** Only after approval, rerun
   the reviewed command with `publish --authorization
   CREATE_ONLY_FEAST_REGISTRY --registry-artifact-output <new-local-path>`.
   The implementation uses `if_generation_match=0` and `retry=None`; never
   retry an ambiguous write. If evidence writing fails after upload, do not
   publish again: use `readback-only --trusted-registry-artifact <same-path>`.
   Retain generation-pinned bytes, SHA-256 and inventory evidence.
3. **PIT validation.** Run the documented bounded validation-sample export only
   after registry readback. This is a Dataproc job (billing/quota risk). Require
   ten cases, 310 comparable values, zero mismatch, `[T-window,T)` exclusion,
   UTC microseconds and no label leakage. Preserve batch ID, usage and manifest.
4. **Immutable bounded training snapshot.** Run the versioned training-source
   export, copy only the bounded completed output, materialize in 1,000-row
   resumable chunks, validate exact 41-input ordering/types/nullability and
   publish only to a new create-only prefix with manifest digest/readback.
5. **F9 KFP/MLflow.** Submit the real four-step/four-candidate bounded pipeline;
   retain graph, component logs, data/feature/Git/image identities, metrics and
   artifacts. Register the validation winner with candidate/latest automation;
   production promotion stays manual. Roll back by immutable model/data/image
   identity, never by retagging unknown bytes.
6. **Block A and data rubric runtime.** First independently review and commit
   the existing dirty source. Then run generator→Redpanda→Flink with bootstrap,
   checkpoint/savepoint recovery, late/duplicate/out-of-order/replay cases,
   immutable Offline sink and Feast PushSource→Redis parity. Capture matched
   baseline/optimized Flink metrics and UI. Separately run DataHub DataJob
   lineage/assertion readback and versioned storage/warehouse benchmarks.
   Spark's 13 points remain a separate user-owned run; do not combine it with
   this queue.
7. **Block B.** Only after registered model and Block A online parity: run real
   Feast Online/Redis retrieval, 41-input assembly, KServe wire/artifact parity,
   FastAPI readiness/error cases, coverage, bounded mutation/property tests,
   Locust HTML, p50/p95/p99/throughput/failures/resources, logs and traces.

Cloud steps require a fresh cost/quota/IAM preflight and explicit authorization.
On a failed or ambiguous immutable write: preserve evidence, perform readback,
and stop; do not overwrite, delete or silently retry.

## F. Outstanding requirements

Every non-VERIFIED row remains listed in the CSV with its exact missing proof
and next safe command. In particular:

- Spark r11–15 were excluded from implementation/execution even though four
  historical local rows have evidence; the pipeline-integration row is still
  PARTIAL and the user must own any new Spark rerun.
- DP1/DP2/DP3 need source-SHA-bound real DagRun evidence; DataHub needs true
  DataFlow/DataJob lineage and full assertions.
- SCD2, feature `created` timestamp, warehouse indexing, final ERD and both
  novel-idea submissions are incomplete.
- Feast Registry publication, PIT, training snapshot, F9/MLflow model registry,
  Online/Redis parity, KServe and Block B integrated runtime are not PASS.
- Coverage, mutation, property-based testing, Locust, component CI/CD,
  observability, rollback and security runtime proof remain incomplete.
- LLM inference, custom model, RAG-through-Feast, agentgateway, agent registry,
  kagent specialists/coordinator, MCP policy proxy, warm fleet, LLM/agent
  telemetry, A/B tests, gateway/domain/HTTPS and centralized secret management
  remain future scored work. Architecture-equivalence decisions do not award
  those points without instructor acceptance and runtime evidence.

Therefore the project is **not** 100/100, ML Platform is **not** closed, and no
submission-ready claim is made.
