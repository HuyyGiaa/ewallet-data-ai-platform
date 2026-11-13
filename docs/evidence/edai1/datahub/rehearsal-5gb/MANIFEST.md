# DataHub governance evidence: 5GB campaign `ab-20261110225456` (optimized)

Evidence for [DATAHUB_GOVERNANCE.md](../../../../edai1/datahub/DATAHUB_GOVERNANCE.md), checkpoints DG01–DG08 (rubric rows 28–33). The owner captured the screenshots from a local DataHub 1.7.0.1 on 2026-11-13. Every file is a byte copy of its original; check with `sha256sum -c SHA256SUMS` in this folder. The machine-readable form is [MANIFEST.json](MANIFEST.json).

## Screenshots (19)

| Checkpoint | Row | File | Shows | SHA-256 |
|---|---:|---|---|---|
| DG01 | 28 | [DG01_dp1_datajob_provenance_5gb.png](screenshots/DG01_dp1_datajob_provenance_5gb.png) | bronze.submit_transformation properties: dag_run_id ab-20261110225456-optimized-dp1, run_id, Dataproc batch, Spark application, git_sha, source and campaign manifest SHA-256 | `7ddb490c10323335…` |
| DG01 | 28 | [DG01_dp1_source_bronze_lineage_5gb.png](screenshots/DG01_dp1_source_bronze_lineage_5gb.png) | dp1_source_to_bronze lineage: 10 Source datasets -> bronze.submit_transformation (10/10 in, 9/9 out) -> 9 Bronze tables; bronze.semantic_validation checks 9 | `9bb03decda21a01c…` |
| DG02 | 29 | [DG02_bronze_transactions_execution.png](screenshots/DG02_bronze_transactions_execution.png) | transactions.non_empty run detail: Passed, last evaluated 2026-11-11 14:20:44 +07:00 (bronze report generated_at 07:20:44Z) | `d8d12dfc2c8c2c38…` |
| DG02 | 29 | [DG02_bronze_transactions_quality_01.png](screenshots/DG02_bronze_transactions_quality_01.png) | bronze_zone.transactions Quality tab: 6 assertions (Custom 6), all passing; channel_schema, compatible_types | `a30670fd08d6ba80…` |
| DG02 | 29 | [DG02_bronze_transactions_quality_02.png](screenshots/DG02_bronze_transactions_quality_02.png) | bronze_zone.transactions assertions delta_readable and non_empty, passing | `e6a9368539505c4e…` |
| DG02 | 29 | [DG02_bronze_transactions_quality_03.png](screenshots/DG02_bronze_transactions_quality_03.png) | bronze_zone.transactions assertions persisted_data_scan and required_columns, passing | `c4809675c4af294e…` |
| DG03 | 30 | [DG03_dp2_bronze_silver_lineage_5gb.png](screenshots/DG03_dp2_bronze_silver_lineage_5gb.png) | dp2_bronze_to_curated lineage: 9 Bronze -> silver.submit_transformation (9/9 in, 9/9 out) -> 9 Silver | `213b1c85604b11e9…` |
| DG03 | 30 | [DG03_dp2_silver_transformation_provenance.png](screenshots/DG03_dp2_silver_transformation_provenance.png) | silver.submit_transformation properties: dag_run_id ab-20261110225456-optimized-dp2, run_id ...-silver-r261110225457 | `c5410f823a910061…` |
| DG04 | 30 | [DG04_dp2_gold_lineage_overview.png](screenshots/DG04_dp2_gold_lineage_overview.png) | dp2_bronze_to_curated lineage: 8 Silver -> gold.submit_transformation (8/8 in, 12/12 out) -> 12 core Gold tables | `ca47aae4f65c2383…` |
| DG04 | 30 | [DG04_gold_transformation_provenance.png](screenshots/DG04_gold_transformation_provenance.png) | gold.submit_transformation properties: dag_run_id ...-optimized-dp2, run_id ...-gold-r261110225458, Dataproc batch, Spark application | `4cc5347ab73fdea9…` |
| DG04 | 30 | [DG04_merchant_history_scd2_direct_lineage.png](screenshots/DG04_merchant_history_scd2_direct_lineage.png) | dim_merchant_scd2 direct upstreams: silver_zone.merchant_history and task gold.submit_transformation only (1-2 of 2) | `80f045b0cb4c5951…` |
| DG05 | 31 | [DG05_silver_transactions_assertions_overview.png](screenshots/DG05_silver_transactions_assertions_overview.png) | silver_zone.transactions Quality tab: 17 assertions (Custom 17), passing; account_id_exists_in_accounts FK | `2412392999ce11bf…` |
| DG05 | 31 | [DG05_silver_transactions_fk_validation_pass.png](screenshots/DG05_silver_transactions_fk_validation_pass.png) | transactions.account_id_exists_in_accounts: 0 orphan keys, Passed, last evaluated 2026-11-11 15:04:08 +07:00 (silver report 08:04:08Z) | `e188443ed10a88e4…` |
| DG06 | 31 | [DG06_gold_scd2_assertions_overview.png](screenshots/DG06_gold_scd2_assertions_overview.png) | gold_zone.dim_merchant_scd2 Quality tab: 14 assertions (Custom 14), passing | `0761c05a1ebaacd9…` |
| DG06 | 31 | [DG06_gold_scd2_non_overlapping_intervals_pass.png](screenshots/DG06_gold_scd2_non_overlapping_intervals_pass.png) | dim_merchant_scd2.contiguous_non_overlapping_intervals: Passed, last evaluated 2026-11-11 15:20:35 +07:00 (core-gold report 08:20:35Z) | `11b996b907089ed7…` |
| DG07 | 32 | [DG07_dp3_historical_feature_lineage.png](screenshots/DG07_dp3_historical_feature_lineage.png) | dp3_historical_features lineage: silver_zone.transactions and silver_zone.devices -> features.submit_transformation -> 4 feat_*_behavior tables | `0f36ff66388f2333…` |
| DG07 | 32 | [DG07_dp3_pipeline_provenance.png](screenshots/DG07_dp3_pipeline_provenance.png) | dp3_historical_features DataFlow properties: dag_run_id ab-20261110225456-optimized-dp3, benchmark, git_sha, manifests, spark_profile optimized | `aa1c454f1afdaa8f…` |
| DG08 | 33 | [DG08_feature_quality_execution_pass.png](screenshots/DG08_feature_quality_execution_pass.png) | dp3_features.created_shared_by_run run detail: Passed, last evaluated 2026-11-11 15:52:45 +07:00 (features report 08:52:45Z) | `2f58d38f01d4168e…` |
| DG08 | 33 | [DG08_feature_quality_overview.png](screenshots/DG08_feature_quality_overview.png) | gold_zone.feat_user_behavior Quality tab: 14 assertions, including cross-table dp3_features.created_shared_by_run | `8d9c9eb3725ed464…` |

## Raw outputs (5)

| File | Content | SHA-256 |
|---|---|---|
| [03_lineage_dry_run.json](raw/03_lineage_dry_run.json) | Lineage dry run: 44 datasets, 3 DataFlows, 8 DataJobs, 34 upstreamLineage aspects, 141 records, schema_source observed | `2668b4423886b605…` |
| [04_lineage_publish.json](raw/04_lineage_publish.json) | Lineage publication: PUBLISH_WITH_READBACK, 141 published, 141 readback MATCH | `572270ee74a9c34f…` |
| [05_validation_artifact.json](raw/05_validation_artifact.json) | datahub-quality-validation-v1 artifact: 263 outcomes, 263 PASS (DP1 54, DP2 153, DP3 56) | `692b18c06012038a…` |
| [06_assertions_dry_run.json](raw/06_assertions_dry_run.json) | Assertion dry run: 263 definitions, 263 runs, 0 not published | `840f85f332c9d313…` |
| [07_assertions_publish.json](raw/07_assertions_publish.json) | Assertion publication: 263 definitions, 263 run events, 0 skipped, 263 verified records with raw API readback | `55a9b52f6b7e6b6a…` |

## Not integrated (11)

Captures saved in the same session that are drafts or terminal views. They are not evidence of a checkpoint, and several show personal browser tabs or the host prompt. Their SHA-256 values are in MANIFEST.json.

| Source file | Reason |
|---|---|
| `Screenshot From 2026-11-13 13-39-31.png` | draft DP1 task view captured before the final DG01 image; shows browser tabs unrelated to the evidence |
| `Screenshot From 2026-11-13 13-43-13.png` | draft DP1 lineage capture superseded by DG01_dp1_source_bronze_lineage_5gb.png |
| `Screenshot From 2026-11-13 13-47-55.png` | terminal during the GraphQL-timeout incident and the artifact/dry-run commands; outputs are kept as raw JSON instead; shows the user's host prompt |
| `Screenshot From 2026-11-13 14-01-48.png` | terminal jq inspection of the assertion outputs; superseded by raw/0[5-7]_*.json; shows the user's host prompt |
| `Screenshot From 2026-11-13 14-03-45.png` | terminal jq inspection of the assertion outputs; superseded by raw JSON; shows the user's host prompt |
| `Screenshot From 2026-11-13 14-32-28.png` | terminal of the assertion publish command; superseded by raw/07_assertions_publish.json; shows the user's host prompt |
| `Screenshot From 2026-11-13 14-33-44.png` | terminal jq summary of the assertion publication (263/263/0); superseded by raw/07_assertions_publish.json; shows the user's host prompt |
| `Screenshot From 2026-11-13 14-36-40.png` | draft of DG02 with personal browser tabs; superseded by DG02_bronze_transactions_quality_01.png |
| `Screenshot From 2026-11-13 14-37-03.png` | draft of DG02 with personal browser tabs; superseded by DG02_bronze_transactions_quality_02.png |
| `Screenshot From 2026-11-13 14-37-11.png` | draft of DG02 with personal browser tabs; superseded by DG02_bronze_transactions_quality_03.png |
| `Screenshot From 2026-11-13 15-08-48.png` | draft DP3 task view without lineage edges; superseded by DG07_dp3_historical_feature_lineage.png |
