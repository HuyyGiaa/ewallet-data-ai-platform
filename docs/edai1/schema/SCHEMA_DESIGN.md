# Schema Design

## 1. Introduction

The schema is the contract that connects ingestion, data quality, analytics and point-in-time feature computation. This report describes:

- the tables of every zone and their roles;
- how schema evolution is handled;
- the dimension–fact model and the naming conventions;
- two schema extensions that have been prototyped: a Type 2 slowly changing merchant dimension, and a `created` materialisation timestamp on the feature tables.

## 2. Medallion data architecture

The canonical inventory contains 39 logical tables: 8 Source, 8 Bronze, 8 Silver and 15 Gold. DataHub represents 32 physical datasets because the eight logical Source tables share one immutable manifest dataset.

```mermaid
flowchart LR
    SRC[Source\n8 logical Parquet datasets\nimmutable manifest] --> BR[Bronze\n8 source-shaped Delta tables]
    BR --> SI[Silver\n8 cleansed/conformed Delta tables]
    SI --> DIM[Gold dimensions\n5 tables]
    SI --> FACT[Gold facts\n3 tables]
    DIM --> OBT[Gold OBT\n1 table]
    FACT --> OBT
    DIM --> AGG[Gold aggregate/feature\n2 tables]
    FACT --> AGG
    SI --> PIT[Historical PIT features\n4 tables]
```

- **Source:** deterministic, immutable Parquet shards plus a completion manifest.
- **Bronze:** source-shaped Delta; preserves raw business content while reconciling transaction V1/V2 physical files.
- **Silver:** cleans, normalizes and deduplicates; transaction-like tables gain operational date columns used for partitioning.
- **Core Gold:** full-refresh dimensions, facts, a denormalized OBT, a 90-day feature aggregate and merchant analytics.
- **Historical features:** four event-time snapshots with strict `[T-window, T)` semantics.

Inventory source: `data_platform/contracts/schema_inventory.py`; canonical lists: `data_platform/storage/scripts/config.py`.

## 3. Table catalog

### 3.1 Source, Bronze and Silver

Each row below represents three verified logical tables with the same business name: `source.<name> → bronze.<name> → silver.<name>`. Source and Bronze preserve source grain; Silver enforces the stated logical key and normalized types. Cloud writes are run-scoped and immutable.

| Table path across zones | Business purpose | Grain and key | Important columns / evolution | Silver partition | Downstream |
|---|---|---|---|---|---|
| `users` | Customer identity and KYC profile | One row per `user_id` | `full_name`, contact fields, `kyc_verified`, `created_at` | None | `dim_user`; user features |
| `accounts` | E-wallet accounts owned by users | One row per `account_id`; business relation `user_id` | `account_type`, `currency`, `created_at` | None | `dim_account`; balances; transaction joins |
| `merchants` | Merchant master data | One row per `merchant_id` | `merchant_name`, `category` | None | `dim_merchant`; merchant analytics/features |
| `devices` | Devices associated with users | One row per `device_id`; relation `user_id` | `device_type`, `os`, `first_seen_at` | None | `dim_device`; device features |
| `transactions` | Financial transaction attempts | Bronze may include controlled duplicates; Silver is one row per `transaction_id` | V1 lacks `channel`; Bronze adds nullable channel before union, Silver normalizes missing values to `UNKNOWN`; amount, balance, status and event time | `event_date` | `fact_transactions`; all PIT behavior features |
| `balance_snapshots` | Daily account closing balance | One row per `(account_id, snapshot_date)` | `closing_balance` | `snapshot_date` | `fact_balance_snapshot` |
| `login_events` | Authentication attempts | One row per `login_id` | `user_id`, `device_id`, `is_success`, `login_ts` | `login_date` | `fact_login_events` |
| `fraud_labels` | Synthetic fraud ground truth | One row per `transaction_id` | `label`, nullable conditional `fraud_type`; excluded from feature inputs | None | Validation/training boundary; no Gold feature edge |

This catalog accounts for 24 logical tables: the same eight business datasets in Source, Bronze and Silver.

### 3.2 Gold core and historical feature tables

| Gold table | Purpose | Grain / logical key | Important columns | Partition | Upstream |
|---|---|---|---|---|---|
| `dim_user` | User descriptive attributes | One row per `user_id` | identity, contact, KYC, `created_at` | None | `silver.users` |
| `dim_account` | Account descriptive attributes | One row per `account_id` | `user_id`, type, currency, `created_at` | None | `silver.accounts` |
| `dim_merchant` | Merchant descriptive attributes | One row per `merchant_id` | name, category | None | `silver.merchants` |
| `dim_device` | Device descriptive attributes | One row per `device_id` | `user_id`, type, OS, first seen | None | `silver.devices` |
| `dim_date` | Shared calendar dimension | One row per `date_key` / calendar date | day, month, quarter, year, weekday/weekend | None | Silver transaction, login and balance dates |
| `fact_transactions` | Central transaction fact | One row per `transaction_id` | dimension keys, `date_key`, type/status/channel, amounts, `timestamp`, `ingested_at` | `event_date` | `silver.transactions` |
| `fact_login_events` | Login-attempt fact | One row per `login_id` | `user_id`, `device_id`, `date_key`, result, `login_ts` | `event_date` | `silver.login_events` |
| `fact_balance_snapshot` | Daily balance fact | One row per `(account_id, snapshot_date)` | `date_key`, closing balance | `snapshot_date` | `silver.balance_snapshots` |
| `obt_transaction_enriched` | Denormalized transaction analytics | One row per `transaction_id` | fact metrics plus user/account/device/merchant/date attributes | `event_date` | Transaction fact + five dimensions |
| `feat_user_90d` | Latest-window user aggregate | One row per `user_id` | 4 metrics, `event_timestamp`, `created_timestamp` (write time) | None | Transaction fact + user dimension |
| `opt_merchant_performance` | Merchant aggregate analytics | One row per `merchant_id` | transaction/amount/success/failure/user counts | None | Transaction fact + merchant dimension |
| `feat_user_behavior` | Point-in-time user behavior | `(user_id, event_timestamp)` | ten historical user metrics | None | `silver.transactions` |
| `feat_account_behavior` | Point-in-time account behavior | `(account_id, event_timestamp)` | eleven historical account metrics | None | `silver.transactions` |
| `feat_device_behavior` | Point-in-time device behavior | `(device_id, event_timestamp)` | age, count, amount and failure-rate metrics | None | `silver.transactions`, `silver.devices` |
| `feat_merchant_behavior` | Point-in-time payment-merchant behavior | `(merchant_id, event_timestamp)` for eligible payments | eight historical merchant metrics | None | `silver.transactions` |

Core builders are in `data_platform/processing/spark/gold/`; PIT builders are under `data_platform/processing/spark/gold/features/`. YAML contracts under `data_platform/contracts/` define the strongest fact, OBT, Silver transaction/label and feature contracts.

## 4. Schema evolution

The generator produces two transaction physical schemas. V1 does not contain `channel`; V2 does. `data_platform/ingestion/spark/source_to_bronze.py` adds a nullable string `channel` to V1 and performs a name-aligned union. Silver then normalizes missing historical values to `UNKNOWN`, validates the domain and deduplicates by transaction identity.

This is controlled additive evolution: downstream tables expose one stable transaction schema while retaining older rows. It does not imply arbitrary backward/forward compatibility for every future change.

The screenshots below come from the local 500k-user dataset:

![Source V1/V2 schema evolution](../../evidence/storage/07_schema_evolution_source_500k.png)

![Unified Bronze schema after evolution handling](../../evidence/storage/08_schema_evolution_500k.png)

Their SHA-256 values are respectively `1821c5472d13c74304db9fc3295309198b25da8615b989d9322b71d94a8c7c2a` and `9d809903451e5c258aeb267cde95ad2ce240df6d160ab21dae8ff54320311d73`.

## 5. Prototype: Type 2 slowly changing merchant dimension

### 5.1 Motivation and constraint

Merchant attributes such as name and category change over time. A fraud analysis of a past transaction should see the merchant *as it was then*.

`dim_merchant` was chosen for the prototype because its two descriptive attributes (`merchant_name`, `category`) make version changes easy to verify. The source `silver.merchants` holds only the current state, with no effective timestamp. A real history therefore requires retained merchant snapshots. The current pipeline builds `dim_merchant` as a current-state table.

### 5.2 Algorithm

The prototype builder takes one state per `(merchant_id, snapshot_ts)` and uses PySpark window functions:

1. Order the snapshots by `snapshot_ts` within each `merchant_id`.
2. Compare a hash of name and category with the previous snapshot (`lag`), and drop consecutive unchanged snapshots.
3. Set `valid_from_ts` to the snapshot time of each remaining change, and `valid_to_ts` to the next change (`lead`).
4. Set `is_current = true` only where `valid_to_ts` is null.

Versions use the half-open interval `[valid_from_ts, valid_to_ts)`, so a fact exactly at a boundary resolves to the new version.

### 5.3 Fixture result

The prototype was verified on a deterministic local fixture, which deliberately includes an unchanged February snapshot:

| merchant_id | merchant_name | category | valid_from_ts | valid_to_ts | is_current |
|---|---|---|---|---|---|
| `m1` | Corner Shop | GROCERY | 2026-01-01 | 2026-03-01 | false |
| `m1` | Corner Shop | PREMIUM_GROCERY | 2026-03-01 | 2026-04-15 | false |
| `m1` | Corner Market | PREMIUM_GROCERY | 2026-04-15 | null | true |
| `m2` | Transit Hub | TRANSPORT | 2026-01-10 | null | true |

![Local fixture: input merchant snapshots](../../evidence/edai1/dp2/01_scd2_input_snapshots.png)

*Input snapshots for two merchants. The repeated `m1` state on 2026-02-01 does not create a new version.*

![Local fixture: SCD2 version history](../../evidence/edai1/dp2/02_scd2_version_history.png)

*The SCD2 output, with `valid_from_ts`, an exclusive `valid_to_ts`, and `is_current`. `m1` has three versions; `m2` has one current version.*

![Local fixture: as-of lookups](../../evidence/edai1/dp2/03_scd2_historical_lookup.png)

*As-of lookups before and exactly at the category boundary select the correct version. `is_current` describes whether a version is current now, not whether it was valid at the time looked up.*

Four focused tests passed with Spark 4.1.1:

- version transitions;
- unchanged-snapshot collapse;
- exactly one current version per key;
- adjacent, non-overlapping intervals, with lookups before, on and after a boundary.

**Status.** The SCD2 builder and its tests are still under development and are not yet part of the pipeline code in this repository version. They have run only against the fixture shown above.

## 6. Prototype: feature materialisation timestamp

Each feature row carries `event_timestamp`: the business time the features describe, and the Feast timestamp used for point-in-time lookup. DP3 windows end one microsecond before the event, enforcing `[T − window, T)`.

`feat_user_90d` additionally records its write time in `created_timestamp`.

**Design of the extension.** The extension adds a `created` column, holding the UTC materialisation time, to all five `feat_` tables:

- It is captured once per DP3 run and shared by all four point-in-time builders.
- It is excluded from semantic A/B fingerprints, but still checked for schema and nulls.
- Feast keeps `event_timestamp` as its timestamp field and ignores `created`, so the 31 model features are unchanged.

Existing outputs are not rewritten, and no `created` value is inferred from event time.

![Local fixture: user and account feature schemas](../../evidence/edai1/dp3/01_feature_timestamp_schema_user_account.png)

*Spark schemas of the user and account feature tables, with both `event_timestamp` and `created`.*

![Local fixture: device and merchant feature schemas](../../evidence/edai1/dp3/02_feature_timestamp_schema_device_merchant.png)

*Device and merchant feature schemas with the additional non-null `created` column.*

![Local fixture: timestamp validation](../../evidence/edai1/dp3/03_feature_timestamp_validation.png)

*Sample values distinguish event time from the shared materialisation time. All five tables report `created_null_count=0`.*

![Local fixture: Feast feature inventory](../../evidence/edai1/dp3/04_feast_feature_inventory.png)

*Feast still exposes exactly 31 model features (9 user, 10 account, 5 device, 7 merchant).*

**Status.** Sixteen focused tests passed locally. Like the SCD2 prototype, this extension is still under development and is not part of the pipeline code in this repository version. No cloud feature output contains `created` yet.

## 7. Dimension–fact relationships

Relationships are logical contracts enforced by transformations and validators; Delta files do not declare database foreign-key constraints.

```mermaid
erDiagram
    DIM_USER ||--o{ DIM_ACCOUNT : owns
    DIM_USER ||--o{ FACT_TRANSACTIONS : initiates
    DIM_USER ||--o{ FACT_LOGIN_EVENTS : authenticates
    DIM_ACCOUNT ||--o{ FACT_TRANSACTIONS : posts
    DIM_ACCOUNT ||--o{ FACT_BALANCE_SNAPSHOT : has_daily_balance
    DIM_ACCOUNT ||--o{ FACT_TRANSACTIONS : counterparty_optional
    DIM_DEVICE ||--o{ FACT_TRANSACTIONS : originates
    DIM_DEVICE ||--o{ FACT_LOGIN_EVENTS : originates
    DIM_MERCHANT o|--o{ FACT_TRANSACTIONS : receives_optional_payment
    DIM_DATE ||--o{ FACT_TRANSACTIONS : event_date
    DIM_DATE ||--o{ FACT_LOGIN_EVENTS : login_date
    DIM_DATE ||--o{ FACT_BALANCE_SNAPSHOT : snapshot_date
    FACT_TRANSACTIONS ||--|| OBT_TRANSACTION_ENRICHED : enriches
    DIM_USER ||--o| FEAT_USER_90D : aggregates
```

`gold_fact_transactions.yml` declares logical foreign keys to user, account, device, merchant and date dimensions; merchant and counterparty account are conditional/nullable. The OBT uses LEFT JOINs so its population remains equal to `fact_transactions`. Historical PIT features join back to entities by their entity key and event timestamp, but are not conventional fact-table foreign-key targets.

The diagram above is derived from the builders and contracts. No ERD export from a database tool is included.

## 8. Naming conventions

| Zone | Convention | Example |
|---|---|---|
| Source | Business table name under the immutable dataset-version path | `source.users` |
| Bronze | Business table name; the zone is part of the dataset identity (storage root and schema) | `bronze.transactions` |
| Silver | Same business name, qualified by the zone | `silver.transactions` |
| Gold | Semantic prefixes: `dim_`, `fact_`, `obt_`, `feat_`, plus `opt_` for analytics | `gold.fact_transactions`, `gold.dim_merchant` |

Bronze and Silver use the zone qualifier instead of `raw_` / `stg_` prefixes. Because the zone is part of every dataset's identity, names never collide across zones.

## 9. Validation and provenance

### Source and tests

- Source/Bronze reconciliation: `data_platform/ingestion/spark/source_to_bronze.py`.
- Silver transformations and quality: `data_platform/processing/spark/silver/` and `data_platform/quality/validate_silver.py`.
- Gold builders and validation: `data_platform/processing/spark/gold/` and `data_platform/quality/validate_gold.py`.
- Inventory guard: `tests/unit/test_schema_inventory.py` requires 8/8/8/15 tables.
- Historical PIT baseline: 31 focused/regression tests passed; Gold validation reported 99 PASS / 0 FAIL on the local 4M-transaction dataset.

Historical local PIT artifact:

`/home/huyy-giaa/ewallet-evidence/edai1-deep-audit-20261029/20261029T071108Z/source_evidence/f5_historical_feature_baseline.json`

- source commit `99ddfe708d7b3f8253e0444a966d62fcd5a655da`;
- measured `2026-10-04` with Spark 4.1.1;
- four historical feature outputs and strict PIT tests PASS;
- explicitly local baseline evidence, not canonical cloud execution.

Canonical cloud identities reused by this report are dataset `canonical-100gb-v1`, Silver run `ab-20261025134342-optimized-silver-r261025181923`, core Gold run `ab-20261025134342-optimized-gold-r261025181923`, feature run `ab-20261025134342-optimized-feat-r261026025955`, and implementation identity `bbae48caef7f65fda0c9375a7a49afb2eca5eef4`. 

### Screenshot integrity

| File | SHA-256 |
|---|---|
| `dp2/01_scd2_input_snapshots.png` | `0a744457f5939180b021cdfb99ff9b59d779a9bc4c55b2ab89008a7783d137bb` |
| `dp2/02_scd2_version_history.png` | `7ed8ef1d4db999e9bf073be58f18ff40b2c9ca6e44931e7ad02765e99394d48e` |
| `dp2/03_scd2_historical_lookup.png` | `9cf9af0e173d16811459f2783153f798c10b857b970585ae9eef143e2c8a475b` |
| `dp3/01_feature_timestamp_schema_user_account.png` | `3e66bc134381ccc7d072386af3835ca092ba1f4c26b218246d3098ae7154aca2` |
| `dp3/02_feature_timestamp_schema_device_merchant.png` | `ad2026c42b4dc2da9013e142adf2b6ddae3f02ddc3d20e42639079cc006a8661` |
| `dp3/03_feature_timestamp_validation.png` | `843937be92d393537c62114ad86f011f8d6a0fe8d82c1f67165b3f62ec0a2c5e` |
| `dp3/04_feast_feature_inventory.png` | `0f239c7efafa6c993acbabf0f70f447ed9f2dab4db3bf230dd99395f299a6fa1` |

## 10. Limitations

- **SCD2 and `created`.** The SCD2 dimension and the `created` feature column are prototypes verified on local fixtures. They are not yet integrated into the submitted pipeline, and no cloud output contains them.
- **No ERD export.** The dimension–fact diagram is derived from the source code; no ERD export from a database tool (for example DBeaver) is included.
- **Naming.** Bronze and Silver tables are distinguished by zone qualification rather than by `raw_` / `stg_` prefixes.
