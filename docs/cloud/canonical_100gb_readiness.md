# Canonical 100 GB cloud data readiness

The bounded cloud path is ready for a user-operated canonical run. This is a
readiness record, not a claim that the canonical dataset or its Spark outputs
already exist.

## Measured source plan

`configs/generation/cloud-canonical-100gb.yaml` was calibrated with one
deterministic 100,000-transaction shard. The shard produced 27,059,611 bytes
in 20.585 seconds with 300,478,464 bytes peak RSS. The resulting plan is 3,696
shards, 369,600,000 logical transactions, and an estimated 100,012,322,256
Parquet bytes. Local disk safety passed with more than 141 GiB remaining after
the staging allowance.

The actual completed GCS manifest is authoritative. Do not describe the
dataset as 100 GB until that manifest reports a size inside the profile's
95-105 GB accepted range.

## USER_RUN: generate and publish the immutable source

Run from the repository root in the `fintech` Conda environment:

```bash
export GCP_PROJECT_ID=ewallet-data-ai-platform-hg26
export GCP_REGION=asia-southeast1
export EWALLET_DATA_BUCKET=ewallet-data-ai-platform-hg26-data
export DATAHUB_GMS_URL=http://127.0.0.1:8080
export DATASET_VERSION=canonical-100gb-v1
export CALIBRATION_REPORT=tmp/source-calibration/cloud-canonical-100gb.json

python -m ops.gcp.calibrate_source_scale \
  --profile configs/generation/cloud-canonical-100gb.yaml \
  --config data_platform/generation/config/settings.yaml \
  --report "$CALIBRATION_REPORT"

export SHARD_COUNT="$(python -c 'import json,os; print(json.load(open(os.environ["CALIBRATION_REPORT"]))["derived_plan"]["shard_count"])')"
test "$SHARD_COUNT" = "3696"

python -m ops.gcp.generate_source_dataset \
  --environment gcp \
  --config data_platform/generation/config/settings.yaml \
  --dataset-version "$DATASET_VERSION" \
  --global-seed 7001 \
  --shard-count "$SHARD_COUNT" \
  --users-per-shard 12500 \
  --merchants-per-shard 300 \
  --transactions-per-shard 100000

python -m ops.gcp.generate_source_dataset \
  --environment gcp \
  --config data_platform/generation/config/settings.yaml \
  --dataset-version "$DATASET_VERSION" \
  --global-seed 7001 \
  --shard-count "$SHARD_COUNT" \
  --users-per-shard 12500 \
  --merchants-per-shard 300 \
  --transactions-per-shard 100000 \
  --execute
```

The execute command is resumable and verifies every uploaded object. Keep the
same plan and dataset version when resuming.

## USER_RUN: orchestrate Bronze, Silver, Gold, and features

Start the Airflow scheduler/API service, unpause the three DAGs, and assign one
immutable timestamp to all lineage IDs:

```bash
export CANONICAL_STAMP="$(date -u +%Y%m%d-%H%M%S)"
export BRONZE_RUN="canonical-bronze-${CANONICAL_STAMP}"
export SILVER_RUN="canonical-silver-${CANONICAL_STAMP}"
export GOLD_RUN="canonical-gold-${CANONICAL_STAMP}"
export FEATURE_RUN="canonical-features-${CANONICAL_STAMP}"

airflow dags unpause dp1_source_to_bronze
airflow dags trigger dp1_source_to_bronze \
  --run-id "canonical-dp1-${CANONICAL_STAMP}" \
  --conf "{\"dataset_version\":\"${DATASET_VERSION}\",\"bronze_run_id\":\"${BRONZE_RUN}\",\"dataproc_ttl\":\"8h\",\"data_bucket\":\"${EWALLET_DATA_BUCKET}\"}"
```

Wait for DP1 to pass, then run DP2:

```bash
airflow dags unpause dp2_bronze_to_curated
airflow dags trigger dp2_bronze_to_curated \
  --run-id "canonical-dp2-${CANONICAL_STAMP}" \
  --conf "{\"dataset_version\":\"${DATASET_VERSION}\",\"bronze_run_id\":\"${BRONZE_RUN}\",\"silver_run_id\":\"${SILVER_RUN}\",\"gold_run_id\":\"${GOLD_RUN}\",\"dataproc_ttl\":\"8h\",\"data_bucket\":\"${EWALLET_DATA_BUCKET}\"}"
```

Wait for DP2 to pass, then run DP3:

```bash
airflow dags unpause dp3_historical_features
airflow dags trigger dp3_historical_features \
  --run-id "canonical-dp3-${CANONICAL_STAMP}" \
  --conf "{\"dataset_version\":\"${DATASET_VERSION}\",\"silver_run_id\":\"${SILVER_RUN}\",\"gold_run_id\":\"${GOLD_RUN}\",\"feature_run_id\":\"${FEATURE_RUN}\",\"dataproc_ttl\":\"8h\",\"data_bucket\":\"${EWALLET_DATA_BUCKET}\"}"
```

Do not reuse any run ID after a partial or completed run. Inspect Dataproc and
Airflow logs before choosing a new ID after a failure.

Machine-readable evidence: [100 GB readiness evidence](../evidence/cloud/s1b_100gb_readiness.json).
