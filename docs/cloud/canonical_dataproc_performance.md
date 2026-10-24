# Canonical Dataproc performance profile

Canonical DP1 opts into the shared `canonical` Dataproc performance profile.
Bounded and pilot submissions continue to use `default` unless the caller
explicitly supplies `--performance-profile canonical` or the equivalent
Airflow parameter.

The canonical profile adds exactly these submission properties:

```text
spark.dynamicAllocation.profile=performance
spark.dynamicAllocation.initialExecutors=10
spark.dynamicAllocation.maxExecutors=20
```

The submission remains on runtime 2.2 and `dataproc.tier=standard`. Executor
cores and memory are not overridden. AQE, skew handling and
`spark.sql.shuffle.partitions=400` remain owned by
`configs/environments/gcp/platform.yaml` and the existing optimized Spark
session. TTL remains an explicit submission argument and is `8h` for the
canonical run.

With the preserved Dataproc Serverless default shape of four vCPUs per driver
or executor, the workload starts with approximately 44 vCPUs (one driver plus
10 executors) and can scale to approximately 84 vCPUs (one driver plus 20
executors). The executor cap is a ceiling rather than a reservation.

## DP1 dry-run

```bash
export DATASET_VERSION=canonical-100gb-v1
export CANONICAL_STAMP="$(date -u +%Y%m%d-%H%M%S)"
export BRONZE_RUN="canonical-bronze-${CANONICAL_STAMP}"

python -m ops.gcp.submit_source_to_bronze \
  --dataset-version "$DATASET_VERSION" \
  --run-id "$BRONZE_RUN" \
  --ttl 8h \
  --performance-profile canonical
```

The output must contain the three canonical properties above,
`dataproc.tier=standard`, `--version=2.2`, and `--ttl=8h`. It must also say
`DRY RUN — no Dataproc batch was submitted`.

## DP1 USER_RUN through Airflow

```bash
airflow dags unpause dp1_source_to_bronze
airflow dags trigger dp1_source_to_bronze \
  --run-id "canonical-dp1-${CANONICAL_STAMP}" \
  --conf "{\"dataset_version\":\"${DATASET_VERSION}\",\"bronze_run_id\":\"${BRONZE_RUN}\",\"dataproc_ttl\":\"8h\",\"dataproc_performance_profile\":\"canonical\",\"data_bucket\":\"${EWALLET_DATA_BUCKET}\"}"
```

DP2 and DP3 expose the same `dataproc_performance_profile` parameter. Leave it
as `default` until their canonical performance setting is explicitly approved;
they can later opt into `canonical` without changing submitter code.
