"""Generate schema diagrams from observed Delta schemas and logical relationships.

Inputs are ``docs/edai1/schema/generated/observed_schemas.json`` and
``data_platform/contracts/relationships/schema_relationships.yml``. Outputs are deterministic, so
a test can prove the committed diagrams still match their inputs:

* Mermaid ER diagrams per zone, viewable in GitHub;
* DBML for Silver and Gold, importable at dbdiagram.io;
* an ERD-import SQL file for DBeaver or a similar tool. Its constraints exist only
  in that throwaway database, never in the Delta tables;
* a Markdown inventory with column tables and zone-to-zone column changes.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import yaml


GENERATED_DIR = Path("docs/edai1/schema/generated")
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
