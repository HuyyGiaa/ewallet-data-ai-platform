"""Render the dbdiagram.io DBML and schema inventory of the canonical 5GB campaign.

Inputs:

* ``data_platform/schema/generated/observed_schemas.json`` - the metadata-only export
  of one campaign profile (``data_platform/schema/export_observed_schemas.py``, owner-run);
* ``data_platform/contracts/relationships/schema_relationships.yml`` - logical keys,
  relationships and the validator rule behind each one;
* the campaign evidence folder - both profiles' campaign documents and validation
  reports, which prove that each ``validated`` relationship's rule passed on the
  real 5GB tables.

Before writing anything the renderer refuses a snapshot that is not bound to the
canonical campaign (wrong dataset, benchmark, profile or source manifest, or a
legacy snapshot without provenance), a table set that differs from the dataset's
expected inventory, a key or relationship naming a missing column, a type mismatch
across a relationship, and a ``validated`` relationship whose rule did not PASS in
every profile. Outputs are deterministic, so a test proves the committed files
still match their inputs:

* ``silver.dbml`` and ``gold.dbml`` (core Gold plus historical features), for
  import at dbdiagram.io;
* ``SCHEMA_INVENTORY.md`` - every table of every zone with its columns, keys and
  relationships;
* Trino SQL for the read-only schema-viz stack: table registration (local
  metastore only), metadata queries and provenance queries.
"""

from __future__ import annotations

import argparse
import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from data_platform.contracts.schema_inventory import ZONES, zone_inventory
from data_platform.metadata.datahub.assertions.campaign_validation import report_path, verify_report
from data_platform.metadata.datahub.campaign_evidence import CampaignEvidence, load_campaign
from data_platform.storage.scripts.config import BRONZE_SCHEMA, GOLD_SCHEMA, SILVER_SCHEMA, TRINO_CATALOG
from data_platform.schema.export_observed_schemas import OBSERVED_SCHEMA_VERSION, comparison_problems


GENERATED_DIR = Path("data_platform/schema/generated")
RELATIONSHIPS = Path("data_platform/contracts/relationships/schema_relationships.yml")
CANONICAL_EVIDENCE = Path("docs/evidence/edai1/spark/rehearsal-5gb-ab-20261110225456")
CANONICAL_BENCHMARK = "ab-20261110225456"
CANONICAL_DATASET = "rehearsal-5gb-v1"
PROFILES = ("baseline", "optimized")
TRINO_SCHEMAS = {"bronze": BRONZE_SCHEMA, "silver": SILVER_SCHEMA, "gold": GOLD_SCHEMA, "features": GOLD_SCHEMA}
SOURCE_CATALOG = "hive"
SOURCE_SCHEMA = "source_zone"
SOURCE_TRINO_TYPES = {"string": "varchar", "double": "double", "bool": "boolean", "int8": "tinyint",
                      "int16": "smallint", "int32": "integer", "int64": "bigint",
                      "timestamp[us]": "timestamp(6)", "date32[day]": "date"}
GOLD_ROLES = (("dim_", "Dimension"), ("fact_", "Fact"), ("obt_", "One big table"),
              ("feat_", "Feature"), ("opt_", "Analytics aggregate"))
HEADER_COLORS = {"Dimension": "#1F6FB2", "Fact": "#C0392B", "One big table": "#7D3C98",
                 "Feature": "#1E8449", "Analytics aggregate": "#7D3C98", "Silver": "#5D6D7E"}
DBML_FILES = {"silver.dbml": ("silver",), "gold.dbml": ("gold", "features")}
PROVENANCE_TABLES = (("source", "transactions_v2"), ("bronze", "transactions"), ("silver", "merchant_history"),
                     ("gold", "dim_merchant_scd2"), ("features", "feat_user_behavior"))
EdgeKey = tuple[str, str, str, str]


class SchemaRenderError(ValueError):
    """The inputs do not describe the canonical campaign consistently."""


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class Relationship:
    """One logical foreign key and the executed rule that checks it."""

    zone: str
    table: str
    column: str
    target_zone: str
    target_table: str
    target_column: str
    nullable: bool
    enforcement: str
    rule: str | None = None
    coverage: str = "partial"
    coverage_rule: str | None = None


def load_relationships(document: Mapping[str, Any]) -> list[Relationship]:
    """Parse relationships; a reference may name another zone as a prefix."""

    result = []
    for zone, items in document["relationships"].items():
        for item in items:
            parts = str(item["references"]).split(".")
            target_zone, target_table, target_column = parts if len(parts) == 3 else (zone, *parts)
            result.append(Relationship(
                zone, item["table"], item["column"], target_zone, target_table, target_column,
                bool(item["nullable"]), str(item["enforcement"]), item.get("rule"),
                str(item.get("coverage", "partial")), item.get("coverage_rule"),
            ))
    return result


@dataclass(frozen=True)
class EdgeSemantics:
    """Cardinality of one logical relationship and whether the parent is optional."""

    cardinality: str
    parent_optional: bool
    child_optional: bool


def edge_semantics(item: Relationship, document: Mapping[str, Any]) -> EdgeSemantics:
    """Derive cardinality from the logical keys, never from column names.

    The child side is "one" only when the referencing column is the child's whole
    key. A parent row may lack a child unless ``coverage`` is ``complete``.

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
    if item.enforcement not in ("validated", "builder_only"):
        raise ValueError(f"{name} has unknown enforcement {item.enforcement!r}")
    one_to_one = child_keys == [item.column]
    if one_to_one and item.nullable:
        raise ValueError(f"{name} is the key of {item.table} and cannot be nullable")
    if item.coverage == "complete" and not one_to_one:
        raise ValueError(f"{name}: coverage applies only to a one-to-one edge")
    return EdgeSemantics("one-to-one" if one_to_one else "many-to-one",
                         parent_optional=item.nullable,
                         child_optional=one_to_one and item.coverage != "complete")


@dataclass(frozen=True)
class RuleEvidence:
    """Executed rule results of the canonical campaign, per profile."""

    benchmark_id: str
    results: Mapping[str, Mapping[str, tuple[bool, ...]]]

    def passed_everywhere(self, rule: str) -> bool:
        """Whether ``rule`` ran in every profile and passed every time it ran."""

        return all(rule in values and all(values[rule]) for values in self.results.values())

    def describe(self, rule: str) -> str:
        """Human-readable result of one rule across profiles."""

        if self.passed_everywhere(rule):
            return f"`{rule}` PASS in {' and '.join(self.results)} ({self.benchmark_id})"
        return f"`{rule}` NOT PASSED in every profile"


def load_rule_evidence(evidence_dir: Path, benchmark_id: str, dataset_version: str) -> tuple[
        dict[str, CampaignEvidence], RuleEvidence]:
    """Load both profiles' campaigns and their verified validation reports.

    Raises:
        CampaignEvidenceError: A campaign document or report is missing or inconsistent.
    """

    campaigns: dict[str, CampaignEvidence] = {}
    results: dict[str, dict[str, tuple[bool, ...]]] = {}
    for profile in PROFILES:
        campaign = load_campaign(evidence_dir / "manifests" / profile, benchmark_id=benchmark_id,
                                 spark_profile=profile, dataset_version=dataset_version)
        campaigns[profile] = campaign
        found: dict[str, list[bool]] = {}
        for stage in campaign.stages.values():
            path = report_path(evidence_dir / "validation_reports", profile, stage.stage)
            report = json.loads(path.read_text(encoding="utf-8"))
            verify_report(report, campaign, stage)
            for item in report["results"]:
                found.setdefault(str(item["rule_name"]), []).append(item.get("passed") is True)
        results[profile] = {rule: tuple(values) for rule, values in found.items()}
    return campaigns, RuleEvidence(benchmark_id, results)


def require_current_snapshot(observed: Mapping[str, Any], campaigns: Mapping[str, CampaignEvidence]) -> CampaignEvidence:
    """Return the campaign profile the snapshot was exported from, or refuse it.

    Raises:
        SchemaRenderError: The snapshot is a legacy or foreign snapshot, or its table
            inventory differs from the dataset's expected inventory.
    """

    if observed.get("schema_version") != OBSERVED_SCHEMA_VERSION:
        raise SchemaRenderError(
            f"observed_schemas.json has schema {observed.get('schema_version')!r}, not {OBSERVED_SCHEMA_VERSION}: "
            "it is not a campaign-bound export (a legacy 100GB snapshot is refused)")
    provenance = observed.get("provenance") or {}
    profile = provenance.get("spark_profile")
    if profile not in campaigns:
        raise SchemaRenderError(f"observed snapshot profile {profile!r} is not one of {sorted(campaigns)}")
    campaign = campaigns[profile]
    for key, value in campaign.identity().items():
        if provenance.get(key) != value:
            raise SchemaRenderError(f"observed snapshot {key} is {provenance.get(key)!r}, not {value!r}")
    expected_runs = {stage.zone: stage.run_id for stage in campaign.stages.values()}
    if provenance.get("run_ids") != expected_runs:
        raise SchemaRenderError("observed snapshot run ids differ from the campaign")
    inventory = zone_inventory(campaign.dataset_version)
    try:
        inventory.require_tables("source", list(observed["source"]["tables"]))
        for zone in ZONES[1:]:
            entry = observed["zones"][zone]
            if entry["run_id"] != expected_runs[zone]:
                raise SchemaRenderError(f"{zone} run {entry['run_id']} is not the campaign's {expected_runs[zone]}")
            inventory.require_tables(zone, list(entry["tables"]))
    except (KeyError, ValueError) as exc:
        raise SchemaRenderError(f"observed snapshot inventory: {exc}") from exc
    return campaign


# ----------------------------------------------------------------------- validation


def _columns(observed: Mapping[str, Any], zone: str, table: str) -> dict[str, str] | None:
    found = observed["zones"].get(zone, {}).get("tables", {}).get(table)
    return None if found is None else {item["name"]: str(item["type"]) for item in found["columns"]}


def validate_contract(observed: Mapping[str, Any], document: Mapping[str, Any], evidence: RuleEvidence) -> list[str]:
    """Return every problem between the relationship contract, the schema and the rule results."""

    problems: list[str] = []
    for zone, tables in document["keys"].items():
        for table, keys in tables.items():
            known = _columns(observed, zone, table)
            if known is None or not set(keys) <= set(known):
                problems.append(f"key {zone}.{table}{keys} is not in the observed schema")
    for zone, tables in (document.get("key_rules") or {}).items():
        for table, rule in tables.items():
            if not evidence.passed_everywhere(rule):
                problems.append(f"key rule {rule} of {zone}.{table} did not pass in every profile")
    for zone, tables in (document.get("unique") or {}).items():
        for table, items in tables.items():
            known = _columns(observed, zone, table) or {}
            for item in items:
                if not set(item["columns"]) <= set(known):
                    problems.append(f"unique {zone}.{table}{item['columns']} is not in the observed schema")
                if not evidence.passed_everywhere(item["rule"]):
                    problems.append(f"unique rule {item['rule']} did not pass in every profile")
    for item in load_relationships(document):
        source = _columns(observed, item.zone, item.table)
        target = _columns(observed, item.target_zone, item.target_table)
        name = f"{item.zone}.{item.table}.{item.column}"
        if source is None or item.column not in source:
            problems.append(f"{name} is not in the observed schema")
            continue
        if target is None or item.target_column not in target:
            problems.append(f"{item.target_zone}.{item.target_table}.{item.target_column} is not observed")
            continue
        if source[item.column] != target[item.target_column]:
            problems.append(f"{name} type {source[item.column]} differs from its parent type "
                            f"{target[item.target_column]}")
        try:
            edge_semantics(item, document)
        except (KeyError, ValueError) as exc:
            problems.append(str(exc))
        if item.enforcement == "validated" and (not item.rule or not evidence.passed_everywhere(item.rule)):
            problems.append(f"{name} is validated but its rule {item.rule!r} did not pass in every profile")
        if item.coverage_rule and not evidence.passed_everywhere(item.coverage_rule):
            problems.append(f"{name} coverage rule {item.coverage_rule} did not pass in every profile")
    for zone, items in (document.get("temporal") or {}).items():
        for item in items:
            if not set(item["columns"]) <= set(_columns(observed, zone, item["table"]) or {}):
                problems.append(f"temporal {zone}.{item['table']}{item['columns']} is not observed")
            if not evidence.passed_everywhere(item["rule"]):
                problems.append(f"temporal rule {item['rule']} did not pass in every profile")
    for zone, groups in (document.get("groups") or {}).items():
        grouped = [table for tables in groups.values() for table in tables]
        observed_tables = sorted(observed["zones"][zone]["tables"])
        if sorted(grouped) != observed_tables:
            problems.append(f"{zone} groups do not cover each observed table exactly once")
    for zone, tables in (document.get("column_notes") or {}).items():
        for table, notes in tables.items():
            missing = set(notes) - set(_columns(observed, zone, table) or {})
            if missing:
                problems.append(f"notes name unobserved columns of {zone}.{table}: {sorted(missing)}")
    return problems


# --------------------------------------------------------------------------- render


def gold_role(table: str) -> str:
    """Return a Gold table's role from its naming prefix."""

    return next((role for prefix, role in GOLD_ROLES if table.startswith(prefix)), "Other")


def _role(zone: str, table: str) -> str:
    return "Silver" if zone == "silver" else gold_role(table)


def _text(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _qualified(zone: str, table: str) -> str:
    return f"{TRINO_SCHEMAS[zone]}.{table}"


def _ref_expression(item: Relationship, document: Mapping[str, Any]) -> tuple[str, EdgeSemantics]:
    semantics = edge_semantics(item, document)
    child = f"{_qualified(item.zone, item.table)}.{item.column}"
    parent = f"{_qualified(item.target_zone, item.target_table)}.{item.target_column}"
    operator = "-" if semantics.cardinality == "one-to-one" else ">"
    return f"{child} {operator} {parent}", semantics


def _check_text(item: Relationship, evidence: RuleEvidence) -> str:
    if item.enforcement == "validated":
        return f"validated by {evidence.describe(str(item.rule))}"
    text = "builder_only: guaranteed by the builder; no orphan rule"
    if item.coverage_rule:
        text += f"; population check {evidence.describe(item.coverage_rule)}"
    return text


def render_dbml(
    zones: Sequence[str],
    observed: Mapping[str, Any],
    document: Mapping[str, Any],
    evidence: RuleEvidence,
    campaign: CampaignEvidence,
) -> str:
    """Render the given zones as DBML: the validated logical model on the observed columns."""

    name = "_".join(zones)
    links = [item for item in load_relationships(document) if item.zone in zones and item.target_zone in zones]
    referencing = {(item.zone, item.table, item.column): item for item in links}
    run_text = ", ".join(f"{zone} run {observed['zones'][zone]['run_id']}" for zone in zones)
    lines = [
        "// Generated by data_platform/schema/render_schema_diagrams.py. Do not edit by hand.",
        "// LOGICAL model of the observed Delta tables of one campaign profile. Keys and Refs",
        "// come from data_platform/contracts/relationships/schema_relationships.yml; Delta",
        "// itself declares no primary or foreign key and every column is physically nullable.",
        "// `not null`/`null` on a referencing column is the business rule its validator checks.",
        "",
        f"Project edai1_{name} {{",
        "  database_type: 'Delta Lake (Spark 3.5)'",
        "  Note: " + _text(
            f"Observed from {campaign.dataset_version}, benchmark {campaign.benchmark_id}, profile "
            f"{campaign.spark_profile} ({run_text}); source manifest {campaign.source_manifest_sha256[:12]}. "
            "Ref `>` = many-to-one, `-` = one-to-one; every Ref note names the executed validator rule."),
        "}",
        "",
    ]
    for zone in zones:
        zone_notes = (document.get("column_notes") or {}).get(zone, {})
        temporal = {item["table"]: item for item in (document.get("temporal") or {}).get(zone, [])}
        incoming_temporal = {item["references"]: item for item in (document.get("temporal") or {}).get(zone, [])}
        for table, schema in sorted(observed["zones"][zone]["tables"].items()):
            keys = document["keys"].get(zone, {}).get(table, [])
            notes = zone_notes.get(table, {})
            role = _role(zone, table)
            note = f"{role}. Grain: one row per ({', '.join(keys)})."
            if schema["partition_columns"]:
                note += f" Partitioned by {', '.join(schema['partition_columns'])}."
            if table in temporal:
                note += (f" As-of join to {temporal[table]['references']}: {temporal[table]['interval']}"
                         f" ({evidence.describe(temporal[table]['rule'])}).")
            if table in incoming_temporal:
                link = incoming_temporal[table]
                note += (f" {link['table']} resolves a version as of its event time: {link['interval']}"
                         f" ({evidence.describe(link['rule'])}). Not a key-based Ref.")
            lines.append(f"Table {_qualified(zone, table)} [headercolor: {HEADER_COLORS[role]}, note: {_text(note)}] {{")
            for column in schema["columns"]:
                settings = []
                if len(keys) == 1 and column["name"] == keys[0]:
                    settings.append("pk")
                link = referencing.get((zone, table, column["name"]))
                text = notes.get(column["name"])
                if link is not None:
                    if "pk" not in settings:
                        settings.append("null" if link.nullable else "not null")
                    rule = "optional" if link.nullable else "required"
                    reference = f"Logical FK to {link.target_table}.{link.target_column} ({rule})."
                    text = f"{text} {reference}" if text else reference
                if text:
                    settings.append(f"note: {_text(text)}")
                suffix = f" [{', '.join(settings)}]" if settings else ""
                lines.append(f"  {column['name']} {column['type']}{suffix}")
            indexes = []
            if len(keys) > 1:
                indexes.append(f"    ({', '.join(keys)}) [pk]")
            for item in (document.get("unique") or {}).get(zone, {}).get(table, []):
                indexes.append(f"    ({', '.join(item['columns'])}) [unique, name: {_text('uk_' + table + '_' + '_'.join(item['columns']))}]")
            if indexes:
                lines += ["  indexes {", *indexes, "  }"]
            lines += ["}", ""]
    for zone in zones:
        for group, tables in (document.get("groups") or {}).get(zone, {}).items():
            lines.append(f"TableGroup {group} {{")
            lines += [f"  {_qualified(zone, table)}" for table in tables]
            lines += ["}", ""]
    for item in links:
        expression, semantics = _ref_expression(item, document)
        participation = "optional parent" if semantics.parent_optional else "required parent"
        if semantics.cardinality == "one-to-one":
            participation += ", every parent has one child" if not semantics.child_optional else ", parent may have no child"
        lines.append(f"// {item.table}.{item.column} -> {item.target_table}.{item.target_column}: "
                     f"{semantics.cardinality}, {participation}; {_check_text(item, evidence)}")
        lines.append(f"Ref fk_{item.table}_{item.column}: {expression}")
    return "\n".join(lines) + "\n"


def column_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[tuple[str, list[str], list[str], list[str]]]:
    """Compare two zones table by table: columns added, removed and retyped."""

    rows = []
    for table in sorted(set(before["tables"]) & set(after["tables"])):
        left = {item["name"]: item["type"] for item in before["tables"][table]["columns"]}
        right = {item["name"]: item["type"] for item in after["tables"][table]["columns"]}
        rows.append((table, sorted(set(right) - set(left)), sorted(set(left) - set(right)),
                     sorted(name for name in set(left) & set(right) if left[name] != right[name])))
    return rows


def render_inventory(observed: Mapping[str, Any], document: Mapping[str, Any], evidence: RuleEvidence,
                     campaign: CampaignEvidence) -> str:
    """Render the Markdown inventory of every zone, column, key and relationship."""

    source = observed["source"]
    zones = observed["zones"]
    lines = [
        "# Schema Inventory",
        "",
        "Generated by `data_platform/schema/render_schema_diagrams.py` from [observed_schemas.json](observed_schemas.json) "
        "and `data_platform/contracts/relationships/schema_relationships.yml`. Do not edit by hand.",
        "",
        f"Observed from `{campaign.dataset_version}`, benchmark `{campaign.benchmark_id}`, profile "
        f"`{campaign.spark_profile}`, exported {observed['provenance']['exported_at']}. Types are the physical "
        "Parquet (Source) or Delta types. Keys are logical; Delta declares none.",
        "",
        "| Zone | Trino schema | Run | Tables | Columns |",
        "|---|---|---|---:|---:|",
        f"| Source | `{SOURCE_CATALOG}.{SOURCE_SCHEMA}` | dataset `{source['dataset_version']}` | "
        f"{len(source['tables'])} physical / {len({item['logical_table'] for item in source['tables'].values()})} logical | "
        f"{sum(len(item['columns']) for item in source['tables'].values())} |",
    ]
    for zone in ZONES[1:]:
        tables = zones[zone]["tables"]
        lines.append(f"| {zone.capitalize()} | `{TRINO_CATALOG}.{TRINO_SCHEMAS[zone]}` | `{zones[zone]['run_id']}` | "
                     f"{len(tables)} | {sum(len(item['columns']) for item in tables.values())} |")
    lines += ["", "## Source", "", "| Physical dataset | Logical table | Rows | Files | Columns |", "|---|---|---:|---:|---|"]
    for table, schema in sorted(source["tables"].items()):
        names = ", ".join(f"`{item['name']}` {item['type']}" for item in schema["columns"])
        lines.append(f"| `{table}` | `{schema['logical_table']}` | {schema['rows']:,} | {schema['files']:,} | {names} |")
    for zone in ZONES[1:]:
        keys = document["keys"].get(zone, {})
        lines += ["", f"## {zone.capitalize()}", "", "| Table | Role | Key | Partition | Columns |", "|---|---|---|---|---|"]
        for table, schema in sorted(zones[zone]["tables"].items()):
            names = ", ".join(f"`{item['name']}` {item['type']}" for item in schema["columns"])
            role = "-" if zone in ("bronze", "silver") else gold_role(table)
            lines.append(f"| `{table}` | {role} | {', '.join(keys.get(table, [])) or '-'} | "
                         f"{', '.join(schema['partition_columns']) or '-'} | {names} |")
    lines += ["", "## Bronze to Silver column changes", "", "| Table | Added | Removed | Type changed |", "|---|---|---|---|"]
    for table, added, removed, retyped in column_changes(zones["bronze"], zones["silver"]):
        lines.append(f"| `{table}` | {', '.join(added) or '-'} | {', '.join(removed) or '-'} | {', '.join(retyped) or '-'} |")
    lines += ["", "## Logical relationships", "", "| Zone | From | To | Cardinality | Nullable | Evidence |",
              "|---|---|---|---|---|---|"]
    for item in load_relationships(document):
        semantics = edge_semantics(item, document)
        cardinality = semantics.cardinality
        if cardinality == "one-to-one":
            cardinality += " (partial)" if semantics.child_optional else " (complete)"
        lines.append(f"| {item.zone} | `{item.table}.{item.column}` | `{item.target_table}.{item.target_column}` | "
                     f"{cardinality} | {'yes' if item.nullable else 'no'} | {_check_text(item, evidence)} |")
    for zone, items in (document.get("temporal") or {}).items():
        for item in items:
            lines.append(f"| {zone} | `{item['table']}.({', '.join(item['columns'])})` | `{item['references']}` "
                         f"(as of event time) | many-to-one per point in time | no | {evidence.describe(item['rule'])} |")
    return "\n".join(lines) + "\n"


def render_trino_registration(observed: Mapping[str, Any]) -> str:
    """Render the statements that make the observed runs browsable in the schema-viz Trino.

    Delta tables are registered from their existing logs; Source Parquet datasets
    become external Hive tables. Both write only the container's local metastore.
    """

    lines = [
        "-- Generated by data_platform/schema/render_schema_diagrams.py. Do not edit by hand.",
        "-- Run once against the task-owned Trino (infra/docker/schema-viz) as user `registrar`.",
        "-- Every statement writes only that container's local file metastore; none writes to GCS.",
        "",
    ]
    for schema in dict.fromkeys(TRINO_SCHEMAS.values()):
        lines.append(f"CREATE SCHEMA IF NOT EXISTS {TRINO_CATALOG}.{schema};")
    lines.append("")
    for zone, schema in TRINO_SCHEMAS.items():
        root = observed["zones"][zone]["root"]
        lines.append(f"-- {zone}: run {observed['zones'][zone]['run_id']}")
        for table in sorted(observed["zones"][zone]["tables"]):
            lines.append(f"CALL {TRINO_CATALOG}.system.register_table(schema_name => '{schema}', "
                         f"table_name => '{table}', table_location => '{root}/{table}');")
        lines.append("")
    source = observed["source"]
    lines += [f"-- source: dataset {source['dataset_version']} (Parquet, external tables)",
              f"CREATE SCHEMA IF NOT EXISTS {SOURCE_CATALOG}.{SOURCE_SCHEMA};", ""]
    for table, schema in sorted(source["tables"].items()):
        body = ",\n".join(f"    {column['name']} {SOURCE_TRINO_TYPES[column['type']]}" for column in schema["columns"])
        lines += [f"CREATE TABLE IF NOT EXISTS {SOURCE_CATALOG}.{SOURCE_SCHEMA}.{table} (", body,
                  f") WITH (external_location = '{source['root']}/{table}', format = 'PARQUET');", ""]
    return "\n".join(lines)


def render_trino_metadata_queries(observed: Mapping[str, Any]) -> str:
    """Render the read-only metadata queries behind SC01, SC05, SC07 and SC08.

    They read catalog metadata and Delta logs only; no statement scans table data.
    """

    zone_schemas = list(dict.fromkeys(TRINO_SCHEMAS.values()))
    in_list = ", ".join(f"'{schema}'" for schema in zone_schemas)
    lines = [
        "-- Generated by data_platform/schema/render_schema_diagrams.py. Do not edit by hand.",
        "-- Read-only, metadata only. Run as user `viewer`.",
        "",
        "-- SC01 all-zone inventory: tables per Trino schema",
        f"SHOW TABLES FROM {SOURCE_CATALOG}.{SOURCE_SCHEMA};",
        *(f"SHOW TABLES FROM {TRINO_CATALOG}.{schema};" for schema in zone_schemas),
        "SELECT table_schema, count(*) AS tables",
        f"FROM {TRINO_CATALOG}.information_schema.tables",
        f"WHERE table_schema IN ({in_list})",
        "GROUP BY table_schema ORDER BY table_schema;",
        "",
        "-- SC05 dim_merchant_scd2 physical schema",
        f"DESCRIBE {TRINO_CATALOG}.{GOLD_SCHEMA}.dim_merchant_scd2;",
        "",
        "-- SC07 feature timestamp columns and types",
        "SELECT table_name, column_name, data_type",
        f"FROM {TRINO_CATALOG}.information_schema.columns",
        f"WHERE table_schema = '{GOLD_SCHEMA}' AND table_name LIKE 'feat\\_%' ESCAPE '\\'",
        "  AND column_name IN ('event_timestamp', 'created', 'created_timestamp')",
        "ORDER BY table_name, column_name;",
        "",
        "-- SC08 naming convention: every registered table with its zone and role",
        "SELECT table_schema, table_name,",
        f"       CASE WHEN table_schema <> '{GOLD_SCHEMA}' THEN 'zone-qualified business name'",
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
        "-- Provenance: the Delta commit behind one table of each observed run",
        *(f'SELECT version, timestamp, operation FROM {TRINO_CATALOG}.{TRINO_SCHEMAS[zone]}."{table}$history";'
          for zone, table in PROVENANCE_TABLES if zone != "source"),
        "",
    ]
    return "\n".join(lines)


def render_trino_provenance_queries(observed: Mapping[str, Any]) -> str:
    """Render ``SHOW CREATE TABLE`` for one table per zone; it prints the GCS location.

    ``SHOW CREATE TABLE`` needs table ownership, so it runs as ``registrar``. It only
    reads metadata.
    """

    lines = ["-- Generated by data_platform/schema/render_schema_diagrams.py. Do not edit by hand.",
             "-- Run as user `registrar` (SHOW CREATE TABLE needs ownership). Metadata only.", ""]
    for zone, table in PROVENANCE_TABLES:
        if zone == "source":
            lines.append(f"SHOW CREATE TABLE {SOURCE_CATALOG}.{SOURCE_SCHEMA}.{table};")
        else:
            lines.append(f"SHOW CREATE TABLE {TRINO_CATALOG}.{TRINO_SCHEMAS[zone]}.{table};")
    return "\n".join(lines) + "\n"


def render_all(
    observed: Mapping[str, Any],
    document: Mapping[str, Any],
    campaigns: Mapping[str, CampaignEvidence],
    evidence: RuleEvidence,
    comparison: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Validate every input and return each generated file by name.

    Raises:
        SchemaRenderError: Any input disagrees with the canonical campaign.
    """

    campaign = require_current_snapshot(observed, campaigns)
    document = copy.deepcopy(dict(document))
    problems = validate_contract(observed, document, evidence)
    if comparison is not None:
        problems += comparison_problems(observed, comparison)
    if problems:
        raise SchemaRenderError("schema inputs are inconsistent: " + "; ".join(problems))
    files = {name: render_dbml(zones, observed, document, evidence, campaign) for name, zones in DBML_FILES.items()}
    files["SCHEMA_INVENTORY.md"] = render_inventory(observed, document, evidence, campaign)
    files["trino_register_tables.sql"] = render_trino_registration(observed)
    files["trino_metadata_queries.sql"] = render_trino_metadata_queries(observed)
    files["trino_provenance_queries.sql"] = render_trino_provenance_queries(observed)
    return files


def main(argv: Sequence[str] | None = None) -> int:
    """Validate the inputs and write every generated file next to the observed snapshot."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=GENERATED_DIR)
    parser.add_argument("--relationships", type=Path, default=RELATIONSHIPS)
    parser.add_argument("--evidence-dir", type=Path, default=CANONICAL_EVIDENCE)
    parser.add_argument("--benchmark-id", default=CANONICAL_BENCHMARK)
    parser.add_argument("--dataset-version", default=CANONICAL_DATASET)
    parser.add_argument("--check", action="store_true", help="fail if a committed file differs; write nothing")
    args = parser.parse_args(argv)
    observed_path = args.directory / "observed_schemas.json"
    if not observed_path.is_file():
        raise SystemExit(f"{observed_path} is missing: run the owner metadata export first "
                         "(OWNER_METADATA_EXPORT_PENDING)")
    observed = json.loads(observed_path.read_text(encoding="utf-8"))
    document = yaml.safe_load(args.relationships.read_text(encoding="utf-8"))
    campaigns, evidence = load_rule_evidence(args.evidence_dir, args.benchmark_id, args.dataset_version)
    comparison_path = args.evidence_dir / "comparison.json"
    comparison = json.loads(comparison_path.read_text(encoding="utf-8")) if comparison_path.is_file() else None
    files = render_all(observed, document, campaigns, evidence, comparison)
    stale = [name for name, content in files.items()
             if not (args.directory / name).is_file() or (args.directory / name).read_text(encoding="utf-8") != content]
    if args.check:
        if stale:
            raise SystemExit(f"generated files are stale: {stale}")
        return 0
    for name, content in files.items():
        (args.directory / name).write_text(content, encoding="utf-8")
    print(json.dumps({"written": sorted(files), "changed": stale}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
