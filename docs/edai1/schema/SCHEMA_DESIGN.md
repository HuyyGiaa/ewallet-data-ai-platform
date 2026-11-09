# Schema Design

## 1. Introduction

The schema is the contract that connects ingestion, data quality, analytics and point-in-time feature computation. This report describes:

- the tables of every zone and their roles;
- how schema evolution is handled;
- the dimension–fact model and the naming conventions, visualized on the real cloud tables with DBeaver and Trino;
- two schema extensions implemented in the pipeline code and tested locally, but not yet materialized by a cloud run: a Type 2 slowly changing merchant dimension, and a `created` materialisation timestamp on the feature tables.

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

## 5. Type 2 slowly changing merchant dimension

### 5.1 Motivation and constraint

Merchant attributes such as name and category change over time. A fraud analysis of a past transaction should see the merchant *as it was then*.

`silver.merchants` holds only the current state, with no effective timestamp, so it cannot establish history. History must come from persisted snapshots; it is never derived from the current-state table at Gold time.

### 5.2 Design

**Source extension.** The generator can emit a tenth physical table, `merchant_history`: a full snapshot of every merchant at `generation.temporal.start_timestamp` (the first transaction instant) and every 30 days after it. A configured share of merchants change name and/or category once or twice; other snapshots repeat the previous state. The last snapshot equals `merchants`. The history is **synthetic**: every row has `history_source = synthetic`, and the dataset manifest declares it under `source_extensions.merchant_history` with the generator module. A private seeded RNG keeps the nine legacy tables byte-identical to a run without the extension.

| Step | Code | Table |
|---|---|---|
| Generate | `data_platform/generation/src/offline/merchant_history.py` (enabled by `config/settings_merchant_history.yaml`) | `source.merchant_history` |
| Ingest | `source_to_bronze.py` (only when the manifest declares the extension) | `bronze.merchant_history` |
| Clean | `clean_merchant_history` in `silver/cleaners.py` (`--include-merchant-history`) | `silver.merchant_history` |
| Build | `build_dim_merchant_scd2` → `gold/scd2.py` (`gold_pipeline.py --include-merchant-scd2`) | `gold.dim_merchant_scd2` |

`gold.dim_merchant` is unchanged: one row per merchant, used by the fact, OBT, feature and analytics joins. The versioned table is a separate Gold dimension so that no existing join can fan out.

**Which datasets carry it.** `data_platform/config/dataset_identity.py` records the extension per dataset version. `rehearsal-5gb-v1` carries it; `canonical-100gb-v1` was published without it and keeps its 8/8/8/15 inventory. The Silver and Gold submitters read the registry and pass the flags; Bronze checks the source manifest against it. A run that requests SCD2 on a dataset without history fails, rather than inventing history.

**Builder.** One state per `(merchant_id, snapshot_ts)`:

1. Drop exact duplicate snapshots; reject null keys, null effective times and conflicting states at the same time (`Scd2ContractError`).
2. Order by `snapshot_ts` within `merchant_id` and open a version where name or category differs, null-safely, from the previous snapshot (`lag`).
3. `valid_from_ts` is the snapshot time; `valid_to_ts` is the next version's start (`lead`); `is_current = valid_to_ts IS NULL`.
4. `merchant_sk` is SHA-256 of `merchant_id` and the `valid_from_ts` microseconds, so a rebuild gives the same keys.

Versions use the half-open interval `[valid_from_ts, valid_to_ts)` in UTC. An as-of lookup (`lookup_as_of`) matches `valid_from_ts <= t AND (valid_to_ts IS NULL OR t < valid_to_ts)`, so a fact exactly at a boundary resolves to the new version.

**Validation.** `validate_gold --scope core-gold --include-merchant-scd2` checks types, non-null keys, unique `(merchant_id, valid_from_ts)` and `merchant_sk`, `valid_to_ts > valid_from_ts`, the current flag against the open end, exactly one current version per merchant, contiguous non-overlapping intervals, no redundant versions, the current version against `dim_merchant`, and that every transaction with a merchant resolves to exactly one version. `validate_silver --include-merchant-history` checks the snapshots against `merchants`.

### 5.3 Earlier fixture result

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

The screenshots above come from the earlier prototype fixture.

**Status.** Implemented in the DP2 core Gold path and tested locally: 27 unit tests (builder, as-of lookup, corrupted inputs) and a local Spark/Delta run of generator → Bronze → Silver → Gold with the real validators. No cloud run has produced `dim_merchant_scd2` yet; rubric row 35 still needs the owner's 5GB/100GB runs. See [PHASE_B_SCD2_FEATURE_TIMESTAMPS.md](PHASE_B_SCD2_FEATURE_TIMESTAMPS.md).

## 6. Feature materialisation timestamp

Each feature row carries `event_timestamp`: the business time the features describe, and the Feast timestamp used for point-in-time lookup. DP3 windows end one microsecond before the event, enforcing `[T − window, T)`.

`feat_user_90d` additionally records its write time in `created_timestamp`.

**Design.** All five `feat_` tables carry a `created` column, holding the UTC materialisation time:

- DP3 captures one timezone-aware UTC instant at the start of the run, writes it to all four point-in-time tables and records it in each table's Delta commit; DP2 core Gold does the same for `feat_user_90d`, where `created` equals the legacy `created_timestamp` (kept for existing readers). The builders accept the instant as a parameter, so tests use a fixed clock.
- `created` is appended after the existing columns. Feature values are unchanged: on a generated dataset the new builders' outputs, minus `created`, are bit-identical to the previous builders'.
- It is excluded from semantic A/B fingerprints, but still checked for schema and nulls.
- Feast keeps `event_timestamp` as its timestamp field. The Feast export accepts the legacy schema or the legacy schema plus `created` and writes exactly the legacy columns, so the offline snapshot, online materialization and the 31 model features are unchanged.

Existing outputs are not rewritten, and no `created` value is inferred from event time. No rule requires `created >= event_timestamp`, because a replay of future-dated synthetic events can be materialized before its event times.

**Validation.** `validate_gold --scope features` checks that both columns are non-null timestamps, that `created` has one value per table shared by the four DP3 tables, and that every `created` equals the materialization clock the run recorded in the Delta commit (`userMetadata`). That provenance check, not a row-level comparison, is what rejects a `created` copied from `event_timestamp`: the two may legitimately be equal. It also recomputes each table's 1-hour count with strict `[T − 1h, T)` bounds on a deterministic entity sample.

![Local fixture: user and account feature schemas](../../evidence/edai1/dp3/01_feature_timestamp_schema_user_account.png)

*Spark schemas of the user and account feature tables, with both `event_timestamp` and `created`.*

![Local fixture: device and merchant feature schemas](../../evidence/edai1/dp3/02_feature_timestamp_schema_device_merchant.png)

*Device and merchant feature schemas with the additional non-null `created` column.*

![Local fixture: timestamp validation](../../evidence/edai1/dp3/03_feature_timestamp_validation.png)

*Sample values distinguish event time from the shared materialisation time. All five tables report `created_null_count=0`.*

![Local fixture: Feast feature inventory](../../evidence/edai1/dp3/04_feast_feature_inventory.png)

*Feast still exposes exactly 31 model features (9 user, 10 account, 5 device, 7 merchant).*

The screenshots above come from the earlier prototype fixture.

**Status.** Implemented in the DP2 and DP3 builders and tested locally (unit tests, corrupted-input tests and the local Spark/Delta run). No cloud feature output contains `created` yet; rubric row 36 still needs the owner's 5GB/100GB runs.

## 7. Dimension–fact relationships

### 7.1 Physical keys, logical keys, lineage and diagram links

The Gold tables are Delta tables on GCS. Delta records columns, types and partition columns, but no primary or foreign keys. Four kinds of "relationship" must therefore be kept apart:

| Kind | Where it is defined | What it contains here |
|---|---|---|
| Physical metadata | Delta transaction log; Trino `information_schema` | Columns, types, partition columns. Every column is physically nullable (Spark default). No key constraints |
| Logical relationships | [`schema_relationships.yml`](../../../data_platform/contracts/relationships/schema_relationships.yml), [`gold_fact_transactions.yml`](../../../data_platform/contracts/gold_fact_transactions.yml), `data_platform/quality/validate_gold.py` | Logical keys, and foreign keys produced by the builders. 19 Gold foreign keys are checked for orphans by `validate_foreign_key`; non-null keys are a validator rule |
| Lineage between zones | Builders; DataHub (§5 of [ALL_ZONES_SCHEMA](ALL_ZONES_SCHEMA.md#5-lineage-between-zones)) | Which table each Gold table is built from |
| Diagram-only links | DBeaver virtual keys | A copy of the logical relationships, stored in the DBeaver workspace, used to draw the ER diagram |

A link in the ER diagram is therefore a logical relationship, never a constraint that the storage enforces.

**Where each check runs.** The logical contract is checked at two levels, which cover different relationships:

| Level | Code | Relationships checked | Datasets it has run on |
|---|---|---|---|
| Local validators | `validate_silver.py`, `validate_gold.py` | Orphan checks for 10 Silver and 19 Gold foreign keys (`validated`). The `fraud_labels → transactions` check runs only with `--include-fraud-labels` | Local canonical rebuilds (f3/f4c evidence) |
| Cloud runtime invariants | `ops/gcp/summarize_spark_output.py` | Gold key uniqueness. Orphan checks only for `fact_transactions.{user_id, account_id, device_id, merchant_id, date_key}`. OBT/fact row parity | The 100 GB cloud DP2 runs |

The 100 GB cloud run executes neither validator, so `validated` means "checked by a validator rule", not "re-checked on the 100 GB data". `builder_only` edges (`fraud_labels → transactions`, `obt_transaction_enriched → fact_transactions`) follow from the builder alone.

**Diagram representation.** [silver.dbml](generated/silver.dbml) and [gold.dbml](generated/gold.dbml) draw the *logical* model on the observed columns:
- **Required references:** `not null` on the referencing column and DBML `>` (exactly one parent).
- **Conditional references** (`merchant_id`, `counterparty_account_id`): `null` and `>?` (zero or one parent).
- **One-to-one edges:** `-`, with the parent first, wherever the referencing column is the child's whole key. These are `feat_user_90d`, `opt_merchant_performance`, `obt_transaction_enriched` and `fraud_labels`. Their builders produce exactly one child row per parent row (`coverage: complete` in the contract).

Each Ref carries a comment naming its validator rule and whether the cloud run re-checks it. The physical state, with every Delta column nullable and no constraints, is stated in each file's header and table notes.

### 7.2 The Gold star

The diagram covers the five dimensions and three facts. The Mermaid rendering below is generated from the contracts ([GOLD_ERD.md](generated/GOLD_ERD.md)); it is an explanatory drawing, not a database export.

```mermaid
erDiagram
    DIM_USER ||--o{ DIM_ACCOUNT : user_id
    DIM_USER ||--o{ DIM_DEVICE : user_id
    DIM_USER ||--o{ FACT_TRANSACTIONS : user_id
    DIM_ACCOUNT ||--o{ FACT_TRANSACTIONS : account_id
    DIM_DEVICE ||--o{ FACT_TRANSACTIONS : device_id
    DIM_MERCHANT |o--o{ FACT_TRANSACTIONS : merchant_id
    DIM_ACCOUNT |o--o{ FACT_TRANSACTIONS : counterparty_account_id
    DIM_DATE ||--o{ FACT_TRANSACTIONS : date_key
    DIM_USER ||--o{ FACT_LOGIN_EVENTS : user_id
    DIM_DEVICE ||--o{ FACT_LOGIN_EVENTS : device_id
    DIM_DATE ||--o{ FACT_LOGIN_EVENTS : date_key
    DIM_ACCOUNT ||--o{ FACT_BALANCE_SNAPSHOT : account_id
    DIM_DATE ||--o{ FACT_BALANCE_SNAPSHOT : date_key
```

The same model is drawn by DBeaver on the registered cloud tables (runbook §G):

| Evidence | Content | Status |
|---|---|---|
| E05 | Native DBeaver diagram `EDAI1_Gold_ERD`: all 15 Gold tables, 19 virtual links | OWNER_SCREENSHOT_PENDING |
| E06 | The same diagram zoomed on the 8-table dimensional core (13 links), and the virtual keys of `fact_transactions` | OWNER_SCREENSHOT_PENDING |
| E09 | Native DBeaver diagram `EDAI1_Silver_ERD`: 8 Silver tables, 11 virtual links | OWNER_SCREENSHOT_PENDING |

**Grain.**

| Table | Grain (logical key) | Partition |
|---|---|---|
| `dim_user` | one row per `user_id` | - |
| `dim_account` | one row per `account_id` | - |
| `dim_merchant` | one row per `merchant_id` | - |
| `dim_device` | one row per `device_id` | - |
| `dim_date` | one row per `date_key` (calendar day, `yyyyMMdd` integer) | - |
| `fact_transactions` | one row per `transaction_id` | `event_date` |
| `fact_login_events` | one row per `login_id` | `event_date` |
| `fact_balance_snapshot` | one row per `(account_id, snapshot_date)` | `snapshot_date` |

**Relationships.** Each edge references the parent's whole logical key, so each child row has at most one parent: every edge is many-to-one. *Optional* means the column may be null; the conditional rules of `gold_fact_transactions.yml` require `merchant_id` for payments and `counterparty_account_id` for transfers.

| Child column | Parent | Optional | Enforcement |
|---|---|---|---|
| `fact_transactions.user_id` | `dim_user` | no | validated |
| `fact_transactions.account_id` | `dim_account` | no | validated |
| `fact_transactions.device_id` | `dim_device` | no | validated |
| `fact_transactions.merchant_id` | `dim_merchant` | yes (payments) | validated |
| `fact_transactions.counterparty_account_id` | `dim_account` | yes (transfers) | validated |
| `fact_transactions.date_key` | `dim_date` | no | validated |
| `fact_login_events.user_id`, `.device_id`, `.date_key` | `dim_user`, `dim_device`, `dim_date` | no | validated |
| `fact_balance_snapshot.account_id`, `.date_key` | `dim_account`, `dim_date` | no | validated |
| `dim_account.user_id`, `dim_device.user_id` | `dim_user` | no | validated |

Every join column has the same physical type on both sides (string keys, integer `date_key`). This is checked against the recorded Delta schemas by `tests/unit/test_schema_visualization.py`.

Outside the diagram:

- `obt_transaction_enriched` is one row per `transaction_id`, one-to-one with `fact_transactions`. It is built with LEFT JOINs to the five dimensions, so its population equals the fact's.
- `feat_user_90d` and `opt_merchant_performance` are aggregates keyed by a dimension key.
- The four point-in-time feature tables are keyed by `(entity, event_timestamp)`. They are validated against their dimension, but are snapshots, not facts.

### 7.3 Star or snowflake?

The model is a **star schema with three fact tables sharing conformed dimensions** (a fact constellation). It is not a snowflake:

- Each fact carries the keys of all its dimensions directly. `fact_transactions` holds `user_id` even though `dim_account` also links to the user, so an analytical query joins the fact to each dimension in one step.
- `dim_user`, `dim_account`, `dim_device` and `dim_date` are shared by several facts with the same keys, which is what makes them conformed.
- Two links run between dimensions: `dim_account.user_id` and `dim_device.user_id` both point to `dim_user`. They give the model a snowflake-like trait (an outrigger), but `dim_user` is not a normalized sub-table of another dimension: two facts reference it directly. No attribute was split out of a dimension into a lookup table; for example, merchant `category` stays in `dim_merchant`.

No dimension was added or split to make the schema look like a snowflake. One gap: the fact's `user_id` and the account's `user_id` are both copied from Silver, and no validator checks that they agree. The fact builder is a pure projection of `silver.transactions`.

The dimensions are current-state (full refresh). The Type 2 merchant dimension of §5 is a prototype outside the pipeline.

## 8. Naming conventions

| Layer | Convention | Physical name in Trino | GCS path | Example |
|---|---|---|---|---|
| Source | Business dataset name under the immutable dataset version | `hive.source_zone.<dataset>` | `source/canonical-100gb-v1/<dataset>/` | `users`; `transactions_v1`, `transactions_v2` |
| Bronze | `bronze.<table>` | `delta.bronze_zone.<table>` | `…/bronze/<table>` | `delta.bronze_zone.transactions` |
| Silver | `silver.<table>` | `delta.silver_zone.<table>` | `…/silver/<table>` | `delta.silver_zone.transactions` |
| Gold dimension | `gold.dim_<entity>` | `delta.gold_zone.dim_<entity>` | `…/gold/dim_<entity>` | `dim_merchant` |
| Gold fact | `gold.fact_<event>` | `delta.gold_zone.fact_<event>` | `…/gold/fact_<event>` | `fact_transactions` |
| Gold OBT | `gold.obt_<subject>` | `delta.gold_zone.obt_<subject>` | `…/gold/obt_<subject>` | `obt_transaction_enriched` |
| Gold features | `gold.feat_<entity>_<window or behavior>` | `delta.gold_zone.feat_…` | `…/gold/feat_…` | `feat_user_90d`, `feat_device_behavior` |
| Gold analytics | `gold.opt_<subject>` | `delta.gold_zone.opt_…` | `…/gold/opt_…` | `opt_merchant_performance` |

The schema names `bronze_zone`, `silver_zone` and `gold_zone` are the `BRONZE_SCHEMA`/`SILVER_SCHEMA`/`GOLD_SCHEMA` constants of `data_platform/storage/scripts/config.py`. The Trino catalog listing confirmed them on 2026-11-08 (readback query E07). Of the 31 Delta tables, the 16 Bronze/Silver tables are unprefixed; the 15 Gold tables split into 5 dimension, 3 fact, 1 OBT, 5 feature and 1 analytics aggregate.

**Why no `raw_` / `stg_` prefix.** Such prefixes tell a raw copy of a table from its cleaned copy when both live in one namespace. Here the zone is already part of every identifier, at three levels: the Trino schema (`bronze_zone` / `silver_zone`), the storage path (`…/bronze/…` / `…/silver/…`) and the DataHub dataset name (`bronze.transactions`).

So `bronze.transactions` plays the role of `raw_transactions`, and `silver.transactions` the role of `stg_transactions`. A prefix would only repeat the zone (`bronze_zone.raw_transactions`). It would also break the rule that one business table keeps one name across Source, Bronze and Silver, which the ingestion and Silver builders rely on to pair tables by name.

Gold is different: all its tables share one schema. There the prefix is the only thing that tells a dimension from a fact, so Gold uses it.

| Evidence | Content | Status |
|---|---|---|
| E07 | The three zone schemas expanded in DBeaver, plus the E07 query result | OWNER_SCREENSHOT_PENDING (query result already in the [readback](../../evidence/edai1/schema/trino_metadata_readback_20261108T171351Z.txt)) |

The DataHub screenshots in [ALL_ZONES_SCHEMA](ALL_ZONES_SCHEMA.md#5-lineage-between-zones) were captured on 2026-10-25/26 with older display names (`agt_merchant_performance`, a `feature.` namespace). The current code, storage and Trino catalog use `opt_merchant_performance` and `gold_zone`.

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

### Schema visualization (rows 34, 37, 38)

| Category | Result |
|---|---|
| SOURCE_VERIFIED | The 13 ERD edges equal the Gold relationships of `schema_relationships.yml`, which equal the `validate_foreign_key` rules. Optionality equals `gold_fact_transactions.yml` |
| LOCAL_TESTED | `tests/unit/test_schema_visualization.py` and `tests/unit/test_schema_diagrams.py`: join columns exist with equal types; edges are many-to-one onto the parent key; SQL is metadata-only; naming rules hold; links resolve |
| TRINO_METADATA_VERIFIED | 2026-11-08: the 31 Delta tables of the canonical runs and the 9 Source datasets, registered in a local Trino 483, return the recorded columns (40 tables, 293 columns) and their `gs://` locations ([readback](../../evidence/edai1/schema/trino_metadata_readback_20261108T171351Z.txt), [summary](../../evidence/edai1/schema/trino_verification_20261108.json)) |
| Production unchanged | `_delta_log` and Source control objects identical before and after (generation-level listing) |
| DBEAVER_VISUAL_VERIFIED | Pending: the ER diagram is drawn by the owner (runbook §G) |
| OWNER_SCREENSHOT_PENDING | E05, E06, E07 |

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

- **SCD2 and `created`.** Both are in the pipeline code and tested locally, but no cloud run has materialized them. `canonical-100gb-v1` has no merchant history, so its runs keep the 15-table Gold inventory; the history is synthetic and declared as such.
- **ERD screenshots pending.** The DBeaver connection, the virtual keys and the diagram are fully specified and the underlying Trino metadata is verified, but the DBeaver screenshots (E05–E07) have not been captured yet.
- **Virtual, not physical, keys.** DBeaver draws the relationships from virtual keys stored in the DBeaver workspace. Delta has no key constraints, and DBeaver cannot display optionality; [GOLD_ERD.md](generated/GOLD_ERD.md) records it.
- **Unchecked user consistency.** No rule checks that `fact_transactions.user_id` equals the `user_id` of its `account_id` in `dim_account`.
- **Naming.** Bronze and Silver tables are distinguished by zone qualification rather than by `raw_` / `stg_` prefixes (§8); whether this counts as the "similar" convention the rubric allows is the assessor's call.
- **Reproduction.** [SCHEMA_VISUALIZATION_DBEAVER.md](../../runbooks/edai1/SCHEMA_VISUALIZATION_DBEAVER.md).
