# EDAI-1 lakehouse and warehouse evidence (5GB campaign)

Lakehouse benchmark `storage-5gb-20261112t142522` (Dataproc Serverless 2.2, Spark 3.5.3, Delta 3.2.0, tool commit `f9be2776`) and the PostgreSQL index benchmark on 3,816,574 Gold rows, analysed in [STORAGE_OPTIMIZATION.md](../../../../edai1/storage/STORAGE_OPTIMIZATION.md).

Every screenshot is an owner capture copied byte-for-byte from `~/Pictures/Screenshots/`. The SHA-256 of each original was checked before and after the copy and equals the committed file. Images were not edited. `SHA256SUMS` covers every screenshot and raw file; check it with `sha256sum -c SHA256SUMS` from this folder. `MANIFEST.json` holds the same inventory in machine-readable form.

Large data stays out of Git: the 351 MB warehouse Parquet snapshot, the Delta tables and the Spark event logs remain in `~/ewallet-evidence/` and the benchmark bucket. The PostgreSQL password file was never copied.

## Lakehouse screenshots

| Checkpoint | File | Original file name | What is visible | SHA-256 (first 16) |
|---|---|---|---|---|
| ST08 | [ST00a_evidence_download_from_benchmark_root.png](lakehouse/screenshots/ST00a_evidence_download_from_benchmark_root.png) | `Screenshot From 2026-11-13 00-42-39.png` | Terminal: gcloud storage cp of the job's evidence/ folder (11 files) | `1d6b65b874827eb7` |
| ST01 | [ST00b_trino_register_benchmark_arms.png](lakehouse/screenshots/ST00b_trino_register_benchmark_arms.png) | `Screenshot From 2026-11-13 01-00-26.png` | Terminal: register_table of six arms in the schema-viz Trino | `196b0b913907363e` |
| ST01 | [ST00c_dbeaver_trino_viewer_connection.png](lakehouse/screenshots/ST00c_dbeaver_trino_viewer_connection.png) | `Screenshot From 2026-11-13 01-02-14.png` | DBeaver: Trino connection 127.0.0.1:8091/storage_bench, user viewer, no password | `723c84a2092a2a69` |
| ST01 | [ST01_fact_baseline_history_clone_v0.png](lakehouse/screenshots/ST01_fact_baseline_history_clone_v0.png) | `ST01_delta_baseline_clone.png` | DBeaver/Trino: fact_transactions_baseline$history = CLONE sourceVersion 0 | `e70d906fbf8f1ab6` |
| ST01 | [ST01_fact_compacted_history_clone_v0.png](lakehouse/screenshots/ST01_fact_compacted_history_clone_v0.png) | `Screenshot From 2026-11-13 01-07-38.png` | DBeaver/Trino: fact_transactions_compacted$history = CLONE only (no OPTIMIZE commit) | `9b1a07e7ef4c217d` |
| ST01 | [ST01_fact_optimized_history_clone_then_zorder.png](lakehouse/screenshots/ST01_fact_optimized_history_clone_then_zorder.png) | `ST01_fact_transactions_zorder_history.png` | DBeaver/Trino: fact_transactions_optimized$history = CLONE v0, OPTIMIZE zOrderBy merchant_id v1 | `6af920fc4803462c` |
| ST01 | [ST01_obt_baseline_history_clone_v0.png](lakehouse/screenshots/ST01_obt_baseline_history_clone_v0.png) | `ST01_obt_baseline_history.png` | DBeaver/Trino: obt baseline history = CLONE | `541771f60f8cbd4f` |
| ST01 | [ST01_obt_compacted_history_clone_v0.png](lakehouse/screenshots/ST01_obt_compacted_history_clone_v0.png) | `ST01_obt_compacted_history.png` | DBeaver/Trino: obt compacted history = CLONE only | `610484ce9d9ee97b` |
| ST01 | [ST01_obt_optimized_history_clone_then_zorder.png](lakehouse/screenshots/ST01_obt_optimized_history_clone_then_zorder.png) | `ST01_obt_optimized_zorder_history.png` | DBeaver/Trino: obt optimized history = CLONE, OPTIMIZE | `aecb7afa8127310c` |
| ST02 | [ST02_fact_baseline_layout.png](lakehouse/screenshots/ST02_fact_baseline_layout.png) | `ST02_baseline_file_layout.png` | DBeaver/Trino: 151 files, 23.16 MiB avg, 24,117,881-24,508,097 B, 151 partitions | `f6c5f0ea6f30be7b` |
| ST02 | [ST02_obt_baseline_layout.png](lakehouse/screenshots/ST02_obt_baseline_layout.png) | `ST02_obt_baseline_file_layout.png` | DBeaver/Trino: 151 files, 26.98 MiB avg, 28,090,019-28,554,997 B, 151 partitions | `637c10366e8bdeda` |
| ST03 | [ST03a_spark_ui_sql_delta_operations.png](lakehouse/screenshots/ST03a_spark_ui_sql_delta_operations.png) | `Screenshot From 2026-11-13 00-50-17.png` | Dataproc Spark UI (batch storage-5gb-20261112t142522-storage): SQL/DataFrame list, Delta snapshot and clone operations | `455ad51d956c8ba5` |
| ST03 | [ST03b_spark_ui_sql_dataframe_1714_completed.png](lakehouse/screenshots/ST03b_spark_ui_sql_dataframe_1714_completed.png) | `Screenshot From 2026-11-13 00-52-24.png` | Dataproc Spark UI: app-20261112172914785-0221, 1714 completed queries, 0 failed | `ce84d6460a1b920b` |
| ST03 | [ST03c_spark_ui_compaction_optimize_query_52.png](lakehouse/screenshots/ST03c_spark_ui_compaction_optimize_query_52.png) | `Screenshot From 2026-11-13 00-54-57.png` | Dataproc Spark UI: query 52 Execute OptimizeTableCommand on the compacted arm, 4.62 s | `a96e13968151314d` |
| ST04 | [ST04_fact_compacted_layout_unchanged.png](lakehouse/screenshots/ST04_fact_compacted_layout_unchanged.png) | `ST04_compacted_file_layout.png` | DBeaver/Trino: compacted arm identical to baseline (151 files, 23.16 MiB) | `8ac43468f6e912dd` |
| ST04 | [ST04_fact_optimized_layout.png](lakehouse/screenshots/ST04_fact_optimized_layout.png) | `ST04_optimized_file_layout.png` | DBeaver/Trino: Z-order arm 151 files, same sizes as baseline | `55ebd80df83b3f97` |
| ST04 | [ST04_fact_baseline_vs_optimized_file_paths.png](lakehouse/screenshots/ST04_fact_baseline_vs_optimized_file_paths.png) | `ST04_baseline_vs_optimized_paths.png` | DBeaver/Trino: baseline files under runs/ab-... (shallow clone), optimized files under benchmarks/storage/... (rewritten) | `61ce1209f8dfe890` |
| ST04 | [ST04_obt_compacted_layout_unchanged.png](lakehouse/screenshots/ST04_obt_compacted_layout_unchanged.png) | `ST04_obt_compacted_file_layout.png` | DBeaver/Trino: obt compacted arm identical to baseline | `978f53e51f9b60d7` |
| ST04 | [ST04_obt_optimized_layout.png](lakehouse/screenshots/ST04_obt_optimized_layout.png) | `ST04_obt_optimized_file_layout.png` | DBeaver/Trino: obt Z-order arm 151 files, same sizes as baseline | `9aaae275d68f34aa` |
| ST05 | [ST05_fact_entity_lookup_baseline_overview.png](lakehouse/screenshots/ST05_fact_entity_lookup_baseline_overview.png) | `ST05_baseline_explain_overview.png` | Trino EXPLAIN ANALYZE overview, merchant lookup, baseline | `a5dc400ff20d2c55` |
| ST05 | [ST05_fact_entity_lookup_baseline_scan.png](lakehouse/screenshots/ST05_fact_entity_lookup_baseline_scan.png) | `ST05_baseline_explain_physical_input.png` | Trino scan: 669,508 rows, physical input 7.89 MB, 7 splits | `271aee4d715e1917` |
| ST05 | [ST05_fact_entity_lookup_compacted_overview.png](lakehouse/screenshots/ST05_fact_entity_lookup_compacted_overview.png) | `ST05_compacted_explain_overview.png` | Trino EXPLAIN ANALYZE overview, merchant lookup, compacted | `96a22ae31285d6af` |
| ST05 | [ST05_fact_entity_lookup_compacted_scan.png](lakehouse/screenshots/ST05_fact_entity_lookup_compacted_scan.png) | `ST05_compacted_explain_physical_input.png` | Trino scan: 669,508 rows, 7.89 MB, 7 splits | `4071442769fb3ff7` |
| ST05 | [ST05_fact_date_range_baseline_overview.png](lakehouse/screenshots/ST05_fact_date_range_baseline_overview.png) | `ST05_date_range_baseline_overview.png` | Trino overview, date range, baseline, Execution 3.06 s | `c165391448a98c58` |
| ST05 | [ST05_fact_date_range_baseline_scan.png](lakehouse/screenshots/ST05_fact_date_range_baseline_scan.png) | `ST05_date_range_baseline_scan.png` | Trino scan: 860,389 rows, 5.69 MB, 7 splits | `13aa0d7f20e3cc41` |
| ST05 | [ST05_fact_date_range_compacted_overview.png](lakehouse/screenshots/ST05_fact_date_range_compacted_overview.png) | `ST05_date_range_compacted_overview.png` | Trino overview, date range, compacted, Execution 2.39 s | `1b9f6400a796a244` |
| ST05 | [ST05_fact_date_range_compacted_scan.png](lakehouse/screenshots/ST05_fact_date_range_compacted_scan.png) | `ST05_date_range_compacted_scan.png` | Trino scan: 860,389 rows, 5.69 MB, 7 splits | `d9f1cdce2c4036ba` |
| ST05 | [ST05_obt_entity_lookup_baseline_overview.png](lakehouse/screenshots/ST05_obt_entity_lookup_baseline_overview.png) | `ST05_obt_entity_lookup_baseline_overview.png` | Trino overview, obt merchant lookup, baseline, Execution 3.82 s | `f4c15f210915838e` |
| ST05 | [ST05_obt_entity_lookup_baseline_scan.png](lakehouse/screenshots/ST05_obt_entity_lookup_baseline_scan.png) | `ST05_obt_entity_lookup_baseline_scan.png` | Trino scan: 689,163 rows, 13.38 MB, 7 splits | `441acb53ecb927a1` |
| ST05 | [ST05_obt_entity_lookup_compacted_overview.png](lakehouse/screenshots/ST05_obt_entity_lookup_compacted_overview.png) | `ST05_obt_entity_lookup_compacted_overview.png` | Trino overview, obt merchant lookup, compacted, Execution 3.82 s | `e5e89f584f454bd5` |
| ST05 | [ST05_obt_entity_lookup_compacted_scan.png](lakehouse/screenshots/ST05_obt_entity_lookup_compacted_scan.png) | `ST05_obt_entity_lookup_compacted_scan.png` | Trino scan: 689,163 rows, 13.38 MB, 7 splits | `8a4c03e24fbe0cfd` |
| ST05 | [ST05_obt_date_range_baseline_overview.png](lakehouse/screenshots/ST05_obt_date_range_baseline_overview.png) | `ST05_obt_date_range_baseline_overview.png` | Trino overview, obt date range, baseline, Execution 2.17 s | `7ed1db899a8f0b97` |
| ST05 | [ST05_obt_date_range_baseline_scan.png](lakehouse/screenshots/ST05_obt_date_range_baseline_scan.png) | `ST05_obt_date_range_baseline_scan.png` | Trino scan: 860,389 rows, 5.69 MB, 7 splits | `56a98411a3d457cf` |
| ST05 | [ST05_obt_date_range_compacted_overview.png](lakehouse/screenshots/ST05_obt_date_range_compacted_overview.png) | `ST05_obt_date_range_compacted_overview.png` | Trino overview, obt date range, compacted, Execution 2.46 s | `7a152a2d8470b010` |
| ST05 | [ST05_obt_date_range_compacted_scan.png](lakehouse/screenshots/ST05_obt_date_range_compacted_scan.png) | `ST05_obt_date_range_compacted_scan.png` | Trino scan: 860,389 rows, 5.69 MB, 7 splits | `8923d822d29f67bc` |
| ST06 | [ST06_spark_ui_optimize_queries_52_and_58.png](lakehouse/screenshots/ST06_spark_ui_optimize_queries_52_and_58.png) | `Screenshot From 2026-11-13 00-53-41.png` | Dataproc Spark UI: SQL list filtered on OPTIMIZE, queries 52 (compaction) and 58 (Z-order) via DeltaOptimizeBuilder | `c8eb251ee0e0734c` |
| ST07 | [ST07_fact_entity_lookup_optimized_overview.png](lakehouse/screenshots/ST07_fact_entity_lookup_optimized_overview.png) | `ST07_optimized_explain_overview.png` | Trino overview, merchant lookup, Z-order arm, Execution 3.26 s | `a33c3e15d664d14c` |
| ST07 | [ST07_fact_entity_lookup_optimized_scan.png](lakehouse/screenshots/ST07_fact_entity_lookup_optimized_scan.png) | `ST07_optimized_explain_physical_input.png` | Trino scan: 669,508 rows, 7.89 MB, 7 splits (same as baseline) | `86d370bb08d95c42` |
| ST07 | [ST07_fact_date_range_optimized_overview.png](lakehouse/screenshots/ST07_fact_date_range_optimized_overview.png) | `ST07_date_range_optimized_overview.png` | Trino overview, date range, Z-order arm, Execution 2.50 s | `5f69a083e5d80b10` |
| ST07 | [ST07_fact_date_range_optimized_scan.png](lakehouse/screenshots/ST07_fact_date_range_optimized_scan.png) | `ST07_date_range_optimized_scan.png` | Trino scan: 860,389 rows, 5.69 MB, 7 splits | `ed81ce38cd638c77` |
| ST07 | [ST07_obt_entity_lookup_optimized_overview.png](lakehouse/screenshots/ST07_obt_entity_lookup_optimized_overview.png) | `ST07_obt_entity_lookup_optimized_overview.png` | Trino overview, obt merchant lookup, Z-order arm, Execution 3.53 s | `b0fb5b98063d499a` |
| ST07 | [ST07_obt_entity_lookup_optimized_scan.png](lakehouse/screenshots/ST07_obt_entity_lookup_optimized_scan.png) | `ST07_obt_entity_lookup_optimized_scan.png` | Trino scan: 689,163 rows, 13.38 MB, 7 splits (same as baseline) | `99ff4ca9ef28a994` |
| ST07 | [ST07_obt_date_range_optimized_overview.png](lakehouse/screenshots/ST07_obt_date_range_optimized_overview.png) | `ST07_obt_date_range_optimized_overview.png` | Trino overview, obt date range, Z-order arm, Execution 2.29 s | `9521b634648d77de` |
| ST07 | [ST07_obt_date_range_optimized_scan.png](lakehouse/screenshots/ST07_obt_date_range_optimized_scan.png) | `ST07_obt_date_range_optimized_scan.png` | Trino scan: 860,389 rows, 5.69 MB, 7 splits | `f2853ce79655e920` |
| ST05/ST07/ST08 | [ST08_benchmark_summary.png](lakehouse/screenshots/ST08_benchmark_summary.png) | `Screenshot From 2026-11-13 00-45-41.png` | Terminal: storage_benchmark_report summary (rewrite, verdicts, pruning, recommendation) | `ab15b92645a18f9a` |
| ST09 | [ST09_fact_pruned_overview.png](lakehouse/screenshots/ST09_fact_pruned_overview.png) | `ST09_fact_transactions_pruned_overview.png` | SUPERSEDED (first run with the pre-fix script; not evidence for the corrected comparison). Trino overview, partition filter, Execution 1.99 s | `e2ea69de2c53929f` |
| ST09 | [ST09_fact_pruned_scan.png](lakehouse/screenshots/ST09_fact_pruned_scan.png) | `ST09_fact_transactions_pruned_physical_input.png` | SUPERSEDED (first run with the pre-fix script; not evidence for the corrected comparison). Trino scan: PARTITION_KEY [2026-05-25, 2026-05-31], 860,389 rows, 5.69 MB, 7 splits | `752b8af2bca7a64f` |
| ST09 | [ST09_fact_stats_only_overview.png](lakehouse/screenshots/ST09_fact_stats_only_overview.png) | `ST09_fact_transactions_stats_only_overview.png` | SUPERSEDED (first run with the pre-fix script; not evidence for the corrected comparison). Trino overview, timestamp range, Execution 4.36 s | `6d422417b67e18ad` |
| ST09 | [ST09_fact_stats_only_scan_session_tz_0700.png](lakehouse/screenshots/ST09_fact_stats_only_scan_session_tz_0700.png) | `ST09_fact_transactions_stats_only_physical_input.png` | SUPERSEDED (first run with the pre-fix script; not evidence for the corrected comparison). Trino scan: 983,526 rows, 15.31 MB, 8 splits, output 860,298; filter in Asia/Ho_Chi_Minh (session-zone caveat) | `f957b49d24396371` |
| ST09 | [ST09_fact_full_scan_overview.png](lakehouse/screenshots/ST09_fact_full_scan_overview.png) | `ST09_fact_transactions_full_scan_overview.png` | SUPERSEDED (first run with the pre-fix script; not evidence for the corrected comparison). Trino overview, format_datetime filter, Execution 35.38 s | `226a4096696efd75` |
| ST09 | [ST09_fact_full_scan_scan.png](lakehouse/screenshots/ST09_fact_full_scan_scan.png) | `ST09_fact_transactions_full_scan_physical_input.png` | SUPERSEDED (first run with the pre-fix script; not evidence for the corrected comparison). Trino scan: 18,600,000 rows, 178.81 MB, 151 splits | `0338a496bc65dbe7` |
| ST09 | [ST09_obt_pruned_overview.png](lakehouse/screenshots/ST09_obt_pruned_overview.png) | `ST09_obt_pruned_overview.png` | SUPERSEDED (first run with the pre-fix script; not evidence for the corrected comparison). Trino overview, obt partition filter, Execution 2.21 s | `f0bd5fc0bbbca7f1` |
| ST09 | [ST09_obt_pruned_scan.png](lakehouse/screenshots/ST09_obt_pruned_scan.png) | `ST09_obt_pruned_scan.png` | SUPERSEDED (first run with the pre-fix script; not evidence for the corrected comparison). Trino scan, obt partition filter, 7 splits | `e4549052e385f53c` |
| ST09 | [ST09_obt_stats_only_overview.png](lakehouse/screenshots/ST09_obt_stats_only_overview.png) | `ST09_obt_stats_only_overview.png` | SUPERSEDED (first run with the pre-fix script; not evidence for the corrected comparison). Trino overview, obt timestamp range, Execution 3.51 s | `cce101f2582d9a04` |
| ST09 | [ST09_obt_stats_only_scan_session_tz_0700.png](lakehouse/screenshots/ST09_obt_stats_only_scan_session_tz_0700.png) | `ST09_obt_stats_only_scan.png` | SUPERSEDED (first run with the pre-fix script; not evidence for the corrected comparison). Trino scan, obt timestamp range (session-zone caveat) | `78c61e8ac645a4da` |
| ST09 | [ST09_obt_full_scan_overview.png](lakehouse/screenshots/ST09_obt_full_scan_overview.png) | `ST09_obt_full_scan_overview.png` | SUPERSEDED (first run with the pre-fix script; not evidence for the corrected comparison). Trino overview, obt format_datetime filter, Execution 50.02 s | `21bbbe4af5cd8b3c` |
| ST09 | [ST09_obt_full_scan_scan.png](lakehouse/screenshots/ST09_obt_full_scan_scan.png) | `ST09_obt_full_scan_scan.png` | SUPERSEDED (first run with the pre-fix script; not evidence for the corrected comparison). Trino scan, obt full scan, 151 splits | `eedeece7f1e11e94` |
| ST09 | [ST09_utc_canonical_script_fact_blocks.png](lakehouse/screenshots/ST09_utc_canonical_script_fact_blocks.png) | `Screenshot From 2026-11-13 03-32-10.png` | DBeaver: regenerated storage_queries.sql (sha256 ed5b4812) open on the fact_transactions ST09 blocks: each side's result query followed by its EXPLAIN ANALYZE, UTC literals | `6e277d19e18bf146` |
| ST09 | [ST09_utc_canonical_script_obt_blocks.png](lakehouse/screenshots/ST09_utc_canonical_script_obt_blocks.png) | `Screenshot From 2026-11-13 03-32-25.png` | DBeaver: the same script on the obt_transaction_enriched ST09 blocks | `af7d93c01b09111e` |
| ST09 | [ST09_fact_transactions_pruned_result_utc.png](lakehouse/screenshots/ST09_fact_transactions_pruned_result_utc.png) | `ST09_fact_transactions_pruned_result_utc.png` | DBeaver/Trino, corrected UTC SQL, fact_transactions pruned result: 860,389 rows, total 894,991,837,952.66, average 1,040,217.666605 | `20e7e38c71c1508a` |
| ST09 | [ST09_fact_transactions_pruned_overview_utc.png](lakehouse/screenshots/ST09_fact_transactions_pruned_overview_utc.png) | `ST09_fact_transactions_pruned_overview_utc.png` | Trino EXPLAIN ANALYZE overview, fact_transactions pruned (UTC SQL): Execution 3.08 s | `4cf41b1e568ba245` |
| ST09 | [ST09_fact_transactions_pruned_physical_input_utc.png](lakehouse/screenshots/ST09_fact_transactions_pruned_physical_input_utc.png) | `ST09_fact_transactions_pruned_physical_input_utc.png` | Trino scan, fact_transactions pruned (UTC SQL): PARTITION_KEY [[2026-05-25, 2026-05-31]], 860,389 input rows, physical input 5.69 MB, 7 splits | `e9ec4b559d9432e1` |
| ST09 | [ST09_fact_transactions_stats_only_result_utc.png](lakehouse/screenshots/ST09_fact_transactions_stats_only_result_utc.png) | `ST09_fact_transactions_stats_only_result_utc.png` | DBeaver/Trino, corrected UTC SQL, fact_transactions stats_only result: 860,389 rows, total 894,991,837,952.66, average 1,040,217.666605 | `ee64bb847c78a458` |
| ST09 | [ST09_fact_transactions_stats_only_overview_utc.png](lakehouse/screenshots/ST09_fact_transactions_stats_only_overview_utc.png) | `ST09_fact_transactions_stats_only_overview_utc.png` | Trino EXPLAIN ANALYZE overview, fact_transactions stats_only (UTC SQL): Execution 4.93 s | `4cc1feb7ef95723f` |
| ST09 | [ST09_fact_transactions_stats_only_physical_input_utc.png](lakehouse/screenshots/ST09_fact_transactions_stats_only_physical_input_utc.png) | `ST09_fact_transactions_stats_only_physical_input_utc.png` | Trino scan, fact_transactions stats_only (UTC SQL): ScanFilterProject on the UTC timestamp range, no PARTITION_KEY, 860,389 input rows, 13.39 MB, 7 splits | `483b2860fd1648ab` |
| ST09 | [ST09_fact_transactions_full_scan_result_utc.png](lakehouse/screenshots/ST09_fact_transactions_full_scan_result_utc.png) | `ST09_fact_transactions_full_scan_result_utc.png` | DBeaver/Trino, corrected UTC SQL, fact_transactions full_scan result: 860,389 rows, total 894,991,837,952.66, average 1,040,217.666605 | `ea4ba1273511a400` |
| ST09 | [ST09_fact_transactions_full_scan_overview_utc.png](lakehouse/screenshots/ST09_fact_transactions_full_scan_overview_utc.png) | `ST09_fact_transactions_full_scan_overview_utc.png` | Trino EXPLAIN ANALYZE overview, fact_transactions full_scan (UTC SQL): Execution 29.36 s | `b9f3f3cddad6a6f7` |
| ST09 | [ST09_fact_transactions_full_scan_physical_input_utc.png](lakehouse/screenshots/ST09_fact_transactions_full_scan_physical_input_utc.png) | `ST09_fact_transactions_full_scan_physical_input_utc.png` | Trino scan, fact_transactions full_scan (UTC SQL): format_datetime(at_timezone(timestamp, UTC)) filter, 18,600,000 input rows, 95.37 % filtered, 178.81 MB, 151 splits | `8f7eb6e59a729ce6` |
| ST09 | [ST09_obt_pruned_result_utc.png](lakehouse/screenshots/ST09_obt_pruned_result_utc.png) | `ST09_obt_pruned_result_utc.png` | DBeaver/Trino, corrected UTC SQL, obt_transaction_enriched pruned result: 860,389 rows, total 894,991,837,952.66, average 1,040,217.666605 | `6d93b7909a7aed4d` |
| ST09 | [ST09_obt_pruned_overview_utc.png](lakehouse/screenshots/ST09_obt_pruned_overview_utc.png) | `ST09_obt_pruned_overview_utc.png` | Trino EXPLAIN ANALYZE overview, obt_transaction_enriched pruned (UTC SQL): Execution 3.24 s | `6352a15c06acfb5c` |
| ST09 | [ST09_obt_pruned_scan_utc.png](lakehouse/screenshots/ST09_obt_pruned_scan_utc.png) | `ST09_obt_pruned_scan_utc.png` | Trino scan, obt_transaction_enriched pruned (UTC SQL): PARTITION_KEY [[2026-05-25, 2026-05-31]], 860,389 input rows, 5.69 MB, 7 splits | `05da5db48321384f` |
| ST09 | [ST09_obt_stats_only_result_utc.png](lakehouse/screenshots/ST09_obt_stats_only_result_utc.png) | `ST09_obt_stats_only_result_utc.png` | DBeaver/Trino, corrected UTC SQL, obt_transaction_enriched stats_only result: 860,389 rows, total 894,991,837,952.66, average 1,040,217.666605 | `bb9e2c3615d42436` |
| ST09 | [ST09_obt_stats_only_overview_utc.png](lakehouse/screenshots/ST09_obt_stats_only_overview_utc.png) | `ST09_obt_stats_only_overview_utc.png` | Trino EXPLAIN ANALYZE overview, obt_transaction_enriched stats_only (UTC SQL): Execution 4.59 s | `dd8af15e616f2024` |
| ST09 | [ST09_obt_stats_only_scan_utc.png](lakehouse/screenshots/ST09_obt_stats_only_scan_utc.png) | `ST09_obt_stats_only_scan_utc.png` | Trino scan, obt_transaction_enriched stats_only (UTC SQL): UTC timestamp range, no PARTITION_KEY, 860,389 input rows, 13.39 MB, 7 splits | `e9e9e236ae63f05f` |
| ST09 | [ST09_obt_full_scan_result_utc.png](lakehouse/screenshots/ST09_obt_full_scan_result_utc.png) | `ST09_obt_full_scan_result_utc.png` | DBeaver/Trino, corrected UTC SQL, obt_transaction_enriched full_scan result: 860,389 rows, total 894,991,837,952.66, average 1,040,217.666605 | `944dd5813f1e34dd` |
| ST09 | [ST09_obt_full_scan_overview_utc.png](lakehouse/screenshots/ST09_obt_full_scan_overview_utc.png) | `ST09_obt_full_scan_overview_utc.png` | Trino EXPLAIN ANALYZE overview, obt_transaction_enriched full_scan (UTC SQL): Execution 45.85 s | `5c5a936ec6834257` |
| ST09 | [ST09_obt_full_scan_scan_utc.png](lakehouse/screenshots/ST09_obt_full_scan_scan_utc.png) | `ST09_obt_full_scan_scan_utc.png` | Trino scan, obt_transaction_enriched full_scan (UTC SQL): UTC formatted-day filter, 18,600,000 input rows, 95.37 % filtered, 178.77 MB, 151 splits | `46b4861248d232d1` |

## Warehouse screenshots

| Checkpoint | File | Original file name | What is visible | SHA-256 (first 16) |
|---|---|---|---|---|
| WH02 | [WH00_before_phase_terminal.png](warehouse/screenshots/WH00_before_phase_terminal.png) | `Screenshot From 2026-11-12 22-32-19.png` | Terminal: warehouse before phase on edai1-wh5gb-evidence-postgres, median 174.073 ms | `c46ed39e89624fb2` |
| WH01 | [WH01_provenance_table.png](warehouse/screenshots/WH01_provenance_table.png) | `WH01a_warehouse_provenance.png` | DBeaver/PostgreSQL: benchmark_provenance (3,816,574 rows, snapshot SHA-256, source ab-20261110225456 v0) | `ea1599439270b703` |
| WH01 | [WH01_table_schema.png](warehouse/screenshots/WH01_table_schema.png) | `WH01a_warehouse_table_schema.png` | DBeaver: fact_transactions columns (12) | `5444320239ddac49` |
| WH01 | [WH01_row_count.png](warehouse/screenshots/WH01_row_count.png) | `WH01b_warehouse_row_count.png` | DBeaver: 3,816,574 rows, 2026-05-01..2026-05-31, 55,623 merchants | `42826a05491e814f` |
| WH02 | [WH02_index_list_before.png](warehouse/screenshots/WH02_index_list_before.png) | `WH02a_before_index_list.png` | DBeaver: only fact_transactions_pkey | `24c636e3442b6857` |
| WH02 | [WH02_explain_before_parallel_seq_scan.png](warehouse/screenshots/WH02_explain_before_parallel_seq_scan.png) | `WH02b_before_explain_analyze.png` | DBeaver: Parallel Seq Scan, Rows Removed by Filter 1,272,191, Execution 220.804 ms | `3390b3610b2c80f0` |
| WH03 | [WH03_create_index_statistics.png](warehouse/screenshots/WH03_create_index_statistics.png) | `WH03a_create_index.png` | DBeaver Statistics: CREATE INDEX, 2.693 s | `4655d3f7397db903` |
| WH03 | [WH03_vacuum_analyze.png](warehouse/screenshots/WH03_vacuum_analyze.png) | `WH03b_vacuum_analyze.png` | DBeaver Statistics: VACUUM (ANALYZE), 0.363 s | `91837c8a69243367` |
| WH03 | [WH03_index_definition_size.png](warehouse/screenshots/WH03_index_definition_size.png) | `WH03c_index_definition_size.png` | DBeaver: pkey 277 MB, covering index 234 MB | `d69a857850f74f42` |
| WH03 | [WH03_index_phase_terminal.png](warehouse/screenshots/WH03_index_phase_terminal.png) | `Screenshot From 2026-11-12 23-22-56.png` | Terminal: index --already-created verifies the definition, 245,489,664 bytes | `4038b555d46bd193` |
| WH04 | [WH04_explain_after_index_only_scan.png](warehouse/screenshots/WH04_explain_after_index_only_scan.png) | `WH04_after_explain_analyze.png` | DBeaver: Index Only Scan, Heap Fetches 0, Execution 0.085 ms | `8e668bf38c8944ce` |
| WH05 | [WH05_after_phase_terminal_summary.png](warehouse/screenshots/WH05_after_phase_terminal_summary.png) | `WH05a_warehouse_benchmark_runtime.png` | Terminal: after phase summary.txt | `7cb472173622ee3c` |
| WH05 | [WH05_read_latency_comparison.png](warehouse/screenshots/WH05_read_latency_comparison.png) | `WH05b_read_performance_comparison.png` | DBeaver: benchmark_runs read/interleaved medians and access paths | `382a90d9970c0b46` |
| WH06 | [WH06_write_overhead_wal.png](warehouse/screenshots/WH06_write_overhead_wal.png) | `WH06a_write_overhead.png` | DBeaver: insert median 717.881 vs 895.781 ms, WAL 267 vs 312 MB | `f369bbf91d72d0fa` |
| WH06 | [WH06_twin_tables_equivalence.png](warehouse/screenshots/WH06_twin_tables_equivalence.png) | `WH06b_semantic_equivalence.png` | DBeaver: 610,275 rows each side, same_rows true | `ed5ec8a524dcd814` |
| WH06 | [WH06_query_result_verification.png](warehouse/screenshots/WH06_query_result_verification.png) | `WH06c_query_result_verification.png` | DBeaver: query result tx_count 2, total 3,783,906.04 | `6c514db3ed43d0da` |

## Raw artifacts

| File | Bytes | SHA-256 (first 16) |
|---|---:|---|
| [lakehouse/raw/evidence/comparison.csv](lakehouse/raw/evidence/comparison.csv) | 3,574 | `b997132a30251769` |
| [lakehouse/raw/evidence/comparison.json](lakehouse/raw/evidence/comparison.json) | 305,990 | `408e10259f58ed3c` |
| [lakehouse/raw/evidence/equivalence.json](lakehouse/raw/evidence/equivalence.json) | 1,024 | `321c322d6d92d535` |
| [lakehouse/raw/evidence/layout_baseline.json](lakehouse/raw/evidence/layout_baseline.json) | 67,307 | `fad50332ed2a5207` |
| [lakehouse/raw/evidence/layout_compacted.json](lakehouse/raw/evidence/layout_compacted.json) | 38,602 | `1efb2f055871a709` |
| [lakehouse/raw/evidence/layout_optimized.json](lakehouse/raw/evidence/layout_optimized.json) | 38,602 | `5124ad2af08f529f` |
| [lakehouse/raw/evidence/partition_pruning.json](lakehouse/raw/evidence/partition_pruning.json) | 28,092 | `2a5da7c1a99fd54a` |
| [lakehouse/raw/evidence/queries_baseline.json](lakehouse/raw/evidence/queries_baseline.json) | 26,818 | `9556ef16e5153562` |
| [lakehouse/raw/evidence/queries_compacted.json](lakehouse/raw/evidence/queries_compacted.json) | 26,838 | `a184c7bba258c156` |
| [lakehouse/raw/evidence/queries_optimized.json](lakehouse/raw/evidence/queries_optimized.json) | 26,834 | `afb84e2ee2bd20d1` |
| [lakehouse/raw/evidence/rewrite.json](lakehouse/raw/evidence/rewrite.json) | 8,604 | `587ec4a133822385` |
| [lakehouse/raw/layout_before.json](lakehouse/raw/layout_before.json) | 74,295 | `31b5ef9df848faa4` |
| [lakehouse/raw/plan.json](lakehouse/raw/plan.json) | 84,006 | `b30d1ecc8e8a9f3d` |
| [lakehouse/raw/storage-dry-run.txt](lakehouse/raw/storage-dry-run.txt) | 6,992 | `3514e7a704e6d82f` |
| [lakehouse/raw/storage-execute.txt](lakehouse/raw/storage-execute.txt) | 8,917 | `59042daf5ce618d7` |
| [lakehouse/raw/storage-register.txt](lakehouse/raw/storage-register.txt) | 1,968 | `00d6f8cd5d259fdd` |
| [lakehouse/raw/storage-summary.txt](lakehouse/raw/storage-summary.txt) | 2,660 | `e4b1ef8401c05dfb` |
| [lakehouse/raw/trino/storage_queries.sql](lakehouse/raw/trino/storage_queries.sql) | 13,391 | `ed5b48123ff7d01b` |
| [lakehouse/raw/trino/storage_register.sql](lakehouse/raw/trino/storage_register.sql) | 1,868 | `1c0b1556fd62b1e4` |
| [warehouse/raw/before.json](warehouse/raw/before.json) | 11,161 | `a89c5a911e0e4231` |
| [warehouse/raw/dbeaver_session.sql](warehouse/raw/dbeaver_session.sql) | 3,758 | `5c17bd49178738db` |
| [warehouse/raw/index.json](warehouse/raw/index.json) | 990 | `255cb9d91b0a6722` |
| [warehouse/raw/provenance.json](warehouse/raw/provenance.json) | 10,162 | `4d0bb9f97c21d5ea` |
| [warehouse/raw/report.json](warehouse/raw/report.json) | 32,853 | `967bfc152150f97e` |
| [warehouse/raw/summary.txt](warehouse/raw/summary.txt) | 1,022 | `76a451f8d23b4951` |

## Screenshots reviewed and not included

| Original file name | Reason |
|---|---|
| `Screenshot From 2026-11-12 21-55-06.png` | Instance setup; shows a placeholder EDAI1_WH_PG_PASSWORD value used only for config rendering; kept out of Git as a precaution |
| `Screenshot From 2026-11-12 21-56-05.png` | Same as 21-55-06 |
| `Screenshot From 2026-11-12 22-00-05.png` | Same as 21-55-06 |
| `Screenshot From 2026-11-12 22-13-14.png` | Same as 21-55-06; also a source listing |
| `Screenshot From 2026-11-12 22-25-00.png` | Shows the assistant chat panel |
| `Screenshot From 2026-11-12 22-39-00.png` | DBeaver driver-download dialog; no result |
| `Screenshot From 2026-11-12 22-41-13.png` | DBeaver connection list only |
| `Screenshot From 2026-11-12 22-42-23.png` | Duplicate of WH01_table_schema |
| `Screenshot From 2026-11-12 22-46-41.png` | Session script opened, nothing executed |
| `Screenshot From 2026-11-12 23-08-39.png` | Session script opened, nothing executed |
| `Screenshot From 2026-11-12 23-10-28.png` | Duplicate of WH01_provenance_table |
| `Screenshot From 2026-11-12 23-20-55.png` | Shows the assistant chat panel; failed 'after' before index.json existed (rerun succeeded) |
| `Screenshot From 2026-11-12 23-39-34.png` | Shows the assistant chat panel; same output as WH05_after_phase_terminal_summary |
| `Screenshot From 2026-11-12 23-53-50.png` | Shows the assistant chat panel |
| `Screenshot From 2026-11-12 23-54-42.png` | Shows the assistant chat panel; plan output is kept as raw/plan.json |
| `Screenshot From 2026-11-12 23-58-53.png` | Terraform workstream; assistant chat panel |
| `Screenshot From 2026-11-13 00-00-04.png` | Terraform workstream; assistant chat panel |
| `Screenshot From 2026-11-13 00-02-14.png` | Rubric spreadsheet (IaC rows); other workstream |
| `Screenshot From 2026-11-13 00-05-00.png` | Terraform init; assistant chat panel |
| `Screenshot From 2026-11-13 00-10-22.png` | Terraform GKE module source; other workstream |
| `Screenshot From 2026-11-13 00-12-24.png` | Terraform state listing; other workstream |
| `Screenshot From 2026-11-13 00-15-43.png` | Preflight (Delta jars); editor shows another workstream's files |
| `Screenshot From 2026-11-13 00-23-20.png` | Preflight (batch/cluster lists); editor shows another workstream's files |
| `Screenshot From 2026-11-13 00-24-24.png` | Same as 00-23-20 |
| `Screenshot From 2026-11-13 00-25-23.png` | Same as 00-23-20 |
| `Screenshot From 2026-11-13 00-36-19.png` | Batch submission; editor shows another workstream's files; raw/storage-execute.txt holds the same output |
| `Screenshot From 2026-11-13 00-58-20.png` | Duplicate of ST08_benchmark_summary plus Trino start-up |
| `Screenshot From 2026-11-13 01-04-24.png` | Query script opened, nothing executed |
| `Screenshot From 2026-11-13 01-34-33.png` | Repeat of ST05 baseline merchant lookup (01:28 run); the earlier capture is kept |
| `IAC_TF01_terraform_backend_init.png, TF02_terraform_managed_resources.png, TF03_terraform_modules_gke_code.png` | Terraform workstream, not this task |
| `Screenshot From 2026-11-13 03-21-57.png, Screenshot From 2026-11-13 03-25-16.png, FS11-03_incremental_materialization_readback.png, FS11-00B_feast_offline_snapshot_gcs.png, FS11-00C_feast_snapshot_manifest_validation.png, FS11-00D_feast_historical_retrieval.png` | Saved between the two ST09 capture sessions; Feature Store row 11 workstream (ewallet-feature-store-runtime), not this task |

## Superseded artifacts

Kept by checksum only, so that each benchmark has one active query script.

| Artifact | SHA-256 | Produced by | Why superseded |
|---|---|---|---|
| `lakehouse/raw/trino/storage_queries.sql` | `9fff09399f49cd8bf24de56cfd391acfb077e407d68a5c17dcf53cadaac25282` | ops.gcp.storage_benchmark_report trino-sql at tool commit f9be2776 | The script the 2026-11-13 ST01-ST09 captures ran. Its ST09 stats_only literal had no time zone, so DBeaver's +07:00 session shifted that window (860,298 rows instead of 860,389). Superseded by the UTC-pinned regeneration; the old content stays in Git history at commit ac80dcad. |
| `~/ewallet-evidence/edai1-docker-storage-20261112t142522/storage-trino/storage_queries.sql (owner working copy)` | `a32f466765d4a8091d4f0f1618be350baa3b9b087100995688988d57b636b03d` | the script above, edited and saved in DBeaver by the owner | Overwritten in place on 2026-11-13 by the regeneration before it was copied; not recoverable. Its only known addition, the baseline-vs-optimized file-path query visible in ST04_fact_baseline_vs_optimized_file_paths.png, is now part of the generator. |
