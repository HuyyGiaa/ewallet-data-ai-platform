# EDAI-1 screenshot audit (N13 quality gate + N15 genuine captures)

Companion to [RUBRIC_EVIDENCE_INDEX.md](RUBRIC_EVIDENCE_INDEX.md).

**Classes:**

| Class | Meaning |
|---|---|
| `NATIVE_UI` | Screenshot of the real application UI, captured in the current campaigns (N9–N13, 2026-11-03/04) |
| `HISTORICAL_UI` | Screenshot of the real application UI from an earlier, identified campaign or run |
| `REAL_TERMINAL` | Screenshot of a real terminal window running real commands. `LOCAL FIXTURE` marks synthetic test fixtures |
| `DERIVED_RENDER` | An image rendered from real recorded output (JSON, logs, `kubectl`/`curl` text). It is **not** equivalent to a native UI screenshot and never counts as one |

**Method.**
1. Every `![…](…)` link in every Markdown file under this sheet's documentation folder was followed.
2. The target file was checked to exist and to decode as PNG/JPEG.
3. The image was classified, and mapped to the rubric rows it is cited for.
4. Representative images of each class were opened and inspected visually. Captions and run IDs were cross-checked against the original evidence and the provenance manifests.

**A row "qualifies"** only if an image of an acceptable class shows the literal column-D deliverable. An image that shows a failure, a local fixture, or only a derived render does not qualify on its own.

## Totals

- **59 embedded image paths, 49 distinct image contents.** N15 adds three genuine Ubuntu terminal views of historical evidence.
- **All files exist and decode.** 0 broken links.

| Class | Paths |
|---|---:|
| HISTORICAL_UI | 41 |
| REAL_TERMINAL | 18 |
| DERIVED_RENDER | 0 |
| NATIVE_UI (current campaign) | 0 |

No EDAI-1 benchmark was rerun during N15. The three new images display preserved historical JSON/log evidence in a real Ubuntu terminal and are labelled `GENUINE_HISTORICAL_EVIDENCE_VIEW`.

## Coverage of rows whose deliverable requires screenshots

| Row | Deliverable needs | Qualifying images (class) | Verdict |
|---:|---|---|---|
| 11 | Spark UI per optimization step + Airflow integration | 5GB: Spark Environment (DP1, Silver ×2, Gold), SQL plans (Silver, Gold, DP3), stage metrics (DP1, DP3 ×2, Gold) and 6 DagRuns (NATIVE_UI) | **QUALIFIES** |
| 12 | Spark UI showing the skew problem and its fix | 5GB Gold stage 173 task metrics (NATIVE_UI, both profiles show the skew) | **PARTIAL**: problem diagnosed; no measured fix |
| 13 | Spark UI / result for schema evolution | 5GB Silver plan with the `UNKNOWN` channel mapping (NATIVE_UI); V1/V2 rules PASS in the validation logs | **PARTIAL**: no measured V1/V2 split |
| 14 | Spark UI for the other offline problem (duplicates) | 5GB Silver plan with the `row_number()` dedup; Silver validation log (NATIVE_UI) | **QUALIFIES** |
| 15 | Airflow integration | 6 DagRuns of the 5GB campaign, DP1–DP3 × both profiles (NATIVE_UI) | **QUALIFIES** |
| 16 | Flink UI showing problems (baseline) | Flink topology, resources, backpressure, watermark (HISTORICAL_UI, `22c0c7f`) | **QUALIFIES** |
| 17 | Late-arrival handling | Watermark views (HISTORICAL_UI); `LATE` output (REAL_TERMINAL) | **QUALIFIES** (historical source) |
| 18 | Other streaming problem | Backpressure baseline vs optimized (HISTORICAL_UI); dedup 12,180 → 12,000 and burst (REAL_TERMINAL) | **QUALIFIES** |
| 22 | Airflow image of DP1, ingest | 5GB DP1 DagRuns, both profiles, all Success (NATIVE_UI) | **QUALIFIES** |
| 23 | DP1 validate | Same DagRuns plus the `bronze.semantic_validation` log (54 PASS) | **QUALIFIES** |
| 24 | DP2 ingest | 5GB DP2 DagRuns, `silver` and `gold` groups all Success (NATIVE_UI) | **QUALIFIES** |
| 25 | DP2 validate | Silver and Gold `semantic_validation` logs (Gold 93 PASS) | **QUALIFIES** |
| 26 | DP3 ingest | 5GB DP3 DagRuns, both profiles (NATIVE_UI); Baseline task list shows a refresh lag on the publish task | **QUALIFIES** |
| 27 | DP3 validate | `features.semantic_validation` log with the PIT recomputation rules (53 PASS) | **QUALIFIES** |
| 28 | DataHub: DP1 tables + producing pipeline | Only the 100GB HISTORICAL_UI lineage, no longer the basis | **PENDING** DG01 (5GB) |
| 29 | DataHub: DP1 validation | Only a 100GB HISTORICAL_UI assertion | **PENDING** DG02 |
| 30 | DataHub: DP2 lineage | Only 100GB HISTORICAL_UI lineage | **PENDING** DG03, DG04 |
| 31 | DataHub: DP2 validation | No DP2 assertion UI | **PENDING** DG05, DG06 |
| 32 | DataHub: DP3 lineage | Only 100GB HISTORICAL_UI lineage | **PENDING** DG07 |
| 33 | DataHub: DP3 validation | Only 100GB HISTORICAL_UI metadata-contract assertions | **PENDING** DG08 |
| 34 | Tables on all zones | — | **PENDING** SC01–SC03 (5GB Trino inventory, dbdiagram.io ERDs) |
| 35 | SCD2 dimension | Gold validation log with the `dim_merchant_scd2` rules on the 5GB output (NATIVE_UI) | **PENDING** SC05, SC06 (table schema and versions) |
| 36 | `feat_` with `event_timestamp` + `created` | DP3 validation log with the timestamp rules (NATIVE_UI) | **PENDING** SC07 (physical columns) |
| 37 | Dim ↔ fact relationships | — | **PENDING** SC02–SC04 (dbdiagram.io) |
| 38 | Naming convention | — | **PENDING** SC08 |

Rows 3–10, 19–21 and 39–40 need documentation, code or proof rather than a specific UI. Their images are supporting evidence:
- Docker: the 5GB owner captures DK01–DK05 are integrated (see *Docker and storage owner captures* below). The October images and transcripts are historical.
- Storage: the 5GB owner captures ST01–ST09 (Trino/DBeaver, Dataproc Spark UI) and WH01–WH06 (PostgreSQL in DBeaver) are integrated. The 500k-fixture and synthetic-PostgreSQL images are historical.

## N15 additions

| Image | Class | Rows | Proof boundary |
|---|---|---|---|
| [03_docker_image_ab_historical_terminal.png](../evidence/edai1/docker/03_docker_image_ab_historical_terminal.png) | REAL_TERMINAL / GENUINE_HISTORICAL_EVIDENCE_VIEW | 3,4 | Historical JSON view; no rebuild |
| [04_docker_compose_historical_terminal.png](../evidence/edai1/docker/04_docker_compose_historical_terminal.png) | REAL_TERMINAL / GENUINE_HISTORICAL_EVIDENCE_VIEW | 3,4 | Historical records; no Compose rerun |
| [08_postgres_warehouse_index_ab_historical_terminal.png](../evidence/edai1/storage/08_postgres_warehouse_index_ab_historical_terminal.png) | REAL_TERMINAL / GENUINE_HISTORICAL_EVIDENCE_VIEW | 21 | Historical JSON view; no benchmark rerun |
- PIT: Cloud Run and Cloud Logging, HISTORICAL_UI.

**Summary.** Of the 25 rows whose deliverable needs screenshots:

| Result | Count | Rows |
|---|---:|---|
| Qualify | 15 | 15, 16, 17, 18, 22, 23, 24, 26, 27, 28, 29, 30, 32, 33, 38 |
| Partial | 4 | 11, 13, 34, 36 |
| Missing or not qualifying | 6 | 12, 14, 25, 31, 35, 37 |

## Docker and storage owner captures (2026-11-13)

The owner captured these images during the 5GB Docker run `20261112t142522`, the lakehouse batch `storage-5gb-20261112t142522` and the PostgreSQL benchmark. Each image was opened and read. Its values were cross-checked against the run's raw JSON: Docker `result.json`, lakehouse `comparison.json`/`partition_pruning.json`/`rewrite.json`, warehouse `report.json`. The originals were copied byte-for-byte, with SHA-256 checked before and after the copy. Per-image checkpoint, original file name, visible content and SHA-256 are in the manifests, together with the screenshots reviewed and not included (assistant panels, other workstreams, a placeholder password variable).

| Set | Images | Class | Rows | Manifest |
|---|---:|---|---|---|
| Docker DK01–DK05 | 10 | REAL_TERMINAL, editor | 3, 4 | [docker/5gb](../evidence/edai1/docker/5gb/MANIFEST.md) |
| Lakehouse ST01–ST09 | 77 (12 of them superseded) | NATIVE_UI (Trino 483 via DBeaver; Dataproc Spark UI), REAL_TERMINAL | 20 | [storage/5gb](../evidence/edai1/storage/5gb/MANIFEST.md) |
| Warehouse WH01–WH06 | 16 | NATIVE_UI (PostgreSQL 16.15 via DBeaver), REAL_TERMINAL | 21 | same |

Two findings came out of the image audit:
- **DK04a** is a genuine failed attempt: `$SPEC` was unset and both images refused the spec with `INVALID_SPEC`. It is kept and labelled as a failure. The runbook now guards the variables.
- **The first ST09 session** (01:46–02:16) ran a script whose `stats_only` literal had no zone. Under DBeaver's +07:00 session, its window read 860,298 rows instead of 860,389. The generator was fixed, and the owner reran all six ST09 cases at 03:34–03:47 with the regenerated script. Those 18 captures, plus 2 captures of the script itself, replace the 12 first-session ST09 images, which stay in the manifest marked SUPERSEDED. The new captures show identical results on all three sides (860,389 · 894,991,837,952.66 · 1,040,217.666605) and distinct plans: partition key with 7 splits, file statistics with 7 splits, full scan with 151 splits.

## 5GB rehearsal additions (2026-11-11)

The audit adds 21 `NATIVE_UI` images of benchmark `ab-20261110225456`, captured by the owner during and right after the runs. Each was opened and read, and its run, batch, application or stage was cross-checked against the Airflow metadata, Dataproc and the event logs. Per-image purpose, verified reference and SHA-256 are in the [screenshot manifest](../evidence/edai1/spark/rehearsal-5gb-ab-20261110225456/screenshots/MANIFEST.md).

| Images | Shows | Rows |
|---|---|---|
| 6 Airflow DagRun views (DP1–DP3 × Baseline/Optimised) | Graph and task list, all Success | 15, 22–27 |
| 4 `semantic_validation` task logs | Rule-level PASS lines and summaries: Bronze 54, Silver, Gold 93 incl. SCD2, features 53 incl. PIT | 23, 25, 27, 35, 36 |
| 4 Spark Environment views | AQE on/off, shuffle partitions, advisory size, runtime versions | 11 |
| 3 SQL physical plans | Silver dedup window, Gold date repartition, DP3 point-in-time window | 11, 13, 14, 40 |
| 4 Spark stage views | Source listing (186 paths), DP3 200 against 23 coalesced tasks, Gold write-layout skew | 11, 12 |

The row-11 verdict above ("no DP2/DP3 Spark UI") is superseded: Spark UI now exists for every pipeline and both profiles, embedded in [BATCH_PROCESSING_AND_ORCHESTRATION.md](BATCH_PROCESSING_AND_ORCHESTRATION.md). The historical Airflow and DP1 Spark images listed below are cited only by archived reports. Row 12 stays PARTIAL. The images diagnose skew, but no method is shown to improve it.

The owner's comparison-submission terminal capture was not integrated, because it also shows unrelated editor content.

## Per-image classification

| Image | Renders | Class | Source / identity | Rows | Embedded in |
|---|---|---|---|---|---|
| [01_dp1_optimized_run_success_20261030.png](../evidence/edai1/airflow/01_dp1_optimized_run_success_20261030.png) | yes | HISTORICAL_UI | Airflow 3 UI, 2026-10-26..31 runs | 15,22,23 | archived `AIRFLOW_DP1_DP2_DP3_EVIDENCE.md` (not in submission) |
| [02_dp2_method2_run_failed_20261031.png](../evidence/edai1/airflow/02_dp2_method2_run_failed_20261031.png) | yes | HISTORICAL_UI | Airflow 3 UI, 2026-10-26..31 runs | 15,24,25 | archived `AIRFLOW_DP1_DP2_DP3_EVIDENCE.md` (not in submission) |
| [03_dp3_historical_optimized_run_success_20261026.png](../evidence/edai1/airflow/03_dp3_historical_optimized_run_success_20261026.png) | yes | HISTORICAL_UI | Airflow 3 UI, 2026-10-26..31 runs | 15,26,27 | archived `AIRFLOW_DP1_DP2_DP3_EVIDENCE.md` (not in submission) |
| [dp1_bronze_result_bound_published_20261031.jpg](../evidence/edai1/datahub/dp1_bronze_result_bound_published_20261031.jpg) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 29 | not embedded: 100GB history, no longer cited for rows 28–34 |
| [dp1_source_bronze_historical_20261025.png](../evidence/edai1/datahub/dp1_source_bronze_historical_20261025.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 28,34 | not embedded: 100GB history, no longer cited for rows 28–34 |
| [dp2_bronze_silver_historical_20261025.png](../evidence/edai1/datahub/dp2_bronze_silver_historical_20261025.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 30,34 | not embedded: 100GB history, no longer cited for rows 28–34 |
| [dp2_dataflow_jobs_historical_20261025.png](../evidence/edai1/datahub/dp2_dataflow_jobs_historical_20261025.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 30 | not embedded: 100GB history, no longer cited for rows 28–34 |
| [dp2_silver_gold_historical_20261025.png](../evidence/edai1/datahub/dp2_silver_gold_historical_20261025.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 30,34 | not embedded: 100GB history, no longer cited for rows 28–34 |
| [dp3_historical_features_historical_20261026.png](../evidence/edai1/datahub/dp3_historical_features_historical_20261026.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 32,34 | not embedded: 100GB history, no longer cited for rows 28–34 |
| [feature_metadata_contract_detail_historical_20261029.png](../evidence/edai1/datahub/feature_metadata_contract_detail_historical_20261029.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 33 | not embedded: 100GB history, no longer cited for rows 28–34 |
| [feature_metadata_contract_list_historical_20261029.png](../evidence/edai1/datahub/feature_metadata_contract_list_historical_20261029.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 33 | not embedded: 100GB history, no longer cited for rows 28–34 |
| [feature_metadata_contract_logic_historical_20261029.png](../evidence/edai1/datahub/feature_metadata_contract_logic_historical_20261029.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 33 | not embedded: 100GB history, no longer cited for rows 28–34 |
| [01_scd2_input_snapshots.png](../evidence/edai1/dp2/01_scd2_input_snapshots.png) | yes | REAL_TERMINAL | LOCAL FIXTURE terminal (not production runtime) | 35 | not embedded (local fixture; 5GB SC05–SC07 pending) |
| [02_scd2_version_history.png](../evidence/edai1/dp2/02_scd2_version_history.png) | yes | REAL_TERMINAL | LOCAL FIXTURE terminal (not production runtime) | 35 | not embedded (local fixture; 5GB SC05–SC07 pending) |
| [03_scd2_historical_lookup.png](../evidence/edai1/dp2/03_scd2_historical_lookup.png) | yes | REAL_TERMINAL | LOCAL FIXTURE terminal (not production runtime) | 35 | not embedded (local fixture; 5GB SC05–SC07 pending) |
| [01_feature_timestamp_schema_user_account.png](../evidence/edai1/dp3/01_feature_timestamp_schema_user_account.png) | yes | REAL_TERMINAL | LOCAL FIXTURE terminal (not production runtime) | 36 | not embedded (local fixture; 5GB SC05–SC07 pending) |
| [02_feature_timestamp_schema_device_merchant.png](../evidence/edai1/dp3/02_feature_timestamp_schema_device_merchant.png) | yes | REAL_TERMINAL | LOCAL FIXTURE terminal (not production runtime) | 36 | not embedded (local fixture; 5GB SC05–SC07 pending) |
| [03_feature_timestamp_validation.png](../evidence/edai1/dp3/03_feature_timestamp_validation.png) | yes | REAL_TERMINAL | LOCAL FIXTURE terminal (not production runtime) | 36 | not embedded (local fixture; 5GB SC05–SC07 pending) |
| [04_feast_feature_inventory.png](../evidence/edai1/dp3/04_feast_feature_inventory.png) | yes | REAL_TERMINAL | LOCAL FIXTURE terminal (not production runtime) | 36 | not embedded (local fixture; 5GB SC05–SC07 pending) |
| [01_dp1_baseline_airflow_run_success_20261030.png](../evidence/edai1/n13_curated/01_dp1_baseline_airflow_run_success_20261030.png) | yes | HISTORICAL_UI | Airflow 3 UI, 2026-10-26..31 runs | 11,15,22,23 | AIRFLOW_DP1_DP2_DP3_EVIDENCE.md, RUBRIC_EVIDENCE_INDEX.md |
| [01_schema_evolution_source_500k.png](../evidence/edai1/novel_ideas/01_schema_evolution_source_500k.png) | yes | REAL_TERMINAL | historical local terminal | 6,13 | data_generator/DATA_GENERATION_AND_SIMULATION.md, novel_ideas/01_DETERMINISTIC_SHARDING.md |
| [02_pit_cloud_run_success.png](../evidence/edai1/novel_ideas/02_pit_cloud_run_success.png) | yes | HISTORICAL_UI | Google Cloud Run / Logging, 2026-10-31 | 40 | novel_ideas/02_PIT_LEAKAGE_PREVENTION.md |
| [03_pit_cloud_logging_310_pass.png](../evidence/edai1/novel_ideas/03_pit_cloud_logging_310_pass.png) | yes | HISTORICAL_UI | Google Cloud Run / Logging, 2026-10-31 | 40 | novel_ideas/02_PIT_LEAKAGE_PREVENTION.md |
| [01_dp1_baseline_spark_environment.png](../evidence/edai1/spark/01_dp1_baseline_spark_environment.png) | yes | HISTORICAL_UI | Spark UI (Dataproc history), ab-20261029174838 | 11,12 | archived `OPTIMIZATION_MATRIX.md` (not in submission) |
| [02_dp1_optimized_spark_environment.png](../evidence/edai1/spark/02_dp1_optimized_spark_environment.png) | yes | HISTORICAL_UI | Spark UI (Dataproc history), ab-20261029174838 | 11,12 | archived `OPTIMIZATION_MATRIX.md` (not in submission) |
| [03_dp1_optimized_spark_stages.png](../evidence/edai1/spark/03_dp1_optimized_spark_stages.png) | yes | HISTORICAL_UI | Spark UI (Dataproc history), ab-20261029174838 | 11,12 | archived `OPTIMIZATION_MATRIX.md` (not in submission) |
| [01_layout_before_500k.png](../evidence/edai1/storage/01_layout_before_500k.png) | yes | HISTORICAL_UI | DBeaver/Trino, local 500k dataset, 2026-08-25 | 20 | not embedded since 2026-11-12 (historical; superseded by the 5GB rerun) |
| [02_benchmark_before_500k.png](../evidence/edai1/storage/02_benchmark_before_500k.png) | yes | REAL_TERMINAL | historical local terminal | 20 | not embedded since 2026-11-12 (historical; superseded by the 5GB rerun) |
| [03_partition_pruning_before_500k.png](../evidence/edai1/storage/03_partition_pruning_before_500k.png) | yes | HISTORICAL_UI | DBeaver/Trino, local 500k dataset, 2026-08-25 | 20 | not embedded since 2026-11-12 (historical; superseded by the 5GB rerun) |
| [04_layout_after_500k.png](../evidence/edai1/storage/04_layout_after_500k.png) | yes | HISTORICAL_UI | DBeaver/Trino, local 500k dataset, 2026-08-25 | 20 | not embedded since 2026-11-12 (historical; superseded by the 5GB rerun) |
| [05_partition_pruning_after_500k.png](../evidence/edai1/storage/05_partition_pruning_after_500k.png) | yes | HISTORICAL_UI | DBeaver/Trino, local 500k dataset, 2026-08-25 | 20 | not embedded since 2026-11-12 (historical; superseded by the 5GB rerun) |
| [06_benchmark_after_500k.png](../evidence/edai1/storage/06_benchmark_after_500k.png) | yes | REAL_TERMINAL | historical local terminal | 20 | not embedded since 2026-11-12 (historical; superseded by the 5GB rerun) |
| [03_flink_100eps_baseline_job.png](../evidence/edai1/streaming/03_flink_100eps_baseline_job.png) | yes | HISTORICAL_UI | local Flink UI, source 22c0c7f | 16,17,18 | streaming/FLINK_STREAMING.md |
| [04_flink_100eps_baseline_resources.png](../evidence/edai1/streaming/04_flink_100eps_baseline_resources.png) | yes | HISTORICAL_UI | local Flink UI, source 22c0c7f | 16,17,18 | streaming/FLINK_STREAMING.md |
| [05_flink_100eps_baseline_backpressure.png](../evidence/edai1/streaming/05_flink_100eps_baseline_backpressure.png) | yes | HISTORICAL_UI | local Flink UI, source 22c0c7f | 16,17,18 | streaming/FLINK_STREAMING.md |
| [06_flink_100eps_baseline_watermark.png](../evidence/edai1/streaming/06_flink_100eps_baseline_watermark.png) | yes | HISTORICAL_UI | local Flink UI, source 22c0c7f | 16,17,18 | streaming/FLINK_STREAMING.md |
| [07_flink_100eps_optimized_job.png](../evidence/edai1/streaming/07_flink_100eps_optimized_job.png) | yes | HISTORICAL_UI | local Flink UI, source 22c0c7f | 16,17,18 | streaming/FLINK_STREAMING.md |
| [08_flink_100eps_optimized_backpressure.png](../evidence/edai1/streaming/08_flink_100eps_optimized_backpressure.png) | yes | HISTORICAL_UI | local Flink UI, source 22c0c7f | 16,17,18 | streaming/FLINK_STREAMING.md |
| [09_flink_100eps_optimized_watermarks.png](../evidence/edai1/streaming/09_flink_100eps_optimized_watermarks.png) | yes | HISTORICAL_UI | local Flink UI, source 22c0c7f | 16,17,18 | streaming/FLINK_STREAMING.md |
| [10_flink_100eps_optimized_resources.png](../evidence/edai1/streaming/10_flink_100eps_optimized_resources.png) | yes | HISTORICAL_UI | local Flink UI, source 22c0c7f | 16,18 | streaming/FLINK_STREAMING.md |
| [11_flink_100eps_comparison.png](../evidence/edai1/streaming/11_flink_100eps_comparison.png) | yes | REAL_TERMINAL | historical local terminal | 10,16,18 | data_generator/DATA_GENERATION_AND_SIMULATION.md, streaming/FLINK_STREAMING.md |
| [01_streaming_feature_late_duplicate.png](../evidence/flink/01_streaming_feature_late_duplicate.png) | yes | REAL_TERMINAL | historical local terminal | 9,10,17,18,19 | data_generator/DATA_GENERATION_AND_SIMULATION.md, streaming/FLINK_STREAMING.md |
| [02_burst_detected_true.png](../evidence/flink/02_burst_detected_true.png) | yes | REAL_TERMINAL | historical local terminal | 18 | streaming/FLINK_STREAMING.md |
| [dp1_bronze_result_bound_published_20261031.jpg](../evidence/images/datahub/assertions/dp1_bronze_result_bound_published_20261031.jpg) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 29 | DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md |
| [feature_metadata_contract_detail_historical_20261029.png](../evidence/images/datahub/assertions/feature_metadata_contract_detail_historical_20261029.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 33 | DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md |
| [feature_metadata_contract_list_historical_20261029.png](../evidence/images/datahub/assertions/feature_metadata_contract_list_historical_20261029.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 33 | DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md |
| [feature_metadata_contract_logic_historical_20261029.png](../evidence/images/datahub/assertions/feature_metadata_contract_logic_historical_20261029.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 33 | DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md |
| [feature_device_schema_historical_20261029.png](../evidence/images/datahub/datasets_schema/feature_device_schema_historical_20261029.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 34,36 | DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md |
| [dp1_source_bronze_historical_20261025.png](../evidence/images/datahub/lineage/dp1_source_bronze_historical_20261025.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 28,34 | DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md |
| [dp2_bronze_silver_historical_20261025.png](../evidence/images/datahub/lineage/dp2_bronze_silver_historical_20261025.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 30,34 | DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md |
| [dp2_dataflow_jobs_historical_20261025.png](../evidence/images/datahub/lineage/dp2_dataflow_jobs_historical_20261025.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 30 | DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md |
| [dp2_silver_gold_historical_20261025.png](../evidence/images/datahub/lineage/dp2_silver_gold_historical_20261025.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 30,34 | DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md |
| [dp3_historical_features_historical_20261026.png](../evidence/images/datahub/lineage/dp3_historical_features_historical_20261026.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 32,34 | DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md |
| [01_merchant_skew_profile_500k.png](../evidence/spark/01_merchant_skew_profile_500k.png) | yes | REAL_TERMINAL | historical local terminal | 5,12 | data_generator/DATA_GENERATION_AND_SIMULATION.md; archived `OPTIMIZATION_MATRIX.md` |
| [07_schema_evolution_source_500k.png](../evidence/storage/07_schema_evolution_source_500k.png) | yes | REAL_TERMINAL | historical local terminal | 6,13 | not embedded since 2026-11-11 (local images; the 5GB report cites only campaign evidence) |
| [08_schema_evolution_500k.png](../evidence/storage/08_schema_evolution_500k.png) | yes | HISTORICAL_UI | DBeaver/Trino, local 500k dataset, 2026-08-25 | 6,13 | SCHEMA_DESIGN.md; archived `OPTIMIZATION_MATRIX.md` |
