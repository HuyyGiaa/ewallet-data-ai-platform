**# DataHub Metadata and Lineage**

**## Goal**

DataHub is used as the metadata catalog and lineage layer for the

E-wallet data platform.

The objective is to make the movement of transaction data across the

lakehouse visible and traceable.

The main lineage demonstrated in this project is:

\`\`\`text

Bronze transactions

        ↓

Silver transactions

        ↓

Gold fact\_transactions

        ↓

Gold obt\_transaction\_enriched

        ↓

Gold feat\_user\_90d

\`\`\`

This allows the project to show where a downstream analytical table or

feature originates from and which upstream datasets contribute to it.

**## Architecture**

The metadata flow is:

\`\`\`text

Delta Lake tables

stored in MinIO

        ↓

Trino

        ↓

DataHub ingestion

        ↓

DataHub Metadata Catalog

        ↓

Dataset Lineage

\`\`\`

The local services used in this integration are:

\| Component | Purpose | Endpoint |

\|---|---|---|

\| MinIO | Delta Lake object storage | \`localhost:9000\` |

\| Trino | SQL query engine and metadata source | \`localhost:8081\` |

\| DataHub GMS | Metadata service | \`localhost:8080\` |

\| DataHub UI | Metadata and lineage visualization | \`localhost:9002\` |

DataHub does not store the transaction data itself.

The actual Delta Lake data remains stored in MinIO. DataHub stores and

displays metadata about those datasets.

**## Trino Metadata Ingestion**

DataHub ingests dataset metadata from the Trino \`delta\` catalog.

The ingestion configuration is stored in:

\`\`\`text

lineage/trino\_recipe.yml

\`\`\`

The recipe limits metadata ingestion to the three lakehouse schemas:

\`\`\`text

bronze\_zone

silver\_zone

gold\_zone

\`\`\`

Profiling was disabled because the objective of this integration is

metadata discovery and lineage visualization rather than scanning the

full analytical dataset.

The ingestion is executed with:

\`\`\`bash

DATAHUB\_TELEMETRY\_ENABLED=false \\

python -m datahub ingest -c lineage/trino\_recipe.yml

\`\`\`

The final ingestion run completed successfully and produced metadata

events for the registered Trino datasets.

**## Trino Table Registration**

The Delta tables physically exist in MinIO, but Trino uses its metastore

to determine which tables are available through SQL.

Therefore, a Delta table can exist in storage without automatically

appearing in:

\`\`\`sql

SHOW TABLES FROM delta.\<schema>;

\`\`\`

For example, the Silver transaction table was registered using the

Delta Lake connector:

\`\`\`sql

CALL delta.system.register\_table(

    schema\_name => 'silver\_zone',

    table\_name => 'transactions',

    table\_location => 's3://silver-zone/transactions'

);

\`\`\`

The Gold OBT and feature tables were registered in the same way.

Registration does not copy or recreate the data.

It only associates a logical Trino table name with the existing Delta

table stored in MinIO.

Conceptually:

\`\`\`text

delta.silver\_zone.transactions

                ↓

        Trino metadata

                ↓

s3://silver-zone/transactions

                ↓

        Delta transaction log

        \+ Parquet data files

\`\`\`

This step was required before DataHub could discover the corresponding

datasets through the Trino ingestion source.

**## Registered Lineage Datasets**

The transaction lineage uses the following DataHub assets:

\| Layer | Dataset |

\|---|---|

\| Bronze | \`delta.bronze\_zone.transactions\` |

\| Silver | \`delta.silver\_zone.transactions\` |

\| Gold Fact | \`delta.gold\_zone.fact\_transactions\` |

\| Gold OBT | \`delta.gold\_zone.obt\_transaction\_enriched\` |

\| Gold Feature | \`delta.gold\_zone.feat\_user\_90d\` |

The assets are represented in DataHub using the Trino data platform.

For example:

\`\`\`text

urn\:li\:dataset:

(

    urn\:li\:dataPlatform\:trino,

    delta.gold\_zone.fact\_transactions,

    PROD

)

\`\`\`

**## Dataset Lineage**

The lineage relationships are:

\`\`\`text

delta.bronze\_zone.transactions

                ↓

delta.silver\_zone.transactions

                ↓

delta.gold\_zone.fact\_transactions

                ↓

delta.gold\_zone.obt\_transaction\_enriched

                ↓

delta.gold\_zone.feat\_user\_90d

\`\`\`

Each transition represents an actual transformation stage in the

offline data pipeline.

**### Bronze to Silver**

\`\`\`text

bronze\_zone.transactions

        ↓

silver\_zone.transactions

\`\`\`

Bronze preserves the ingested transaction data.

The Silver transformation performs cleaning and transaction

deduplication.

For the final offline dataset:

\`\`\`text

Bronze transactions: 4,080,000

Silver transactions: 4,000,000

\`\`\`

The difference corresponds to the duplicate transaction rows removed

during Silver processing.

**### Silver to Gold Fact**

\`\`\`text

silver\_zone.transactions

        ↓

gold\_zone.fact\_transactions

\`\`\`

The cleaned Silver transactions are transformed into the analytical

transaction fact table.

This table becomes one of the primary transaction datasets used by

downstream analytical workloads.

**### Gold Fact to OBT**

\`\`\`text

gold\_zone.fact\_transactions

        ↓

gold\_zone.obt\_transaction\_enriched

\`\`\`

The transaction fact is enriched with dimensional information to create

an analytical One Big Table.

The OBT provides a convenient flattened representation for downstream

analytics and feature engineering.

**### OBT to User Feature**

\`\`\`text

gold\_zone.obt\_transaction\_enriched

        ↓

gold\_zone.feat\_user\_90d

\`\`\`

The enriched transaction data is aggregated into user-level features.

\`feat\_user\_90d\` represents a downstream feature dataset derived from

historical transaction behavior.

**## Lineage Implementation**

The lineage relationships are created through the DataHub Python SDK.

The implementation is stored in:

\`\`\`text

lineage/create\_lineage.py

\`\`\`

The script defines explicit upstream and downstream dataset

relationships.

Conceptually:

\`\`\`python

client.lineage.add\_lineage(

    upstream=upstream\_dataset,

    downstream=downstream\_dataset,

)

\`\`\`

Four lineage edges are registered:

\`\`\`text

Bronze Transactions → Silver Transactions

Silver Transactions → Gold Fact Transactions

Gold Fact Transactions → Transaction OBT

Transaction OBT → User 90-day Features

\`\`\`

The lineage relationships are based on the actual Spark transformation

pipeline implemented in the project.

This coursework implementation registers these relationships explicitly

through the DataHub SDK rather than automatically extracting Spark job

lineage.

**## Data Contract Expectations**

A lightweight data contract is used to describe the minimum

expectations for the transaction pipeline.

Important transaction fields include:

\| Field | Expectation |

\|---|---|

\| \`transaction\_id\` | Required and unique after Silver deduplication |

\| \`user\_id\` | Required for user-level processing |

\| \`account\_id\` | Required for account-level transaction tracking |

\| \`timestamp\` | Required business event timestamp |

\| \`amount\` | Numeric transaction amount |

\| \`status\` | Valid transaction status |

\| \`channel\` | Transaction channel where available |

Important pipeline expectations include:

\`\`\`text

Bronze

    may contain intentional duplicates

        ↓

Silver

    transaction\_id must be deduplicated

    required transaction fields must remain valid

        ↓

Gold

    analytical tables must preserve the expected transaction population

    and transformation relationships

\`\`\`

These expectations are enforced primarily by the existing validation

scripts rather than by DataHub itself.

DataHub is used to expose the datasets and their dependencies.

**## Why Lineage Is Useful**

Without lineage, a downstream dataset such as:

\`\`\`text

feat\_user\_90d

\`\`\`

appears only as an isolated table.

With lineage, it is possible to trace its origin:

\`\`\`text

feat\_user\_90d

        ↑

obt\_transaction\_enriched

        ↑

fact\_transactions

        ↑

silver.transactions

        ↑

bronze.transactions

\`\`\`

This is useful for several reasons.

If an upstream transaction dataset changes, lineage identifies which

downstream datasets may be affected.

If a feature contains incorrect values, lineage helps trace the problem

back through the transformation chain.

Lineage also documents the architecture in a form that can be explored

directly rather than relying only on static diagrams.

**## Evidence**

The DataHub lineage explorer successfully displays the complete

transaction path across Bronze, Silver and Gold layers.

![Transaction lineage]\(evidence/datahub/01\_transaction\_lineage.png)

The graph shows:

\`\`\`text

bronze\_zone.transactions

        ↓

silver\_zone.transactions

        ↓

gold\_zone.fact\_transactions

        ↓

gold\_zone.obt\_transaction\_enriched

        ↓

gold\_zone.feat\_user\_90d

\`\`\`

The DataHub interface also identifies direct and indirect dependencies.

For example, from \`fact\_transactions\`:

\`\`\`text

Direct upstream:

silver\_zone.transactions

Indirect upstream:

bronze\_zone.transactions

Direct downstream:

obt\_transaction\_enriched

Indirect downstream:

feat\_user\_90d

\`\`\`

**## Design Decisions**

**### Trino as the Metadata Source**

Trino was selected as the DataHub ingestion source because the

lakehouse tables are already exposed through the Trino Delta catalog.

This avoids creating a separate metadata representation only for

DataHub.

The resulting path is:

\`\`\`text

Delta Lake / MinIO

        ↓

Trino catalog

        ↓

DataHub

\`\`\`

**### Explicit Lineage Registration**

The coursework uses explicit SDK lineage registration.

This keeps the implementation simple and makes the lineage relationships

consistent with the transformations that were actually implemented.

Automatic Spark lineage extraction would require additional integration

and operational complexity that is not necessary for the current

coursework objective.

**### Limited Metadata Scope**

Only the Bronze, Silver and Gold schemas relevant to the lakehouse are

included in the ingestion recipe.

Profiling is disabled because scanning millions of rows is unnecessary

for demonstrating metadata catalog and lineage functionality.

**## Trade-offs**

The current lineage registration is explicit.

If a new transformation is added, its lineage edge must also be added

to the lineage configuration or script.

A larger production system could automate lineage collection directly

from Spark, SQL query history, orchestration metadata or other

integrations.

The current approach is intentionally smaller and easier to verify for

the coursework environment.

Another limitation is that DataHub depends on the tables being visible

through the Trino metastore.

A Delta table may physically exist in MinIO but remain undiscoverable

to DataHub until it has been registered in Trino.

**## Result**

The DataHub integration successfully provides a metadata and lineage

view of the transaction pipeline.

The final demonstrated lineage is:

\`\`\`text

Raw / Bronze Transactions

        ↓

Clean Silver Transactions

        ↓

Gold Transaction Fact

        ↓

Enriched Transaction OBT

        ↓

90-day User Features

\`\`\`

This completes the metadata and lineage layer of the E-wallet data

platform and provides traceability from ingested transaction data to

downstream analytical features.