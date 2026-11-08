"""Generate schema diagrams from observed Delta schemas and logical relationships.

Inputs are ``docs/edai1/schema/generated/observed_schemas.json`` and
``data_platform/contracts/relationships/schema_relationships.yml``. Outputs are deterministic, so
a test can prove the committed diagrams still match their inputs:

* Mermaid ER diagrams per zone, viewable in GitHub;
* DBML for Silver and Gold, importable at dbdiagram.io;
* an ERD-import SQL file for DBeaver or a similar tool. Its constraints exist only
  in that throwaway database, never in the Delta tables;
* a Markdown inventory with column tables and zone-to-zone column changes;
* Trino SQL for the read-only DBeaver workflow: registering the observed cloud runs
  in a local metastore, and the metadata queries behind each piece of evidence;
* the Gold dimension-fact ERD specification: edges, cardinality and the DBeaver
  virtual keys that reproduce it.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from data_platform.storage.scripts.config import (
    BRONZE_SCHEMA,
    GOLD_SCHEMA,
    SILVER_SCHEMA,
    TRINO_CATALOG,
)


GENERATED_DIR = Path("docs/edai1/schema/generated")
# Physical Trino location of each observed zone. Feature tables share the Gold schema,
# as in data_platform.storage.scripts.config.TRINO_REGISTRATION_LAYERS.
TRINO_SCHEMAS = {"bronze": BRONZE_SCHEMA, "silver": SILVER_SCHEMA, "gold": GOLD_SCHEMA, "features": GOLD_SCHEMA}
SOURCE_CATALOG = "hive"
SOURCE_SCHEMA = "source_zone"
SOURCE_TRINO_TYPES = {"string": "varchar", "double": "double", "bool": "boolean", "int8": "tinyint",
                      "timestamp[us]": "timestamp(6)", "date32[day]": "date"}
ERD_DIMENSIONS = ("dim_user", "dim_account", "dim_merchant", "dim_device", "dim_date")
ERD_FACTS = ("fact_transactions", "fact_login_events", "fact_balance_snapshot")
# One table per observed run, whose physical location the provenance queries print.
PROVENANCE_TABLES = (("source", "transactions_v2"), ("bronze", "transactions"), ("silver", "transactions"),
                     ("gold", "fact_transactions"), ("features", "feat_user_behavior"))
RELATIONSHIPS = Path("data_platform/contracts/relationships/schema_relationships.yml")
ZONE_TITLES = {"bronze": "Bronze", "silver": "Silver", "gold": "Gold core", "features": "Gold features"}
GOLD_ROLES = (("dim_", "Dimension"), ("fact_", "Fact"), ("obt_", "One big table"),
              ("feat_", "Feature"), ("opt_", "Analytics aggregate"))
SQL_TYPES = {"string": "TEXT", "long": "BIGINT", "integer": "INTEGER", "double": "DOUBLE PRECISION",
             "byte": "SMALLINT", "boolean": "BOOLEAN", "timestamp": "TIMESTAMPTZ", "timestamp_ntz": "TIMESTAMP",
             "date": "DATE"}


@dataclass(frozen=True)
class Relationship:
    """One logical foreign key."""

    zone: str
    table: str
    column: str
    target_zone: str
    target_table: str
    target_column: str
    nullable: bool
    enforcement: str


def load_relationships(document: Mapping[str, object]) -> list[Relationship]:
    """Parse relationships; a reference may name another zone as a prefix."""

    result = []
    for zone, items in document["relationships"].items():
        for item in items:
            parts = str(item["references"]).split(".")
            target_zone, target_table, target_column = parts if len(parts) == 3 else (zone, *parts)
            result.append(Relationship(zone, item["table"], item["column"], target_zone, target_table,
                                       target_column, bool(item["nullable"]), item["enforcement"]))
    return result


def validate_relationships(
    observed: Mapping[str, object], document: Mapping[str, object]
) -> list[str]:
    """Return every key or relationship that names a missing table or column."""

    zones = observed["zones"]

    def columns(zone: str, table: str) -> set[str] | None:
        found = zones.get(zone, {}).get("tables", {}).get(table)
        return None if found is None else {item["name"] for item in found["columns"]}

    problems = []
    for zone, tables in document["keys"].items():
        for table, keys in tables.items():
            known = columns(zone, table)
            if known is None or not set(keys) <= known:
                problems.append(f"key {zone}.{table}{keys} is not in the observed schema")
    for item in load_relationships(document):
        source, target = columns(item.zone, item.table), columns(item.target_zone, item.target_table)
        if source is None or item.column not in source:
            problems.append(f"{item.zone}.{item.table}.{item.column} is not in the observed schema")
        if target is None or item.target_column not in target:
            problems.append(f"{item.target_zone}.{item.target_table}.{item.target_column} is not observed")
    return problems


def _entity(name: str) -> str:
    return name.upper()


def render_mermaid(zone: str, observed: Mapping[str, object], document: Mapping[str, object]) -> str:
    """Render one zone as a Mermaid ER diagram, marking keys and logical references."""

    tables = observed["zones"][zone]["tables"]
    keys = document["keys"].get(zone, {})
    links = [item for item in load_relationships(document) if item.zone == zone]
    foreign = {(item.table, item.column) for item in links}
    lines = ["erDiagram"]
    for item in links:
        if item.target_zone != zone:
            continue
        marker = "o|" if item.nullable else "||"
        lines.append(f"    {_entity(item.target_table)} {marker}--o{{ {_entity(item.table)} : {item.column}")
    for table in sorted(tables):
        lines.append(f"    {_entity(table)} {{")
        for column in tables[table]["columns"]:
            flags = []
            if column["name"] in keys.get(table, ()):
                flags.append("PK")
            if (table, column["name"]) in foreign:
                flags.append("FK")
            suffix = f" {','.join(flags)}" if flags else ""
            lines.append(f"        {column['type']} {column['name']}{suffix}")
        lines.append("    }")
    return "\n".join(lines) + "\n"


def render_dbml(zones: Sequence[str], observed: Mapping[str, object], document: Mapping[str, object]) -> str:
    """Render zones as DBML. Keys and references are logical."""

    lines = ["// Generated. Keys and references are logical; Delta enforces neither.", ""]
    for zone in zones:
        for table, schema in sorted(observed["zones"][zone]["tables"].items()):
            keys = document["keys"].get(zone, {}).get(table, [])
            lines.append(f"Table {zone}.{table} {{")
            for column in schema["columns"]:
                mark = " [pk]" if len(keys) == 1 and column["name"] == keys[0] else ""
                lines.append(f"  {column['name']} {column['type']}{mark}")
            if len(keys) > 1:
                lines += ["  indexes {", f"    ({', '.join(keys)}) [pk]", "  }"]
            lines += ["}", ""]
    for item in load_relationships(document):
        if item.zone in zones and item.target_zone in zones:
            lines.append(f"Ref: {item.zone}.{item.table}.{item.column} > "
                         f"{item.target_zone}.{item.target_table}.{item.target_column}")
    return "\n".join(lines) + "\n"


def render_erd_sql(zones: Sequence[str], observed: Mapping[str, object], document: Mapping[str, object]) -> str:
    """Render PostgreSQL DDL for loading into a throwaway database for an ERD tool."""

    lines = [
        "-- ERD import only. Load into an empty scratch PostgreSQL database and reverse",
        "-- engineer it in DBeaver. These constraints are logical; they do not exist in",
        "-- the Delta tables, and this file holds no data.",
        "",
    ]
    for zone in zones:
        lines += [f"CREATE SCHEMA IF NOT EXISTS {zone};", ""]
    for zone in zones:
        for table, schema in sorted(observed["zones"][zone]["tables"].items()):
            keys = document["keys"].get(zone, {}).get(table, [])
            body = [f'    "{column["name"]}" {SQL_TYPES[column["type"]]}' for column in schema["columns"]]
            if keys:
                joined = ", ".join(f'"{key}"' for key in keys)
                body.append(f"    PRIMARY KEY ({joined})")
            lines += [f"CREATE TABLE {zone}.{table} (", ",\n".join(body), ");", ""]
    for item in load_relationships(document):
        target_keys = document["keys"].get(item.target_zone, {}).get(item.target_table, [])
        if item.zone in zones and item.target_zone in zones and target_keys == [item.target_column]:
            lines.append(
                f'ALTER TABLE {item.zone}.{item.table} ADD FOREIGN KEY ("{item.column}") '
                f'REFERENCES {item.target_zone}.{item.target_table} ("{item.target_column}");'
            )
    return "\n".join(lines) + "\n"


def gold_role(table: str) -> str:
    """Return a Gold table's role from its naming prefix."""

    return next((role for prefix, role in GOLD_ROLES if table.startswith(prefix)), "Other")


def column_changes(
    before: Mapping[str, object], after: Mapping[str, object]
) -> list[tuple[str, list[str], list[str], list[str]]]:
    """Compare two zones table by table: columns added, removed and retyped."""

    rows = []
    for table in sorted(set(before["tables"]) & set(after["tables"])):
        left = {item["name"]: item["type"] for item in before["tables"][table]["columns"]}
        right = {item["name"]: item["type"] for item in after["tables"][table]["columns"]}
        rows.append((
            table,
            sorted(set(right) - set(left)),
            sorted(set(left) - set(right)),
            sorted(name for name in set(left) & set(right) if left[name] != right[name]),
        ))
    return rows


def render_markdown(observed: Mapping[str, object], document: Mapping[str, object]) -> str:
    """Render the inventory that links every generated diagram."""

    zones = observed["zones"]
    links = load_relationships(document)
    lines = [
        "# Schema Diagrams",
        "",
        "Generated from the Delta logs of real runs by `ops/schema/render_schema_diagrams.py`. "
        "Do not edit by hand.",
        "",
        "Delta declares no primary or foreign keys. **PK** and **FK** below are logical: they "
        "describe how builders and validators treat the data. "
        "Physical metadata is limited to column names, types and partition columns.",
        "",
        "| Zone | Run | Tables | Columns |",
        "|---|---|---:|---:|",
    ]
    for zone, title in ZONE_TITLES.items():
        tables = zones[zone]["tables"]
        total = sum(len(item["columns"]) for item in tables.values())
        lines.append(f"| {title} | `{zones[zone]['run_id']}` | {len(tables)} | {total} |")
    lines += [
        "",
        "| Artifact | Use |",
        "|---|---|",
        "| [bronze.mmd](bronze.mmd), [silver.mmd](silver.mmd), [gold.mmd](gold.mmd), "
        "[features.mmd](features.mmd) | Mermaid ER diagrams |",
        "| [silver.dbml](silver.dbml), [gold.dbml](gold.dbml) | Import at dbdiagram.io |",
        "| [gold_erd_import.sql](gold_erd_import.sql) | Load into a scratch PostgreSQL database, "
        "then reverse engineer in DBeaver |",
        "| [ZONE_COLUMNS.md](ZONE_COLUMNS.md) | Every table of every zone with its columns |",
        "| [GOLD_ERD.md](GOLD_ERD.md) | Gold dimension-fact ERD: grain, cardinality, DBeaver virtual keys |",
        "| [trino_register_tables.sql](trino_register_tables.sql) | Register the observed runs in the "
        "task-owned Trino (local metastore only) |",
        "| [trino_metadata_queries.sql](trino_metadata_queries.sql) | Read-only metadata queries "
        "for evidence E01-E08 |",
        "| [trino_provenance_queries.sql](trino_provenance_queries.sql) | Print the GCS location of "
        "one table per zone (E08) |",
        "| [observed_schemas.json](observed_schemas.json) | Source of these diagrams |",
        "",
        "## Zone-to-zone column changes",
        "",
        "Source to Bronze is one to one by table name; its column mapping is checked by "
        "`data_platform/ingestion/spark/source_to_bronze.py`. Bronze stores naive timestamps "
        "(`timestamp_ntz`) and Silver stores them as `timestamp`.",
        "",
        "### Bronze to Silver",
        "",
        "| Table | Added | Removed | Type changed |",
        "|---|---|---|---|",
    ]
    for table, added, removed, retyped in column_changes(zones["bronze"], zones["silver"]):
        lines.append(f"| `{table}` | {', '.join(added) or '-'} | {', '.join(removed) or '-'} | "
                     f"{', '.join(retyped) or '-'} |")
    lines += ["", "## Gold tables", "", "| Table | Role | Logical key | Partition | Columns |", "|---|---|---|---|---:|"]
    for zone in ("gold", "features"):
        for table, schema in sorted(zones[zone]["tables"].items()):
            key = ", ".join(document["keys"][zone].get(table, []))
            partition = ", ".join(schema["partition_columns"]) or "-"
            lines.append(f"| `{table}` | {gold_role(table)} | `{key}` | {partition} | {len(schema['columns'])} |")
    lines += ["", "## Logical relationships", "",
              "`validated` means a validator rule checks the relationship on real rows. "
              "`builder_only` means it follows from the transformation.", "",
              "| Zone | From | To | Nullable | Enforcement |", "|---|---|---|---|---|"]
    for item in links:
        lines.append(f"| {item.zone} | `{item.table}.{item.column}` | "
                     f"`{item.target_table}.{item.target_column}` | "
                     f"{'yes' if item.nullable else 'no'} | {item.enforcement} |")
    return "\n".join(lines) + "\n"


def render_trino_registration(observed: Mapping[str, object]) -> str:
    """Render the statements that make the observed runs browsable in the local Trino.

    Delta tables are registered from their existing transaction logs. Source Parquet
    datasets become external Hive tables with the columns read from their footers.
    Both only write the task-owned file metastore; neither writes to the table location.
    """

    lines = [
        "-- Generated by ops/schema/render_schema_diagrams.py. Do not edit by hand.",
        "-- Run once against the task-owned Trino (infra/docker/schema-viz) as user",
        "-- `registrar`. Every statement writes only that container's local file",
        "-- metastore; no statement writes to GCS. Re-running fails on existing tables:",
        "-- recreate the stack with `down -v` instead.",
        "",
    ]
    for schema in dict.fromkeys(TRINO_SCHEMAS.values()):
        lines.append(f"CREATE SCHEMA IF NOT EXISTS {TRINO_CATALOG}.{schema};")
    lines.append("")
    for zone, schema in TRINO_SCHEMAS.items():
        root = observed["zones"][zone]["root"]
        lines.append(f"-- {zone}: run {observed['zones'][zone]['run_id']}")
        for table in sorted(observed["zones"][zone]["tables"]):
            lines.append(
                f"CALL {TRINO_CATALOG}.system.register_table(schema_name => '{schema}', "
                f"table_name => '{table}', table_location => '{root}/{table}');"
            )
        lines.append("")
    source = observed["source"]
    lines += [f"-- source: dataset {source['dataset_version']} (Parquet, external tables)",
              f"CREATE SCHEMA IF NOT EXISTS {SOURCE_CATALOG}.{SOURCE_SCHEMA};", ""]
    for table, schema in sorted(source["tables"].items()):
        body = ",\n".join(f"    {column['name']} {SOURCE_TRINO_TYPES[column['type']]}"
                          for column in schema["columns"])
        lines += [
            f"CREATE TABLE IF NOT EXISTS {SOURCE_CATALOG}.{SOURCE_SCHEMA}.{table} (",
            body,
            f") WITH (external_location = '{source['root']}/{table}', format = 'PARQUET');",
            "",
        ]
    return "\n".join(lines)


def render_trino_metadata_queries(observed: Mapping[str, object]) -> str:
    """Render the read-only metadata queries behind evidence items E01-E08.

    They read catalog metadata and Delta transaction logs only: no statement scans
    table data, so they are safe to run against the 100 GB canonical dataset.
    """

    def columns_query(catalog: str, schema: str) -> list[str]:
        return [
            "SELECT table_name, ordinal_position, column_name, data_type, is_nullable",
            f"FROM {catalog}.information_schema.columns",
            f"WHERE table_schema = '{schema}'",
            "ORDER BY table_name, ordinal_position;",
        ]

    zone_schemas = list(dict.fromkeys(TRINO_SCHEMAS.values()))
    in_list = ", ".join(f"'{schema}'" for schema in zone_schemas)
    lines = [
        "-- Generated by ops/schema/render_schema_diagrams.py. Do not edit by hand.",
        "-- Read-only. Run as user `viewer`; it cannot write. Metadata only: no query",
        "-- below reads table data files.",
        "",
        "-- E08 catalogs and schemas: two GCS-backed catalogs, one per storage format",
        "SHOW CATALOGS;",
        f"SHOW SCHEMAS FROM {TRINO_CATALOG};",
        f"SHOW SCHEMAS FROM {SOURCE_CATALOG};",
        "",
        f"-- E01 Source: {len(observed['source']['tables'])} physical Parquet datasets",
        f"SHOW TABLES FROM {SOURCE_CATALOG}.{SOURCE_SCHEMA};",
        *columns_query(SOURCE_CATALOG, SOURCE_SCHEMA),
        "",
    ]
    for item, zone in (("E02", "bronze"), ("E03", "silver"), ("E04", "gold")):
        schema = TRINO_SCHEMAS[zone]
        lines += [f"-- {item} {zone.capitalize()}", f"SHOW TABLES FROM {TRINO_CATALOG}.{schema};",
                  *columns_query(TRINO_CATALOG, schema), ""]
    lines += [
        "-- E06 keys and join columns of the ERD tables",
        *(f"DESCRIBE {TRINO_CATALOG}.{GOLD_SCHEMA}.{table};" for table in ERD_DIMENSIONS + ERD_FACTS),
        "",
        "-- E07 naming convention: one row per registered table with its layer and Gold role",
        "SELECT table_schema, table_name,",
        "       CASE WHEN table_schema <> 'gold_zone' THEN 'zone-qualified (no prefix)'",
        "            WHEN table_name LIKE 'dim\\_%' ESCAPE '\\' THEN 'dimension'",
        "            WHEN table_name LIKE 'fact\\_%' ESCAPE '\\' THEN 'fact'",
        "            WHEN table_name LIKE 'obt\\_%' ESCAPE '\\' THEN 'one big table'",
        "            WHEN table_name LIKE 'feat\\_%' ESCAPE '\\' THEN 'feature'",
        "            WHEN table_name LIKE 'opt\\_%' ESCAPE '\\' THEN 'analytics aggregate'",
        "            ELSE 'unclassified' END AS naming_role",
        f"FROM {TRINO_CATALOG}.information_schema.tables",
        f"WHERE table_schema IN ({in_list})",
        "ORDER BY table_schema, table_name;",
        "",
        "-- E08 provenance: the Delta commit behind one table of each observed run",
        *(f'SELECT version, timestamp, operation FROM {TRINO_CATALOG}.{TRINO_SCHEMAS[zone]}."{table}$history";'
          for zone, table in PROVENANCE_TABLES if zone != "source"),
        "",
    ]
    return "\n".join(lines)


def render_trino_provenance_queries(observed: Mapping[str, object]) -> str:
    """Render the statements that print the physical GCS location of one table per zone.

    ``SHOW CREATE TABLE`` needs table ownership under Trino's file access control, which
    the read-only ``viewer`` does not have. The statements only read metadata.
    """

    lines = [
        "-- Generated by ops/schema/render_schema_diagrams.py. Do not edit by hand.",
        "-- E08 provenance. Run as user `registrar`: SHOW CREATE TABLE needs table",
        "-- ownership, which the read-only `viewer` lacks. Every statement only reads metadata.",
        "",
    ]
    for zone, table in PROVENANCE_TABLES:
        if zone == "source":
            lines.append(f"SHOW CREATE TABLE {SOURCE_CATALOG}.{SOURCE_SCHEMA}.{table};")
        else:
            lines.append(f"SHOW CREATE TABLE {TRINO_CATALOG}.{TRINO_SCHEMAS[zone]}.{table};")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class ErdEdge:
    """One Gold relationship drawn in the dimension-fact ERD."""

    child: str
    column: str
    parent: str
    parent_column: str
    optional: bool
    cardinality: str
    kind: str
    enforcement: str


def gold_erd_edges(document: Mapping[str, object]) -> list[ErdEdge]:
    """Return the ERD edges among the ERD tables, with their cardinality.

    Every edge must reference the parent's whole logical key, which is what makes
    the child-to-parent side "at most one". The child side is "one" only when the
    referencing column is itself the child's whole key.

    Raises:
        ValueError: An edge references something other than the parent's key.
    """

    tables = set(ERD_DIMENSIONS + ERD_FACTS)
    keys = document["keys"]["gold"]
    edges = []
    for item in load_relationships(document):
        if item.zone != "gold" or item.target_zone != "gold":
            continue
        if item.table not in tables or item.target_table not in tables:
            continue
        if keys[item.target_table] != [item.target_column]:
            raise ValueError(f"{item.table}.{item.column} does not reference the key of {item.target_table}")
        cardinality = "one-to-one" if keys[item.table] == [item.column] else "many-to-one"
        kind = "dimension-to-dimension" if item.table.startswith("dim_") else "fact-to-dimension"
        edges.append(ErdEdge(item.table, item.column, item.target_table, item.target_column,
                             item.nullable, cardinality, kind, item.enforcement))
    return edges


def render_gold_erd(observed: Mapping[str, object], document: Mapping[str, object]) -> str:
    """Render the Gold dimension-fact ERD specification and its DBeaver virtual keys."""

    tables = observed["zones"]["gold"]["tables"]
    keys = document["keys"]["gold"]
    edges = gold_erd_edges(document)
    lines = [
        "# Gold Dimension-Fact ERD Specification",
        "",
        "Generated by `ops/schema/render_schema_diagrams.py` from "
        "[observed_schemas.json](observed_schemas.json) and "
        "`data_platform/contracts/relationships/schema_relationships.yml`. Do not edit by hand.",
        "",
        "Delta declares no primary or foreign keys, so a database tool discovers none. Each edge "
        "below is a **logical** relationship: the builders produce it and, where marked "
        "`validated`, `data_platform/quality/validate_gold.py` checks it for orphans. In DBeaver "
        "it is reproduced as a *virtual* key, stored only in the DBeaver workspace.",
        "",
        "## Diagram (source-derived, not a database export)",
        "",
        "```mermaid",
        "erDiagram",
    ]
    for edge in edges:
        child_end = "|o" if edge.optional else "||"
        parent_end = "o|" if edge.cardinality == "one-to-one" else "o{"
        lines.append(f"    {_entity(edge.parent)} {child_end}--{parent_end} {_entity(edge.child)} : {edge.column}")
    for table in ERD_DIMENSIONS + ERD_FACTS:
        lines.append(f"    {_entity(table)} {{")
        linked = {edge.column for edge in edges if edge.child == table}
        for column in tables[table]["columns"]:
            flags = [flag for flag, present in (("PK", column["name"] in keys[table]),
                                                ("FK", column["name"] in linked)) if present]
            suffix = f" {','.join(flags)}" if flags else ""
            lines.append(f"        {column['type']} {column['name']}{suffix}")
        lines.append("    }")
    lines += [
        "```",
        "",
        "## Grain",
        "",
        "| Table | Role | Grain (logical key) | Partition | Columns |",
        "|---|---|---|---|---:|",
    ]
    for table in ERD_DIMENSIONS + ERD_FACTS:
        partition = ", ".join(tables[table]["partition_columns"]) or "-"
        lines.append(f"| `{table}` | {gold_role(table)} | one row per `{', '.join(keys[table])}` | "
                     f"{partition} | {len(tables[table]['columns'])} |")
    lines += [
        "",
        "## Relationships",
        "",
        "*Optional* means the referencing column may be null; a null key has no parent row. "
        "Physically every Delta column is nullable, so non-null keys are a validator rule, not "
        "a storage constraint.",
        "",
        "| # | Child column | Parent key | Cardinality | Optional | Kind | Enforcement |",
        "|---:|---|---|---|---|---|---|",
    ]
    for number, edge in enumerate(edges, start=1):
        lines.append(f"| {number} | `{edge.child}.{edge.column}` | `{edge.parent}.{edge.parent_column}` | "
                     f"{edge.cardinality} | {'yes' if edge.optional else 'no'} | {edge.kind} | "
                     f"{edge.enforcement} |")
    lines += [
        "",
        "## DBeaver virtual keys",
        "",
        "Create the virtual unique keys first; a virtual foreign key must reference one.",
        "",
        "| Table | Virtual unique key columns |",
        "|---|---|",
    ]
    for table in ERD_DIMENSIONS + ERD_FACTS:
        lines.append(f"| `{GOLD_SCHEMA}.{table}` | `{', '.join(keys[table])}` |")
    lines += [
        "",
        "| # | On table | Column | References | Referenced virtual key |",
        "|---:|---|---|---|---|",
    ]
    for number, edge in enumerate(edges, start=1):
        lines.append(f"| {number} | `{GOLD_SCHEMA}.{edge.child}` | `{edge.column}` | "
                     f"`{GOLD_SCHEMA}.{edge.parent}` | `{edge.parent_column}` |")
    return "\n".join(lines) + "\n"


def render_zone_columns(observed: Mapping[str, object], document: Mapping[str, object]) -> str:
    """Render every table of every zone with its columns, as Trino shows them."""

    source = observed["source"]
    lines = [
        "# Columns per Zone",
        "",
        "Generated by `ops/schema/render_schema_diagrams.py` from "
        "[observed_schemas.json](observed_schemas.json). Do not edit by hand. Types are the "
        "physical Parquet/Delta types; *key* is the logical key from "
        "`data_platform/contracts/relationships/schema_relationships.yml` (Delta declares none).",
        "",
        f"## Source — `{SOURCE_CATALOG}.{SOURCE_SCHEMA}` (dataset `{source['dataset_version']}`, Parquet)",
        "",
        "| Physical dataset | Logical table | Rows | Files | Columns |",
        "|---|---|---:|---:|---|",
    ]
    for table, schema in sorted(source["tables"].items()):
        names = ", ".join(f"`{item['name']}` {item['type']}" for item in schema["columns"])
        lines.append(f"| `{table}` | `{schema['logical_table']}` | {schema['rows']:,} | {schema['files']:,} | "
                     f"{names} |")
    for zone, title in ZONE_TITLES.items():
        keys = document["keys"].get(zone, {})
        lines += ["", f"## {title} — `{TRINO_CATALOG}.{TRINO_SCHEMAS[zone]}` (run `{observed['zones'][zone]['run_id']}`)",
                  "", "| Table | Key | Partition | Columns |", "|---|---|---|---|"]
        for table, schema in sorted(observed["zones"][zone]["tables"].items()):
            names = ", ".join(f"`{item['name']}` {item['type']}" for item in schema["columns"])
            key = ", ".join(keys.get(table, [])) or "-"
            partition = ", ".join(schema["partition_columns"]) or "-"
            lines.append(f"| `{table}` | {key} | {partition} | {names} |")
    return "\n".join(lines) + "\n"


def render_all(observed: Mapping[str, object], document: Mapping[str, object]) -> dict[str, str]:
    """Return every generated file by name, after validating the inputs."""

    problems = validate_relationships(observed, document)
    if problems:
        raise ValueError("schema relationships do not match observed schemas: " + "; ".join(problems))
    files = {f"{zone}.mmd": render_mermaid(zone, observed, document) for zone in ZONE_TITLES}
    files["silver.dbml"] = render_dbml(("silver",), observed, document)
    files["gold.dbml"] = render_dbml(("gold", "features"), observed, document)
    files["gold_erd_import.sql"] = render_erd_sql(("gold", "features"), observed, document)
    files["SCHEMA_DIAGRAMS.md"] = render_markdown(observed, document)
    files["GOLD_ERD.md"] = render_gold_erd(observed, document)
    files["ZONE_COLUMNS.md"] = render_zone_columns(observed, document)
    files["trino_register_tables.sql"] = render_trino_registration(observed)
    files["trino_metadata_queries.sql"] = render_trino_metadata_queries(observed)
    files["trino_provenance_queries.sql"] = render_trino_provenance_queries(observed)
    return files


def main(argv: Sequence[str] | None = None) -> int:
    """Write every artifact next to the observed schema file."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=GENERATED_DIR)
    parser.add_argument("--relationships", type=Path, default=RELATIONSHIPS)
    args = parser.parse_args(argv)
    observed = json.loads((args.directory / "observed_schemas.json").read_text(encoding="utf-8"))
    document = yaml.safe_load(args.relationships.read_text(encoding="utf-8"))
    for name, content in render_all(observed, document).items():
        (args.directory / name).write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
