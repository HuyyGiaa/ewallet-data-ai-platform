# Schema Design evidence: 5GB campaign `ab-20261110225456` (optimized)

Evidence for [SCHEMA_DESIGN.md](../../../../edai1/schema/SCHEMA_DESIGN.md), checkpoints SC01–SC08 (rubric rows 34–38). Campaign `rehearsal-5gb-v1`, profile `optimized`, bucket `ewallet-data-ai-platform-hg26-rehearsal-5gb-data`. Every file is a byte copy of its original; check with `sha256sum -c SHA256SUMS` in this folder. The machine-readable form is [MANIFEST.json](MANIFEST.json).

## Screenshots (9)

| Checkpoint | Rows | File | Context | Shows | SHA-256 |
|---|---|---|---|---|---|
| SC01 | 34 | [SC01_physical_schema_inventory_5gb.png](screenshots/SC01_physical_schema_inventory_5gb.png) | DBeaver 26.1.3 on the isolated 5GB Trino metastore | Per-zone table count over hive/delta information_schema: Source 10, Bronze 9, Silver 9, Gold + Features 16, TOTAL 44 (fetched 2026-11-13 17:34:54 +07:00). The navigator tree still shows a cached storage_bench node from the historical metastore; the 5GB metastore has no such schema (runtime/09_verify_viewer.txt). | `ee35681143ee2142…` |
| SC02 | 34, 37 | [SC02_silver_erd_5gb.png](screenshots/SC02_silver_erd_5gb.png) | dbdiagram.io import of data_platform/schema/generated/silver.dbml | Silver logical ERD: 9 tables with their columns, logical keys, NOT NULL markers and the 12 references. | `42c82117836fb1f8…` |
| SC03 | 34, 37 | [SC03_gold_erd_5gb.png](screenshots/SC03_gold_erd_5gb.png) | dbdiagram.io import of data_platform/schema/generated/gold.dbml | Gold logical ERD: 12 core Gold tables and 4 historical feature tables with their references (21). | `fdf7949c35f8bc71…` |
| SC04 | 37 | [SC04_gold_fact_dimension_relationships.png](screenshots/SC04_gold_fact_dimension_relationships.png) | dbdiagram.io, gold.dbml with fact_transactions selected | fact_transactions and its highlighted many-to-one references to dim_user, dim_account (account_id and counterparty_account_id), dim_merchant, dim_device and dim_date, and the one-to-one reference from obt_transaction_enriched. | `6491a38cb0e5dbb0…` |
| SC05 | 35 | [SC05_scd2_physical_schema.png](screenshots/SC05_scd2_physical_schema.png) | DBeaver, DESCRIBE delta.gold_zone.dim_merchant_scd2 | Physical columns and Trino types of the SCD2 dimension: merchant_sk, merchant_id, merchant_name, category (varchar), valid_from_ts, valid_to_ts (timestamp(3) with time zone), is_current (boolean). | `548350fbb6c4e5e8…` |
| SC06 | 35 | [SC06_scd2_runtime_validation.png](screenshots/SC06_scd2_runtime_validation.png) | DBeaver, aggregate query over delta.gold_zone.dim_merchant_scd2 | 78,294 versions, 55,800 merchants, 55,800 current versions, 22,494 historical versions, 55,800 current versions with valid_to_ts IS NULL (fetched 17:38:20 +07:00). | `2dba41d6b468c486…` |
| SC07 | 36 | [SC07_historical_feature_timestamp_schema.png](screenshots/SC07_historical_feature_timestamp_schema.png) | DBeaver, delta.information_schema.columns | event_timestamp and created on the four DP3 feature tables, all 8 columns timestamp(3) with time zone. | `425bdd6d7a75a812…` |
| SC08 | 38 | [SC08_schema_naming_conventions.png](screenshots/SC08_schema_naming_conventions.png) | DBeaver, prefix classification over the 44 registered tables | Result: TOTAL 44; Source 10, Bronze 9, Silver 9; gold_zone Dimension 6, Fact 3, Feature 5, OBT 1, Analytical Output 1; no Unclassified row (fetched 17:42:17 +07:00). Shows the inventory part of the query. | `1016fac0dbd7f9bb…` |
| SC08 | 38 | [SC08_schema_naming_conventions_detail.png](screenshots/SC08_schema_naming_conventions_detail.png) | Same query, scrolled | The classification CASE (zone for Source/Bronze/Silver; dim_, fact_, feat_, obt_, opt_ prefixes for Gold; ELSE Unclassified) with the same 9-row result. | `2c59c9b7af7dbf85…` |

## Runtime artifacts (15)

Genuine outputs of the isolated 5GB Trino registration and verification, run on 2026-11-13.

| File | Content | SHA-256 |
|---|---|---|
| [08_register.txt](runtime/08_register.txt) | registrar run of trino_register_tables.sql into the empty 5GB metastore: 4 CREATE SCHEMA, 34 CALL register_table, 10 CREATE TABLE, exit 0 | `d8c5b72c71970399…` |
| [08_register_started_at.txt](runtime/08_register_started_at.txt) | UTC start time of the registration | `510b9788fdc5b467…` |
| [09_verify_viewer.txt](runtime/09_verify_viewer.txt) | viewer verification: per-zone counts 10/9/9/16, total 44, the four previously missing tables, SCD2 and Silver merchant_history reads, one representative read per zone | `5c67f568d3fe5366…` |
| [09a_verify_viewer_first_attempt.txt](runtime/09a_verify_viewer_first_attempt.txt) | first viewer verification run; stopped at SHOW CREATE TABLE (Access Denied for viewer, by design); kept unchanged | `4867b3d23929fb48…` |
| [10_provenance_registrar.txt](runtime/10_provenance_registrar.txt) | registrar SHOW CREATE TABLE of one table per zone, showing the 5GB GCS locations | `94ceabf379bacab1…` |
| [11_metastore_locations_5gb.tsv](runtime/11_metastore_locations_5gb.tsv) | location of all 44 registrations read from the 5GB file metastore; equal to the generated registration SQL | `4d023fa2edf0796f…` |
| [12_viewer_readonly_probe.txt](runtime/12_viewer_readonly_probe.txt) | viewer denied CREATE SCHEMA (delta, hive) and CALL register_table; table count unchanged | `95300661aeab12fe…` |
| [SWITCH_METASTORE.md](runtime/SWITCH_METASTORE.md) | procedure used to switch between the 5GB and historical metastores (the override is now infra/docker/schema-viz/compose.metastore-5gb.yml) | `d0eb59f594ccc40a…` |
| [old_metastore_files_sha256_after.txt](runtime/old_metastore_files_sha256_after.txt) | SHA-256 of the same 97 files after the 5GB registration, read through a read-only mount; identical | `2a068268ca88d8b0…` |
| [old_metastore_files_sha256_before.txt](runtime/old_metastore_files_sha256_before.txt) | SHA-256 of the 97 historical metastore files before the switch | `2a068268ca88d8b0…` |
| [old_metastore_inventory_before.tsv](runtime/old_metastore_inventory_before.tsv) | historical metastore before the switch: 40 100GB tables plus 6 storage_bench tables | `7e12e78f43190af9…` |
| [preflight/gcs_missing_locations.txt](runtime/preflight/gcs_missing_locations.txt) | preflight: read-only listing of the four previously missing 5GB locations (Delta logs and Parquet files present) | `321c239ff7e00d55…` |
| [preflight/metastore_delta_locations_before.tsv](runtime/preflight/metastore_delta_locations_before.tsv) | preflight: the historical metastore's Delta registrations pointed to ab-20261025134342 (100GB) | `c9d3ed9c1781c153…` |
| [register_sql_sha256.txt](runtime/register_sql_sha256.txt) | SHA-256 of trino_register_tables.sql as executed (before the generator moved; only its header comment differs from the current file) | `e0ad6f265e26d054…` |
| [verify_5gb_metastore.sql](runtime/verify_5gb_metastore.sql) | the verification SQL executed for 09_verify_viewer.txt | `1293fc619a120573…` |

## Not integrated (13)

Unnamed captures from the same session. They are diagnostics, drafts or captures of other work, not checkpoint evidence. Their SHA-256 values are in MANIFEST.json.

| Source file | Reason |
|---|---|
| `Screenshot From 2026-11-13 15-41-44.png` | terminal (VS Code) during the DataHub stop and the generator check; not a checkpoint |
| `Screenshot From 2026-11-13 15-44-47.png` | Docker/Storage storage_queries.sql in DBeaver; another workstream |
| `Screenshot From 2026-11-13 15-46-55.png` | SHOW TABLES on the historical metastore (8 Silver tables) during the missing-table diagnosis |
| `Screenshot From 2026-11-13 15-47-07.png` | same diagnosis; SHOW CREATE TABLE denied for viewer |
| `Screenshot From 2026-11-13 15-47-25.png` | same diagnosis on the historical metastore |
| `Screenshot From 2026-11-13 15-49-07.png` | same diagnosis, Gold tables of the historical metastore |
| `Screenshot From 2026-11-13 15-49-17.png` | same diagnosis, Source tables of the historical metastore |
| `Screenshot From 2026-11-13 15-49-27.png` | terminal inspection of the registration SQL |
| `Screenshot From 2026-11-13 15-49-46.png` | VS Code window with an assistant chat; not evidence |
| `Screenshot From 2026-11-13 15-53-41.png` | VS Code window with an assistant chat and an unrelated Feast terminal |
| `Screenshot From 2026-11-13 15-54-59.png` | VS Code window with an assistant chat and an unrelated Feast terminal |
| `Screenshot From 2026-11-13 17-31-10.png` | draft SHOW TABLES on the 5GB metastore before SC01; superseded by SC01 |
| `Screenshot From 2026-11-13 17-31-18.png` | draft SHOW TABLES on the 5GB metastore before SC01; superseded by SC01 |
