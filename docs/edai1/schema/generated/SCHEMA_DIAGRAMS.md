# Schema Diagrams

Generated from the Delta logs of real runs by `ops/schema/render_schema_diagrams.py`. Do not edit by hand.

Delta declares no primary or foreign keys. **PK** and **FK** below are logical: they describe how builders and validators treat the data. Physical metadata is limited to column names, types and partition columns.

| Zone | Run | Tables | Columns |
|---|---|---:|---:|
| Bronze | `ab-20261025134342-optimized-bronze-r261025174525` | 8 | 45 |
| Silver | `ab-20261025134342-optimized-silver-r261025181923` | 8 | 47 |
| Gold core | `ab-20261025134342-optimized-gold-r261025181923` | 11 | 103 |
| Gold features | `ab-20261025134342-optimized-feat-r261026025955` | 4 | 39 |

| Artifact | Use |
|---|---|
| [bronze.mmd](bronze.mmd), [silver.mmd](silver.mmd), [gold.mmd](gold.mmd), [features.mmd](features.mmd) | Mermaid ER diagrams |
| [silver.dbml](silver.dbml), [gold.dbml](gold.dbml) | Import at dbdiagram.io |
| [gold_erd_import.sql](gold_erd_import.sql) | Load into a scratch PostgreSQL database, then reverse engineer in DBeaver |
| [ZONE_COLUMNS.md](ZONE_COLUMNS.md) | Every table of every zone with its columns |
| [GOLD_ERD.md](GOLD_ERD.md) | Gold dimension-fact ERD: grain, cardinality, DBeaver virtual keys |
| [trino_register_tables.sql](trino_register_tables.sql) | Register the observed runs in the task-owned Trino (local metastore only) |
| [trino_metadata_queries.sql](trino_metadata_queries.sql) | Read-only metadata queries for evidence E01-E08 |
| [trino_provenance_queries.sql](trino_provenance_queries.sql) | Print the GCS location of one table per zone (E08) |
| [observed_schemas.json](observed_schemas.json) | Source of these diagrams |

## Zone-to-zone column changes

Source to Bronze is one to one by table name; its column mapping is checked by `data_platform/ingestion/spark/source_to_bronze.py`. Bronze stores naive timestamps (`timestamp_ntz`) and Silver stores them as `timestamp`.

### Bronze to Silver

| Table | Added | Removed | Type changed |
|---|---|---|---|
| `accounts` | - | - | created_at |
| `balance_snapshots` | - | - | - |
| `devices` | - | - | first_seen_at |
| `fraud_labels` | - | - | label |
| `login_events` | login_date | - | login_ts |
| `merchants` | - | - | - |
| `transactions` | event_date | - | ingested_at, timestamp |
| `users` | - | - | created_at |

## Gold tables

| Table | Role | Logical key | Partition | Columns |
|---|---|---|---|---:|
| `dim_account` | Dimension | `account_id` | - | 5 |
| `dim_date` | Dimension | `date_key` | - | 8 |
| `dim_device` | Dimension | `device_id` | - | 5 |
| `dim_merchant` | Dimension | `merchant_id` | - | 3 |
| `dim_user` | Dimension | `user_id` | - | 6 |
| `fact_balance_snapshot` | Fact | `account_id, snapshot_date` | snapshot_date | 4 |
| `fact_login_events` | Fact | `login_id` | event_date | 7 |
| `fact_transactions` | Fact | `transaction_id` | event_date | 17 |
| `feat_user_90d` | Feature | `user_id` | - | 7 |
| `obt_transaction_enriched` | One big table | `transaction_id` | event_date | 32 |
| `opt_merchant_performance` | Analytics aggregate | `merchant_id` | - | 9 |
| `feat_account_behavior` | Feature | `account_id, event_timestamp` | - | 12 |
| `feat_device_behavior` | Feature | `device_id, event_timestamp` | - | 7 |
| `feat_merchant_behavior` | Feature | `merchant_id, event_timestamp` | - | 9 |
| `feat_user_behavior` | Feature | `user_id, event_timestamp` | - | 11 |

## Logical relationships

`validated` means a validator rule checks the relationship on real rows. `builder_only` means it follows from the transformation.

| Zone | From | To | Nullable | Enforcement |
|---|---|---|---|---|
| silver | `accounts.user_id` | `users.user_id` | no | validated |
| silver | `devices.user_id` | `users.user_id` | no | validated |
| silver | `transactions.user_id` | `users.user_id` | no | validated |
| silver | `transactions.account_id` | `accounts.account_id` | no | validated |
| silver | `transactions.device_id` | `devices.device_id` | no | validated |
| silver | `transactions.merchant_id` | `merchants.merchant_id` | yes | validated |
| silver | `transactions.counterparty_account_id` | `accounts.account_id` | yes | validated |
| silver | `login_events.user_id` | `users.user_id` | no | validated |
| silver | `login_events.device_id` | `devices.device_id` | no | validated |
| silver | `balance_snapshots.account_id` | `accounts.account_id` | no | validated |
| silver | `fraud_labels.transaction_id` | `transactions.transaction_id` | no | builder_only |
| gold | `dim_account.user_id` | `dim_user.user_id` | no | validated |
| gold | `dim_device.user_id` | `dim_user.user_id` | no | validated |
| gold | `fact_transactions.user_id` | `dim_user.user_id` | no | validated |
| gold | `fact_transactions.account_id` | `dim_account.account_id` | no | validated |
| gold | `fact_transactions.device_id` | `dim_device.device_id` | no | validated |
| gold | `fact_transactions.merchant_id` | `dim_merchant.merchant_id` | yes | validated |
| gold | `fact_transactions.counterparty_account_id` | `dim_account.account_id` | yes | validated |
| gold | `fact_transactions.date_key` | `dim_date.date_key` | no | validated |
| gold | `fact_login_events.user_id` | `dim_user.user_id` | no | validated |
| gold | `fact_login_events.device_id` | `dim_device.device_id` | no | validated |
| gold | `fact_login_events.date_key` | `dim_date.date_key` | no | validated |
| gold | `fact_balance_snapshot.account_id` | `dim_account.account_id` | no | validated |
| gold | `fact_balance_snapshot.date_key` | `dim_date.date_key` | no | validated |
| gold | `obt_transaction_enriched.transaction_id` | `fact_transactions.transaction_id` | no | builder_only |
| gold | `feat_user_90d.user_id` | `dim_user.user_id` | no | validated |
| gold | `opt_merchant_performance.merchant_id` | `dim_merchant.merchant_id` | no | validated |
| features | `feat_user_behavior.user_id` | `dim_user.user_id` | no | validated |
| features | `feat_account_behavior.account_id` | `dim_account.account_id` | no | validated |
| features | `feat_device_behavior.device_id` | `dim_device.device_id` | no | validated |
| features | `feat_merchant_behavior.merchant_id` | `dim_merchant.merchant_id` | no | validated |
