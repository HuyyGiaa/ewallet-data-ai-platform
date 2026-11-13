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
import ast
import copy
import json
from dataclasses import dataclass, field
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
    coverage: str = "partial"


def load_relationships(document: Mapping[str, object]) -> list[Relationship]:
    """Parse relationships; a reference may name another zone as a prefix."""

    result = []
    for zone, items in document["relationships"].items():
        for item in items:
            parts = str(item["references"]).split(".")
            target_zone, target_table, target_column = parts if len(parts) == 3 else (zone, *parts)
            result.append(Relationship(zone, item["table"], item["column"], target_zone, target_table,
                                       target_column, bool(item["nullable"]), item["enforcement"],
                                       str(item.get("coverage", "partial"))))
    return result


@dataclass(frozen=True)
class EdgeSemantics:
    """Cardinality and participation of one logical relationship.

    ``parent_optional``: a child row may have no parent (the referencing column is
    nullable in the logical contract). ``child_optional``: for a one-to-one edge, a
    parent row may have no child row.
    """

    cardinality: str
    parent_optional: bool
    child_optional: bool


def edge_semantics(item: Relationship, document: Mapping[str, object]) -> EdgeSemantics:
    """Derive cardinality from the logical keys, never from column names.

    The child side is "one" only when the referencing column is the child's whole
    key. Participation of the parent in a one-to-one edge comes from ``coverage``.

    Raises:
        ValueError: The edge does not reference the parent's whole key, a key column
            is declared nullable, or ``coverage`` is invalid for the edge.
    """

    child_keys = document["keys"][item.zone][item.table]
    parent_keys = document["keys"][item.target_zone][item.target_table]
    name = f"{item.table}.{item.column}"
    if parent_keys != [item.target_column]:
        raise ValueError(f"{name} does not reference the whole key of {item.target_table}")
    if item.coverage not in ("partial", "complete"):
        raise ValueError(f"{name} has unknown coverage {item.coverage!r}")
    one_to_one = child_keys == [item.column]
    if one_to_one and item.nullable:
        raise ValueError(f"{name} is the key of {item.table} and cannot be nullable")
    if item.coverage == "complete" and not one_to_one:
        raise ValueError(f"{name}: coverage applies only to a one-to-one edge")
    return EdgeSemantics(
        "one-to-one" if one_to_one else "many-to-one",
        parent_optional=item.nullable,
        child_optional=one_to_one and item.coverage != "complete",
    )


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_SOURCES = ("data_platform/quality/validate_silver.py", "data_platform/quality/validate_gold.py")
CLOUD_SUMMARY_SOURCE = "ops/gcp/summarize_spark_output.py"
EdgeKey = tuple[str, str, str, str]


def _string(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise ValueError(f"expected a string literal at line {node.lineno}")


def _table_argument(node: ast.expr) -> str:
    """Return the table a validator argument names: ``tables["x"]`` or a variable ``x``."""

    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return str(node.slice.value)
    if isinstance(node, ast.Name):
        return node.id
    raise ValueError(f"cannot read a table name at line {node.lineno}")


def validator_edges(source: str) -> dict[EdgeKey, str]:
    """Return every foreign-key check of a validator: (child, column, parent, parent column) -> rule.

    Reads ``validate_foreign_key(child, parent, column, parent_column, rule)`` and
    ``validate_exact_transaction_coverage(labels, transactions, prefix)``, whose
    orphan rule ``<prefix>.transaction_fk`` checks ``transaction_id``.
    """

    found: dict[EdgeKey, str] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "validate_foreign_key" and len(node.args) == 5:
            child, parent, column, parent_column, rule = node.args
            key = (_table_argument(child), _string(column), _table_argument(parent), _string(parent_column))
            found[key] = _string(rule)
        elif node.func.id == "validate_exact_transaction_coverage" and len(node.args) == 3:
            labels, transactions, prefix = node.args
            key = (_table_argument(labels), "transaction_id", _table_argument(transactions), "transaction_id")
            found[key] = f"{_string(prefix)}.transaction_fk"
    return found


def cloud_reference_checks(source: str) -> frozenset[EdgeKey]:
    """Return the references the 100 GB cloud run checks for orphans.

    ``ops/gcp/summarize_spark_output.py`` is the only runtime check of the cloud
    DP2 Gold output: ``no_orphans(<fact>, dimension, key)`` over a literal list of
    (dimension, key) pairs inside ``_invariants``. The cloud run executes neither
    ``validate_silver.py`` nor ``validate_gold.py``.
    """

    function = next((node for node in ast.walk(ast.parse(source))
                     if isinstance(node, ast.FunctionDef) and node.name == "_invariants"), None)
    if function is None:
        raise ValueError("summarize_spark_output has no _invariants function")
    facts: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for node in ast.walk(function):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "no_orphans" and isinstance(node.args[0], ast.Constant)):
            facts.add(_string(node.args[0]))
        if isinstance(node, ast.Tuple) and node.elts and all(
                isinstance(item, ast.Tuple) and len(item.elts) == 2
                and all(isinstance(part, ast.Constant) and isinstance(part.value, str) for part in item.elts)
                for item in node.elts):
            pairs += [(_string(item.elts[0]), _string(item.elts[1])) for item in node.elts]
    if len(facts) != 1 or not pairs:
        raise ValueError("cannot read the cloud reference checks of _invariants")
    fact = facts.pop()
    return frozenset((fact, key, dimension, key) for dimension, key in pairs)


@dataclass(frozen=True)
class Evidence:
    """Which checks exist for each edge, read from the checking code itself."""

    validators: Mapping[EdgeKey, str] = field(default_factory=dict)
    cloud: frozenset[EdgeKey] = frozenset()


def load_evidence(root: Path = REPOSITORY_ROOT) -> Evidence:
    validators: dict[EdgeKey, str] = {}
    for path in VALIDATOR_SOURCES:
        validators.update(validator_edges((root / path).read_text(encoding="utf-8")))
    cloud = cloud_reference_checks((root / CLOUD_SUMMARY_SOURCE).read_text(encoding="utf-8"))
    return Evidence(validators, cloud)


def _edge_key(item: Relationship) -> EdgeKey:
    return (item.table, item.column, item.target_table, item.target_column)


def describe_checks(item: Relationship, evidence: Evidence) -> str:
    """Say which code checks an edge, keeping validated and builder_only apart."""

    rule = evidence.validators.get(_edge_key(item))
    if item.enforcement == "validated":
        text = f"validated by rule {rule} (local validator runs)" if rule else "validated"
    else:
        text = "builder_only: follows from the builder"
        if rule:
            text += f"; rule {rule} exists but is not counted as validation"
    cloud = "checked" if _edge_key(item) in evidence.cloud else "not checked"
    return f"{text}; {cloud} on the 100GB cloud run"


def effective_document(observed: Mapping[str, object], document: Mapping[str, object]) -> dict[str, object]:
    """Merge ``pending_observation`` entries whose tables are now in the observed schema.

    A pending key joins once its table is observed; a pending relationship joins
    once both of its tables are. Nothing unobserved is ever drawn.
    """

    pending = document.get("pending_observation") or {}
    merged = copy.deepcopy({key: value for key, value in document.items() if key != "pending_observation"})
    zones = observed["zones"]

    def observed_table(zone: str, table: str) -> bool:
        return table in zones.get(zone, {}).get("tables", {})

    for zone, tables in (pending.get("keys") or {}).items():
        for table, keys in tables.items():
            if observed_table(zone, table):
                merged.setdefault("keys", {}).setdefault(zone, {})[table] = list(keys)
    for zone, items in (pending.get("relationships") or {}).items():
        for item in items:
            target = str(item["references"]).split(".")
            target_zone, target_table = (target[0], target[1]) if len(target) == 3 else (zone, target[0])
            if observed_table(zone, item["table"]) and observed_table(target_zone, target_table):
                merged.setdefault("relationships", {}).setdefault(zone, []).append(dict(item))
    return merged


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
        try:
            edge_semantics(item, document)
        except (KeyError, ValueError) as exc:
            problems.append(str(exc))
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
        semantics = edge_semantics(item, document)
        parent = "o|" if semantics.parent_optional else "||"
        child = "o{"
        if semantics.cardinality == "one-to-one":
            child = "o|" if semantics.child_optional else "||"
        lines.append(f"    {_entity(item.target_table)} {parent}--{child} {_entity(item.table)} : {item.column}")
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


DBML_HEADER = (
    "// Generated by ops/schema/render_schema_diagrams.py. Do not edit by hand.",
    "//",
    "// LOGICAL model of the observed Delta tables. Keys, `not null`/`null` on referencing",
    "// columns and every Ref come from data_platform/contracts/relationships/schema_relationships.yml.",
    "// They are not physical constraints: Delta declares no primary or foreign key, and",
    "// every Delta column is physically nullable (observed_schemas.json). Columns without",
    "// a setting keep DBML's default (nullable), which is also their physical state.",
    "//",
    "// Ref operators (DBML): `>` many-to-one with a required parent; `>?` many-to-one whose",
    "// referencing column may be NULL; `-` one-to-one where every parent row has exactly one",
    "// child row; `-?` one-to-one where a parent may have no child. The referencing column",
    "// is on the left of `>`/`>?` and on the right of `-`/`-?`.",
    "//",
    "// Checks per Ref: `validated` = a rule in validate_silver.py/validate_gold.py, run by",
    "// the local canonical rebuilds; `builder_only` = guaranteed by the builder only.",
    "// The 100GB cloud run re-checks only the references marked `checked`.",
)


def _dbml_text(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render_dbml(
    zones: Sequence[str], observed: Mapping[str, object], document: Mapping[str, object],
    evidence: Evidence | None = None,
) -> str:
    """Render zones as DBML: the validated logical model on the observed columns."""

    evidence = evidence if evidence is not None else load_evidence()
    links = [item for item in load_relationships(document) if item.zone in zones and item.target_zone in zones]
    referencing = {(item.zone, item.table, item.column): item for item in links}
    lines = [*DBML_HEADER, ""]
    for zone in zones:
        for table, schema in sorted(observed["zones"][zone]["tables"].items()):
            keys = document["keys"].get(zone, {}).get(table, [])
            grain = f"Logical grain: one row per ({', '.join(keys)})." if keys else "No logical key."
            lines.append(f"Table {zone}.{table} [note: {_dbml_text(grain + ' Physical Delta: no constraints.')}] {{")
            for column in schema["columns"]:
                settings = []
                if len(keys) == 1 and column["name"] == keys[0]:
                    settings.append("pk")
                link = referencing.get((zone, table, column["name"]))
                if link is not None:
                    if "pk" not in settings:
                        settings.append("null" if link.nullable else "not null")
                    rule = "optional" if link.nullable else "required"
                    settings.append(f"note: {_dbml_text(f'Logical FK to {link.target_table}.{link.target_column} ({rule}); {link.enforcement}')}")
                suffix = f" [{', '.join(settings)}]" if settings else ""
                lines.append(f"  {column['name']} {column['type']}{suffix}")
            if len(keys) > 1:
                lines += ["  indexes {", f"    ({', '.join(keys)}) [pk]", "  }"]
            lines += ["}", ""]
    for item in links:
        semantics = edge_semantics(item, document)
        child = f"{item.zone}.{item.table}.{item.column}"
        parent = f"{item.target_zone}.{item.target_table}.{item.target_column}"
        participation = "optional" if semantics.parent_optional else "required"
        if semantics.cardinality == "one-to-one":
            participation += ", every parent has a child" if not semantics.child_optional else ", parent may have no child"
            expression = f"{parent} {'-?' if semantics.child_optional else '-'} {child}"
        else:
            expression = f"{child} {'>?' if semantics.parent_optional else '>'} {parent}"
        lines.append(f"// {item.table}.{item.column} -> {item.target_table}.{item.target_column}: "
                     f"{semantics.cardinality}, {participation}; {describe_checks(item, evidence)}")
        lines.append(f"Ref fk_{item.table}_{item.column}: {expression}")
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
            required = {item.column for item in load_relationships(document)
                        if item.zone == zone and item.table == table and not item.nullable}
            body = [f'    "{column["name"]}" {SQL_TYPES[column["type"]]}'
                    + (" NOT NULL" if column["name"] in required else "") for column in schema["columns"]]
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


def render_markdown(
    observed: Mapping[str, object], document: Mapping[str, object], evidence: Evidence | None = None,
) -> str:
    """Render the inventory that links every generated diagram."""

    evidence = evidence if evidence is not None else load_evidence()
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
              "`validated` means a rule in `validate_silver.py` or `validate_gold.py` checks the "
              "relationship; those validators run in the local canonical rebuilds. `builder_only` "
              "means it follows from the transformation. The last column says whether the 100GB "
              "cloud run re-checks it (`ops/gcp/summarize_spark_output.py`).", "",
              "| Zone | From | To | Cardinality | Nullable | Enforcement | 100GB cloud check |",
              "|---|---|---|---|---|---|---|"]
    for item in links:
        semantics = edge_semantics(item, document)
        cardinality = semantics.cardinality
        if cardinality == "one-to-one":
            cardinality += " (partial)" if semantics.child_optional else " (complete)"
        cloud = "yes" if _edge_key(item) in evidence.cloud else "no"
        lines.append(f"| {item.zone} | `{item.table}.{item.column}` | "
                     f"`{item.target_table}.{item.target_column}` | {cardinality} | "
                     f"{'yes' if item.nullable else 'no'} | {item.enforcement} | {cloud} |")
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
        cardinality = edge_semantics(item, document).cardinality
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


def render_all(
    observed: Mapping[str, object], document: Mapping[str, object], evidence: Evidence | None = None,
) -> dict[str, str]:
    """Return every generated file by name, after validating the inputs."""

    evidence = evidence if evidence is not None else load_evidence()
    document = effective_document(observed, document)
    problems = validate_relationships(observed, document)
    if problems:
        raise ValueError("schema relationships do not match observed schemas: " + "; ".join(problems))
    files = {f"{zone}.mmd": render_mermaid(zone, observed, document) for zone in ZONE_TITLES}
    files["silver.dbml"] = render_dbml(("silver",), observed, document, evidence)
    files["gold.dbml"] = render_dbml(("gold", "features"), observed, document, evidence)
    files["gold_erd_import.sql"] = render_erd_sql(("gold", "features"), observed, document)
    files["SCHEMA_DIAGRAMS.md"] = render_markdown(observed, document, evidence)
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
