# Canonical stage-specific Spark benchmark runbook

This runbook prepares a later full-scale comparison on `canonical-100gb-v1`
(100,156,470,260 source bytes; 369,600,000 logical transactions). No run in
this source-only milestone is a new performance result. Final DP1/DP2/DP3
execution is deferred until after the MLOps milestone.

## Why the global profile was rejected

The previous global optimized package was 9.74% slower and used about 9.05%
more DCU than baseline. It also changed physical upstream layouts between
downstream pairs, so the result was not stage-causal. Event logs found no AQE
skew split and no canonical shuffled join-key skew. The generator still has
logical merchant, time and channel skew, but `skewJoin` stays false and no skew
speedup is claimed.

## Common resource contract

Baseline and candidates share Standard tier, Dataproc runtime 2.2.89 / Spark
3.5.3, TTL 8h, 4-core driver/executors, 9600m heap, 3840m overhead, dynamic
allocation min/initial/max 2/6/7, allocation ratio 0.3, 192m broadcast
threshold, 300s idle timeout and shuffle tracking. Maximum provisioning remains
one driver plus seven executors: 32 vCPU and about 3,200 GB disk.

## Provisional profiles

| Stage | Profile | AQE | skewJoin | Shuffle | Allocation profile |
|---|---|---:|---:|---:|---|
| every stage | baseline | false | false | 200 | unset |
| DP1 Bronze | candidate (`optimized`) | true | false | 200 | performance |
| DP2 Silver | candidate (`optimized`) | false | false | 304 | unset |
| DP2 Gold | candidate (`optimized`) | true | false | 400 | unset |
| DP3 Features | candidate (`optimized`) | false | false | 200 | unset |

`optimized` is retained only as the candidate identity in immutable run IDs and
evidence. The authoritative runtime lookup is `(stage, profile)` and unknown
values fail closed. These are benchmark candidates, not production-optimal
claims.

## Compute partitions and physical output layout

`spark.sql.shuffle.partitions` controls compute parallelism. A separate,
table-specific writer policy controls file fragmentation using supported
`repartition` operations and `maxRecordsPerFile`. Existing physical
partition columns remain unchanged. In particular, Gold OBT has a fixed total
writer-task budget keyed by `event_date`; it cannot recreate `dates × 400`
files simply because Gold computes with 400 shuffle partitions. Baseline and
candidate share the same writer policy.

## Corrections supported by event-log evidence

- DP1 uses one paginated GCS metadata inventory and reloads only objects whose
  listing metadata lacks MD5.
- Silver consolidates transaction count/null/distinct checks and gives the
  reused cleaned transaction lineage one bounded DISK_ONLY lifecycle.
- Gold reuses fact transactions and justified dimensions, with explicit
  unpersist; output layout is bounded independently from compute.
- DP3 reuses one narrow projected Silver transaction base for all four feature
  builders. All 31 features and `[T-window,T)` semantics remain unchanged.

## Method 2: stage-common-input benchmark

1. DP1 baseline and candidate both read `canonical-100gb-v1`.
2. Compare semantic evidence, then explicitly select one DP1 winner.
3. Silver baseline and candidate both read that exact Bronze run.
4. Gold baseline and candidate both read one exact selected Silver run.
5. DP3 baseline and candidate both read that same Silver run and one fixed Gold
   lineage.

Upstream run IDs are explicit; profile names never infer them. Pair validation
rejects different upstream IDs before submission. Every output prefix is new,
run-scoped and immutable. Failed, incomplete or unequal evidence cannot produce
a next-stage winner.

Create the later benchmark identity. The DP1 wrapper remains convenient because
both runs have the same canonical source. For downstream Method 2 comparisons,
use the stage submitters directly: the combined DP2 DAG is an operational
pipeline and would otherwise make Gold consume each profile's own Silver output.

```bash
export SPARK_BENCHMARK_ID="ab-$(date -u +%Y%m%d%H%M%S)"
export EWALLET_DATA_BUCKET=ewallet-data-ai-platform-hg26-data
./scripts/start_airflow_local.sh

# Dry-run only first. Both DP1 runs share canonical-100gb-v1.
./scripts/run_dp1_canonical.sh --profile baseline --benchmark-id "$SPARK_BENCHMARK_ID"
./scripts/run_dp1_canonical.sh --profile optimized --benchmark-id "$SPARK_BENCHMARK_ID"

# Prepare one fail-closed local pair manifest before either stage submission.
# The command refuses different inputs, reused outputs, bad profile/stage IDs,
# or an existing manifest path. Repeat with the corresponding stage and roots.
ATTEMPT_ID="r$(date -u +%y%m%d%H%M%S)"
BASELINE_SILVER="${SPARK_BENCHMARK_ID}-baseline-silver-${ATTEMPT_ID}"
CANDIDATE_SILVER="${SPARK_BENCHMARK_ID}-optimized-silver-${ATTEMPT_ID}"
SELECTED_BRONZE="REPLACE_WITH_SELECTED_BRONZE_RUN"
python -m ops.gcp.prepare_spark_stage_pair \
  --stage dp2-silver \
  --upstream-run-id "$SELECTED_BRONZE" \
  --baseline-output-run-id "$BASELINE_SILVER" \
  --baseline-output-root "gs://${EWALLET_DATA_BUCKET}/lakehouse/runs/${BASELINE_SILVER}/silver" \
  --candidate-output-run-id "$CANDIDATE_SILVER" \
  --candidate-output-root "gs://${EWALLET_DATA_BUCKET}/lakehouse/runs/${CANDIDATE_SILVER}/silver" \
  --output "docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/plans/dp2-silver.json"

# Render both Silver submissions. They read exactly the same Bronze run.
for PROFILE in baseline optimized; do
  if [[ "$PROFILE" == baseline ]]; then
    OUTPUT_VAR="$BASELINE_SILVER"
  else
    OUTPUT_VAR="$CANDIDATE_SILVER"
  fi
  python -m ops.gcp.submit_bronze_to_silver \
    --dataset-version canonical-100gb-v1 \
    --source-bronze-run-id "$SELECTED_BRONZE" \
    --run-id "$OUTPUT_VAR" --spark-profile "$PROFILE" \
    --benchmark-id "$SPARK_BENCHMARK_ID" \
    --git-sha "$(git rev-parse HEAD)" --ttl 8h
done

# After Silver equality and explicit winner selection, Gold uses one Silver.
SELECTED_SILVER="REPLACE_WITH_SELECTED_SILVER_RUN"
BASELINE_GOLD="${SPARK_BENCHMARK_ID}-baseline-gold-${ATTEMPT_ID}"
CANDIDATE_GOLD="${SPARK_BENCHMARK_ID}-optimized-gold-${ATTEMPT_ID}"
python -m ops.gcp.prepare_spark_stage_pair \
  --stage dp2-gold --upstream-run-id "$SELECTED_SILVER" \
  --baseline-output-run-id "$BASELINE_GOLD" \
  --baseline-output-root "gs://${EWALLET_DATA_BUCKET}/lakehouse/runs/${BASELINE_GOLD}/gold" \
  --candidate-output-run-id "$CANDIDATE_GOLD" \
  --candidate-output-root "gs://${EWALLET_DATA_BUCKET}/lakehouse/runs/${CANDIDATE_GOLD}/gold" \
  --output "docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/plans/dp2-gold.json"
for PROFILE in baseline optimized; do
  if [[ "$PROFILE" == baseline ]]; then
    OUTPUT_VAR="$BASELINE_GOLD"
  else
    OUTPUT_VAR="$CANDIDATE_GOLD"
  fi
  python -m ops.gcp.submit_silver_to_gold \
    --dataset-version canonical-100gb-v1 \
    --source-silver-run-id "$SELECTED_SILVER" \
    --run-id "$OUTPUT_VAR" --spark-profile "$PROFILE" \
    --benchmark-id "$SPARK_BENCHMARK_ID" \
    --git-sha "$(git rev-parse HEAD)" --ttl 8h
done

# DP3 uses the same selected Silver and one fixed selected Gold for both runs.
SELECTED_GOLD="REPLACE_WITH_SELECTED_GOLD_RUN"
BASELINE_FEATURES="${SPARK_BENCHMARK_ID}-baseline-feat-${ATTEMPT_ID}"
CANDIDATE_FEATURES="${SPARK_BENCHMARK_ID}-optimized-feat-${ATTEMPT_ID}"
python -m ops.gcp.prepare_spark_stage_pair \
  --stage dp3-features --upstream-run-id "$SELECTED_SILVER" \
  --upstream-run-id "$SELECTED_GOLD" \
  --baseline-output-run-id "$BASELINE_FEATURES" \
  --baseline-output-root "gs://${EWALLET_DATA_BUCKET}/lakehouse/runs/${BASELINE_FEATURES}/gold" \
  --candidate-output-run-id "$CANDIDATE_FEATURES" \
  --candidate-output-root "gs://${EWALLET_DATA_BUCKET}/lakehouse/runs/${CANDIDATE_FEATURES}/gold" \
  --output "docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/plans/dp3-features.json"
for PROFILE in baseline optimized; do
  if [[ "$PROFILE" == baseline ]]; then
    OUTPUT_VAR="$BASELINE_FEATURES"
  else
    OUTPUT_VAR="$CANDIDATE_FEATURES"
  fi
  python -m ops.gcp.submit_historical_features \
    --dataset-version canonical-100gb-v1 \
    --source-silver-run-id "$SELECTED_SILVER" \
    --source-core-gold-run-id "$SELECTED_GOLD" \
    --run-id "$OUTPUT_VAR" --spark-profile "$PROFILE" \
    --benchmark-id "$SPARK_BENCHMARK_ID" \
    --git-sha "$(git rev-parse HEAD)" --ttl 8h
done

# Add --execute only after reviewing the pair manifest, roots, common resource
# settings and both effective Spark-property dry runs.
```

A retry always receives a fresh attempt/output ID. Never delete or reuse failed
batches or canonical output prefixes. The existing quota handoff guard must pass
before each real submit; handoff wait time is excluded from Spark runtime.

## Semantic-equivalence gate

Each stage summary contains exact table inventory, schema hash, partition-column
contract, row count, required-null counts, business invariants, explicit source
run IDs, Git SHA, stage/profile/property snapshot, and physical file count/bytes
with min/median/p95/max sizes. Content uses canonical column order, explicit
nulls and normalized timestamps with two independent commutative hash
aggregates. The result is named high-confidence semantic equivalence; it is not
claimed as collision-free cryptographic identity.

The gate fails closed for missing evidence, schema/row/null/invariant/fingerprint
differences or source-lineage differences. Physical file layout may differ and
is measured rather than treated as semantic inequality. Bounded `exceptAll` may
be used for fixtures, but is not the default 100 GB path.

Future summary command (dry-run first):

```bash
STAGE="REPLACE_WITH_STAGE"
PROFILE="REPLACE_WITH_BASELINE_OR_OPTIMIZED"
OUTPUT_RUN="REPLACE_WITH_OUTPUT_RUN_ID"
SOURCE_RUN="REPLACE_WITH_EXPLICIT_COMMON_UPSTREAM_RUN"
VALIDATION_BATCH="abval-$(date -u +%y%m%d%H%M%S)"
SUMMARY_URI="gs://${EWALLET_DATA_BUCKET}/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/${PROFILE}/${STAGE}-summary.json"
python -m ops.gcp.submit_spark_output_summary \
  --batch-id "$VALIDATION_BATCH" \
  --stage "$STAGE" \
  --benchmark-id "$SPARK_BENCHMARK_ID" \
  --spark-profile "$PROFILE" \
  --output-run-id "$OUTPUT_RUN" \
  --source-run-id "$SOURCE_RUN" \
  --dataset-version canonical-100gb-v1 \
  --git-sha "$(git rev-parse HEAD)" \
  --summary-uri "$SUMMARY_URI"
```

Compare summaries only after both exist:

```bash
python -m ops.gcp.validate_spark_output_equivalence \
  --baseline-summary baseline-summary.json \
  --optimized-summary candidate-summary.json \
  --output "docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/equivalence/${STAGE}.json"
```

The stage validation JSON supplied to the collector must contain application
runtime, spill bytes, task count, output file count and output bytes. The
collector adds batch runtime, DCU, shuffle usage, runtime properties and driver
log URI. Only complete, succeeded and equivalent evidence is eligible for
explicit winner review.

Prepare that small stage-level metric document from the reviewed event-log and
output-summary evidence, then collect the Managed Spark usage. `BATCH_ID` is the
original stage batch/output run, not the separate summary batch.

```bash
STAGE="REPLACE_WITH_STAGE"
PROFILE="REPLACE_WITH_BASELINE_OR_OPTIMIZED"
BATCH_ID="REPLACE_WITH_STAGE_OUTPUT_RUN_ID"
UPSTREAM_RUN="REPLACE_WITH_EXPLICIT_COMMON_UPSTREAM_RUN"
VALIDATION_JSON="docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/${PROFILE}/${STAGE}-application.json"
EVIDENCE_JSON="docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/${PROFILE}/${STAGE}.json"

# VALIDATION_JSON must contain numeric application_runtime_seconds,
# spill_bytes, task_count, output_file_count, and output_total_bytes.
python -m ops.gcp.collect_spark_benchmark_evidence \
  --project ewallet-data-ai-platform-hg26 \
  --region asia-southeast1 \
  --batch-id "$BATCH_ID" \
  --benchmark-id "$SPARK_BENCHMARK_ID" \
  --spark-profile "$PROFILE" \
  --stage "$STAGE" \
  --dataset-version canonical-100gb-v1 \
  --git-sha "$(git rev-parse HEAD)" \
  --upstream-run-id "$UPSTREAM_RUN" \
  --output-run-id "$BATCH_ID" \
  --validation-json "$VALIDATION_JSON" \
  --output "$EVIDENCE_JSON"

# After all four stage pairs pass semantic equivalence and have complete
# runtime evidence, render the comparison. Winner adoption remains manual.
python -m ops.gcp.compare_spark_benchmark \
  --evidence-root "docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}"
```

For DP3, pass both selected Silver and fixed Gold as repeated
`--upstream-run-id` values to the summary and collector commands. Quota handoff
wait is reported separately and never counted as application runtime.

The pair planner is deliberately small: it validates and records comparison
identity, common inputs, isolated output roots and resolved properties. Stage
submitters still own command construction; the evidence tools still own output
equivalence and winner review. This avoids a second monolithic pipeline runner.
