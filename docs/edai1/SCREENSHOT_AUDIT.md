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
| 11 | Spark UI per optimization step + Airflow integration | DP1 baseline/optimized Spark Environment + Stages (HISTORICAL_UI ×3); DP1 Airflow runs (HISTORICAL_UI ×2) | **PARTIAL**: no DP2/DP3 Spark UI |
| 12 | Spark UI showing the skew problem and its fix | Merchant skew profile (REAL_TERMINAL, local 500k); Spark Environment shows only AQE settings | **MISSING**: no Spark UI skew diagnosis (task-time skew) |
| 13 | Spark UI / result for schema evolution | DBeaver query of V1/V2 rows (HISTORICAL_UI); source V1/V2 (REAL_TERMINAL) | **PARTIAL**: result shown, no Spark UI |
| 14 | Spark UI for the other offline problem (duplicates) | — | **MISSING** |
| 15 | Airflow integration | DP1 ×2, DP2, DP3 Airflow runs (HISTORICAL_UI) | **QUALIFIES** |
| 16 | Flink UI showing problems (baseline) | Flink topology, resources, backpressure, watermark (HISTORICAL_UI, `22c0c7f`) | **QUALIFIES** |
| 17 | Late-arrival handling | Watermark views (HISTORICAL_UI); `LATE` output (REAL_TERMINAL) | **QUALIFIES** (historical source) |
| 18 | Other streaming problem | Backpressure baseline vs optimized (HISTORICAL_UI); dedup 12,180 → 12,000 and burst (REAL_TERMINAL) | **QUALIFIES** |
| 22 | Airflow image of DP1, ingest | DP1 optimized and baseline runs, 4/4 Success (HISTORICAL_UI) | **QUALIFIES** |
| 23 | DP1 validate | Same images; `verify_source_manifest` and `verify_bronze_delta` Success | **QUALIFIES** |
| 24 | DP2 ingest | DP2 Method-2 run: Silver/Gold transform tasks succeeded, run FAILED (HISTORICAL_UI) | **QUALIFIES** for the ingest stage (failure visible) |
| 25 | DP2 validate | Same image shows the final Gold gate **FAILED** | **NOT QUALIFYING** (shows failure) |
| 26 | DP3 ingest | DP3 replacement run, 5/5 Success (HISTORICAL_UI) | **QUALIFIES** |
| 27 | DP3 validate | Same image; physical validation tasks Success | **QUALIFIES** (screenshot); the row is PARTIAL because PIT validation is not an Airflow task |
| 28 | DataHub: DP1 tables + producing pipeline | DP1 Source → Bronze lineage (HISTORICAL_UI) | **QUALIFIES** |
| 29 | DataHub: DP1 validation | Result-bound assertion "Passing" (HISTORICAL_UI, JPG) | **QUALIFIES** |
| 30 | DataHub: DP2 lineage | Bronze → Silver, DataFlow/DataJobs, Silver → Gold (HISTORICAL_UI) | **QUALIFIES** |
| 31 | DataHub: DP2 validation | No DP2 assertion UI | **MISSING** |
| 32 | DataHub: DP3 lineage | DP3 feature lineage (HISTORICAL_UI) | **QUALIFIES** |
| 33 | DataHub: DP3 validation | Feature metadata-contract assertions list/detail/logic (HISTORICAL_UI) | **QUALIFIES** for schema/physical validation; PIT results are machine-readable only |
| 34 | Tables and relationships on each zone | DataHub lineage per zone, now embedded in [ALL_ZONES_SCHEMA.md](schema/ALL_ZONES_SCHEMA.md) (HISTORICAL_UI) | **PARTIAL**: cross-zone lineage shown; no within-zone relationship diagram |
| 35 | SCD2 dimension | LOCAL FIXTURE terminal only (REAL_TERMINAL, fixture); source not in the submission tree | **NOT QUALIFYING** |
| 36 | `feat_` with `event_timestamp` + `created` | LOCAL FIXTURE terminal; DataHub feature schema shows `event_timestamp` only | **PARTIAL** |
| 37 | Dim ↔ fact relationships (DBeaver-like export) | — | **MISSING** |
| 38 | Naming convention | DataHub images show zone-qualified names and Gold prefixes (HISTORICAL_UI) | **QUALIFIES** (screenshot); the naming itself is a semantic gap |

Rows 3–10, 19–21 and 39–40 need documentation, code or proof rather than a specific UI. Their images are supporting evidence:
- Docker: raw historical JSON/log/Compose transcripts plus two N15 genuine terminal views.
- Storage: DBeaver HISTORICAL_UI ×4 and benchmark REAL_TERMINAL ×2.
- Warehouse: raw historical JSON/log plus an N15 genuine terminal view; there is still no database UI capture.

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

## Per-image classification

| Image | Renders | Class | Source / identity | Rows | Embedded in |
|---|---|---|---|---|---|
| [01_dp1_optimized_run_success_20261030.png](../evidence/edai1/airflow/01_dp1_optimized_run_success_20261030.png) | yes | HISTORICAL_UI | Airflow 3 UI, 2026-10-26..31 runs | 15,22,23 | AIRFLOW_DP1_DP2_DP3_EVIDENCE.md |
| [02_dp2_method2_run_failed_20261031.png](../evidence/edai1/airflow/02_dp2_method2_run_failed_20261031.png) | yes | HISTORICAL_UI | Airflow 3 UI, 2026-10-26..31 runs | 15,24,25 | AIRFLOW_DP1_DP2_DP3_EVIDENCE.md |
| [03_dp3_historical_optimized_run_success_20261026.png](../evidence/edai1/airflow/03_dp3_historical_optimized_run_success_20261026.png) | yes | HISTORICAL_UI | Airflow 3 UI, 2026-10-26..31 runs | 15,26,27 | AIRFLOW_DP1_DP2_DP3_EVIDENCE.md |
| [dp1_bronze_result_bound_published_20261031.jpg](../evidence/edai1/datahub/dp1_bronze_result_bound_published_20261031.jpg) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 29 | datahub/DATAHUB_GOVERNANCE.md |
| [dp1_source_bronze_historical_20261025.png](../evidence/edai1/datahub/dp1_source_bronze_historical_20261025.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 28,34 | ALL_ZONES_SCHEMA.md, datahub/DATAHUB_GOVERNANCE.md |
| [dp2_bronze_silver_historical_20261025.png](../evidence/edai1/datahub/dp2_bronze_silver_historical_20261025.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 30,34 | ALL_ZONES_SCHEMA.md, datahub/DATAHUB_GOVERNANCE.md |
| [dp2_dataflow_jobs_historical_20261025.png](../evidence/edai1/datahub/dp2_dataflow_jobs_historical_20261025.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 30 | datahub/DATAHUB_GOVERNANCE.md |
| [dp2_silver_gold_historical_20261025.png](../evidence/edai1/datahub/dp2_silver_gold_historical_20261025.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 30,34 | ALL_ZONES_SCHEMA.md, datahub/DATAHUB_GOVERNANCE.md |
| [dp3_historical_features_historical_20261026.png](../evidence/edai1/datahub/dp3_historical_features_historical_20261026.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 32,34 | ALL_ZONES_SCHEMA.md, datahub/DATAHUB_GOVERNANCE.md |
| [feature_metadata_contract_detail_historical_20261029.png](../evidence/edai1/datahub/feature_metadata_contract_detail_historical_20261029.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 33 | datahub/DATAHUB_GOVERNANCE.md |
| [feature_metadata_contract_list_historical_20261029.png](../evidence/edai1/datahub/feature_metadata_contract_list_historical_20261029.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 33 | datahub/DATAHUB_GOVERNANCE.md |
| [feature_metadata_contract_logic_historical_20261029.png](../evidence/edai1/datahub/feature_metadata_contract_logic_historical_20261029.png) | yes | HISTORICAL_UI | DataHub UI, 2026-10-25..31 campaign | 33 | datahub/DATAHUB_GOVERNANCE.md |
| [01_scd2_input_snapshots.png](../evidence/edai1/dp2/01_scd2_input_snapshots.png) | yes | REAL_TERMINAL | LOCAL FIXTURE terminal (not production runtime) | 35 | SCHEMA_DESIGN.md |
| [02_scd2_version_history.png](../evidence/edai1/dp2/02_scd2_version_history.png) | yes | REAL_TERMINAL | LOCAL FIXTURE terminal (not production runtime) | 35 | SCHEMA_DESIGN.md |
| [03_scd2_historical_lookup.png](../evidence/edai1/dp2/03_scd2_historical_lookup.png) | yes | REAL_TERMINAL | LOCAL FIXTURE terminal (not production runtime) | 35 | SCHEMA_DESIGN.md |
| [01_feature_timestamp_schema_user_account.png](../evidence/edai1/dp3/01_feature_timestamp_schema_user_account.png) | yes | REAL_TERMINAL | LOCAL FIXTURE terminal (not production runtime) | 36 | SCHEMA_DESIGN.md |
| [02_feature_timestamp_schema_device_merchant.png](../evidence/edai1/dp3/02_feature_timestamp_schema_device_merchant.png) | yes | REAL_TERMINAL | LOCAL FIXTURE terminal (not production runtime) | 36 | SCHEMA_DESIGN.md |
| [03_feature_timestamp_validation.png](../evidence/edai1/dp3/03_feature_timestamp_validation.png) | yes | REAL_TERMINAL | LOCAL FIXTURE terminal (not production runtime) | 36 | SCHEMA_DESIGN.md |
| [04_feast_feature_inventory.png](../evidence/edai1/dp3/04_feast_feature_inventory.png) | yes | REAL_TERMINAL | LOCAL FIXTURE terminal (not production runtime) | 36 | SCHEMA_DESIGN.md |
| [01_dp1_baseline_airflow_run_success_20261030.png](../evidence/edai1/n13_curated/01_dp1_baseline_airflow_run_success_20261030.png) | yes | HISTORICAL_UI | Airflow 3 UI, 2026-10-26..31 runs | 11,15,22,23 | AIRFLOW_DP1_DP2_DP3_EVIDENCE.md, RUBRIC_EVIDENCE_INDEX.md |
| [01_schema_evolution_source_500k.png](../evidence/edai1/novel_ideas/01_schema_evolution_source_500k.png) | yes | REAL_TERMINAL | historical local terminal | 6,13 | data_generator/DATA_GENERATION_AND_SIMULATION.md, novel_ideas/01_DETERMINISTIC_SHARDING.md |
| [02_pit_cloud_run_success.png](../evidence/edai1/novel_ideas/02_pit_cloud_run_success.png) | yes | HISTORICAL_UI | Google Cloud Run / Logging, 2026-10-31 | 40 | novel_ideas/02_PIT_LEAKAGE_PREVENTION.md |
| [03_pit_cloud_logging_310_pass.png](../evidence/edai1/novel_ideas/03_pit_cloud_logging_310_pass.png) | yes | HISTORICAL_UI | Google Cloud Run / Logging, 2026-10-31 | 40 | novel_ideas/02_PIT_LEAKAGE_PREVENTION.md |
| [01_dp1_baseline_spark_environment.png](../evidence/edai1/spark/01_dp1_baseline_spark_environment.png) | yes | HISTORICAL_UI | Spark UI (Dataproc history), ab-20261029174838 | 11,12 | OPTIMIZATION_MATRIX.md |
| [02_dp1_optimized_spark_environment.png](../evidence/edai1/spark/02_dp1_optimized_spark_environment.png) | yes | HISTORICAL_UI | Spark UI (Dataproc history), ab-20261029174838 | 11,12 | OPTIMIZATION_MATRIX.md |
| [03_dp1_optimized_spark_stages.png](../evidence/edai1/spark/03_dp1_optimized_spark_stages.png) | yes | HISTORICAL_UI | Spark UI (Dataproc history), ab-20261029174838 | 11,12 | OPTIMIZATION_MATRIX.md |
| [01_layout_before_500k.png](../evidence/edai1/storage/01_layout_before_500k.png) | yes | HISTORICAL_UI | DBeaver/Trino, local 500k dataset, 2026-08-25 | 20 | storage/STORAGE_OPTIMIZATION.md |
| [02_benchmark_before_500k.png](../evidence/edai1/storage/02_benchmark_before_500k.png) | yes | REAL_TERMINAL | historical local terminal | 20 | storage/STORAGE_OPTIMIZATION.md |
| [03_partition_pruning_before_500k.png](../evidence/edai1/storage/03_partition_pruning_before_500k.png) | yes | HISTORICAL_UI | DBeaver/Trino, local 500k dataset, 2026-08-25 | 20 | storage/STORAGE_OPTIMIZATION.md |
| [04_layout_after_500k.png](../evidence/edai1/storage/04_layout_after_500k.png) | yes | HISTORICAL_UI | DBeaver/Trino, local 500k dataset, 2026-08-25 | 20 | storage/STORAGE_OPTIMIZATION.md |
| [05_partition_pruning_after_500k.png](../evidence/edai1/storage/05_partition_pruning_after_500k.png) | yes | HISTORICAL_UI | DBeaver/Trino, local 500k dataset, 2026-08-25 | 20 | storage/STORAGE_OPTIMIZATION.md |
| [06_benchmark_after_500k.png](../evidence/edai1/storage/06_benchmark_after_500k.png) | yes | REAL_TERMINAL | historical local terminal | 20 | storage/STORAGE_OPTIMIZATION.md |
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
| [01_merchant_skew_profile_500k.png](../evidence/spark/01_merchant_skew_profile_500k.png) | yes | REAL_TERMINAL | historical local terminal | 5,12 | OPTIMIZATION_MATRIX.md, data_generator/DATA_GENERATION_AND_SIMULATION.md |
| [07_schema_evolution_source_500k.png](../evidence/storage/07_schema_evolution_source_500k.png) | yes | REAL_TERMINAL | historical local terminal | 6,13 | SCHEMA_DESIGN.md |
| [08_schema_evolution_500k.png](../evidence/storage/08_schema_evolution_500k.png) | yes | HISTORICAL_UI | DBeaver/Trino, local 500k dataset, 2026-08-25 | 6,13 | OPTIMIZATION_MATRIX.md, SCHEMA_DESIGN.md |
