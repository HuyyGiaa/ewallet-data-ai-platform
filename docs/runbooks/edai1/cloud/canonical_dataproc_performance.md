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
export GIT_SHA="$(git rev-parse HEAD)"
./scripts/start_airflow_local.sh

# Prepare one fail-closed local pair manifest before either stage submission.
# The command refuses different inputs, reused outputs, bad profile/stage IDs,
# or an existing manifest path. Repeat with the corresponding stage and roots.
ATTEMPT_ID="r$(date -u +%y%m%d%H%M%S)"
BASELINE_BRONZE="${SPARK_BENCHMARK_ID}-baseline-bronze-${ATTEMPT_ID}"
CANDIDATE_BRONZE="${SPARK_BENCHMARK_ID}-optimized-bronze-${ATTEMPT_ID}"
DP1_PAIR="docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/pairs/dp1-bronze/${BASELINE_BRONZE}--${CANDIDATE_BRONZE}.json"
python -m ops.gcp.prepare_spark_stage_pair \
  --stage dp1-bronze --dataset-version canonical-100gb-v1 \
  --git-sha "$GIT_SHA" --upstream-run-id canonical-100gb-v1 \
  --baseline-output-run-id "$BASELINE_BRONZE" \
  --baseline-output-root "gs://${EWALLET_DATA_BUCKET}/lakehouse/runs/${BASELINE_BRONZE}/bronze" \
  --candidate-output-run-id "$CANDIDATE_BRONZE" \
  --candidate-output-root "gs://${EWALLET_DATA_BUCKET}/lakehouse/runs/${CANDIDATE_BRONZE}/bronze" \
  --output "$DP1_PAIR"

# Dry-run only first. Both DP1 runs consume this exact immutable pair.
./scripts/run_dp1_canonical.sh --pair-manifest "$DP1_PAIR" --profile baseline
./scripts/run_dp1_canonical.sh --pair-manifest "$DP1_PAIR" --profile optimized

BASELINE_SILVER="${SPARK_BENCHMARK_ID}-baseline-silver-${ATTEMPT_ID}"
CANDIDATE_SILVER="${SPARK_BENCHMARK_ID}-optimized-silver-${ATTEMPT_ID}"
SELECTED_BRONZE="REPLACE_WITH_SELECTED_BRONZE_RUN"
python -m ops.gcp.prepare_spark_stage_pair \
  --stage dp2-silver --dataset-version canonical-100gb-v1 \
  --git-sha "$GIT_SHA" \
  --upstream-run-id "$SELECTED_BRONZE" \
  --baseline-output-run-id "$BASELINE_SILVER" \
  --baseline-output-root "gs://${EWALLET_DATA_BUCKET}/lakehouse/runs/${BASELINE_SILVER}/silver" \
  --candidate-output-run-id "$CANDIDATE_SILVER" \
  --candidate-output-root "gs://${EWALLET_DATA_BUCKET}/lakehouse/runs/${CANDIDATE_SILVER}/silver" \
  --output "docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/pairs/dp2-silver/${BASELINE_SILVER}--${CANDIDATE_SILVER}.json"
SILVER_PAIR="docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/pairs/dp2-silver/${BASELINE_SILVER}--${CANDIDATE_SILVER}.json"

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
    --git-sha "$GIT_SHA" --pair-manifest "$SILVER_PAIR" --ttl 8h
done

# After Silver equality and explicit winner selection, Gold uses one Silver.
SELECTED_SILVER="REPLACE_WITH_SELECTED_SILVER_RUN"
BASELINE_GOLD="${SPARK_BENCHMARK_ID}-baseline-gold-${ATTEMPT_ID}"
CANDIDATE_GOLD="${SPARK_BENCHMARK_ID}-optimized-gold-${ATTEMPT_ID}"
python -m ops.gcp.prepare_spark_stage_pair \
  --stage dp2-gold --dataset-version canonical-100gb-v1 \
  --git-sha "$GIT_SHA" --upstream-run-id "$SELECTED_SILVER" \
  --baseline-output-run-id "$BASELINE_GOLD" \
  --baseline-output-root "gs://${EWALLET_DATA_BUCKET}/lakehouse/runs/${BASELINE_GOLD}/gold" \
  --candidate-output-run-id "$CANDIDATE_GOLD" \
  --candidate-output-root "gs://${EWALLET_DATA_BUCKET}/lakehouse/runs/${CANDIDATE_GOLD}/gold" \
  --output "docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/pairs/dp2-gold/${BASELINE_GOLD}--${CANDIDATE_GOLD}.json"
GOLD_PAIR="docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/pairs/dp2-gold/${BASELINE_GOLD}--${CANDIDATE_GOLD}.json"
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
    --git-sha "$GIT_SHA" --pair-manifest "$GOLD_PAIR" --ttl 8h
done

# DP3 uses the same selected Silver and one fixed selected Gold for both runs.
SELECTED_GOLD="REPLACE_WITH_SELECTED_GOLD_RUN"
BASELINE_FEATURES="${SPARK_BENCHMARK_ID}-baseline-feat-${ATTEMPT_ID}"
CANDIDATE_FEATURES="${SPARK_BENCHMARK_ID}-optimized-feat-${ATTEMPT_ID}"
python -m ops.gcp.prepare_spark_stage_pair \
  --stage dp3-features --dataset-version canonical-100gb-v1 \
  --git-sha "$GIT_SHA" --upstream-run-id "$SELECTED_SILVER" \
  --upstream-run-id "$SELECTED_GOLD" \
  --baseline-output-run-id "$BASELINE_FEATURES" \
  --baseline-output-root "gs://${EWALLET_DATA_BUCKET}/lakehouse/runs/${BASELINE_FEATURES}/gold" \
  --candidate-output-run-id "$CANDIDATE_FEATURES" \
  --candidate-output-root "gs://${EWALLET_DATA_BUCKET}/lakehouse/runs/${CANDIDATE_FEATURES}/gold" \
  --output "docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/pairs/dp3-features/${BASELINE_FEATURES}--${CANDIDATE_FEATURES}.json"
FEATURE_PAIR="docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}/pairs/dp3-features/${BASELINE_FEATURES}--${CANDIDATE_FEATURES}.json"
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
    --git-sha "$GIT_SHA" --pair-manifest "$FEATURE_PAIR" --ttl 8h
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

The summary and ingestion batches are distinct executions. The summary records
both identities, and the submitter rejects a validation batch ID that reuses an
ingestion/output ID. GCS summary creation uses `if_generation_match=0`, so an
existing object or a concurrent writer causes failure rather than overwrite.

### Completed DP1 pair: post-run operator workflow

Run from the clean, reviewed validation implementation commit descended from
`62f17ba6fe306b5cf74621b787249f8b71ccd8e8`, with authenticated `gcloud`,
Terraform dev outputs, Delta jars and the repository Python environment. The
pair keeps the original ingestion Git SHA; `VALIDATION_GIT_SHA` records the
separate code that performs the read-only scan.

```bash
cd ~/Develop/ewallet-data-ai-platform
test -z "$(git status --porcelain)"
export VALIDATION_GIT_SHA="$(git rev-parse HEAD)"
export PROJECT_ID=ewallet-data-ai-platform-hg26 REGION=asia-southeast1
export EWALLET_DATA_BUCKET=ewallet-data-ai-platform-hg26-data
export SPARK_BENCHMARK_ID=ab-20261029174838
export GIT_SHA=62f17ba6fe306b5cf74621b787249f8b71ccd8e8
export EVIDENCE_ROOT="$HOME/ewallet-evidence/spark-method2-final/$SPARK_BENCHMARK_ID"
export PAIR_MANIFEST="$EVIDENCE_ROOT/dp1-bronze-pair.json"
export BASELINE_RUN=ab-20261029174838-baseline-bronze-r261029174838
export OPTIMIZED_RUN=ab-20261029174838-optimized-bronze-r261029174838
export BASELINE_ROOT="gs://$EWALLET_DATA_BUCKET/lakehouse/runs/$BASELINE_RUN/bronze"
export OPTIMIZED_ROOT="gs://$EWALLET_DATA_BUCKET/lakehouse/runs/$OPTIMIZED_RUN/bronze"

# Read-only active-workload and quota snapshots. Any active benchmark batch,
# unavailable quota response or insufficient quota blocks submission.
gcloud dataproc batches list --project="$PROJECT_ID" --region="$REGION" \
  --filter='state=PENDING OR state=RUNNING OR state=CANCELLING' \
  --format='table(name.basename(),state,createTime)'
gcloud compute project-info describe --project="$PROJECT_ID" --format='json(quotas)'
gcloud compute regions describe "$REGION" --project="$PROJECT_ID" --format='json(quotas)'

# Billing reports lag and alerts do not enforce a cap. Enter the verified gross
# spend from the billing report/export. Unavailable spend blocks execution.
export CUMULATIVE_GROSS_VND=REPLACE_WITH_VERIFIED_GROSS_SPEND
test "$CUMULATIVE_GROSS_VND" -le 4000000
```

Maximum allocation is 29.7 DCU; TTL 8h caps one job at 237.6 DCU-hours. At the
published Standard reference of USD 0.06/DCU-hour, two TTL-bound jobs are USD
28.512 before regional pricing, shuffle, GCS and tax. Reserve 1,000,000 VND for
both sequential jobs and confirm Singapore SKUs on the
[official pricing page](https://cloud.google.com/products/managed-service-for-apache-spark/pricing).
Do not run the validation jobs concurrently.

Render both commands first. Dry-run is non-mutating; only a later explicit
`--execute` submits a billable job.

```bash
export VALIDATION_ATTEMPT="r$(date -u +%y%m%d%H%M%S)"
export BASELINE_VALIDATION="abval-b-$VALIDATION_ATTEMPT"
export OPTIMIZED_VALIDATION="abval-o-$VALIDATION_ATTEMPT"
export BASELINE_SUMMARY_URI="gs://$EWALLET_DATA_BUCKET/evidence/spark_benchmark/$SPARK_BENCHMARK_ID/baseline/dp1-bronze/$BASELINE_RUN-summary.json"
export OPTIMIZED_SUMMARY_URI="gs://$EWALLET_DATA_BUCKET/evidence/spark_benchmark/$SPARK_BENCHMARK_ID/optimized/dp1-bronze/$OPTIMIZED_RUN-summary.json"

python -m ops.gcp.submit_spark_output_summary \
  --batch-id "$BASELINE_VALIDATION" --stage dp1-bronze \
  --benchmark-id "$SPARK_BENCHMARK_ID" --spark-profile baseline \
  --output-run-id "$BASELINE_RUN" --source-run-id canonical-100gb-v1 \
  --pair-manifest "$PAIR_MANIFEST" --dataset-version canonical-100gb-v1 \
  --git-sha "$GIT_SHA" --validation-git-sha "$VALIDATION_GIT_SHA" \
  --summary-uri "$BASELINE_SUMMARY_URI"
python -m ops.gcp.submit_spark_output_summary \
  --batch-id "$OPTIMIZED_VALIDATION" --stage dp1-bronze \
  --benchmark-id "$SPARK_BENCHMARK_ID" --spark-profile optimized \
  --output-run-id "$OPTIMIZED_RUN" --source-run-id canonical-100gb-v1 \
  --pair-manifest "$PAIR_MANIFEST" --dataset-version canonical-100gb-v1 \
  --git-sha "$GIT_SHA" --validation-git-sha "$VALIDATION_GIT_SHA" \
  --summary-uri "$OPTIMIZED_SUMMARY_URI"
```

After review, append `--execute` to the first command only. Wait for terminal
`SUCCEEDED`, download and verify its summary, then repeat once for the second.
Preserve any failure and never retry with the same batch ID.

```bash
export BASELINE_SUMMARY="$EVIDENCE_ROOT/baseline/$BASELINE_RUN-summary.json"
export OPTIMIZED_SUMMARY="$EVIDENCE_ROOT/optimized/$OPTIMIZED_RUN-summary.json"
mkdir -p "$(dirname "$BASELINE_SUMMARY")" "$(dirname "$OPTIMIZED_SUMMARY")"
gcloud storage cp "$BASELINE_SUMMARY_URI" "$BASELINE_SUMMARY"
gcloud storage cp "$OPTIMIZED_SUMMARY_URI" "$OPTIMIZED_SUMMARY"
jq -e '.schema_version == "spark-method2-semantic-summary-v1" and
  .status == "COMPLETE" and .execution.kind == "read_only_semantic_scan"' \
  "$BASELINE_SUMMARY" "$OPTIMIZED_SUMMARY"
```

Application runtime, spill and task count must come from each original
ingestion event log, not the later validation job. Locate the exact application
ID with the saved driver output or Spark History Server, download its event-log
file(s), and retain the GCS URI inventory. The parser rejects a wrong
application name, pair digest or output run ID. File count and bytes come from
the semantic summary's physical layouts.

```bash
for PROFILE in baseline optimized; do
  if [[ "$PROFILE" == baseline ]]; then
    RUN="$BASELINE_RUN"; SUMMARY="$BASELINE_SUMMARY"; ROOT="$BASELINE_ROOT"
  else
    RUN="$OPTIMIZED_RUN"; SUMMARY="$OPTIMIZED_SUMMARY"; ROOT="$OPTIMIZED_ROOT"
  fi
  BATCH_JSON="$EVIDENCE_ROOT/$PROFILE/dataproc-batch.json"
  EVENT_DIR="$EVIDENCE_ROOT/$PROFILE/event-log"
  mkdir -p "$EVENT_DIR"
  EVENT_LOG_GCS_URI=REPLACE_WITH_VERIFIED_ORIGINAL_EVENT_LOG_URI
  gcloud storage cp --recursive "$EVENT_LOG_GCS_URI" "$EVENT_DIR/"
  EVENT_ARGS=()
  while IFS= read -r FILE; do EVENT_ARGS+=(--event-log "$FILE"); done \
    < <(find "$EVENT_DIR" -type f -print | sort)
  PERFORMANCE_JSON="$EVIDENCE_ROOT/$PROFILE/$RUN-original-performance.json"
  VALIDATION_JSON="$EVIDENCE_ROOT/$PROFILE/$RUN-validation.json"
  EVIDENCE_JSON="$EVIDENCE_ROOT/$PROFILE/$RUN-evidence.json"
  python -m ops.gcp.summarize_spark_event_log \
    "${EVENT_ARGS[@]}" --batch-id "$RUN" --output-run-id "$RUN" \
    --pair-manifest-digest "$(jq -r .pair_manifest_digest "$PAIR_MANIFEST")" \
    --source-uri "$EVENT_LOG_GCS_URI" \
    --output "$PERFORMANCE_JSON"
  python -m ops.gcp.build_spark_validation_evidence \
    --summary-json "$SUMMARY" --original-performance-json "$PERFORMANCE_JSON" \
    --pair-manifest "$PAIR_MANIFEST" --spark-profile "$PROFILE" \
    --output "$VALIDATION_JSON"
  python -m ops.gcp.collect_spark_benchmark_evidence \
    --project "$PROJECT_ID" --region "$REGION" --batch-id "$RUN" \
    --benchmark-id "$SPARK_BENCHMARK_ID" --spark-profile "$PROFILE" \
    --stage dp1-bronze --dataset-version canonical-100gb-v1 \
    --git-sha "$GIT_SHA" --upstream-run-id canonical-100gb-v1 \
    --output-run-id "$RUN" --output-root "$ROOT" \
    --pair-manifest "$PAIR_MANIFEST" --validation-json "$VALIDATION_JSON" \
    --batch-description-json "$BATCH_JSON" --output "$EVIDENCE_JSON"
done
```

Only then create equivalence, a stage comparison and the winner artifact. Each
local output is create-only; any semantic mismatch returns non-zero and blocks
winner selection.

```bash
export PAIR_KEY="$BASELINE_RUN--$OPTIMIZED_RUN"
export BASELINE_EVIDENCE="$EVIDENCE_ROOT/baseline/$BASELINE_RUN-evidence.json"
export OPTIMIZED_EVIDENCE="$EVIDENCE_ROOT/optimized/$OPTIMIZED_RUN-evidence.json"
export EQUIVALENCE_JSON="$EVIDENCE_ROOT/equivalence/dp1-bronze/$PAIR_KEY.json"
export COMPARISON_JSON="$EVIDENCE_ROOT/comparison/dp1-bronze/$PAIR_KEY.json"
export COMPARISON_CSV="$EVIDENCE_ROOT/comparison/dp1-bronze/$PAIR_KEY.csv"
export WINNER_JSON="$EVIDENCE_ROOT/winners/dp1-bronze/$PAIR_KEY.json"
python -m ops.gcp.validate_spark_output_equivalence \
  --pair-manifest "$PAIR_MANIFEST" --baseline-summary "$BASELINE_SUMMARY" \
  --optimized-summary "$OPTIMIZED_SUMMARY" \
  --baseline-evidence "$BASELINE_EVIDENCE" \
  --optimized-evidence "$OPTIMIZED_EVIDENCE" --output "$EQUIVALENCE_JSON"
python -m ops.gcp.compare_spark_benchmark \
  --pair-manifest "$PAIR_MANIFEST" --baseline-evidence "$BASELINE_EVIDENCE" \
  --optimized-evidence "$OPTIMIZED_EVIDENCE" \
  --equivalence-evidence "$EQUIVALENCE_JSON" \
  --output-json "$COMPARISON_JSON" --output-csv "$COMPARISON_CSV"
python -m ops.gcp.select_spark_stage_winner \
  --pair-manifest "$PAIR_MANIFEST" --baseline-evidence "$BASELINE_EVIDENCE" \
  --optimized-evidence "$OPTIMIZED_EVIDENCE" \
  --equivalence-evidence "$EQUIVALENCE_JSON" --output "$WINNER_JSON"
```

The same flow supports DP2 Silver, DP2 Gold and DP3 Features by changing the
pair, stage, roots and expected inventory. DP3 repeats selected Silver and fixed
Gold as two `--source-run-id` / `--upstream-run-id` values. Validation-job
runtime remains separate operational evidence and never replaces original
ingestion performance.

The pair planner is deliberately small: it validates and records comparison
identity, common inputs, isolated output roots and resolved properties. Stage
submitters still own command construction; the evidence tools still own output
equivalence and winner review. This avoids a second monolithic pipeline runner.
