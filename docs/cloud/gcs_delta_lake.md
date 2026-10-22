# GCS source and Delta Lake run layout

The active cloud pilot keeps source Parquet and Delta Lake outputs in GCS. It
uses immutable dataset versions and run-specific output roots so a retry never
overwrites evidence from an earlier run.

## Verified R4 pilot

The completed source version is `r4-pilot-1gb-v1`:

```text
gs://ewallet-data-ai-platform-hg26-data/source/r4-pilot-1gb-v1/
├── _control/dataset_manifest.json
└── shards/<shard-id>/<nine physical Parquet tables>
```

The manifest contains 37 deterministic shards, 3,700,000 logical
transactions, 3,774,000 physical transaction rows, and 1,000,154,486 Parquet
bytes. It is the measured 1 GB integration pilot, not the canonical 100 GB
dataset.

The successful Dataproc batch wrote eight logical Bronze Delta tables to:

```text
gs://ewallet-data-ai-platform-hg26-data/lakehouse/runs/
  r4-bronze-20261022-095608/bronze/<table>
```

The job merged transaction schema V1/V2 with a nullable `channel`, preserved
74,000 physical duplicate rows, and retained one fraud-label row per logical
transaction. It read back every Delta table and compared its row count with
the immutable source manifest. All eight comparisons passed.

The successful batch completed in 138.45 seconds with status `PASS`. This R4
checkpoint stops at Bronze: the 10 GB pilot, cloud Silver, Gold, and historical
feature jobs have not run, and the canonical 100 GB dataset has not been
generated. No Spark optimization result is claimed from this correctness
pilot.

Measured evidence is stored in
[`r4_source_bronze_pilot.json`](../evidence/cloud/r4_source_bronze_pilot.json).
The detailed job evidence is also retained beside the run on GCS.

## Resource scheduling lesson

Dataproc Serverless requires at least 12 vCPUs: four for the driver and four
for each of two executors. This project currently has a 12-vCPU regional
quota. A running `e2-standard-4` GKE node therefore leaves only eight vCPUs
and prevents a Dataproc batch from starting.

For the pilot, the GKE node pool was temporarily scaled to zero, the immutable
Dataproc batch ran, and the pool was restored afterwards. This is an explicit
cost and quota scheduling rule for the dev environment. Production-sized runs
must either request more quota or schedule GKE and Dataproc compute windows so
they do not overlap.

## Operational rules

- Source manifests marked `COMPLETE` are immutable; publish a new dataset
  version when generator semantics change.
- Every Spark execution writes under a new run ID.
- Failed run logs and evidence stay available for diagnosis.
- A downstream DAG may consume a run only after reading its PASS evidence.
- Never commit source Parquet, Delta files, Spark event logs, or downloaded
  GCS objects to Git.
