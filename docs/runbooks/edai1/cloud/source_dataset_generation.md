# Cloud source dataset generation

The cloud generator keeps the accepted transaction and fraud logic, but runs
it as deterministic, bounded shards. Each shard is an independent cohort and
contains the same nine physical Parquet tables as the local generator. Source
Parquet is immutable input; Bronze, Silver, and Gold remain Delta tables.

## Why calibration comes first

The repository does not equate a fixed row count with 1 GB, 10 GB, or 100 GB.
Compression, string cardinality, schema evolution, account distribution, and
fraud rows all affect Parquet size. Calibration generates one representative
shard and measures actual bytes, runtime, and peak process RSS. It then derives
the number of equal-size shards required for the requested byte target.

The 1 GB profile is a correctness and cloud-integration pilot. It is not
evidence for the final 100 GB Spark performance claims.

## Calibrate the 1 GB pilot

From the repository root in the `fintech` Conda environment:

```bash
python -m ops.gcp.calibrate_source_scale \
  --profile configs/generation/cloud-pilot-1gb.yaml \
  --config data_platform/generation/config/settings.yaml \
  --report tmp/source-calibration/cloud-pilot-1gb.json
```

Review `target.validated_label`, `derived_plan`, `disk_safety`, peak RSS, and
runtime before publication. The report is a measured planning artifact; the
temporary calibration Parquet is deleted when the command exits.

## Dry-run publication

Use the derived shard count and the unchanged shard geometry from the report:

```bash
python -m ops.gcp.generate_source_dataset \
  --environment gcp \
  --config data_platform/generation/config/settings.yaml \
  --dataset-version <immutable-version> \
  --global-seed 7001 \
  --shard-count <derived-count> \
  --users-per-shard 12500 \
  --merchants-per-shard 300 \
  --transactions-per-shard 100000
```

Dry-run is the default. It generates no local data and writes no GCS object.
Add `--execute` only after reviewing the exact printed plan and GCS prefix.

## Resume and immutability

Publication uploads each table object with a GCS generation precondition and
MD5 transport checksum. A shard manifest is written only after all nine files
read back with matching metadata. The final dataset manifest is written only
after every shard passes. A rerun verifies complete shards and resumes absent
ones. It refuses a conflicting plan, object, shard manifest, or completed
dataset version.

Local shard files under `tmp/source-datasets/` are generated runtime artifacts
and are removed after remote verification. They are ignored by Git. If Gold or
source semantics change, publish a new dataset version; never reuse a stale
completed prefix.

## Operational boundaries

- Keep at least the profile's configured free-disk reserve.
- Do not upload credentials, Terraform state, `.env`, caches, or local output.
- GCS source data is immutable. Failed partial prefixes may be diagnosed and
  resumed only when their remote plan digest matches.
- Generation is an upstream job. Airflow consumes a completed source manifest
  and does not silently generate data inside DP1.

The measured R4 publication and source-to-Bronze result are documented in
[`gcs_delta_lake.md`](gcs_delta_lake.md).
