# Cloud Delta storage optimization runbook

This runbook is scoped to the physical layout experiment for the frozen
`GCS + Delta Lake + Managed Service for Apache Spark` architecture. It is not a
replacement for the Spark runtime A/B. Complete the Spark baseline/optimized
experiment and equivalence gate before integrating or running this work.

## Recovered local work

| Old implementation | What it did | Local assumption | Still valid? | Cloud change required | Decision |
|---|---|---|---|---|---|
| `inspect_delta_layout.py` | Read active Delta `AddFile` metadata and summarized file sizes | fixed MinIO credentials and three local tables | metadata-first method is valid | accept run-scoped `gs://` tables and emit JSON | ADAPT |
| `storage_benchmark.py` | Timed a seven-day Gold aggregation | local Trino catalog | query class is valid | use identical Spark SQL against isolated Delta clones | ADAPT |
| Trino `ALTER TABLE ... EXECUTE optimize` | compacted 2,188 files to 151 | Trino Delta connector and MinIO | compaction question is valid | use pinned Delta `DeltaTable.optimize()` | REPLACE executor |
| `event_date` daily partition | pruned 151 partitions to seven | local 4M-row scale | likely valid at canonical scale | verify bytes/files per partition first | KEEP when evidence passes |
| MinIO client/config | listed and mutated S3-compatible objects | localhost/S3A credentials | no | use GCS ADC and Delta metadata | REMOVE from cloud path |
| local screenshots/manual notes | recorded file/query comparison | manual and non-machine-readable | historical evidence only | emit JSON and CSV under benchmark ID | ADAPT |
| PostgreSQL schema | UUID primary key on source `transactions` | local OLTP | unrelated to Delta layout | none; no BRIN or extra B-tree benchmark existed | REMOVE from this scope |

The historical local result in `docs/05_storage_optimization.md` remains useful
context: 151 daily partitions, 2,188 files before compaction, 151 afterward,
and 2.03x median speedup on one seven-day Trino query. It is not cloud evidence
and must not be presented as the canonical result.

## Pinned capability gate

The repository submits Managed Spark runtime `2.2` and explicitly pins
`delta-spark_2.13:3.2.0` plus `delta-storage:3.2.0`. Runtime 2.2 currently maps
to Spark 3.5.3; Delta 3.2.x is compatible with Spark 3.5.x.

The pinned stack supports:

- `DeltaTable.optimize().executeCompaction()`;
- `executeZOrderBy(...)` and automatic min/max data-skipping statistics;
- `spark.databricks.delta.optimize.repartition.enabled`;
- optimized writes and auto compaction introduced in Delta 3.1.

The experiment intentionally does not enable optimized writes/auto compaction
in DP1/DP2/DP3 because that would alter the frozen Spark A/B. It evaluates a
post-write maintenance operation instead. Liquid clustering is out of scope.

Primary references:

- [Delta optimization documentation](https://docs.delta.io/optimizations-oss/)
- [Delta/Spark compatibility matrix](https://docs.delta.io/releases/)
- [Managed Spark runtime 2.2 components](https://docs.cloud.google.com/managed-spark/docs/concepts/versions/spark-runtime-2.2)

## Metadata-first layout audit

`scripts/inspect_storage_layout.sh` opens the current Delta snapshot through
Delta Lake metadata and reads active `AddFile` actions. It does not count rows
by scanning Parquet. Row counts are reported only when all active actions have
`numRecords` statistics; otherwise the value is explicitly unavailable.

For every table it records Delta version, bytes, active files, min/median/p95/
max/average file size, very-small-file count, partition columns, partition
count, files/bytes/rows per partition, properties and available statistics
columns. The inspector selects a table only when all of these hold:

1. at least 256 MiB of active data;
2. at least 32 active files;
3. at least half of files are below 16 MiB, or median size is below 32 MiB;
4. the table has a representative downstream read workload.

This keeps dimensions and already healthy tables out of the rewrite.
Partition-value metadata supplies date bounds because Delta does not duplicate
partition columns into AddFile min/max statistics. Per-partition row counts are
reported only when every file in that partition has `numRecords`.

## Partition and Z-order decisions

Daily `event_date`/`snapshot_date` partitions are kept when they match actual
time predicates, median partition size is at least 64 MiB, no more than 10% of
partitions are smaller than 64 MiB, and maximum/median byte skew is at most 10x.
Small files inside a healthy date partition are handled by compaction.
High-cardinality IDs are never introduced
as partition columns. Historical feature tables stay unpartitioned in this
first experiment because adding a derived date partition would change their
schema/write contract.

Z-order is considered only for a non-partition equality predicate exercised by
the workload. At least 90% of files must expose min/max metadata and the bounds
must contain at least eight distinct file-level values; otherwise the plan uses
compaction only. Current candidates are `merchant_id` for transaction facts and
the corresponding entity ID for historical feature retrieval. The plan limits
Z-order to two columns and records its metadata evidence and full rewrite cost.
This is evidence of data-skipping potential, not an exact distinct-cardinality
claim. No result is attributed to an individual optimization without a separate
ablation experiment.

## Isolated storage A/B

The plan writes only below:

```text
gs://<data-bucket>/lakehouse/benchmarks/storage/<benchmark-id>/
```

For a compaction-only decision, the Managed Spark job creates two shallow
clones at `baseline/...` and `optimized/...`. For a Z-order candidate it creates
three independent clones at `baseline/...`, `compacted/...` and
`optimized/...`. Every clone is pinned with `VERSION AS OF` the inspected Delta
version. The compacted arm runs plain bin-packing, while the optimized arm runs
Z-order from the same original snapshot. This ablation separates the incremental
effect of Z-order from ordinary compaction instead of crediting both changes to
Z-order. A shallow clone preserves the logical snapshot without rewriting the
source. Rewritten files belong only to the benchmark prefix. No `VACUUM` is run.
Source, Spark A/B and canonical run prefixes are rejected by path guards.

The workload includes several deterministic real read classes when applicable:

- seven-day count/sum/average on transaction facts;
- seven-day equality lookup for one deterministic merchant;
- seven-day failed-payment aggregation using canonical lowercase `failed`;
- bounded entity history for each selected historical feature table.

All arms run in the same Spark application and runtime configuration. Each
query gets one unmeasured warm-up on every layout and five measured runs with a
rotating execution order; the median is compared. The report includes
baseline-to-optimized and, for Z-order candidates, baseline-to-compaction plus
compaction-to-Z-order results. Z-order is recommended only when both the overall
and incremental comparisons improve. Compaction is retained only when its own
baseline comparison improves. Any query regression above 5% makes that arm a
regression. Fewer files alone is not sufficient to claim an improvement.

## Equivalence and evidence

Every selected table must pass schema hash, exact row count, unique and non-null
grain, explicit required columns from the repository data contracts, positive
Silver amount/non-negative Gold amount, non-negative balances,
canonical status domain, no-label-leakage and an order-independent aggregate
row fingerprint. Query aggregates use decimal arithmetic, and every
representative query result must match exactly across baseline, compacted and
Z-order arms. A mismatch fails the batch before PASS evidence is written.

Evidence is written under the benchmark prefix:

```text
evidence/layout_after.json
evidence/layout_compacted.json      # only for Z-order ablation
evidence/queries_before.json
evidence/queries_after.json
evidence/queries_compacted.json     # only for Z-order ablation
evidence/equivalence.json
evidence/comparison.json
evidence/comparison.csv
```

`layout_before.json` and immutable `plan.json` are produced locally by the
inspector and should be retained under
`docs/evidence/storage_optimization/<benchmark-id>/`. Query runtime and Delta
rewrite metrics are collected. Stable task-level scan/shuffle/spill/straggler
metrics are not claimed by the tool; Managed Spark event logs remain the source
when those metrics are available. Missing scan bytes are emitted as unavailable,
never as zero.

## Future user run

Run this only after the active Spark A/B and its equivalence comparison pass,
and after this commit is cherry-picked.
`SOURCE_SPARK_GIT_SHA` remains the frozen SHA used by every Spark A/B stage;
`TOOL_GIT_SHA` identifies the later commit that supplies this storage tool.
The inspector verifies the former against the completed Spark evidence and
records both values in the immutable plan. Source run IDs must match the
canonical `ab-<timestamp>-optimized-<stage>-r<timestamp>` grammar, and each
table URI must exactly match its expected bucket, run, layer and table. Before
packaging, the submitter also requires `HEAD == TOOL_GIT_SHA` and a clean
tracked worktree.

```bash
cd ~/Develop/ewallet-data-ai-platform
conda activate fintech

export STORAGE_BENCHMARK_ID="storage-ab-$(date -u +%Y%m%d%H%M%S)"
export DATASET_VERSION="canonical-100gb-v1"
export SOURCE_SPARK_GIT_SHA="7e6f050028706a2a930a733771d97e04f1df2864"
export TOOL_GIT_SHA="$(git rev-parse HEAD)"
export DATA_BUCKET="ewallet-data-ai-platform-hg26-data"

# Set these from the completed OPTIMIZED Spark A/B evidence.
export SPARK_BENCHMARK_ID="ab-20261025112811"
export OPT_SILVER_RUN="<ab-id>-optimized-silver-<run-suffix>"
export OPT_GOLD_RUN="<ab-id>-optimized-gold-<run-suffix>"
export OPT_FEAT_RUN="<ab-id>-optimized-feat-<run-suffix>"
export EVIDENCE_DIR="docs/evidence/storage_optimization/${STORAGE_BENCHMARK_ID}"
export SPARK_EVIDENCE_ROOT="docs/evidence/spark_benchmark/${SPARK_BENCHMARK_ID}"

./scripts/inspect_storage_layout.sh \
  --benchmark-id "$STORAGE_BENCHMARK_ID" \
  --dataset-version "$DATASET_VERSION" \
  --source-spark-git-sha "$SOURCE_SPARK_GIT_SHA" \
  --tool-git-sha "$TOOL_GIT_SHA" \
  --data-bucket "$DATA_BUCKET" \
  --source-run-id "$OPT_SILVER_RUN" \
  --source-run-id "$OPT_GOLD_RUN" \
  --source-run-id "$OPT_FEAT_RUN" \
  --spark-evidence-root "$SPARK_EVIDENCE_ROOT" \
  --table "dp2-silver.transactions=gs://${DATA_BUCKET}/lakehouse/runs/${OPT_SILVER_RUN}/silver/transactions" \
  --table "dp2-gold.fact_transactions=gs://${DATA_BUCKET}/lakehouse/runs/${OPT_GOLD_RUN}/gold/fact_transactions" \
  --table "dp2-gold.obt_transaction_enriched=gs://${DATA_BUCKET}/lakehouse/runs/${OPT_GOLD_RUN}/gold/obt_transaction_enriched" \
  --table "dp3-features.feat_user_behavior=gs://${DATA_BUCKET}/lakehouse/runs/${OPT_FEAT_RUN}/gold/feat_user_behavior" \
  --table "dp3-features.feat_account_behavior=gs://${DATA_BUCKET}/lakehouse/runs/${OPT_FEAT_RUN}/gold/feat_account_behavior" \
  --table "dp3-features.feat_device_behavior=gs://${DATA_BUCKET}/lakehouse/runs/${OPT_FEAT_RUN}/gold/feat_device_behavior" \
  --table "dp3-features.feat_merchant_behavior=gs://${DATA_BUCKET}/lakehouse/runs/${OPT_FEAT_RUN}/gold/feat_merchant_behavior" \
  --output-dir "$EVIDENCE_DIR"

terraform -chdir=infra/terraform/gcp/environments/dev output -json \
  > tmp/storage-terraform-output.json

# Default: print lineage, current layout, input/output paths, partition/Z-order
# decisions, estimated rewrite scope and the exact command. It does not mutate.
./scripts/run_storage_benchmark.sh \
  --plan "$EVIDENCE_DIR/plan.json" \
  --terraform-output-json tmp/storage-terraform-output.json

# Future explicit mutation, only after reviewing plan.json.
./scripts/run_storage_benchmark.sh \
  --plan "$EVIDENCE_DIR/plan.json" \
  --terraform-output-json tmp/storage-terraform-output.json \
  --execute
```

If a submitted batch fails after creating any benchmark path, keep it as
evidence and retry with a new benchmark ID. Never reuse, replace or clean a
partial immutable prefix during this experiment.
