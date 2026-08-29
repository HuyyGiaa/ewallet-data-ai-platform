**# E-Wallet Gold Zone Schema Design**

**## 1. Goal**

**### 1.1 Objective**

The Gold Zone provides business-ready datasets for:

\- analytical queries and BI workloads

\- offline feature engineering

\- downstream streaming feature integration

\- ML training/scoring support

The project follows a Medallion Architecture:

Bronze -> Silver -> Gold

Bronze stores raw source data in Delta Lake.

Silver performs:

\- schema standardization

\- data cleaning

\- deduplication

\- channel normalization after Bronze schema evolution

\- business validation

Gold provides:

\- dimension tables

\- fact tables

\- an OBT table

\- offline feature tables

\- analytical outputs



**### 1.2 Modeling Approach**

The Gold Zone uses:

\- Fact-Dimension modeling

\- One Big Table (OBT) for denormalized analytical queries

\- Feature tables for ML workloads

\- Delta Lake on MinIO object storage



**### 1.3 Naming Convention**

Gold tables follow the naming convention:

dim\_\*   -> Dimension tables

fact\_\*  -> Fact tables

obt\_\*   -> One Big Table / denormalized analytical table

feat\_\*  -> ML feature tables

opt\_\*   -> analytical / optimization-oriented output



Current Gold tables:

Dimensions:

\- dim\_user

\- dim\_account

\- dim\_merchant

\- dim\_device

\- dim\_date

Facts:

\- fact\_transactions

\- fact\_login\_events

\- fact\_balance\_snapshot

OBT:

\- obt\_transaction\_enriched

Feature:

\- feat\_user\_90d

Analytical:

\- opt\_merchant\_performance



**### 1.4 Input Data Profile**

The offline source contains seven datasets:

\| Dataset | Main Key | Main Timestamp | Purpose |

\|---|---|---|---|

\| users | user\_id | created\_at | User information |

\| accounts | account\_id | created\_at | E-wallet accounts |

\| merchants | merchant\_id | - | Merchant information |

\| devices | device\_id | first\_seen\_at | User devices |

\| transactions | transaction\_id | timestamp | Financial transactions |

\| balance\_snapshots | account\_id + snapshot\_date | snapshot\_date | Daily balances |

\| login\_events | login\_id | login\_ts | Authentication events |

Final offline volume:

\| Dataset | Rows |

\|---|---:|

\| users | 500,000 |

\| accounts | 500,000 |

\| merchants | 300 |

\| devices | 500,000 |

\| transactions (Bronze) | 4,080,000 |

\| balance\_snapshots | 3,895,224 |

\| login\_events | 3,997,362 |

Known data characteristics/problems:

\- transaction duplicates are intentionally injected

\- merchant traffic is skewed

\- channel has schema evolution behavior

\- transaction\_id and login\_id have high cardinality

\- failed transactions are valid business events

\- failed login attempts are valid events

\- merchant\_id can legitimately be NULL for non-payment transactions

\- counterparty\_account\_id can legitimately be NULL for non-transfer transactions

Streaming source:

Kafka/Redpanda topic:

transactions.raw

Baseline rate:

approximately 50 events/minute

Burst traffic:

approximately 30x baseline during configured burst windows

Streaming problems include:

\- burst traffic

\- late-arriving events

\- duplicated events



**### 1.5 Assumptions**

The mini-coursework runs locally with limited compute resources.

The current batch implementation uses deterministic full-refresh writes for Gold tables.

Incremental MERGE/UPSERT is considered the target production strategy but is not yet implemented.

Gold data is designed for analytics and downstream ML feature consumption.



**### 1.6 Initial SLA Targets**

These are coursework design targets used to guide the implementation.

\| Component | Target |

\|---|---|

\| Silver batch pipeline | successful scheduled execution |

\| Gold batch pipeline | successful scheduled execution |

\| Batch data-quality validation | must pass before downstream processing |

\| Offline feature freshness | <= 60 minutes |

\| Streaming feature freshness | <= 5 minutes |

\| Unified feature freshness | <= 15 minutes |

The final implementation demonstrated successful scheduled batch execution,
validation gates and 5-minute streaming windows. Unified feature refresh remains
a planned extension.



\---

**## 2. Dimension Tables**

**### 2.1 dim\_user**

Grain:

one row per user

Primary Key:

user\_id

Columns:

\- user\_id

\- full\_name

\- email

\- phone

\- kyc\_verified

\- created\_at

Source:

silver.users

SCD Strategy:

The current generator does not produce historical changes for user attributes.

Therefore, SCD Type 2 is not implemented for this mini-coursework.



**### 2.2 dim\_account**

Grain:

one row per account

Primary Key:

account\_id

Relationship:

user\_id -> dim\_user.user\_id

Columns:

\- account\_id

\- user\_id

\- account\_type

\- currency

\- created\_at

Source:

silver.accounts



**### 2.3 dim\_merchant**

Grain:

one row per merchant

Primary Key:

merchant\_id

Columns:

\- merchant\_id

\- merchant\_name

\- category

Source:

silver.merchants



**### 2.4 dim\_device**

Grain:

one row per device

Primary Key:

device\_id

Relationship:

user\_id -> dim\_user.user\_id

Columns:

\- device\_id

\- user\_id

\- device\_type

\- os

\- first\_seen\_at

Source:

silver.devices



**### 2.5 dim\_date**

Grain:

one row per calendar date

Primary Key:

date\_key

Example:

2026-08-09 -> 20260809

Columns:

\- date\_key

\- calendar\_date

\- day

\- month

\- quarter

\- year

\- day\_of\_week

\- is\_weekend

Date values are generated from:

\- transactions.timestamp

\- login\_events.login\_ts

\- balance\_snapshots.snapshot\_date



\---

**## 3. Fact Tables**

**### 3.1 fact\_transactions**

Grain:

one row per transaction

Primary Key:

transaction\_id

Dimension relationships:

\- user\_id -> dim\_user

\- account\_id -> dim\_account

\- device\_id -> dim\_device

\- merchant\_id -> dim\_merchant

\- date\_key -> dim\_date

Measures:

\- amount

\- old\_balance

\- new\_balance

Business attributes:

\- type

\- status

\- channel

\- currency

Temporal fields:

\- timestamp

\- ingested\_at

\- event\_date

Storage strategy:

partitioned by event\_date

Source:

silver.transactions

Duplicate handling is performed in the Silver pipeline before Gold loading.



**### 3.2 fact\_login\_events**

Grain:

one row per login attempt

Primary Key:

login\_id

Dimension relationships:

\- user\_id -> dim\_user

\- device\_id -> dim\_device

\- date\_key -> dim\_date

Columns:

\- login\_id

\- user\_id

\- device\_id

\- date\_key

\- is\_success

\- login\_ts

\- event\_date

Storage strategy:

partitioned by event\_date

Note:

is\_success = false represents a valid failed authentication event and is not treated as invalid data.



**### 3.3 fact\_balance\_snapshot**

Type:

Periodic Snapshot Fact

Grain:

one row per account per day

Logical composite key:

(account\_id, snapshot\_date)

Dimension relationships:

\- account\_id -> dim\_account

\- date\_key -> dim\_date

Measure:

closing\_balance

Storage strategy:

partitioned by snapshot\_date

Source:

silver.balance\_snapshots



\---

**## 4. OBT Table**

**### 4.1 obt\_transaction\_enriched**

OBT means One Big Table.

Purpose:

Provide a denormalized transaction dataset for BI and analytical queries so consumers do not need to repeatedly join the transaction fact with common dimensions.

Grain:

one row per transaction

Source datasets:

fact\_transactions

\+ dim\_user

\+ dim\_account

\+ dim\_device

\+ dim\_merchant

\+ dim\_date

Core columns include:

Transaction:

\- transaction\_id

\- type

\- status

\- channel

\- currency

\- amount

\- old\_balance

\- new\_balance

\- timestamp

\- event\_date

User:

\- user\_id

\- kyc\_verified

Account:

\- account\_id

\- account\_type

\- account\_currency

Device:

\- device\_id

\- device\_type

\- os

Merchant:

\- merchant\_id

\- merchant\_name

\- merchant\_category

Date:

\- date\_key

\- day\_of\_week

\- month

\- quarter

\- year

\- is\_weekend

Join strategy:

All dimension enrichment uses LEFT JOIN.

This preserves the transaction grain even when optional dimension keys such as merchant\_id are NULL.

Validation contract:

fact\_transactions row count

\=

obt\_transaction\_enriched row count

transaction\_id must remain unique.



\---

**## 5. Refresh & Data Quality**

**### 5.1 Current Refresh Strategy**

Current coursework implementation:

Silver -> Gold:

full refresh using Delta overwrite.

Reason:

\- deterministic local execution

\- simple reruns

\- easier validation for coursework-scale datasets

Target production strategy:

incremental Delta MERGE/UPSERT using stable business keys.



**### 5.2 Silver Data Quality**

Silver validation checks include:

\- non-empty tables

\- required key null checks

\- primary/business key uniqueness

\- positive transaction amount

\- non-negative balances

\- valid transaction type/status

\- channel normalization

\- duplicate removal



**### 5.3 Gold Data Quality**

Gold validation checks:

Dimensions:

\- non-empty

\- required primary keys not NULL

\- primary keys unique

Facts:

\- fact primary/composite keys remain unique

\- required temporal fields are present

\- measures are non-negative

Feature:

\- user\_id unique

\- features are non-negative

\- failed transaction rate is between 0 and 1

\- event\_timestamp and created\_timestamp are present

OBT:

\- transaction\_id unique

\- required transaction fields present

\- fact and OBT row counts match

Cross-layer contracts:

silver.transactions count

\=

gold.fact\_transactions count

silver.login\_events count

\=

gold.fact\_login\_events count

silver.balance\_snapshots count

\=

gold.fact\_balance\_snapshot count

dim\_user count

\=

feat\_user\_90d count

Fact date keys must exist in dim\_date.

Validation failure causes the validation process to return failure instead of silently continuing.



\---

**## 6. Feature Store**

**### 6.1 feat\_user\_90d**

Status:

Implemented

Grain:

one row per user

Features:

\- f\_user\_total\_transactions\_90d

\- f\_user\_avg\_transaction\_amount\_90d

\- f\_user\_failed\_transaction\_rate\_90d

\- f\_user\_distinct\_merchants\_90d

Metadata:

\- event\_timestamp

\- created\_timestamp

The reference timestamp is based on the latest transaction timestamp in the dataset to make offline feature computation reproducible.



**### 6.2 feat\_stream\_5m**

Status:

Implemented as Flink streaming output

Grain:

user\_id + 5-minute event-time window

Streaming features:

\- f\_stream\_transaction\_count\_5m

\- f\_stream\_total\_amount\_5m

The streaming pipeline uses Event Time, Watermarks and allowed lateness.

Burst activity is demonstrated separately by the processing-time burst monitor.



**### 6.3 feat\_user\_unified**

Status:

Planned

Purpose:

Combine offline and streaming user features for downstream ML training/scoring.

Expected inputs:

feat\_user\_90d

\+

feat\_stream\_5m



**### 6.4 Point-in-Time Correctness**

Feature data later than the reference/label timestamp must not be used when constructing training data.

Offline and streaming features should preserve:

\- event\_timestamp

\- created\_timestamp

These fields allow historical feature rows to be selected correctly and duplicated feature rows to be resolved.



\---

**## 7. Data Pipeline Design and Implementation**

**### 7.1 DP1 - Bronze Ingestion**

Status:

Implemented

Flow:

Offline generator

-> source Parquet

-> Delta Lake

-> bronze-zone

Bronze tables:

\- users

\- accounts

\- merchants

\- devices

\- transactions

\- balance\_snapshots

\- login\_events

For transactions, DP1 writes schema V1 first and appends schema V2 with
schema merging so the Bronze Delta table evolves to include `channel`.



**### 7.2 DP2 - Bronze to Silver**

Status:

Implemented and validated

Processing includes:

\- schema casting

\- duplicate handling

\- null/business validation

\- channel normalization

\- date derivation

Output:

silver-zone Delta tables



**### 7.3 DP3 - Silver to Gold**

Status:

Implemented and validated

Processing includes:

\- dimension construction

\- fact construction

\- OBT construction

\- offline feature engineering

\- analytical aggregation

Output:

gold-zone Delta tables



**### 7.4 Streaming Pipeline**

Status:

Implemented and validated

Flow:

transactions.raw

-> PyFlink

-> transaction\_id keyed deduplication with state TTL

-> Event Time

-> Watermark

-> late-event handling

-> 5-minute window aggregation

-> FEATURE / DUPLICATE / LATE outputs

A separate processing-time monitor is used for burst detection.



**### 7.5 Orchestration**

Status:

Implemented and validated

Final dependency:

DP1

-> validate Bronze

-> DP2

-> validate Silver

-> DP3

-> validate Gold

Airflow orchestrates the existing pipeline code rather than containing transformation logic directly.

The final 500,000-user DAG completed all six tasks successfully.



**### 7.6 Monitoring and Recovery**

Current implementation:

\- pipeline logging

\- PASS/FAIL validation gates

\- row-count validation

\- runtime logging

\- Airflow retries

\- Airflow run/task status and logs

\- DataHub lineage

Planned:

\- formal freshness checks

\- unified feature freshness monitoring



**### 7.7 Lineage**

Status:

Implemented

Final lineage:

Bronze

-> Silver

-> Gold Fact

-> OBT

-> Features

DataHub is used to visualize the implemented transaction dataset lineage.



\---

**## 8. Warehouse Optimization**

**### 8.1 Partitioning**

Current partition strategy:

\| Table | Partition |

\|---|---|

\| fact\_transactions | event\_date |

\| fact\_login\_events | event\_date |

\| fact\_balance\_snapshot | snapshot\_date |

\| obt\_transaction\_enriched | event\_date |

Small dimension tables are not partitioned.

Reason:

Partitioning large time-based facts enables partition pruning for date-range workloads while avoiding unnecessary small partitions for dimensions.



**### 8.2 Spark Merchant-Skew Optimization**

Status:

Benchmarked

Workload:

fact\_transactions + dim\_merchant aggregation

Bottleneck:

merchant traffic is intentionally skewed toward a small group of popular merchants

Baseline:

AQE OFF

Skew Join OFF

Optimization:

AQE ON

Skew Join ON

Measurements:

\- top 5% merchants receive 80.02% of merchant traffic

\- baseline median runtime: 13.3611 s

\- optimized median runtime: 13.6310 s

\- baseline average runtime: 13.5166 s

\- optimized average runtime: 13.2560 s

\- optimized plan uses AdaptiveSparkPlan and SortMergeJoin

Trade-off:

AQE and skew join did not provide a significant performance improvement for
this workload because aggregation substantially reduced the data before the join.



**### 8.3 High-Cardinality Optimization**

Status:

Benchmarked

Workload:

exact distinct counting on high-cardinality identifiers

Comparison:

countDistinct

vs

approx\_count\_distinct

Measurements:

\- exact distinct count: 4,000,000

\- approximate distinct count: 4,075,598

\- exact median runtime: 15.1544 s

\- approximate median runtime: 13.1332 s

\- observed relative error: approximately 1.89%

Trade-off:

approximate cardinality reduced median runtime by approximately 13.34% at the
cost of a small estimation error.



**### 8.4 Storage Optimization**

Status:

Implemented and benchmarked

Evaluation:

\- 151 daily partitions

\- active files reduced from 2,188 to 151

\- seven-day scan files reduced from 102 to 7

\- median query runtime reduced from 0.2190 s to 0.1079 s

\- Trino `OPTIMIZE` used for Delta small-file compaction

No `VACUUM` operation was performed so Delta history remains available.



\---

**## 9. Current Implementation Status**

Implemented:

Bronze Delta ingestion

Bronze transaction schema evolution

Silver transformation

Silver data-quality validation

Gold dimensions

Gold facts

Gold OBT

Offline feature table

Gold data-quality validation

Spark merchant-skew benchmark

High-cardinality benchmark

Storage optimization

PyFlink streaming pipeline

Streaming 5-minute feature output

Airflow orchestration

DataHub lineage

Final screenshots and benchmark evidence

Planned:

Unified offline + streaming feature table

Incremental Delta MERGE/UPSERT production strategy

Formal unified feature freshness monitoring
