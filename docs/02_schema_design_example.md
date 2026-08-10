# E-Wallet Gold Zone Schema Design

## 1. Goal

### 1.1 Objective

The Gold Zone provides business-ready datasets for:

- analytical queries and BI workloads
- offline feature engineering
- downstream streaming feature integration
- ML training/scoring support

The project follows a Medallion Architecture:

Bronze -> Silver -> Gold

Bronze stores raw source data in Delta Lake.

Silver performs:
- schema standardization
- data cleaning
- deduplication
- schema evolution handling
- business validation

Gold provides:
- dimension tables
- fact tables
- an OBT table
- offline feature tables
- analytical outputs


### 1.2 Modeling Approach

The Gold Zone uses:

- Fact-Dimension modeling
- One Big Table (OBT) for denormalized analytical queries
- Feature tables for ML workloads
- Delta Lake on MinIO object storage


### 1.3 Naming Convention

Gold tables follow the naming convention:

dim_*   -> Dimension tables

fact_*  -> Fact tables

obt_*   -> One Big Table / denormalized analytical table

feat_*  -> ML feature tables

opt_*   -> analytical / optimization-oriented output


Current Gold tables:

Dimensions:
- dim_user
- dim_account
- dim_merchant
- dim_device
- dim_date

Facts:
- fact_transactions
- fact_login_events
- fact_balance_snapshot

OBT:
- obt_transaction_enriched

Feature:
- feat_user_90d

Analytical:
- opt_merchant_performance


### 1.4 Input Data Profile

The offline source contains seven datasets:

| Dataset | Main Key | Main Timestamp | Purpose |
|---|---|---|---|
| users | user_id | created_at | User information |
| accounts | account_id | created_at | E-wallet accounts |
| merchants | merchant_id | - | Merchant information |
| devices | device_id | first_seen_at | User devices |
| transactions | transaction_id | timestamp | Financial transactions |
| balance_snapshots | account_id + snapshot_date | snapshot_date | Daily balances |
| login_events | login_id | login_ts | Authentication events |

Approximate offline volume:

| Dataset | Rows |
|---|---:|
| users | 50,000 |
| accounts | 50,000 |
| merchants | 300 |
| devices | 50,000 |
| transactions (Bronze) | 408,000 |
| balance_snapshots | ~382,000 |
| login_events | ~400,000 |

Known data characteristics/problems:

- transaction duplicates are intentionally injected
- merchant traffic is skewed
- channel has schema evolution behavior
- transaction_id and login_id have high cardinality
- failed transactions are valid business events
- failed login attempts are valid events
- merchant_id can legitimately be NULL for non-payment transactions
- counterparty_account_id can legitimately be NULL for non-transfer transactions

Streaming source:

Kafka/Redpanda topic:

transactions.raw

Baseline rate:

approximately 50 events/minute

Burst traffic:

approximately 30x baseline during configured burst windows

Streaming problems include:

- burst traffic
- late-arriving events
- duplicated events


### 1.5 Assumptions

The mini-coursework runs locally with limited compute resources.

The current batch implementation uses deterministic full-refresh writes for Gold tables.

Incremental MERGE/UPSERT is considered the target production strategy but is not yet implemented.

Gold data is designed for analytics and downstream ML feature consumption.


### 1.6 Initial SLA Targets

These are coursework targets and will be updated after final measurements.

| Component | Target |
|---|---|
| Silver batch pipeline | successful scheduled execution |
| Gold batch pipeline | successful scheduled execution |
| Batch data-quality validation | must pass before downstream processing |
| Offline feature freshness | <= 60 minutes |
| Streaming feature freshness | <= 5 minutes |
| Unified feature freshness | <= 15 minutes |

Final achieved values will be reported after Airflow and streaming pipelines are completed.


---

## 2. Dimension Tables

### 2.1 dim_user

Grain:

one row per user

Primary Key:

user_id

Columns:

- user_id
- full_name
- email
- phone
- kyc_verified
- created_at

Source:

silver.users

SCD Strategy:

The current generator does not produce historical changes for user attributes.

Therefore, SCD Type 2 is not implemented for this mini-coursework.


### 2.2 dim_account

Grain:

one row per account

Primary Key:

account_id

Relationship:

user_id -> dim_user.user_id

Columns:

- account_id
- user_id
- account_type
- currency
- created_at

Source:

silver.accounts


### 2.3 dim_merchant

Grain:

one row per merchant

Primary Key:

merchant_id

Columns:

- merchant_id
- merchant_name
- category

Source:

silver.merchants


### 2.4 dim_device

Grain:

one row per device

Primary Key:

device_id

Relationship:

user_id -> dim_user.user_id

Columns:

- device_id
- user_id
- device_type
- os
- first_seen_at

Source:

silver.devices


### 2.5 dim_date

Grain:

one row per calendar date

Primary Key:

date_key

Example:

2026-08-09 -> 20260809

Columns:

- date_key
- calendar_date
- day
- month
- quarter
- year
- day_of_week
- is_weekend

Date values are generated from:

- transactions.timestamp
- login_events.login_ts
- balance_snapshots.snapshot_date


---

## 3. Fact Tables

### 3.1 fact_transactions

Grain:

one row per transaction

Primary Key:

transaction_id

Dimension relationships:

- user_id -> dim_user
- account_id -> dim_account
- device_id -> dim_device
- merchant_id -> dim_merchant
- date_key -> dim_date

Measures:

- amount
- old_balance
- new_balance

Business attributes:

- type
- status
- channel
- currency

Temporal fields:

- timestamp
- ingested_at
- event_date

Storage strategy:

partitioned by event_date

Source:

silver.transactions

Duplicate handling is performed in the Silver pipeline before Gold loading.


### 3.2 fact_login_events

Grain:

one row per login attempt

Primary Key:

login_id

Dimension relationships:

- user_id -> dim_user
- device_id -> dim_device
- date_key -> dim_date

Columns:

- login_id
- user_id
- device_id
- date_key
- is_success
- login_ts
- event_date

Storage strategy:

partitioned by event_date

Note:

is_success = false represents a valid failed authentication event and is not treated as invalid data.


### 3.3 fact_balance_snapshot

Type:

Periodic Snapshot Fact

Grain:

one row per account per day

Logical composite key:

(account_id, snapshot_date)

Dimension relationships:

- account_id -> dim_account
- date_key -> dim_date

Measure:

closing_balance

Storage strategy:

partitioned by snapshot_date

Source:

silver.balance_snapshots


---

## 4. OBT Table

### 4.1 obt_transaction_enriched

OBT means One Big Table.

Purpose:

Provide a denormalized transaction dataset for BI and analytical queries so consumers do not need to repeatedly join the transaction fact with common dimensions.

Grain:

one row per transaction

Source datasets:

fact_transactions
+ dim_user
+ dim_account
+ dim_device
+ dim_merchant
+ dim_date

Core columns include:

Transaction:
- transaction_id
- type
- status
- channel
- currency
- amount
- old_balance
- new_balance
- timestamp
- event_date

User:
- user_id
- kyc_verified

Account:
- account_id
- account_type
- account_currency

Device:
- device_id
- device_type
- os

Merchant:
- merchant_id
- merchant_name
- merchant_category

Date:
- date_key
- day_of_week
- month
- quarter
- year
- is_weekend

Join strategy:

All dimension enrichment uses LEFT JOIN.

This preserves the transaction grain even when optional dimension keys such as merchant_id are NULL.

Validation contract:

fact_transactions row count
=
obt_transaction_enriched row count

transaction_id must remain unique.


---

## 5. Refresh & Data Quality

### 5.1 Current Refresh Strategy

Current coursework implementation:

Silver -> Gold:

full refresh using Delta overwrite.

Reason:

- deterministic local execution
- simple reruns
- easier validation for coursework-scale datasets

Target production strategy:

incremental Delta MERGE/UPSERT using stable business keys.


### 5.2 Silver Data Quality

Silver validation checks include:

- non-empty tables
- required key null checks
- primary/business key uniqueness
- positive transaction amount
- non-negative balances
- valid transaction type/status
- channel normalization
- duplicate removal


### 5.3 Gold Data Quality

Gold validation checks:

Dimensions:
- non-empty
- required primary keys not NULL
- primary keys unique

Facts:
- fact primary/composite keys remain unique
- required temporal fields are present
- measures are non-negative

Feature:
- user_id unique
- features are non-negative
- failed transaction rate is between 0 and 1
- event_timestamp and created_timestamp are present

OBT:
- transaction_id unique
- required transaction fields present
- fact and OBT row counts match

Cross-layer contracts:

silver.transactions count
=
gold.fact_transactions count

silver.login_events count
=
gold.fact_login_events count

silver.balance_snapshots count
=
gold.fact_balance_snapshot count

dim_user count
=
feat_user_90d count

Fact date keys must exist in dim_date.

Validation failure causes the validation process to return failure instead of silently continuing.


---

## 6. Feature Store

### 6.1 feat_user_90d

Status:

Implemented

Grain:

one row per user

Features:

- f_user_total_transactions_90d
- f_user_avg_transaction_amount_90d
- f_user_failed_transaction_rate_90d
- f_user_distinct_merchants_90d

Metadata:

- event_timestamp
- created_timestamp

The reference timestamp is based on the latest transaction timestamp in the dataset to make offline feature computation reproducible.


### 6.2 feat_stream_5m

Status:

Planned - PyFlink phase

Expected grain:

user_id + event_timestamp

Planned streaming features:

- f_stream_transaction_count_5m
- f_stream_total_amount_5m
- f_stream_burst_activity_flag

The streaming pipeline will use Event Time and Watermarks.


### 6.3 feat_user_unified

Status:

Planned

Purpose:

Combine offline and streaming user features for downstream ML training/scoring.

Expected inputs:

feat_user_90d
+
feat_stream_5m


### 6.4 Point-in-Time Correctness

Feature data later than the reference/label timestamp must not be used when constructing training data.

Offline and streaming features should preserve:

- event_timestamp
- created_timestamp

These fields allow historical feature rows to be selected correctly and duplicated feature rows to be resolved.


---

## 7. Data Pipeline Design and Implementation

### 7.1 DP1 - Bronze Ingestion

Status:

Implemented

Flow:

Offline generator
-> source Parquet
-> Delta Lake
-> bronze-zone

Bronze tables:

- users
- accounts
- merchants
- devices
- transactions
- balance_snapshots
- login_events


### 7.2 DP2 - Bronze to Silver

Status:

Implemented and validated

Processing includes:

- schema casting
- duplicate handling
- null/business validation
- schema evolution handling
- channel normalization
- date derivation

Output:

silver-zone Delta tables


### 7.3 DP3 - Silver to Gold

Status:

Implemented and validated

Processing includes:

- dimension construction
- fact construction
- OBT construction
- offline feature engineering
- analytical aggregation

Output:

gold-zone Delta tables


### 7.4 Streaming Pipeline

Status:

Planned

Flow:

transactions.raw
-> PyFlink
-> Event Time
-> Watermark
-> duplicate handling
-> window aggregation
-> feat_stream_5m


### 7.5 Orchestration

Status:

Planned

Target dependency:

DP1
-> validate Bronze
-> DP2
-> validate Silver
-> DP3
-> validate Gold

Airflow will orchestrate existing pipeline code rather than contain transformation logic directly.


### 7.6 Monitoring and Recovery

Current implementation:

- pipeline logging
- PASS/FAIL validation gates
- row-count validation
- runtime logging

Planned:

- Airflow retries
- run metadata
- freshness checks
- failure/recovery procedure
- DataHub lineage


### 7.7 Lineage

Planned lineage:

Bronze
-> Silver
-> Gold Facts/Dimensions
-> OBT
-> Features

DataHub will later be used to visualize dataset and pipeline lineage.


---

## 8. Warehouse Optimization

### 8.1 Partitioning

Current partition strategy:

| Table | Partition |
|---|---|
| fact_transactions | event_date |
| fact_login_events | event_date |
| fact_balance_snapshot | snapshot_date |
| obt_transaction_enriched | event_date |

Small dimension tables are not partitioned.

Reason:

Partitioning large time-based facts enables partition pruning for date-range workloads while avoiding unnecessary small partitions for dimensions.


### 8.2 Spark Merchant-Skew Optimization

Status:

To be benchmarked

Planned write-up format:

Workload:

fact_transactions + dim_merchant aggregation

Bottleneck:

merchant traffic is intentionally skewed toward a small group of popular merchants

Baseline:

AQE OFF
Skew Join OFF

Optimization:

AQE / skew handling and join strategy based on actual Spark physical plan

Measurements:

- runtime
- shuffle read
- shuffle write
- stage/task duration
- physical execution plan

Trade-off:

To be documented after measurement.


### 8.3 High-Cardinality Optimization

Status:

To be benchmarked

Workload:

exact distinct counting on high-cardinality identifiers

Comparison:

countDistinct
vs
approx_count_distinct

Measurements:

- runtime
- exact result
- approximate result
- relative error
- execution behavior

Trade-off:

approximate cardinality can improve performance at the cost of estimation error.


### 8.4 Storage Optimization

Status:

To be evaluated

Planned evaluation:

- Delta file count
- small-file behavior
- partition layout
- possible compaction
- before/after scan/runtime metrics


---

## 9. Current Implementation Status

Implemented:

Bronze Delta ingestion
Silver transformation
Silver data-quality validation
Gold dimensions
Gold facts
Gold OBT
Offline feature table
Gold data-quality validation

Planned:

Spark skew benchmark
High-cardinality benchmark
Storage optimization
PyFlink streaming
Streaming features
Unified features
Airflow orchestration
DataHub lineage
Final SLA measurements
Final screenshots and benchmark evidence