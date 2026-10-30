# DP2 Method 2 — One-trigger DAG runbook (proposed implementation)

## What changes

`dp2_bronze_to_curated` performs **one Airflow DAG run** containing four *sequential*
original Dataproc processing batches (Silver B/O, Gold B/O), and four *sequential*
read-only validation Dataproc batches. After both profiles in a stage, it downloads
semantic summaries and the original event logs, collects benchmark evidence,
checks strict high-confidence semantic equivalence, compares original-job performance,
and writes an immutable winner artifact. Only the Silver Winner can become input to
the Gold pair. Original Silver/Gold transformations are unchanged.

Execution order: approval/identity → Silver pair → baseline submit/verify → optimized
submit/verify → two independent summary scans → two performance/evidence collections
→ formal Silver equivalence → compare/winner → Gold pair bound to selected Silver
→ baseline submit/verify → optimized submit/verify → two summary scans →
formal Gold equivalence → compare/winner → DAG SUCCESS.

**One Airflow DAG ≠ one Dataproc job.** A full successful run submits 8 paid
Dataproc batches (4 processing + 4 semantic scans), with quota gates before each.

## Source lineage and branch

1. **Do not base the patch on the earlier canonical SHA `62f17...`.** The attached
   post-run validator modules were developed in the `codex/spark-postrun-validation`
   worktree. Commit the already-tested event-log parser + test-fixture fixes in
   that branch first, then create a NEW worktree/branch from its HEAD.
2. Apply `dp2-method2-refactor.patch` in the NEW worktree; run `git diff --check`,
   Python tests and `airflow dags list-import-errors` before commit.
3. Only run with the new **committed** HEAD and a **clean** worktree. The Bronze
   Winner still points to original DP1 ingestion Git SHA; this is historical
   provenance, not the new DP2 code SHA.
4. The earlier Silver pair `dp2-silver-pair-r261030104547.json` is bound to the
   OLD Git SHA and must not be used for the refactored DAG. The DAG creates a
   new pair under an external artifact folder. Fresh IDs must not collide with
   already submitted Dataproc batches or GCS output prefixes.

## Local dry-run

From the new worktree (after commit):

```bash
BRONZE_WINNER="$HOME/ewallet-evidence/spark-method2-final/ab-20261029174838/postrun-check/dp1-bronze-winner.json"
OUT="$HOME/ewallet-evidence/spark-method2-final/ab-20261029174838/dp2-method2"
STAMP="$(date -u +%y%m%d%H%M%S)"
B="ab-20261029174838"

./scripts/run_dp2_method2.sh "$BRONZE_WINNER" \
  --artifact-root "$OUT" \
  --baseline-silver-run-id "${B}-baseline-silver-r${STAMP}" \
  --optimized-silver-run-id "${B}-optimized-silver-r${STAMP}" \
  --baseline-gold-run-id "${B}-baseline-gold-r${STAMP}" \
  --optimized-gold-run-id "${B}-optimized-gold-r${STAMP}"
```

Save the four run IDs and repeat **exactly those IDs** with `--execute` after
reviewing the printed conf, project, quota and cost. Do not generate another
STAMP before `--execute`. **Never execute from this document's sample with
unconfirmed run IDs.** `--execute` is explicit consent to paid GCP jobs.

## Verification (before any paid trigger)

- `python3 -m pytest -q tests/unit/test_dp2_method2_orchestration.py`
- `python3 -m pytest -q tests/unit/test_spark_postrun_integration.py tests/unit/test_spark_benchmark_evidence.py tests/unit/test_spark_semantic_equivalence.py tests/unit/test_spark_method2_identity_contract.py`
- `python3 -m compileall -q ops/gcp/dp2_method2.py data_platform/orchestration/airflow/dags/dp2_bronze_to_curated.py`
- `bash -n scripts/run_dp2_method2.sh scripts/run_dp2_canonical.sh`
- `airflow dags list-import-errors` (in the actual Airflow 3 environment)
- Inspect GCS output prefix absence, Dataproc quotas, project/bucket, and budget
  before approving Cloud Execution.

## Expected external evidence artifacts

```
dp2-method2/
  dp2-silver-pair.json
  silver/baseline/{quota.json,validation-quota.json,summary.json,original-batch.json,
                   original-performance.json,validation-evidence.json,batch-evidence.json}
  silver/optimized/{...}
  dp2-silver-equivalence.json
  dp2-silver-comparison.{json,csv}
  dp2-silver-winner.json
  dp2-gold-pair.json
  gold/baseline/{...}
  gold/optimized/{...}
  dp2-gold-equivalence.json
  dp2-gold-comparison.{json,csv}
  dp2-gold-winner.json
```

Artifacts are created with exclusive write operations; if a task fails after
creating partial outputs, do **not** blindly reset/clear the DAG task or delete
immutable outputs. Investigate the exact artifact and Dataproc batch status and
reconcile the partial state before retrying. The workflow intentionally has
`retries=0` and does not invent successful validation results.

## Unresolved and required cloud preflight checks

- Source archive did NOT include complete project, full Airflow dependencies,
  Terraform or credentials. Offline tests and syntax checks are possible here;
  end-to-end Airflow import and integration tests must run in your real repo.
- Event-log discovery enumerates `gs://BUCKET/spark-events/*` and selects logs by
  Dataproc create/state time ±30 minutes and explicit immutable Method 2 Spark
  properties. It **fails closed** if no unique matching original event log is
  found. Confirm actual event-log naming, `stateTime`, `zstd` and GCS listing
  permissions during a dry integration test before paying for all eight jobs.
- The existing aggregate `compare_spark_benchmark` enforces **the same Git SHA
  for every stage**. DP1 remains at SHA `62f17...`; refactored DP2 will have a
  newer SHA, so single-stage comparisons and winners work, but four-stage
  aggregate comparison will require a separately approved provenance-aware
  contract change. Do not rewrite DP1 SHA or misreport aggregate PASS.
- This design is a proposed implementation, **not yet production-verified**.
  Neither a full GCP run nor real Airflow import has been performed here.
