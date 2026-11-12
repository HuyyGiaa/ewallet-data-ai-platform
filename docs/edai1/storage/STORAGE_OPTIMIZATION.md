# Lakehouse and Warehouse Storage Optimization

This report covers EDAI-1 rubric rows 20 (Lakehouse optimization, 3 points) and 21 (Data warehouse optimization, 3 points). Both experiments use real data of the 5GB campaign `ab-20261110225456` (`rehearsal-5gb-v1`, optimized profile, source Spark Git SHA `aae22b08…`). The owner runs them and captures ST01–ST09 and WH01–WH06 with [DOCKER_STORAGE_5GB_OWNER_EVIDENCE.md](../../runbooks/edai1/DOCKER_STORAGE_5GB_OWNER_EVIDENCE.md), Stages 2–4.

The earlier results (a local 500k-row Delta fixture and a synthetic 2M-row PostgreSQL table) are historical. They are not part of this report. Their files remain under `docs/evidence/edai1/storage/` for provenance only.

## 1. Requirement → implementation

| Row | Requirement | Implementation | Evidence | Status |
|---:|---|---|---|---|
| 20 | Lakehouse optimization: compaction, Z-order, partitioning, with Before/After | Isolated Delta benchmark on Dataproc ([`inspect_storage_layout.py`](../../../ops/gcp/inspect_storage_layout.py), [`run_storage_benchmark.py`](../../../ops/gcp/run_storage_benchmark.py), [`storage_benchmark_job.py`](../../../ops/gcp/storage_benchmark_job.py)) over Gold `fact_transactions` and `obt_transaction_enriched` | ST01–ST09 | SOURCE_IMPLEMENTED, METADATA_VERIFIED, LOCAL_RUNTIME_VERIFIED (Spark 3.5.3 + Delta 3.2.0 fixture), OWNER_CLOUD_EXECUTION_REQUIRED |
| 21 | Warehouse optimization with Before/After | PostgreSQL 16 index benchmark on real Gold rows ([`data_platform/storage/warehouse/`](../../../data_platform/storage/warehouse/)) | WH01–WH06 | SOURCE_IMPLEMENTED, LOCAL_RUNTIME_VERIFIED, OWNER_SCREENSHOT_PENDING |

## Part A — Lakehouse (row 20)

### A.1 Starting point: the observed 5GB layout (METADATA_VERIFIED)

The inspector read the Delta logs of the optimized Gold run read-only, on 2026-11-12:

| Table | Delta version | Active files | Bytes | File size (min / median / max) | Partitions | Files per partition |
|---|---:|---:|---:|---|---:|---:|
| `fact_transactions` | 0 | 151 | 3,667,633,194 | 23.0 / 23.2 / 23.4 MiB | 151 (`event_date`) | 1 |
| `obt_transaction_enriched` | 0 | 151 | 4,271,165,936 | 26.8 / 27.0 / 27.2 MiB | 151 (`event_date`) | 1 |

Every other 5GB table is laid out the same way:
- The partitioned Silver and Gold tables have one file per daily partition.
- The unpartitioned feature tables have 64 equal files of about 15 MiB.
- Bronze has 24–48 files.

The DP2 writers repartition by `event_date` before writing, so **the campaign produced no small-file problem.** Two consequences follow from the metadata, recorded here before the cloud run:

- **Compaction (ST-B) is expected to be a no-op** on these tables. Delta bin-packing skips a partition that holds a single file. The job measures this and reports `NOT_NEEDED` when OPTIMIZE removes and adds no file. It never reports a compaction gain that did not happen.
- **Z-order (ST-C) has little to work with.** All 151 files share the same minimum `merchant_id` and have only 8 distinct maxima: every file already spans almost the whole key space. After Z-order, each partition is still one ~24 MiB file. That is a single Parquet row group at the default 128 MiB block size, so neither file skipping nor row-group skipping can improve. The inspector's metadata rule records exactly this prediction (`COMPACTION_ONLY`). The Z-order arm is still executed and measured, so the conclusion rests on a run, not on the prediction.
- **Partitioning (ST-D).** The partitions are small (median 23 MiB, all below the 64 MiB guideline), so the metadata rule flags `REVIEW_AFTER_A_B`. The experiment measures what pruning buys on this layout. The partition columns are not changed.

### A.2 Data-derived workload

All predicates come from the data. Only the `merchant_id` and `status` columns of the seven most recent partitions were read, and the chosen values are recorded in `plan.json`:

| Parameter | Value (verification plan, 2026-11-12) |
|---|---|
| Window | `event_date` 2026-05-25 … 2026-05-31: 860,389 rows, 41,218 merchants |
| Merchant | the median-volume merchant in the window (2 rows), smallest id on ties |
| Status domain in the window | `success` 814,001 · `failed` 29,763 · `pending` 16,625 |

| Query | What it exercises |
|---|---|
| `window_aggregation` | seven-day count, sum and average (partition pruning) |
| `window_merchant_lookup` | seven days, one merchant (Z-order candidate) |
| `window_failed_by_channel` | seven days, `status = 'failed'`, grouped by channel |
| Partition pruning sides (ST-D) | the same `window_aggregation` with a partition filter (`pruned`), a `timestamp` range (`stats_only`: no partition filter, but Delta file statistics apply), and `date_format(timestamp)` (`full_scan`: neither applies) |

### A.3 Isolation and safety

- **Isolated clones.** Each table gets three clones below `gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data/lakehouse/benchmarks/storage/<benchmark-id>/{baseline,compacted,optimized}/`:
  - `baseline` is the source snapshot;
  - `compacted` is that snapshot after bin-packing compaction;
  - `optimized` is that snapshot after Z-order on `merchant_id`.

  Each clone is a `SHALLOW CLONE … VERSION AS OF <pinned version>`. The job refuses to run if the live source version differs from the plan, if a target exists, or if a path leaves the benchmark root.
- **Why a shallow clone is safe here.** A shallow clone references the source data files. OPTIMIZE on a clone writes new files under the clone's own directory and removes source files only logically, in the clone's log.

  The local integration test checked this on Spark 3.5.3 with Delta 3.2.0, the versions pinned for Dataproc 2.2. After cloning, compaction, Z-order and even a `VACUUM` with zero retention on a rewritten clone, every source data file and the source `_delta_log` were byte-identical. The cloud job never runs `VACUUM` (a unit test asserts it). Evidence objects are uploaded create-only (`if_generation_match=0`).
- **Bound to the campaign.** The plan is bound to the verified campaign:
  - the committed evidence must pass `ops.edai1_verifier`;
  - table URIs come from the verified `campaign_manifest.json`;
  - the plan records the source Spark Git SHA, the tool Git SHA, the campaign manifest SHA-256 and each Delta version.
- **Submission gate.** The submitter refuses to package unless `HEAD` equals the plan's tool Git SHA and tracked files are clean. It resolves the dataset's own bucket. `--execute` is the only path to a paid batch.

### A.4 Measurement

- **Repetitions.** Each query gets one warm-up per arm, then five measured runs in rotating arm order. All samples, the physical plan and Spark's scan metrics (files, bytes and partitions read) are stored per run.
- **Equivalence.** Results must be identical across arms, and every arm must match the baseline on schema hash, row count, grain uniqueness, business invariants and an order-independent content fingerprint. Any mismatch fails the batch before evidence is written.
- **Verdicts.** These follow a descriptive noise-band rule, not a significance test:
  - `NO_CLEAR_IMPROVEMENT` if the medians differ by less than 5 %;
  - `IMPROVED` / `REGRESSED` if they differ by at least 5 % and the sample ranges do not overlap;
  - `INCONCLUSIVE` otherwise.
- **Comparisons and recommendation.** Three comparisons are kept apart: baseline → compaction, compaction → Z-order, and baseline → optimized. Z-order is credited only through its incremental arm.
- **Cost estimate.** One Standard-tier batch. The Z-order arm rewrites about 7.9 GB, and the fingerprints and full-scan sides read each table several times. For scale, the campaign's comparison batch, which read all 34 tables of both profiles, used 7.2 DCU-hours, and each Gold batch used 2.5–2.9 DCU-hours.

### A.5 Results

**OFFICIAL RESULTS: OWNER_CLOUD_EXECUTION_REQUIRED** (Stage 4 of the runbook). They are filled from `evidence/comparison.json` and `comparison.csv` of the benchmark root.

| Table | Arm | Files rewritten (removed / added) | Rewrite s | Query medians | Verdict |
|---|---|---|---:|---|---|
| fact_transactions | baseline → compaction | PENDING | | | |
| fact_transactions | compaction → Z-order | PENDING | | | |
| fact_transactions | baseline → optimized | PENDING | | | |
| obt_transaction_enriched | (same three arms) | PENDING | | | |

| Table | Pruning side | Files read | Bytes read | Median s |
|---|---|---:|---:|---:|
| fact_transactions | pruned / stats_only / full_scan | PENDING | | |

**Local verification (not 5GB evidence).** The integration test ran on a 20-day, one-file-per-day fixture shaped like `fact_transactions`. It confirmed the mechanics:
- compaction was reported as a no-op;
- Z-order rewrote 20 of 20 files;
- results matched across arms;
- pruned / stats-only / full-scan read 7 / 7 / 20 files.

The stats-only side shows that, on a layout with one day per file, Delta's per-file `timestamp` statistics skip as many files as partition pruning does. Only the full-scan control reads everything.

> **ST01** table identity and campaign snapshot · **ST02** layout before · **ST03** compaction job · **ST04** layout after compaction · **ST05** baseline vs compaction queries · **ST06** Z-order job · **ST07** compaction vs Z-order queries · **ST08** equivalence and rewrite cost · **ST09** partition pruning — all OWNER_SCREENSHOT_PENDING. The runbook names the exact screen and SQL for each.

## Part B — Warehouse (row 21)

### B.1 Data

The PostgreSQL rows come from the 5GB Gold `fact_transactions` table, never from a generator:

- **Extraction.** [`warehouse/source.py`](../../../data_platform/storage/warehouse/source.py) resolves the table through the same verified campaign binding, pins its Delta version and takes the most recent N whole daily partitions. The default N = 31 suits this laptop's disk. The extracted row count must equal the Delta `numRecords` of exactly those files.
- **Snapshot.** The rows are written once as a Parquet snapshot, sorted by `(event_date, transaction_id)`, with a provenance document: source identity, partitions, files, expected rows, snapshot SHA-256 and a distribution profile.
- **Loading.** [`warehouse/load.py`](../../../data_platform/storage/warehouse/load.py) checks the snapshot SHA-256, refuses a non-empty database, streams the rows into `\copy` and runs `VACUUM ANALYZE`. It stores the provenance in a `benchmark_provenance` table that DBeaver shows (WH01).

The verification extraction (2026-11-12, 31 days) gave 3,816,574 rows for 2026-05-01 … 2026-05-31, out of 18,600,000 in 151 partitions:

| Fact | Value |
|---|---|
| Status | `success` 3,610,710 (94.6 %), `failed` 132,152, `pending` 73,712 |
| Rows with a merchant | 1,525,003, over 55,623 merchants |
| Rows per merchant | median 6, p90 11, p99 449, maximum 514 |

PostgreSQL 16.15 runs in its own Compose project ([`compose.warehouse-5gb.yml`](../../../infra/docker/compose.warehouse-5gb.yml)): container `edai1-wh5gb-postgres`, volume `edai1-wh5gb-pgdata`, `127.0.0.1:55432`, a required password and a 4 GiB memory limit. No other PostgreSQL is touched.

### B.2 Index design from the data

- **Query (WH-A).** One merchant's successful payments over the last seven days of the sample. The merchant is the median-volume merchant, chosen from the data.
- **Index (WH-B).** Rule-based, in [`workload.py`](../../../data_platform/storage/warehouse/workload.py):
  - `merchant_id` leads (equality, high cardinality);
  - `event_date` follows (range);
  - `status` and `amount` are included so the query can be answered from the index alone.
- **No partial index.** A partial `WHERE status = 'success'` predicate is used only if it excludes at least 20 % of rows. Here it would exclude 5.4 %, so the historical partial index on `'completed'` (a status value that does not exist in this data) is not reused. The rationale, with the measured selectivity, is stored in the report.

### B.3 Experiments

- **WH-A read.**
  - Without the index: 2 warm-ups, then 11 `EXPLAIN (ANALYZE, BUFFERS)` runs.
  - With the index: the same.
  - Because before and after differ in cache state, an interleaved comparison follows on the same warm table: 10 pairs that alternate index-disabled (planner settings) and index-enabled, with the side that runs first alternating.
- **WH-B index.** The `CREATE INDEX` statement, its duration (`\timing`), definition (`pg_get_indexdef`) and size, plus the access path the planner chooses. The owner can create the index in DBeaver. The tool then verifies the catalog definition against the design.
- **WH-C write.** Two empty twin tables, one with only the primary key and one with the primary key plus the same index, receive the same 8 deterministic batches of real rows (`hashtext(transaction_id) % 50`), in alternating order. The insert execution time and the WAL bytes are recorded per batch.
- **WH-D equivalence.**
  - The table signature is the same before and after indexing: count, exact numeric sum, key range, merchant count and an MD5 of the ordered keys.
  - The query result is the same with and without the index.
  - The twin tables are identical after the inserts.

Every measured run is also inserted into a `benchmark_runs` table, so DBeaver shows the real timings (WH05, WH06).

### B.4 Results

**OFFICIAL RESULTS: PENDING the owner run** (Stage 2). They are filled from `report.json` and confirmed in DBeaver.

| Measure | Without index | With index | Verdict |
|---|---|---|---|
| WH-A query median (ms), access path | PENDING | | |
| WH-A interleaved median (ms) | PENDING | | |
| WH-C insert median per batch (ms) | PENDING | | |
| WH-C WAL bytes (8 batches) | PENDING | | |
| Index size / table size | PENDING | | |
| WH-D semantic equivalence | PENDING | | |

> **WH01** provenance and table · **WH02** plan before · **WH03** CREATE INDEX and definition · **WH04** plan after · **WH05** latency comparison · **WH06** write overhead and equivalence — all OWNER_SCREENSHOT_PENDING, from the generated `dbeaver_session.sql`.

### B.5 Expected trade-off

The index trades storage and slower writes for faster selective reads. Its size and the WAL overhead are measured, not assumed. A write-time difference whose sample ranges overlap is reported as `INCONCLUSIVE`, not as a measured overhead.

## Limitations

- **Lakehouse cloud run.** The cloud lakehouse results require an owner-approved Dataproc batch. Until then, row 20 rests on metadata, the plan and the local mechanism test.
- **Warehouse sample.** The warehouse uses a 31-day sample, about 20 % of the Gold rows. `--days 151` loads the whole table if disk allows. The report states the sample.
- **Not significance tests.** The verdicts are descriptive noise bands over 5 or 11 runs.
- **Single node.** Timings come from one laptop (PostgreSQL) and one Dataproc batch (Delta). Other hardware changes the absolute numbers.

## Reproduction and code references

| Concern | File |
|---|---|
| Layout planning, verdicts, rewrite outcome | `data_platform/storage/cloud_optimization.py` |
| Plan (read-only) | `ops/gcp/inspect_storage_layout.py`, `scripts/inspect_storage_layout.sh` |
| Submitter (dry-run default) | `ops/gcp/run_storage_benchmark.py`, `scripts/run_storage_benchmark.sh` |
| Dataproc job | `ops/gcp/storage_benchmark_job.py` |
| Warehouse | `data_platform/storage/warehouse/` (`source`, `schema`, `postgres`, `load`, `workload`, `benchmark`, `dbeaver`, `__main__`) |
| Tests | `tests/unit/test_cloud_storage_optimization.py`, `tests/unit/test_warehouse_benchmark.py`, `tests/integration/test_storage_benchmark_delta_local.py` |
