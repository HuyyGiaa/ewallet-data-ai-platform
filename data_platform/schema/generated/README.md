# Generated schema artifacts

The files in this folder are generated, never edited by hand. They describe the canonical 5GB campaign: `rehearsal-5gb-v1`, benchmark `ab-20261110225456`, profile `optimized`. They are built from three inputs:

1. **`observed_schemas.json`.** The metadata-only export of that campaign profile, produced by [`export_observed_schemas.py`](../export_observed_schemas.py). The owner runs the export, because it reads GCS (Delta logs and Parquet footers only).
2. **[`schema_relationships.yml`](../../contracts/relationships/schema_relationships.yml).** It holds the logical keys and relationships, plus the validator rule that checks each one.
3. **The campaign's validation reports** in [`docs/evidence/edai1/spark/rehearsal-5gb-ab-20261110225456/`](../../../docs/evidence/edai1/spark/rehearsal-5gb-ab-20261110225456/).

The renderer [`render_schema_diagrams.py`](../render_schema_diagrams.py) refuses to run on a snapshot that is not bound to this campaign, such as the historical 100GB snapshot. It writes every file below except `observed_schemas.json`. Its `--check` mode fails when a committed file is stale.

| File | Use |
|---|---|
| `observed_schemas.json` | Observed columns and types of every zone (export output) |
| `silver.dbml` | Silver ERD: 9 tables, imported at dbdiagram.io |
| `gold.dbml` | Gold ERD: 12 core Gold tables and 4 historical feature tables, imported at dbdiagram.io |
| `SCHEMA_INVENTORY.md` | Every table, column, key and relationship of every zone |
| `trino_register_tables.sql` | Registers the 44 tables in the read-only schema-viz Trino. Writes to the local metastore only |
| `trino_metadata_queries.sql` | Metadata queries for SC01, SC05, SC07 and SC08 |
| `trino_provenance_queries.sql` | `SHOW CREATE TABLE`, which shows the GCS location of one table per zone |

The hand-written, read-only SCD2 and feature queries are in [`../sql/schema_evidence_queries.sql`](../sql/schema_evidence_queries.sql). The design and results are reported in [SCHEMA_DESIGN.md](../../../docs/edai1/schema/SCHEMA_DESIGN.md). The procedures are in [DATAHUB_SCHEMA_OWNER_EVIDENCE.md](../../../docs/runbooks/edai1/DATAHUB_SCHEMA_OWNER_EVIDENCE.md), Stages 1, 2 and 4.
