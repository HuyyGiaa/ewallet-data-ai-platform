**# Batch Data Pipeline**

**## Goal**

The offline pipeline transforms generated E-wallet data through a

Bronze, Silver and Gold lakehouse architecture.

The pipeline is designed to demonstrate:

\- Raw data ingestion

\- Data quality validation

\- Transaction deduplication

\- Analytical data modeling

\- Feature engineering

\- Delta Lake storage

\- Reproducible batch processing with Spark

The implemented data flow is:

\`\`\`text

Offline Data Generator

        ↓

Parquet source files

        ↓

Bronze Delta

        ↓

Bronze Validation

        ↓

Silver Delta

        ↓

Silver Validation

        ↓

Gold Delta

        ↓

Gold Validation

\`\`\`

Airflow orchestrates the same sequence as one batch DAG.

**## Final Offline Dataset**

The final coursework dataset was generated with:

\`\`\`yaml

n\_users: 500000

n\_merchants: 300

n\_devices\_per\_user: 1

days\_history: 90

duplicate\_rate\_offline: 0.02

\`\`\`

The generated dataset contains:

\| Dataset | Rows |

\|---|---:|

\| Users | 500,000 |

\| Accounts | 500,000 |

\| Merchants | 300 |

\| Devices | 500,000 |

\| Transactions before duplicate injection | 4,000,000 |

\| Bronze transactions after duplicate injection | 4,080,000 |

\| Balance snapshots | 3,895,224 |

\| Login events | 3,997,362 |

The generator intentionally introduces data characteristics that are

useful for testing data-engineering behavior rather than producing a

perfectly clean dataset.

Examples include:

\- Duplicate transactions

\- Merchant-key skew

\- Time and channel skew

\- High-cardinality transaction IDs

\- Late and duplicate streaming events

\- Configurable schema evolution

**## Source Data**

The offline generator produces seven source datasets:

\`\`\`text

users

accounts

merchants

devices

transactions

balance\_snapshots

login\_events

\`\`\`

The files are written as Parquet under:

\`\`\`text

data\_generator/output/offline/

\`\`\`

For the transaction dataset, the generator writes two Parquet source files:

\`\`\`text

transactions_v1.parquet

transactions_v2.parquet

\`\`\`

They represent the same logical transaction dataset across two schema versions.

These files represent the source data used by the batch pipeline.

**## Bronze Layer**

**### Purpose**

The Bronze layer stores the ingested version of the source data with

minimal transformation.

Conceptually:

\`\`\`text

Generated Parquet

        ↓

Bronze Delta Lake

\`\`\`

Bronze acts as the first persistent lakehouse layer.

The source Parquet files are converted into Delta Lake tables and

stored in MinIO.

Examples include:

\`\`\`text

s3://bronze-zone/users

s3://bronze-zone/accounts

s3://bronze-zone/transactions

s3://bronze-zone/login\_events

\`\`\`

Each Delta table contains:

\`\`\`text

\_delta\_log/

Parquet data files

\`\`\`

The Delta transaction log provides table metadata and transaction

history while the actual records remain stored in Parquet files.

**### Transaction Duplicates**

The transaction generator intentionally injects a 2% duplicate rate.

For the final dataset:

\`\`\`text

Original transactions: 4,000,000

Duplicates injected:     80,000

Bronze transactions:   4,080,000

\`\`\`

Bronze preserves these duplicate records.

This is intentional because Bronze represents the ingested source

rather than an already-cleaned dataset.

**## Bronze Validation**

A validation step runs after Bronze ingestion.

Its role is to verify that the expected datasets were successfully

written before downstream transformations begin.

Conceptually:

\`\`\`text

Bronze ingestion

      ↓

Bronze validation

      ↓

continue only when valid

\`\`\`

This prevents Silver processing from silently running against missing

or incomplete Bronze inputs.

The batch orchestration later uses this validation as a separate task:

\`\`\`text

bronze\_ingestion

        ↓

validate\_bronze

\`\`\`

**## Silver Layer**

**### Purpose**

Silver contains cleaned and standardized data suitable for downstream

analytical transformations.

The main transaction flow is:

\`\`\`text

bronze.transactions

        ↓

cleaning

        ↓

deduplication

        ↓

silver.transactions

\`\`\`

The Silver pipeline is implemented with Apache Spark.

**## Transaction Deduplication**

Transactions are deduplicated using:

\`\`\`text

transaction\_id

\`\`\`

When duplicate transaction IDs exist, the pipeline keeps the record

with the latest:

\`\`\`text

ingested\_at

\`\`\`

Conceptually:

\`\`\`text

transaction\_id = X

        ↓

multiple records

        ↓

order by ingested\_at

        ↓

keep latest record

\`\`\`

For the final dataset:

\`\`\`text

Bronze transactions: 4,080,000

Silver transactions: 4,000,000

Removed duplicates:    80,000

\`\`\`

Therefore:

\`\`\`text

4,080,000 - 80,000 = 4,000,000

\`\`\`

The final Silver transaction count matches the number of original

transactions before duplicate injection.

**## Why Deduplication Happens in Silver**

Duplicates are preserved in Bronze because Bronze represents the raw

ingested state.

Removing them immediately during Bronze ingestion would make it harder

to inspect or reproduce source-data problems.

The responsibility is therefore separated:

\`\`\`text

Bronze

    preserve source behavior

Silver

    enforce cleaned transaction semantics

\`\`\`

This also makes the duplicate transformation measurable.

**## Silver Storage**

Silver event-oriented tables use date-based partitioning.

For example, transactions are partitioned using:

\`\`\`text

event\_date

\`\`\`

This allows analytical engines to avoid scanning unrelated dates for

queries containing an event-date filter.

The Silver layer is stored as Delta Lake in MinIO.

Example:

\`\`\`text

s3://silver-zone/transactions/

    \_delta\_log/

    event\_date=.../

    event\_date=.../

    ...

\`\`\`

The physical layout and small-file behavior are analyzed separately in:

\`\`\`text

docs/05\_storage\_optimization.md

\`\`\`

**## Silver Validation**

Validation is executed after the Silver transformation.

Important transaction checks include:

\`\`\`text

No duplicate transaction\_id values

Expected row count

Required fields remain available

Transformation completed successfully

\`\`\`

For the final dataset, Silver contains:

\`\`\`text

4,000,000 unique transactions

\`\`\`

The validation step provides a quality gate before the Gold

transformation starts.

The orchestration dependency is:

\`\`\`text

silver\_transformation

        ↓

validate\_silver

\`\`\`

**## Gold Layer**

**### Purpose**

Gold transforms cleaned Silver data into structures intended for

analytics and downstream feature consumption.

The Gold layer contains:

**### Dimensions**

\`\`\`text

dim\_user

dim\_account

dim\_merchant

dim\_device

dim\_date

\`\`\`

**### Facts**

\`\`\`text

fact\_transactions

fact\_login\_events

fact\_balance\_snapshot

\`\`\`

**### Analytical OBT**

\`\`\`text

obt\_transaction\_enriched

\`\`\`

**### Feature Table**

\`\`\`text

feat\_user\_90d

\`\`\`

**### Optional Aggregate**

\`\`\`text

opt\_merchant\_performance

\`\`\`

This produces a total of 11 Gold tables.

**## Dimensional Model**

The Gold layer separates reusable descriptive entities from event

tables.

Conceptually:

\`\`\`text

                 dim\_user

                    │

                 dim\_account

                    │

dim\_device ── fact\_transactions ── dim\_merchant

                    │

                 dim\_date

\`\`\`

This model supports analytical queries while keeping reusable

dimensions separate from transaction facts.

**## Gold Transaction Fact**

The primary analytical transaction table is:

\`\`\`text

gold.fact\_transactions

\`\`\`

The final table contains:

\`\`\`text

4,000,000 transactions

\`\`\`

The transaction population therefore remains consistent between the

clean Silver layer and the Gold fact:

\`\`\`text

Silver transactions

4,000,000

        ↓

Gold fact\_transactions

4,000,000

\`\`\`

This is an important correctness property of the Gold transformation.

**## Transaction OBT**

The project also creates:

\`\`\`text

obt\_transaction\_enriched

\`\`\`

The OBT enriches transaction records with dimensional attributes.

Conceptually:

\`\`\`text

fact\_transactions

      \+

dimensions

      ↓

obt\_transaction\_enriched

\`\`\`

The enrichment uses fact transactions as the main dataset.

LEFT JOIN semantics are used so that a missing dimension record does

not automatically remove a transaction from the analytical dataset.

The OBT provides a convenient flattened representation for analytical

and feature-engineering workloads.

**## User Feature Table**

The Gold layer creates:

\`\`\`text

feat\_user\_90d

\`\`\`

This table contains user-level features derived from historical

transaction behavior.

The feature window is based on the maximum timestamp present in the

dataset rather than the wall-clock time at which the pipeline happens

to run.

Conceptually:

\`\`\`text

maximum dataset timestamp

        ↓

determine feature window

        ↓

aggregate historical behavior

        ↓

feat\_user\_90d

\`\`\`

This makes the feature transformation reproducible when the same

dataset is processed again.

The table can later be consumed by analytical or fraud-detection

workloads.

**## Gold Validation**

The Gold validation step checks that the analytical layer has been

successfully produced and maintains important consistency rules.

Examples include:

\`\`\`text

Gold tables exist

fact\_transactions contains the expected population

transaction counts remain consistent with cleaned Silver data

required analytical outputs were produced

\`\`\`

The orchestration dependency is:

\`\`\`text

gold\_transformation

        ↓

validate\_gold

\`\`\`

**## End-to-End Transaction Path**

The main offline transaction path is:

\`\`\`text

Generated transactions

4,000,000

        ↓

2% duplicates injected

        ↓

Bronze

4,080,000

        ↓

Silver deduplication

        ↓

Silver

4,000,000

        ↓

Gold transformation

        ↓

fact\_transactions

4,000,000

        ↓

obt\_transaction\_enriched

        ↓

feat\_user\_90d

\`\`\`

This path is also represented later through DataHub lineage.

**## Schema Evolution**

The generator contains a configurable schema-evolution scenario for the

transaction \`channel\` field.

The configured evolution boundary is:

\`\`\`yaml

schema\_change\_date: "2026-05-01"

\`\`\`

The final 500k-user dataset contains both schema versions:

\`\`\`text

Schema V1: 899,818 rows

    channel column absent in the source schema

        ↓

Schema V2: 3,180,182 rows

    channel column present in the source schema

\`\`\`

The two versions contain a total of:

\`\`\`text

899,818 + 3,180,182 = 4,080,000 Bronze transactions

\`\`\`

During Bronze ingestion, \`transactions_v1.parquet\` is written first and

\`transactions_v2.parquet\` is then appended with schema evolution enabled.

Delta Lake therefore exposes one unified transaction table.

Rows originating from V1 have \`channel = NULL\`, while rows originating

from V2 contain the \`channel\` field.

The final Bronze verification produced:

\`\`\`text

Total rows:          4,080,000

V1 rows:               899,818

V2 rows:             3,180,182

V1 channel NULL:       899,818

V2 channel present:  3,180,182

\`\`\`

This confirms that the final dataset itself demonstrates the configured

schema evolution rather than relying on a separate synthetic test.

Evidence is stored in:

\`\`\`text

docs/evidence/storage/07_schema_evolution_source_500k.png

docs/evidence/storage/08_schema_evolution_500k.png

\`\`\`

**## Spark Configuration**

The Silver and Gold transformations run using local Spark.

Important configuration includes:

\`\`\`text

local[4]

Adaptive Query Execution enabled

Skew join optimization enabled

Delta Lake support

S3-compatible MinIO storage

\`\`\`

For larger workloads, the Spark driver is started with additional

memory:

\`\`\`bash

PYSPARK\_SUBMIT\_ARGS="--driver-memory 8g pyspark-shell" \\

python \<pipeline>.py

\`\`\`

This configuration was sufficient to process the final

500,000-user workload on the coursework machine.

**## Storage Format**

All Bronze, Silver and Gold lakehouse tables use Delta Lake.

The separation of responsibilities is:

\`\`\`text

MinIO

    physical object storage

Delta Lake

    table format and transaction metadata

Spark

    batch transformation engine

Trino

    analytical SQL query engine

\`\`\`

Trino does not contain another copy of the Gold data.

It queries the Delta tables stored in MinIO.

**## Orchestration**

The batch stages are orchestrated through Apache Airflow.

The final task sequence is:

\`\`\`text

bronze\_ingestion

        ↓

validate\_bronze

        ↓

silver\_transformation

        ↓

validate\_silver

        ↓

gold\_transformation

        ↓

validate\_gold

\`\`\`

The orchestration implementation is documented separately in:

\`\`\`text

docs/06\_airflow\_orchestration.md

\`\`\`

**## Downstream Usage**

The Gold layer supports different downstream consumers.

\`\`\`text

Gold facts / OBT

        ↓

Trino analytics

Gold features

        ↓

Fraud / ML workloads

Pipeline metadata

        ↓

DataHub lineage

\`\`\`

The project therefore separates data preparation from the systems that

later consume analytical or machine-learning-ready data.

**## Design Decisions**

**### Preserve Raw Problems in Bronze**

Bronze intentionally retains source duplicates.

This makes source quality problems observable and allows downstream

cleaning behavior to be measured.

**### Deduplicate in Silver**

Silver is the appropriate layer for enforcing the cleaned transaction

representation.

The deduplication logic can therefore be validated independently from

ingestion.

**### Build Multiple Gold Representations**

The Gold layer contains both normalized fact/dimension structures and a

flattened OBT.

The dimensional model provides reusable analytical entities.

The OBT simplifies downstream analytical and feature workloads.

**### Dataset-Based Feature Window**

Feature generation uses the dataset's maximum timestamp rather than the

current system time.

This improves reproducibility for coursework experiments.

**### Delta Lake for All Lakehouse Layers**

Using the same table format across Bronze, Silver and Gold keeps the

storage model consistent while supporting schema metadata and table

history.

**## Trade-offs**

The current pipeline rewrites batch datasets rather than implementing a

fully incremental production ingestion architecture.

This keeps the coursework implementation easier to reproduce and

validate but would be more expensive as data volume grows.

The local Spark configuration is appropriate for the current machine

and coursework scale but does not represent a distributed production

cluster.

The synthetic dataset is intentionally designed to reproduce specific

data-engineering problems.

It therefore provides controlled and repeatable experiments, although

it does not represent real customer transaction data.

**## Result**

The final offline pipeline successfully processes the generated

E-wallet dataset through the complete lakehouse flow:

\`\`\`text

Parquet

   ↓

Bronze Delta

   ↓

Validation

   ↓

Silver Delta

   ↓

Deduplication

   ↓

Validation

   ↓

Gold Facts / Dimensions / OBT / Features

   ↓

Validation

\`\`\`

For the primary transaction dataset:

\`\`\`text

Bronze: 4,080,000

        ↓

80,000 duplicates removed

        ↓

Silver: 4,000,000

        ↓

Gold fact\_transactions: 4,000,000

\`\`\`

The resulting Gold tables provide analytical and feature-ready datasets

that are later exposed through Trino and documented through DataHub

lineage.